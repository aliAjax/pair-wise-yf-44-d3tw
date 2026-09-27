import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


IMPLEMENT_DATA = {
    "procedure_version": "v3",
    "restoration_owner": "U-9",
    "key_parameters": [
        {"name": "reactor_temp", "value": "180C"},
        {"name": "feed_rate", "value": "12t/h"},
    ],
    "isolation_measures": [{"tag": "XV-101", "position": "closed"}],
}


class RestorationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def _implemented_change(self):
        unit = self.service.create(
            self.admin, "unit", {"name": "Reactor-1", "location": "Plant-A"}
        )
        change = self.service.create(
            self.admin,
            "change",
            {"unit_id": unit["id"], "description": "调整联锁值"},
        )
        self.service.transition(
            self.admin, change["id"], "assess", {"risk_level": "high", "analyst": "E-1"}
        )
        self.service.transition(
            self.admin,
            change["id"],
            "approve",
            {"approvals": ["S-1", "S-2", "S-3"], "permit_id": "MOC-9"},
        )
        change = self.service.transition(
            Actor("eng-1", "engineer"), change["id"], "implement", dict(IMPLEMENT_DATA)
        )
        return unit, change

    def _rollback(self, change):
        return self.service.transition(
            self.admin, change["id"], "rollback", {"reason": "投用后效果不达标"}
        )

    def _checklist(self, change_id):
        return self.service.list("restore_item", data_filters={"change_id": change_id})

    def _restore_all(self, change_id, restorer):
        for item in self._checklist(change_id):
            self.service.transition(
                restorer, item["id"], "restore", {"result": "已恢复原定值"}
            )

    def test_baseline_saved_on_implement(self):
        unit, change = self._implemented_change()
        baseline = change["data"]["baseline"]
        self.assertEqual(baseline["unit_status"], "operating")
        self.assertEqual(baseline["restoration_owner"], "U-9")
        self.assertEqual(
            [p["name"] for p in baseline["parameters"]], ["reactor_temp", "feed_rate"]
        )
        self.assertEqual(baseline["isolations"][0]["tag"], "XV-101")
        self.assertEqual(change["data"]["implemented_by"], "eng-1")

    def test_implement_requires_baseline_fields(self):
        unit = self.service.create(
            self.admin, "unit", {"name": "U-1", "location": "Plant-A"}
        )
        change = self.service.create(
            self.admin, "change", {"unit_id": unit["id"], "description": "d"}
        )
        self.service.transition(
            self.admin, change["id"], "assess", {"risk_level": "low", "analyst": "E-1"}
        )
        self.service.transition(
            self.admin,
            change["id"],
            "approve",
            {"approvals": ["S-1"], "permit_id": "MOC-1"},
        )
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.admin, change["id"], "implement", {"procedure_version": "v1"}
            )
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.admin,
                change["id"],
                "implement",
                {
                    "procedure_version": "v1",
                    "restoration_owner": "U-9",
                    "key_parameters": [{"name": "temp"}],
                },
            )

    def test_rollback_generates_checklist(self):
        unit, change = self._implemented_change()
        self._rollback(change)
        items = self._checklist(change["id"])
        self.assertEqual(len(items), 4)
        self.assertTrue(all(item["status"] == "pending" for item in items))
        self.assertTrue(all(item["data"]["owner"] == "U-9" for item in items))
        categories = sorted(item["data"]["category"] for item in items)
        self.assertEqual(categories, ["isolation", "parameter", "parameter", "unit_status"])

    def test_close_blocked_until_checklist_done(self):
        unit, change = self._implemented_change()
        self._rollback(change)
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.admin, change["id"], "close", {"outcome": "已恢复"}
            )

    def test_restore_items_one_by_one_then_close_when_operating(self):
        unit, change = self._implemented_change()
        self._rollback(change)
        restorer = Actor("U-9", "operator")
        self._restore_all(change["id"], restorer)
        items = self._checklist(change["id"])
        self.assertTrue(all(item["status"] == "restored" for item in items))
        self.assertTrue(all(item["data"]["restored_by"] == "U-9" for item in items))
        closed = self.service.transition(
            self.admin, change["id"], "close", {"outcome": "已恢复原状"}
        )
        self.assertEqual(closed["status"], "closed")

    def test_shutdown_unit_requires_safety_review(self):
        unit, change = self._implemented_change()
        self._rollback(change)
        self.service.transition(
            self.admin, unit["id"], "shutdown", {"reason": "回退后保持停机"}
        )
        self._restore_all(change["id"], Actor("U-9", "operator"))
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.admin, change["id"], "close", {"outcome": "已恢复"}
            )
        reviewer = Actor("S-7", "safety")
        for item in self._checklist(change["id"]):
            reviewed = self.service.transition(reviewer, item["id"], "review", {})
            self.assertEqual(reviewed["status"], "verified")
            self.assertEqual(reviewed["data"]["reviewed_by"], "S-7")
        closed = self.service.transition(
            self.admin, change["id"], "close", {"outcome": "已恢复原状"}
        )
        self.assertEqual(closed["status"], "closed")

    def test_restorer_cannot_self_review(self):
        unit, change = self._implemented_change()
        self._rollback(change)
        item = self._checklist(change["id"])[0]
        self.service.transition(
            Actor("U-9", "operator"), item["id"], "restore", {"result": "已恢复"}
        )
        with self.assertRaises(PermissionDenied):
            self.service.transition(Actor("U-9", "safety"), item["id"], "review", {})

    def test_implementation_participant_cannot_review(self):
        unit, change = self._implemented_change()
        self._rollback(change)
        item = self._checklist(change["id"])[0]
        self.service.transition(
            Actor("U-9", "operator"), item["id"], "restore", {"result": "已恢复"}
        )
        with self.assertRaises(PermissionDenied):
            self.service.transition(Actor("eng-1", "safety"), item["id"], "review", {})

    def test_non_safety_role_cannot_review(self):
        unit, change = self._implemented_change()
        self._rollback(change)
        item = self._checklist(change["id"])[0]
        self.service.transition(
            Actor("U-9", "operator"), item["id"], "restore", {"result": "已恢复"}
        )
        with self.assertRaises(PermissionDenied):
            self.service.transition(Actor("E-2", "engineer"), item["id"], "review", {})

    def test_reopen_sends_item_back_to_pending(self):
        unit, change = self._implemented_change()
        self._rollback(change)
        item = self._checklist(change["id"])[0]
        self.service.transition(
            Actor("U-9", "operator"), item["id"], "restore", {"result": "已恢复"}
        )
        reopened = self.service.transition(
            Actor("S-7", "safety"), item["id"], "reopen", {"reason": "现场复核不通过"}
        )
        self.assertEqual(reopened["status"], "pending")


if __name__ == "__main__":
    unittest.main()
