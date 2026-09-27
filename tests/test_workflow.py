import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


def _resolve(value, created):
    if isinstance(value, str):
        for key, item in created.items():
            value = value.replace("{" + key + "}", str(item))
        return value
    if isinstance(value, list):
        return [_resolve(item, created) for item in value]
    if isinstance(value, dict):
        return {key: _resolve(item, created) for key, item in value.items()}
    return value


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.engineer = Actor("eng-1", "engineer")
        self.owner = Actor("op-owner", "operator")
        self.safety = Actor("safe-1", "safety")
        self.verifier = Actor("ver-1", "verifier")

    def tearDown(self):
        self.tmp.cleanup()

    def _create_approved_implemented_change(self):
        """创建一个已实施（带基线快照）的变更，返回 (unit, change)。"""
        unit = self.service.create(
            self.admin, "unit", {"name": "Reactor-1", "location": "Plant-A"}
        )
        change = self.service.create(
            self.admin, "change",
            {"unit_id": unit["id"], "description": "Change alarm threshold"},
        )
        self.service.transition(
            self.admin, change["id"], "assess",
            {"risk_level": "medium", "analyst": "E-1"},
        )
        self.service.transition(
            self.admin, change["id"], "approve",
            {"approvals": ["S-1", "S-2"], "permit_id": "MOC-1"},
        )
        change = self.service.transition(
            self.admin, change["id"], "implement",
            {
                "procedure_version": "v2",
                "recovery_owner": self.owner.user_id,
                "parameters": [
                    {"name": "反应压力设定", "baseline": "1.2 MPa", "unit": "MPa"},
                ],
                "isolations": [
                    {"name": "进料阀 V-101", "baseline": "全开"},
                ],
            },
        )
        self.assertEqual(change["data"]["implemented_by"], "admin")
        self.assertEqual(change["data"]["baseline"]["unit_status"], "operating")
        return unit, change

    def _rolled_back(self):
        unit, change = self._create_approved_implemented_change()
        change = self.service.transition(
            self.admin, change["id"], "rollback", {"reason": "unexpected drift"}
        )
        items = self.service.recovery_checklist(change["id"])
        self.assertEqual(change["status"], "rolled_back")
        self.assertEqual(len(items), 2)
        refs = {item["data"]["ref"] for item in items}
        self.assertEqual(refs, {"P-1", "I-1"})
        for item in items:
            self.assertEqual(item["status"], "pending")
            self.assertEqual(item["data"]["owner"], self.owner.user_id)
        return unit, change, items

    def test_full_workflow(self):
        created = {}
        steps = [
            {"op": "create", "as": "unit", "kind": "unit", "data": {"name": "Reactor-1", "location": "Plant-A"}},
            {"op": "create", "as": "change", "kind": "change", "data": {"unit_id": "{unit}", "description": "Change alarm threshold"}},
            {"op": "transition", "target": "change", "action": "assess", "data": {"risk_level": "medium", "analyst": "E-1"}, "expect": "assessed"},
            {"op": "transition", "target": "change", "action": "approve", "data": {"approvals": ["S-1", "S-2"], "permit_id": "MOC-1"}, "expect": "approved"},
            {
                "op": "transition", "target": "change", "action": "implement",
                "data": {
                    "procedure_version": "v2",
                    "recovery_owner": "op-owner",
                    "parameters": [{"name": "压力设定", "baseline": "1.2 MPa", "unit": "MPa"}],
                    "isolations": [{"name": "进料阀", "baseline": "全开"}],
                },
                "expect": "implemented",
            },
            {"op": "create", "as": "item", "kind": "action_item", "data": {"change_id": "{change}", "description": "Train operators", "owner": "O-1"}},
            {"op": "transition", "target": "item", "action": "complete", "data": {"completed_by": "O-1", "evidence": "training-log"}, "expect": "completed"},
            {"op": "transition", "target": "item", "action": "verify", "data": {"verifier": "V-1"}, "expect": "verified"},
            {"op": "transition", "target": "change", "action": "commission", "data": {"tests_passed": True}, "expect": "commissioned"},
            {"op": "transition", "target": "change", "action": "rollback", "data": {"reason": "unexpected drift"}, "expect": "rolled_back"},
        ]
        for step in steps:
            if step["op"] == "create":
                entity = self.service.create(
                    self.admin,
                    step["kind"],
                    _resolve(step.get("data", {}), created),
                    step.get("idempotency_key"),
                )
                created[step["as"]] = entity["id"]
            else:
                entity = self.service.transition(
                    self.admin,
                    created[step["target"]],
                    step["action"],
                    _resolve(step.get("data", {}), created),
                    step.get("expected_version"),
                )
            if "expect" in step:
                self.assertEqual(entity["status"], step["expect"])

        # 回退后恢复清单自动生成，逐项恢复+独立复核后才能关闭
        items = self.service.recovery_checklist(created["change"])
        self.assertEqual(len(items), 2)
        for item in items:
            restored = self.service.transition(
                Actor("op-owner", "operator"), item["id"], "restore",
                {"result": "已按基线恢复 " + item["data"]["name"]},
            )
            self.assertEqual(restored["status"], "restored")
            confirmed = self.service.transition(
                self.verifier, item["id"], "confirm_restore", {}
            )
            self.assertEqual(confirmed["status"], "confirmed")

        closed = self.service.transition(
            self.admin, created["change"], "close", {"outcome": "rolled back and restored"}
        )
        self.assertEqual(closed["status"], "closed")

    def test_implement_requires_baseline_and_owner(self):
        unit = self.service.create(
            self.admin, "unit", {"name": "U-1", "location": "P"}
        )
        change = self.service.create(
            self.admin, "change", {"unit_id": unit["id"], "description": "d"}
        )
        for action, data in [
            ("assess", {"risk_level": "low", "analyst": "a"}),
            ("approve", {"approvals": ["x"], "permit_id": "p"}),
        ]:
            self.service.transition(self.admin, change["id"], action, data)
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.admin, change["id"], "implement",
                {
                    "procedure_version": "v1",
                    "recovery_owner": "op-owner",
                    "parameters": [],
                    "isolations": [],
                },
            )
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.admin, change["id"], "implement",
                {
                    "procedure_version": "v1",
                    "recovery_owner": "op-owner",
                    "parameters": [{"name": "p"}],  # 缺 baseline 原值
                    "isolations": [],
                },
            )
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.admin, change["id"], "implement",
                {
                    "procedure_version": "v1",
                    # 缺 recovery_owner
                    "parameters": [{"name": "p", "baseline": "1"}],
                    "isolations": [],
                },
            )
        # 参数与隔离措施允许其中一类为空，但两个键必须给出
        implemented = self.service.transition(
            self.admin, change["id"], "implement",
            {
                "procedure_version": "v1",
                "recovery_owner": "op-owner",
                "parameters": [{"name": "p", "baseline": "1"}],
                "isolations": [],
            },
        )
        self.assertEqual(implemented["status"], "implemented")
        self.assertEqual(len(implemented["data"]["baseline"]["parameters"]), 1)
        self.assertEqual(implemented["data"]["baseline"]["isolations"], [])

    def test_close_blocked_until_checklist_complete(self):
        unit, change, items = self._rolled_back()
        # 没有任何一项确认时不能关闭
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.admin, change["id"], "close", {"outcome": "done"}
            )
        first = items[0]
        self.service.transition(
            self.owner, first["id"], "restore", {"result": "restored"}
        )
        self.service.transition(
            self.verifier, first["id"], "confirm_restore", {}
        )
        # 仍有未完成项
        with self.assertRaises(ValidationError) as ctx:
            self.service.transition(
                self.admin, change["id"], "close", {"outcome": "done"}
            )
        self.assertIn("incomplete", str(ctx.exception))

    def test_only_owner_can_restore(self):
        unit, change, items = self._rolled_back()
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                self.admin, items[0]["id"], "restore", {"result": "x"}
            )
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                self.engineer, items[0]["id"], "restore", {"result": "x"}
            )
        self.service.transition(
            self.owner, items[0]["id"], "restore", {"result": "done"}
        )

    def test_restorer_cannot_self_confirm(self):
        unit, change, items = self._rolled_back()
        # 恢复人换成工程师角色（本身具备复核权限），确保命中的是“不能自审”
        owner = Actor("op-owner", "engineer")
        self.service.transition(
            owner, items[0]["id"], "restore", {"result": "done"}
        )
        with self.assertRaises(PermissionDenied) as ctx:
            self.service.transition(owner, items[0]["id"], "confirm_restore", {})
        self.assertIn("cannot confirm their own", str(ctx.exception))

    def test_shutdown_unit_requires_independent_safety_review(self):
        unit, change, items = self._rolled_back()
        # 装置改为停机状态
        self.service.transition(
            self.admin, unit["id"], "shutdown", {"reason": "rollback"}
        )
        self.service.transition(
            self.owner, items[0]["id"], "restore", {"result": "done"}
        )
        # 非安全员不能复核
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                self.engineer, items[0]["id"], "confirm_restore", {}
            )
        # 参加实施的人（admin 是 implemented_by）即使给了 safety 角色也不能复核
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                Actor("admin", "safety"), items[0]["id"], "confirm_restore", {}
            )
        # 未参加实施的安全员可以复核
        confirmed = self.service.transition(
            self.safety, items[0]["id"], "confirm_restore", {}
        )
        self.assertEqual(confirmed["status"], "confirmed")

    def test_frozen_unit_same_independence_rule(self):
        unit, change, items = self._rolled_back()
        self.service.transition(
            self.admin, unit["id"], "freeze", {"reason": "hold"}
        )
        self.service.transition(
            self.owner, items[0]["id"], "restore", {"result": "done"}
        )
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                Actor("safe-2", "engineer"), items[0]["id"], "confirm_restore", {}
            )
        self.service.transition(
            self.safety, items[0]["id"], "confirm_restore", {}
        )

    def test_reject_send_item_back_to_pending(self):
        unit, change, items = self._rolled_back()
        self.service.transition(
            self.owner, items[0]["id"], "restore", {"result": "first try"}
        )
        rejected = self.service.transition(
            self.safety, items[0]["id"], "reject_restore",
            {"reason": "valve not aligned"},
        )
        self.assertEqual(rejected["status"], "pending")
        # 恢复人重做
        self.service.transition(
            self.owner, items[0]["id"], "restore", {"result": "second try"}
        )
        confirmed = self.service.transition(
            self.safety, items[0]["id"], "confirm_restore", {}
        )
        self.assertEqual(confirmed["status"], "confirmed")

    def test_recovery_item_cannot_be_created_via_api(self):
        with self.assertRaises(PermissionDenied):
            self.service.create(
                self.admin, "recovery_item",
                {"change_id": "x", "name": "y", "baseline": "z", "owner": "o"},
            )

    def test_rollback_without_baseline_rejected(self):
        unit = self.service.create(
            self.admin, "unit", {"name": "U", "location": "P"}
        )
        change = self.service.create(
            self.admin, "change", {"unit_id": unit["id"], "description": "d"}
        )
        # 直接绕过 implement 造一个无基线的 implemented 实体
        entity = self.repo.update_entity(change["id"], 1, "implemented", {"unit_id": unit["id"]})
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.admin, entity["id"], "rollback", {"reason": "x"}
            )


if __name__ == "__main__":
    unittest.main()
