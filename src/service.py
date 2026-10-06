from . import domain, rules
from .domain import ConflictError, DomainError


class Service:
    def __init__(self, repository):
        self.repository = repository

    def create_item(self, payload, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.CREATE_ROLES:
            raise DomainError("forbidden", "当前角色不能创建此类业务记录", 403)
        normalized = domain.normalize_create(payload)
        stable_key = normalized.pop("_stable_key")
        return self.repository.create_item(
            rules.ENTITY_TYPE, stable_key, rules.INITIAL_STATUS, normalized, actor, role
        )

    def _record_source(self, item_id, normalized, actor, role):
        source_type = normalized["source_type"]
        external_id = normalized["external_id"]
        observed_at = normalized["observed_at"]
        body = {
            key: value
            for key, value in normalized.items()
            if key not in ("source_type", "external_id", "observed_at")
        }
        return self.repository.record_source(
            item_id,
            source_type,
            external_id,
            body,
            observed_at,
            actor,
            role,
            rules.reconcile_sources,
            rules.apply_source_change,
        )

    def add_source(self, item_id, payload, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.SOURCE_ROLES:
            raise DomainError("forbidden", "当前角色不能提交来源记录", 403)
        self.repository.get_item(item_id)
        normalized = domain.normalize_source(payload)
        if region and rules.ENFORCE_REGION and role != "regulator" and normalized.get("region") and normalized["region"] != region:
            raise DomainError("region_mismatch", "来源记录不属于当前管辖区域", 403)
        result, created = self._record_source(item_id, normalized, actor, role)
        result["item"] = self.get_item(item_id)
        return result, created

    def act(self, item_id, action, payload, actor, role, expected_version=None, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        item = self.repository.get_item(item_id)
        allowed = rules.ACTION_ROLES.get(action, set())
        if role not in allowed:
            raise DomainError("forbidden", "当前角色不能执行该操作", 403)
        if rules.ENFORCE_REGION and action in rules.REGION_SENSITIVE_ACTIONS and region and role != "regulator":
            if item["payload"].get("region") != region:
                raise DomainError("region_mismatch", "不能处理其他区域的记录", 403)

        if action == "report_revision":
            return self._report_revision(item_id, payload, actor, role)
        if action == "recover_assessment":
            return self.repository.recover_assessment(
                item_id, actor, role, rules.reconcile_sources, rules.rebuild_from_sources
            )

        if action in rules.ACTION_REQUIRES_VERSION and expected_version is None:
            raise DomainError("expected_version_required", "该操作需要 expected_version", 400)

        # 运营方意见的幂等/不可覆盖规则在状态机之前判定，
        # 即使规避动作已因全部确认而释放也同样适用。
        if action == "record_opinion":
            operator = (payload.get("operator") or "").strip()
            opinion = (payload.get("opinion") or "").strip().lower()
            existing = next(
                (entry for entry in item["payload"].get("opinions", [])
                 if entry["operator"] == operator),
                None,
            )
            if existing is not None and existing["opinion"] == opinion:
                return self.get_item(item_id)
            if existing is not None:
                raise ConflictError("opinion_conflict", "该运营方已经提交过不同意见，不能覆盖")

        new_status, new_payload, event_payload = rules.apply_action(item, action, payload, actor, role)
        self.repository.apply_action(
            item_id, action, actor, role, new_status, new_payload, event_payload, expected_version
        )
        return self.get_item(item_id)

    def _report_revision(self, item_id, payload, actor, role):
        """人工修订观测走与来源记录相同的“先对账再放行”链路。"""
        normalized = domain.normalize_source({
            "source_type": payload.get("source") if isinstance(payload.get("source"), str) and payload.get("source").strip() else "manual_revision",
            "external_id": "rev-%s" % payload.get("observed_at", ""),
            "observed_at": payload.get("observed_at"),
            "miss_distance_m": payload.get("miss_distance_m"),
            "covariance_m": payload.get("covariance_m"),
        })
        result, _ = self._record_source(item_id, normalized, actor, role)
        return self.get_item(item_id)

    def get_item(self, item_id):
        item = self.repository.get_item(item_id)
        payload = item["payload"]
        item["sources"] = self.repository.list_sources(item_id)
        item["audit"] = self.repository.audit_trail(item_id)
        # 只展示已存储的评估结果；评估是否有效看 assessment_state。
        item["assessment"] = payload.get("assessment")
        item["assessment_state"] = payload.get("assessment_state")
        item["assessment_error"] = payload.get("assessment_error")
        item["confirmation"] = rules.confirmation_summary(payload)
        return item

    def list_items(self, status=None):
        return self.repository.list_items(status)

    def state(self):
        return self.repository.state_summary()
