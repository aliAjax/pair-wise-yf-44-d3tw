from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError
from .rules import RuleEngine


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        if entity["kind"] == "change" and action == "rollback":
            self._generate_recovery_checklist(updated, actor)
        return updated

    def _generate_recovery_checklist(self, change, actor):
        """回退时依据实施前保存的基线快照逐项生成恢复清单。"""
        existing = self._lookup("recovery_item", "change_id", change["id"])
        if existing:
            return existing
        baseline = change["data"].get("baseline") or {}
        owner = change["data"].get("recovery_owner", "")
        created = []
        for category, label in (("parameters", "参数"), ("isolations", "隔离")):
            for index, entry in enumerate(baseline.get(category, []), start=1):
                ref = "%s-%d" % ("P" if category == "parameters" else "I", index)
                item_data = {
                    "change_id": change["id"],
                    "category": category,
                    "ref": ref,
                    "name": entry.get("name", ""),
                    "baseline": entry.get("baseline", ""),
                    "unit": entry.get("unit", ""),
                    "note": entry.get("note", ""),
                    "label": label,
                    "owner": owner,
                }
                item = self.repository.create_entity(
                    str(uuid4()), "recovery_item", "pending", item_data, "system"
                )
                self.audit.record(
                    item["id"],
                    actor,
                    "create",
                    None,
                    "pending",
                    {"kind": "recovery_item", "change_id": change["id"], "ref": ref},
                )
                created.append(item)
        return created

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None, filters=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        items = self.repository.list_entities(kind=kind, status=status)
        filters = filters or {}
        for field, value in filters.items():
            if value is None:
                continue
            items = [item for item in items if str(item["data"].get(field, "")) == str(value)]
        return items

    def recovery_checklist(self, change_id):
        return self._lookup("recovery_item", "change_id", change_id)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
