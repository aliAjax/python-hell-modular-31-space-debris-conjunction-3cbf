from datetime import datetime, timezone

from .audit import canonical_json
from .domain import ConflictError, DomainError

ENTITY_TYPE = "space_conjunction"
INITIAL_STATUS = "pending"
CREATE_ROLES = {"analyst"}
SOURCE_ROLES = {"analyst", "operator"}
ACTION_ROLES = {
    "assess": {"analyst"},
    "record_opinion": {"operator"},
    "approve": {"coordinator"},
    "execute": {"operator"},
    "resolve": {"coordinator"},
    "cancel": {"coordinator"},
    "report_revision": {"analyst"},
    "recover_assessment": {"analyst", "coordinator"},
}
ENFORCE_REGION = False
REGION_SENSITIVE_ACTIONS = set()
ACTION_REQUIRES_VERSION = {"approve", "execute", "resolve", "cancel", "record_opinion"}

OPINION_APPROVE = "approve"
OPINION_BLOCK = {"reject", "request_review"}
VALID_OPINIONS = OPINION_BLOCK | {OPINION_APPROVE}


def assess(payload):
    ratio = float(payload.get("miss_distance_m", 0)) / max(float(payload.get("covariance_m", 1)), 1.0)
    tca_hours = float(payload.get("hours_to_tca", 24))
    severity = max(0.0, 100.0 - min(95.0, ratio * 20.0))
    urgency = max(0.0, min(20.0, (24.0 - tca_hours) * 0.8))
    score = round(min(100.0, severity + urgency), 2)
    if score >= 80:
        level = "high"
    elif score >= 50:
        level = "medium"
    else:
        level = "low"
    return {"score": score, "level": level, "distance_to_covariance_ratio": round(ratio, 3)}


def parse_iso(value):
    """把 ISO 时间解析成可比较的带时区 datetime（naive 视为 UTC）。"""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def source_key(row):
    return "%s|%s" % (row["source_type"], row["external_id"])


def reconcile_sources(rows):
    """对账：按来源标识取每个来源的最新观测，再做融合。

    演示模型使用逆协方差（精度）加权融合：更确定的来源（协方差更小）
    对融合距离的话语权更大，融合协方差为各来源精度和的倒数。
    返回融合后的距离/协方差与来源指纹；指纹随任一来源最新观测变化。
    rows 中每项需要 source_type、external_id、observed_at、payload 字段。
    """
    latest = {}
    order = 0
    for row in sorted(rows, key=lambda item: item["id"]):
        key = source_key(row)
        when = parse_iso(row["observed_at"])
        previous = latest.get(key)
        if previous is None or when >= previous[0]:
            payload = row["payload"]
            if isinstance(payload, str):
                payload = _json_loads(payload)
            latest[key] = (when, order, {
                "observed_at": row["observed_at"],
                "miss_distance_m": float(payload["miss_distance_m"]),
                "covariance_m": float(payload["covariance_m"]),
            })
        order += 1
    if not latest:
        return None

    total_weight = 0.0
    weighted_distance = 0.0
    fingerprint_parts = []
    newest_seen = None
    for key in sorted(latest):
        when, _, data = latest[key]
        covariance = data["covariance_m"]
        weight = 1.0 / covariance
        total_weight += weight
        weighted_distance += data["miss_distance_m"] * weight
        fingerprint_parts.append([
            key,
            data["observed_at"],
            data["miss_distance_m"],
            data["covariance_m"],
        ])
        if newest_seen is None or when > newest_seen:
            newest_seen = when

    return {
        "miss_distance_m": round(weighted_distance / total_weight, 3),
        "covariance_m": round(1.0 / total_weight, 6),
        "source_count": len(latest),
        "latest_observed_at": max(data[2]["observed_at"] for data in latest.values()),
        "fingerprint": canonical_json(fingerprint_parts),
    }


def _json_loads(value):
    import json

    return json.loads(value)


def _need_status(item, allowed):
    if item["status"] not in allowed:
        raise DomainError("invalid_state", "当前状态 %s 不允许执行该操作" % item["status"])


def _require_number(payload, name, minimum=None):
    try:
        value = float(payload[name])
    except (KeyError, TypeError, ValueError):
        raise DomainError("field_required", "%s 不能为空" % name)
    if minimum is not None and value < minimum:
        raise DomainError("invalid_number", "%s 不能小于 %s" % (name, minimum))
    return value


def _require_text(payload, name):
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise DomainError("field_required", "%s 不能为空" % name)
    return value.strip()


def _compute_assessment(current, assessor):
    age = float(current.get("track_age_hours", 0))
    if age > 6:
        raise DomainError("stale_track", "轨道数据已过期，不能用于风险评估")
    current["hours_to_tca"] = float(current.get("hours_to_tca", 24))
    return assessor(current)


def _invalidate_chain(current):
    """来源变化：既有风险评估、运营方意见和规避动作建议全部失效。"""
    for key in ("assessment", "assessment_error", "approved_maneuver", "command_ref"):
        current.pop(key, None)
    current["opinions"] = []
    current["conflict"] = False


def apply_source_change(item, reconciled, assessor=None):
    """把对账结果写回实体并让既有链路失效后重算。

    仅当实体曾有评估（current/failed）时才自动重算；没有评估的待处理
    实体只刷新基线数据，等分析师显式评估。重算失败时保留失败现场，
    可随后用来源记录恢复。
    """
    assessor = assessor or assess
    current = dict(item["payload"])
    had_assessment = current.get("assessment_state") in ("current", "failed")

    current["miss_distance_m"] = reconciled["miss_distance_m"]
    current["covariance_m"] = reconciled["covariance_m"]
    current["source_fingerprint"] = reconciled["fingerprint"]

    if not had_assessment:
        return item["status"], current, {"recomputed": False, "invalidated": False}

    _invalidate_chain(current)
    try:
        result = _compute_assessment(current, assessor)
    except DomainError as exc:
        current["assessment_state"] = "failed"
        current["assessment_error"] = {"code": exc.code, "message": str(exc)}
        return "assessed", current, {
            "recomputed": False,
            "invalidated": True,
            "failed": True,
            "error": {"code": exc.code, "message": str(exc)},
        }
    current["assessment"] = result
    current["assessment_state"] = "current"
    return "assessed", current, {
        "recomputed": True,
        "invalidated": True,
        "assessment": result,
    }


def rebuild_from_sources(item, reconciled, assessor=None):
    """重算失败后的恢复路径：完全以来源记录重建距离/协方差并重算。"""
    assessor = assessor or assess
    current = dict(item["payload"])
    current["miss_distance_m"] = reconciled["miss_distance_m"]
    current["covariance_m"] = reconciled["covariance_m"]
    current["source_fingerprint"] = reconciled["fingerprint"]
    _invalidate_chain(current)
    result = _compute_assessment(current, assessor)
    current["assessment"] = result
    current["assessment_state"] = "current"
    return "assessed", current, {"recomputed": True, "assessment": result, "recovered": True}


def confirmation_summary(payload):
    operators = list(payload.get("operating_organizations", []))
    opinions = {entry["operator"]: entry["opinion"] for entry in payload.get("opinions", [])}
    approved = [name for name in operators if opinions.get(name) == OPINION_APPROVE]
    blocked = [name for name in operators if opinions.get(name) in OPINION_BLOCK]
    pending = [name for name in operators if name not in opinions]
    return {
        "maneuver": payload.get("approved_maneuver"),
        "required_operators": operators,
        "opinions": opinions,
        "approved_operators": approved,
        "blocked_operators": blocked,
        "pending_operators": pending,
        "all_confirmed": bool(operators) and not pending and not blocked,
    }


def apply_action(item, action, payload, actor, role):
    status = item["status"]
    current = dict(item["payload"])

    if action == "assess":
        _need_status(item, {"pending", "assessed"})
        if current.get("assessment_state") == "failed":
            raise DomainError(
                "assessment_failed",
                "风险评估重算失败且未恢复，请先从来源记录执行 recover_assessment",
                409,
            )
        current["hours_to_tca"] = float(payload.get("hours_to_tca", current.get("hours_to_tca", 24)))
        result = _compute_assessment(current, assess)
        current["assessment"] = result
        current["assessment_state"] = "current"
        current.pop("assessment_error", None)
        return "assessed", current, {"assessment": result, "actor": actor}

    if action == "record_opinion":
        _need_status(item, {"maneuver_pending"})
        opinion = _require_text(payload, "opinion").lower()
        if opinion not in VALID_OPINIONS:
            raise DomainError("invalid_opinion", "意见必须是 approve、reject 或 request_review")
        operator = _require_text(payload, "operator")
        operators = list(current.get("operating_organizations", []))
        if operator not in operators:
            raise DomainError("operator_not_listed", "该运营方不在此接近事件的确认名单中", 403)
        existing = next(
            (entry for entry in current.get("opinions", []) if entry["operator"] == operator),
            None,
        )
        if existing is not None and existing["opinion"] != opinion:
            raise ConflictError("opinion_conflict", "该运营方已经提交过不同意见，不能覆盖")
        entry = {"operator": operator, "opinion": opinion, "reason": payload.get("reason", "")}
        if existing is None:
            current.setdefault("opinions", []).append(entry)
        current["conflict"] = any(
            item_opinion["opinion"] in OPINION_BLOCK for item_opinion in current["opinions"]
        )
        summary = confirmation_summary(current)
        released = summary["all_confirmed"]
        new_status = "coordinating" if released else status
        return new_status, current, {
            "opinion": entry,
            "duplicated": existing is not None,
            "released": released,
            "confirmation": {
                "approved": summary["approved_operators"],
                "pending": summary["pending_operators"],
                "blocked": summary["blocked_operators"],
            },
        }

    if action == "approve":
        _need_status(item, {"assessed", "maneuver_pending"})
        if current.get("assessment_state") != "current":
            raise DomainError("assessment_not_current", "风险评估不是最新结果，不能批准规避动作", 409)
        if not current.get("operating_organizations"):
            raise DomainError("operators_required", "没有可确认的运营方，不能提交规避动作")
        fuel = _require_number(payload, "fuel_cost_m_s", 0)
        budget = float(current.get("fuel_budget_m_s", 0))
        if fuel > budget:
            raise DomainError("fuel_budget_exceeded", "规避燃料超过预算", 409)
        window = _require_text(payload, "maneuver_window")
        # 重新提交的机动建议视为新一轮确认：清掉旧意见与冲突标记。
        current["approved_maneuver"] = {"fuel_cost_m_s": fuel, "maneuver_window": window}
        current["opinions"] = []
        current["conflict"] = False
        return "maneuver_pending", current, {"approved_maneuver": current["approved_maneuver"]}

    if action == "execute":
        _need_status(item, {"coordinating"})
        command_ref = _require_text(payload, "command_ref")
        current["command_ref"] = command_ref
        return "executing", current, {"command_ref": command_ref}

    if action == "resolve":
        _need_status(item, {"executing"})
        report_ref = _require_text(payload, "report_ref")
        current["resolution"] = {"report_ref": report_ref, "resolved_by": actor}
        return "resolved", current, {"report_ref": report_ref}

    if action == "cancel":
        _need_status(item, {"pending", "assessed", "maneuver_pending"})
        reason = _require_text(payload, "reason")
        current["cancellation"] = {"reason": reason, "cancelled_by": actor}
        return "cancelled", current, {"reason": reason}

    raise DomainError("unknown_action", "不支持的操作")
