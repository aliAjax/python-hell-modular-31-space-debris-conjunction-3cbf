from . import domain, rules
from .domain import DomainError


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

    def add_source(self, item_id, payload, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.SOURCE_ROLES:
            raise DomainError("forbidden", "当前角色不能提交来源记录", 403)
        item = self.repository.get_item(item_id)
        normalized = domain.normalize_source(payload)
        if region and rules.ENFORCE_REGION and role != "regulator" and normalized.get("region") and normalized["region"] != region:
            raise DomainError("region_mismatch", "来源记录不属于当前管辖区域", 403)
        source, changed = self.repository.upsert_source(
            item_id,
            normalized.pop("source_type"),
            normalized.pop("external_id"),
            normalized,
            normalized.pop("observed_at"),
            actor,
            role,
        )
        if changed:
            # 来源变化：先对账再放行，自动重算风险评估
            self._reconcile(item_id, actor, role)
        return source

    def _reconcile(self, item_id, actor, role, expected_version=None):
        """从来源记录重算距离/协方差与风险评估。

        重算失败时保留来源记录，把评估标记为失效；可再次调用本方法从来源记录恢复。
        返回 (updated_item, error)。
        """
        item = self.repository.get_item(item_id)
        sources = self.repository.list_sources(item_id)
        new_payload, error = rules.reconcile_payload(item["payload"], sources)
        event_payload = {
            "assessment": new_payload.get("assessment"),
            "assessment_stale": new_payload.get("assessment_stale", False),
            "reconciled": True,
        }
        updated = self.repository.apply_action(
            item_id,
            "reconcile",
            actor or "system",
            role or "system",
            item["status"],
            new_payload,
            event_payload,
            expected_version,
        )
        return updated, error

    def act(self, item_id, action, payload, actor, role, expected_version=None, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        item = self.repository.get_item(item_id)
        if action == "reconcile":
            if role not in rules.ACTION_ROLES.get("reconcile", set()):
                raise DomainError("forbidden", "当前角色不能执行该操作", 403)
            updated, _ = self._reconcile(item_id, actor, role, expected_version)
            return updated
        allowed = rules.ACTION_ROLES.get(action, set())
        if role not in allowed:
            raise DomainError("forbidden", "当前角色不能执行该操作", 403)
        if rules.ENFORCE_REGION and action in rules.REGION_SENSITIVE_ACTIONS and region and role != "regulator":
            if item["payload"].get("region") != region:
                raise DomainError("region_mismatch", "不能处理其他区域的记录", 403)
        if action in rules.ACTION_REQUIRES_VERSION and expected_version is None:
            raise DomainError("expected_version_required", "该操作需要 expected_version", 400)
        new_status, new_payload, event_payload = rules.apply_action(item, action, payload, actor, role)
        self.repository.apply_action(
            item_id, action, actor, role, new_status, new_payload, event_payload, expected_version
        )
        return self.get_item(item_id)

    def get_item(self, item_id):
        item = self.repository.get_item(item_id)
        item["sources"] = self.repository.list_sources(item_id)
        item["audit"] = self.repository.audit_trail(item_id)
        if "assessment" in item["payload"]:
            item["assessment"] = item["payload"]["assessment"]
        else:
            item["assessment"] = rules.assess(item["payload"])
        item["assessment_stale"] = item["payload"].get("assessment_stale", False)
        return item

    def list_items(self, status=None):
        return self.repository.list_items(status)

    def state(self):
        return self.repository.state_summary()
