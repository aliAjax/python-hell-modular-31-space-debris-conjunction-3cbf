from .domain import DomainError

ENTITY_TYPE = "space_conjunction"
INITIAL_STATUS = "pending"
STATUS_PENDING_CONFIRMATION = "pending_confirmation"
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
    "reconcile": {"analyst"},
}
ENFORCE_REGION = False
REGION_SENSITIVE_ACTIONS = set()
ACTION_REQUIRES_VERSION = {"approve", "execute", "resolve", "cancel", "reconcile"}


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


def _source_observation(source):
    payload = source.get("payload") if isinstance(source.get("payload"), dict) else source
    distance = float(payload.get("miss_distance_m", source.get("miss_distance_m", 0)))
    covariance = float(payload.get("covariance_m", source.get("covariance_m", 0)))
    observed_at = source.get("observed_at") or payload.get("observed_at") or ""
    return str(observed_at), distance, covariance


def reconcile_sources(sources):
    """按来源标识取最新观测，返回对账后的 (miss_distance_m, covariance_m)。

    同一来源标识只保留最新一条；全局以最新 observed_at 的观测为准。
    没有任何来源时返回 None，沿用业务记录自身的初始值。
    """
    if not sources:
        return None
    latest_by_key = {}
    for source in sources:
        key = (source.get("source_type"), source.get("external_id"))
        observed_at, _, _ = _source_observation(source)
        rank = (observed_at, source.get("id", 0))
        current = latest_by_key.get(key)
        if current is None or rank > current[0]:
            latest_by_key[key] = (rank, source)
    candidates = [entry[1] for entry in latest_by_key.values()]
    best = max(candidates, key=lambda item: (item.get("observed_at") or "", item.get("id", 0)))
    _, distance, covariance = _source_observation(best)
    if covariance <= 0:
        raise DomainError("reconcile_failed", "最新观测的协方差无效，无法重算风险评估")
    return distance, covariance


def reconcile_payload(payload, sources):
    """从来源记录重算距离/协方差与风险评估。

    重算失败时保留来源记录，把评估标记为失效；来源记录持久存在，可再次重算恢复。
    返回 (new_payload, error)。
    """
    new_payload = dict(payload)
    error = None
    try:
        reconciled = reconcile_sources(sources)
        if reconciled is not None:
            distance, covariance = reconciled
            new_payload["miss_distance_m"] = distance
            new_payload["covariance_m"] = covariance
        result = assess(new_payload)
        new_payload["assessment"] = result
        new_payload["assessment_stale"] = False
    except DomainError as exc:
        new_payload["assessment"] = None
        new_payload["assessment_stale"] = True
        error = exc
    if sources:
        for entry in new_payload.get("opinions", []):
            entry["stale"] = True
    return new_payload, error


def _latest_opinions_by_operator(current):
    latest = {}
    for entry in current.get("opinions", []):
        latest[entry["operator"]] = entry
    return latest


def _has_conflict(current):
    latest = _latest_opinions_by_operator(current)
    return any(
        entry.get("opinion") in {"reject", "request_review"} and not entry.get("stale")
        for entry in latest.values()
    )


def _confirmed_operators(current):
    orgs = current.get("operating_organizations", [])
    latest = _latest_opinions_by_operator(current)
    return [
        org
        for org in orgs
        if org in latest
        and latest[org].get("opinion") == "approve"
        and not latest[org].get("stale")
    ]


def _all_operators_confirmed(current):
    orgs = current.get("operating_organizations", [])
    if not orgs:
        return True
    return len(_confirmed_operators(current)) == len(orgs)


def _pending_operators(current):
    orgs = current.get("operating_organizations", [])
    confirmed = set(_confirmed_operators(current))
    return [org for org in orgs if org not in confirmed]


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


def apply_action(item, action, payload, actor, role):
    status = item["status"]
    current = dict(item["payload"])

    if action == "assess":
        _need_status(item, {"pending", "assessed"})
        age = float(current.get("track_age_hours", 0))
        if age > 6:
            raise DomainError("stale_track", "轨道数据已过期，不能用于风险评估")
        current["hours_to_tca"] = float(payload.get("hours_to_tca", current.get("hours_to_tca", 24)))
        result = assess(current)
        current["assessment"] = result
        current["assessment_stale"] = False
        for entry in current.get("opinions", []):
            entry["stale"] = True
        return "assessed", current, {"assessment": result, "actor": actor}

    if action == "report_revision":
        _need_status(item, {"pending", "assessed", "coordinating", "executing"})
        revision = {
            "observed_at": _require_text(payload, "observed_at"),
            "miss_distance_m": _require_number(payload, "miss_distance_m", 0),
            "covariance_m": _require_number(payload, "covariance_m", 0.001),
            "source": _require_text(payload, "source"),
        }
        if revision["covariance_m"] <= 0:
            raise DomainError("invalid_covariance", "协方差必须大于零")
        current.setdefault("revisions", []).append(revision)
        current["miss_distance_m"] = revision["miss_distance_m"]
        current["covariance_m"] = revision["covariance_m"]
        current["assessment"] = assess(current)
        current["assessment_stale"] = False
        for entry in current.get("opinions", []):
            entry["stale"] = True
        return status, current, {"revision": revision}

    if action == "record_opinion":
        _need_status(item, {"assessed", "coordinating", STATUS_PENDING_CONFIRMATION})
        opinion = _require_text(payload, "opinion").lower()
        if opinion not in {"approve", "reject", "request_review"}:
            raise DomainError("invalid_opinion", "意见必须是 approve、reject 或 request_review")
        operator = _require_text(payload, "operator")
        entry = {"operator": operator, "opinion": opinion, "reason": payload.get("reason", ""), "stale": False}
        current.setdefault("opinions", []).append(entry)
        current["conflict"] = _has_conflict(current)
        if status == STATUS_PENDING_CONFIRMATION and _all_operators_confirmed(current) and not current["conflict"]:
            current.pop("pending_confirmation", None)
            return "coordinating", current, {"opinion": entry, "auto_released": True}
        return status, current, {"opinion": entry}

    if action == "approve":
        _need_status(item, {"assessed", STATUS_PENDING_CONFIRMATION})
        if current.get("assessment_stale"):
            raise DomainError("assessment_stale", "风险评估已失效，请先重算")
        if _has_conflict(current):
            raise DomainError("unresolved_conflict", "存在未解决的运营方冲突意见", 409)
        fuel = _require_number(payload, "fuel_cost_m_s", 0)
        budget = float(current.get("fuel_budget_m_s", 0))
        if fuel > budget:
            raise DomainError("fuel_budget_exceeded", "规避燃料超过预算", 409)
        window = _require_text(payload, "maneuver_window")
        current["approved_maneuver"] = {"fuel_cost_m_s": fuel, "maneuver_window": window}
        if _all_operators_confirmed(current):
            current.pop("pending_confirmation", None)
            return "coordinating", current, {"approved_maneuver": current["approved_maneuver"]}
        confirmed = _confirmed_operators(current)
        pending = _pending_operators(current)
        current["pending_confirmation"] = {"confirmed": confirmed, "pending": pending}
        return STATUS_PENDING_CONFIRMATION, current, {"pending_confirmation": current["pending_confirmation"]}

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
        _need_status(item, {"pending", "assessed"})
        reason = _require_text(payload, "reason")
        current["cancellation"] = {"reason": reason, "cancelled_by": actor}
        return "cancelled", current, {"reason": reason}

    raise DomainError("unknown_action", "不支持的操作")
