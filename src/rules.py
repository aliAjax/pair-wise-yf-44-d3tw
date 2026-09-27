from datetime import datetime, timedelta

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


def _validate_change(actor, data, lookup):
    unit = _find_one(lookup, "unit", "id", data.get("unit_id"))
    if not unit:
        raise ValidationError("unit does not exist")
    if not data.get("description", "").strip():
        raise ValidationError("change description is required")


def _validate_restore_item(actor, data, lookup):
    change = _find_one(lookup, "change", "id", data.get("change_id"))
    if not change:
        raise ValidationError("change does not exist")
    if not data.get("label", "").strip():
        raise ValidationError("restore item label is required")


def required_approval_level(risk_level):
    levels = {"low": 1, "medium": 2, "high": 3, "critical": 4}
    return levels.get(str(risk_level).lower(), 4)


def _validate_assess(actor, entity, data, lookup):
    return {"required_approvals": required_approval_level(data.get("risk_level"))}


def _validate_approve(actor, entity, data, lookup):
    required = int(entity["data"].get("required_approvals", 1))
    approvals = data.get("approvals") or []
    if len(set(approvals)) < required:
        raise ValidationError("not enough distinct approvals")
    return {"approved_by": actor.user_id}


def _validate_implement(actor, entity, data, lookup):
    parameters = data.get("key_parameters") or []
    for param in parameters:
        if not isinstance(param, dict) or not str(param.get("name", "")).strip():
            raise ValidationError("each key parameter needs a name")
        if param.get("value") in (None, ""):
            raise ValidationError("key parameter %s needs a value" % param.get("name"))
    isolations = data.get("isolation_measures") or []
    for measure in isolations:
        if not isinstance(measure, dict) or not str(measure.get("tag", "")).strip():
            raise ValidationError("each isolation measure needs a tag")
        if measure.get("position") in (None, ""):
            raise ValidationError("isolation measure %s needs a position" % measure.get("tag"))
    unit = _find_one(lookup, "unit", "id", entity["data"].get("unit_id"))
    baseline = {
        "unit_id": entity["data"].get("unit_id"),
        "unit_status": unit["status"] if unit else "unknown",
        "parameters": [
            {"name": param["name"], "value": param["value"]} for param in parameters
        ],
        "isolations": [
            {"tag": measure["tag"], "position": measure["position"]}
            for measure in isolations
        ],
        "restoration_owner": data.get("restoration_owner"),
        "captured_by": actor.user_id,
    }
    return {"baseline": baseline, "implemented_by": actor.user_id}


def _validate_rollback(actor, entity, data, lookup):
    if not entity["data"].get("baseline"):
        raise ValidationError("no pre-implementation baseline recorded")
    return {"rolled_back_by": actor.user_id}


def _validate_restore(actor, entity, data, lookup):
    return {"restored_by": actor.user_id}


def _validate_review_restoration(actor, entity, data, lookup):
    restored_by = entity["data"].get("restored_by")
    if restored_by and restored_by == actor.user_id:
        raise PermissionDenied("restorer cannot review their own restoration")
    change = _find_one(lookup, "change", "id", entity["data"].get("change_id"))
    if change:
        participants = {
            change["data"].get("implemented_by"),
            change["data"].get("commissioned_by"),
        }
        if actor.user_id in participants:
            raise PermissionDenied("implementation participant cannot review restoration")
    return {"reviewed_by": actor.user_id}


def _validate_close(actor, entity, data, lookup):
    items = lookup("restore_item", "change_id", entity["id"]) if lookup else []
    unit = _find_one(lookup, "unit", "id", entity["data"].get("unit_id"))
    unfinished = unfinished_restoration_items(
        items or [], unit["status"] if unit else None
    )
    if unfinished:
        raise ValidationError("unfinished restoration items: " + ", ".join(unfinished))
    return {"closed_by": actor.user_id}


def _validate_commission(actor, entity, data, lookup):
    items = lookup("action_item", "change_id", entity["id"]) or [] if lookup else []
    unresolved = [item["id"] for item in items if item["status"] != "verified"]
    if unresolved:
        raise ValidationError("unresolved action items: " + ", ".join(unresolved))
    return {"commissioned_by": actor.user_id}


CUSTOM_CREATE = {'change': _validate_change, 'restore_item': _validate_restore_item}
CUSTOM_TRANSITIONS = {('change', 'assess'): _validate_assess, ('change', 'approve'): _validate_approve, ('change', 'implement'): _validate_implement, ('change', 'commission'): _validate_commission, ('change', 'rollback'): _validate_rollback, ('change', 'close'): _validate_close, ('restore_item', 'restore'): _validate_restore, ('restore_item', 'review'): _validate_review_restoration}


# Unit statuses that require an independent safety review of every
# restoration item before the change can be closed.
REVIEW_REQUIRED_UNIT_STATUSES = ("shutdown", "frozen")


def restoration_checklist_items(change):
    """Build restoration checklist payloads from the stored baseline."""
    baseline = change.get("data", {}).get("baseline") or {}
    items = []
    unit_status = baseline.get("unit_status")
    if unit_status:
        items.append({
            "category": "unit_status",
            "label": "恢复装置运行状态至 %s" % unit_status,
            "target_value": unit_status,
        })
    for param in baseline.get("parameters", []):
        items.append({
            "category": "parameter",
            "label": "恢复工艺参数 %s 至 %s" % (param.get("name"), param.get("value")),
            "target_value": param.get("value"),
            "parameter": param.get("name"),
        })
    for measure in baseline.get("isolations", []):
        items.append({
            "category": "isolation",
            "label": "恢复隔离措施 %s 至 %s" % (measure.get("tag"), measure.get("position")),
            "target_value": measure.get("position"),
            "tag": measure.get("tag"),
        })
    for item in items:
        item["change_id"] = change["id"]
        item["owner"] = baseline.get("restoration_owner")
    return items


def unfinished_restoration_items(items, unit_status):
    """Ids of checklist items that still block closing the change."""
    if unit_status in REVIEW_REQUIRED_UNIT_STATUSES:
        done = ("verified",)
    else:
        done = ("restored", "verified")
    return [item["id"] for item in items if item["status"] not in done]


class RuleEngine:
    ALIASES = {'units': 'unit', 'changes': 'change', 'action_items': 'action_item', 'restore_items': 'restore_item'}
    INITIAL_STATUS = {'unit': 'operating', 'change': 'draft', 'action_item': 'open', 'restore_item': 'pending'}
    TRANSITIONS = {'unit': {'shutdown': (('operating',), 'shutdown'), 'startup': (('shutdown',), 'operating'), 'freeze': (('operating',), 'frozen'), 'unfreeze': (('frozen',), 'operating')}, 'change': {'assess': (('draft',), 'assessed'), 'approve': (('assessed',), 'approved'), 'implement': (('approved',), 'implemented'), 'commission': (('implemented',), 'commissioned'), 'rollback': (('implemented', 'commissioned'), 'rolled_back'), 'close': (('rolled_back',), 'closed')}, 'action_item': {'complete': (('open',), 'completed'), 'verify': (('completed',), 'verified'), 'reopen': (('verified',), 'open')}, 'restore_item': {'restore': (('pending',), 'restored'), 'review': (('restored',), 'verified'), 'reopen': (('restored', 'verified'), 'pending')}}
    CREATE_REQUIRED = {'unit': ('name', 'location'), 'change': ('unit_id', 'description'), 'action_item': ('change_id', 'description', 'owner'), 'restore_item': ('change_id', 'label', 'owner')}
    ACTION_REQUIRED = {('unit', 'shutdown'): ('reason',), ('unit', 'freeze'): ('reason',), ('change', 'assess'): ('risk_level', 'analyst'), ('change', 'approve'): ('approvals', 'permit_id'), ('change', 'implement'): ('procedure_version', 'key_parameters', 'restoration_owner'), ('change', 'commission'): ('tests_passed',), ('change', 'rollback'): ('reason',), ('change', 'close'): ('outcome',), ('action_item', 'complete'): ('completed_by', 'evidence'), ('action_item', 'verify'): ('verifier',), ('action_item', 'reopen'): ('reason',), ('restore_item', 'restore'): ('result',), ('restore_item', 'reopen'): ('reason',)}
    CREATE_ROLES = {'unit': ('admin', 'engineer'), 'change': ('admin', 'engineer'), 'action_item': ('admin', 'safety'), 'restore_item': ('admin', 'safety')}
    ROLE_ACTIONS = {'shutdown': ('admin', 'operator'), 'startup': ('admin', 'operator'), 'freeze': ('admin', 'operator'), 'unfreeze': ('admin', 'operator'), 'assess': ('admin', 'engineer'), 'approve': ('admin', 'safety'), 'implement': ('admin', 'engineer'), 'commission': ('admin', 'engineer'), 'rollback': ('admin', 'engineer'), 'close': ('admin', 'safety'), 'complete': ('admin', 'engineer'), 'verify': ('admin', 'verifier'), 'reopen': ('admin', 'verifier'), ('restore_item', 'restore'): ('admin', 'engineer', 'operator'), ('restore_item', 'review'): ('admin', 'safety'), ('restore_item', 'reopen'): ('admin', 'safety')}

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def _ensure_role(actor, allowed):
        if "*" not in allowed and actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = CUSTOM_CREATE.get(kind)
        if custom:
            custom(actor, data, lookup)
        return dict(data)

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, entity["status"])
            )
        allowed_roles = self.ROLE_ACTIONS.get(
            (kind, action), self.ROLE_ACTIONS.get(action, ("admin",))
        )
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
