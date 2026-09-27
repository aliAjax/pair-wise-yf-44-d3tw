from datetime import datetime, timedelta, timezone

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _validate_change(actor, data, lookup):
    unit = _find_one(lookup, "unit", "id", data.get("unit_id"))
    if not unit:
        raise ValidationError("unit does not exist")
    if not data.get("description", "").strip():
        raise ValidationError("change description is required")


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


def _validate_commission(actor, entity, data, lookup):
    items = lookup("action_item", "change_id", entity["id"]) or [] if lookup else []
    unresolved = [item["id"] for item in items if item["status"] != "verified"]
    if unresolved:
        raise ValidationError("unresolved action items: " + ", ".join(unresolved))
    return {"commissioned_by": actor.user_id}


def _baseline_entries(items, label):
    """Turn user-supplied parameter/isolation rows into a clean baseline list."""
    entries = []
    if not isinstance(items, list):
        raise ValidationError(label + " must be a list")
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValidationError("%s[%d] must be an object" % (label, index))
        name = str(item.get("name", "")).strip()
        if not name:
            raise ValidationError("%s[%d] name is required" % (label, index))
        baseline = str(item.get("baseline", "")).strip()
        if not baseline:
            raise ValidationError("%s[%d] baseline is required" % (label, index))
        entries.append(
            {
                "name": name,
                "baseline": baseline,
                "unit": str(item.get("unit", "") or "").strip(),
                "note": str(item.get("note", "") or "").strip(),
            }
        )
    return entries


def _validate_implement(actor, entity, data, lookup):
    """实施前保存受影响装置状态、关键工艺参数、隔离措施和恢复负责人。"""
    unit = _find_one(lookup, "unit", "id", entity["data"].get("unit_id"))
    if not unit:
        raise ValidationError("unit does not exist")
    for key in ("parameters", "isolations"):
        if key not in data:
            raise ValidationError("missing required field: " + key)
    parameters = _baseline_entries(data.get("parameters"), "parameters")
    isolations = _baseline_entries(data.get("isolations"), "isolations")
    if not parameters and not isolations:
        raise ValidationError(
            "at least one baseline parameter or isolation measure is required before implementation"
        )
    recovery_owner = str(data.get("recovery_owner", "")).strip()
    if not recovery_owner:
        raise ValidationError("recovery_owner is required before implementation")
    return {
        "implemented_by": actor.user_id,
        "recovery_owner": recovery_owner,
        "baseline": {
            "captured_at": _now(),
            "captured_by": actor.user_id,
            "unit_id": unit["id"],
            "unit_name": unit["data"].get("name", ""),
            "unit_status": unit["status"],
            "parameters": parameters,
            "isolations": isolations,
        },
    }


def _validate_rollback(actor, entity, data, lookup):
    if not entity["data"].get("baseline"):
        raise ValidationError(
            "baseline snapshot is missing; rollback checklist cannot be generated"
        )
    return {"rolled_back_by": actor.user_id}


def _load_parent_change(entity, lookup):
    change = _find_one(lookup, "change", "id", entity["data"].get("change_id"))
    if not change:
        raise ValidationError("parent change does not exist")
    return change


def _validate_restore(actor, entity, data, lookup):
    """恢复人逐项填写恢复结果；结果非空，恢复动作进入待复核状态。"""
    change = _load_parent_change(entity, lookup)
    owner = str(entity["data"].get("owner", ""))
    if actor.user_id != owner:
        raise PermissionDenied(
            "only the assigned recovery owner (%s) can restore this item" % owner
        )
    result = str(data.get("result", "")).strip()
    if not result:
        raise ValidationError("recovery result is required")
    return {
        "restored_by": actor.user_id,
        "restored_at": _now(),
        "result": result,
    }


def _validate_confirm_restore(actor, entity, data, lookup):
    """恢复人本人不能自审；装置仍停机/冻结时须由未参加实施的安全员复核。"""
    restorer = entity["data"].get("restored_by")
    if actor.user_id == restorer:
        raise PermissionDenied("restorer cannot confirm their own recovery item")
    change = _load_parent_change(entity, lookup)
    baseline = change["data"].get("baseline") or {}
    unit_id = baseline.get("unit_id")
    unit_status = baseline.get("unit_status")
    if lookup and unit_id:
        unit = _find_one(lookup, "unit", "id", unit_id)
        if unit:
            unit_status = unit["status"]
    if unit_status in ("shutdown", "frozen"):
        if actor.role != "safety":
            raise PermissionDenied(
                "unit is %s: only a safety officer independent of implementation can confirm"
                % unit_status
            )
        implementer = change["data"].get("implemented_by")
        implementers = change["data"].get("implementers")
        involved = {implementer} if implementer else set()
        if isinstance(implementers, list):
            involved.update(implementers)
        if actor.user_id in involved:
            raise PermissionDenied(
                "reviewer must not have participated in the implementation"
            )
    return {"confirmed_by": actor.user_id, "confirmed_at": _now()}


def _validate_close(actor, entity, data, lookup):
    """回退后的变更必须在恢复清单全部逐项确认完成后才能关闭。"""
    if entity["status"] != "rolled_back":
        return {}
    items = lookup("recovery_item", "change_id", entity["id"]) or [] if lookup else []
    if not items:
        raise ValidationError("rollback recovery checklist is empty")
    pending = [item["data"].get("ref", item["id"]) for item in items if item["status"] != "confirmed"]
    if pending:
        raise ValidationError(
            "recovery checklist incomplete, unfinished items: " + ", ".join(str(x) for x in pending)
        )
    return {"closed_by": actor.user_id}


CUSTOM_CREATE = {'change': _validate_change}
CUSTOM_TRANSITIONS = {
    ('change', 'assess'): _validate_assess,
    ('change', 'approve'): _validate_approve,
    ('change', 'implement'): _validate_implement,
    ('change', 'commission'): _validate_commission,
    ('change', 'rollback'): _validate_rollback,
    ('change', 'close'): _validate_close,
    ('recovery_item', 'restore'): _validate_restore,
    ('recovery_item', 'confirm_restore'): _validate_confirm_restore,
}


class RuleEngine:
    ALIASES = {
        'units': 'unit',
        'changes': 'change',
        'action_items': 'action_item',
        'recovery_items': 'recovery_item',
    }
    INITIAL_STATUS = {
        'unit': 'operating',
        'change': 'draft',
        'action_item': 'open',
        'recovery_item': 'pending',
    }
    TRANSITIONS = {
        'unit': {
            'shutdown': (('operating',), 'shutdown'),
            'startup': (('shutdown',), 'operating'),
            'freeze': (('operating',), 'frozen'),
            'unfreeze': (('frozen',), 'operating'),
        },
        'change': {
            'assess': (('draft',), 'assessed'),
            'approve': (('assessed',), 'approved'),
            'implement': (('approved',), 'implemented'),
            'commission': (('implemented',), 'commissioned'),
            'rollback': (('implemented', 'commissioned'), 'rolled_back'),
            'close': (('rolled_back',), 'closed'),
        },
        'action_item': {
            'complete': (('open',), 'completed'),
            'verify': (('completed',), 'verified'),
            'reopen': (('verified',), 'open'),
        },
        'recovery_item': {
            'restore': (('pending',), 'restored'),
            'confirm_restore': (('restored',), 'confirmed'),
            'reject_restore': (('restored',), 'pending'),
        },
    }
    CREATE_REQUIRED = {
        'unit': ('name', 'location'),
        'change': ('unit_id', 'description'),
        'action_item': ('change_id', 'description', 'owner'),
    }
    ACTION_REQUIRED = {
        ('unit', 'shutdown'): ('reason',),
        ('unit', 'freeze'): ('reason',),
        ('change', 'assess'): ('risk_level', 'analyst'),
        ('change', 'approve'): ('approvals', 'permit_id'),
        ('change', 'implement'): ('procedure_version', 'recovery_owner'),
        ('change', 'commission'): ('tests_passed',),
        ('change', 'rollback'): ('reason',),
        ('change', 'close'): ('outcome',),
        ('action_item', 'complete'): ('completed_by', 'evidence'),
        ('action_item', 'verify'): ('verifier',),
        ('action_item', 'reopen'): ('reason',),
        ('recovery_item', 'restore'): ('result',),
        ('recovery_item', 'confirm_restore'): (),
        ('recovery_item', 'reject_restore'): ('reason',),
    }
    CREATE_ROLES = {
        'unit': ('admin', 'engineer'),
        'change': ('admin', 'engineer'),
        'action_item': ('admin', 'safety'),
        # recovery items are generated by the system on rollback, never via API
        'recovery_item': (),
    }
    ROLE_ACTIONS = {
        'shutdown': ('admin', 'operator'),
        'startup': ('admin', 'operator'),
        'freeze': ('admin', 'operator'),
        'unfreeze': ('admin', 'operator'),
        'assess': ('admin', 'engineer'),
        'approve': ('admin', 'safety'),
        'implement': ('admin', 'engineer'),
        'commission': ('admin', 'engineer'),
        'rollback': ('admin', 'engineer'),
        'close': ('admin', 'safety'),
        'complete': ('admin', 'engineer'),
        'verify': ('admin', 'verifier'),
        'reopen': ('admin', 'verifier'),
        ('recovery_item', 'restore'): ('admin', 'engineer', 'operator'),
        ('recovery_item', 'confirm_restore'): ('admin', 'safety', 'verifier', 'engineer'),
        ('recovery_item', 'reject_restore'): ('admin', 'safety', 'verifier', 'engineer'),
    }

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
