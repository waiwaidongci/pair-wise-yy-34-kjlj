import tempfile, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service

PAST=(datetime.now(timezone.utc)-timedelta(days=1)).replace(microsecond=0).isoformat()
FUTURE=(datetime.now(timezone.utc)+timedelta(days=7)).replace(microsecond=0).isoformat()

class RectificationTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.repo=Repository(str(Path(self.tmp.name)/"test.db")); self.service=Service(self.repo)
    def tearDown(self): self.repo.close(); self.tmp.cleanup()
    def _item(self):
        return self.service.create_item({"title":"accident","description":"workplace injury","severity":"serious","quantity":5,"threshold":10},"creator","reporter")
    def _to_corrective(self,item):
        current=self.service.get_item(item["id"],"viewer")
        for target in ("investigating","corrective_action"):
            current=self.service.transition(current["id"],target,current["version"],"investigator","investigator")
        return current
    def _rectification(self,item_id,evidence="现场照片IMG_01.jpg",due=FUTURE,actor="investigator",role="investigator"):
        payload={"kind":"rectification","detail":"更换破损防护栏","assignee":"张三","due_at":due}
        if evidence is not None: payload["evidence"]=evidence
        return self.service.add_record(item_id,payload,actor,role)
    def _record(self,item_id,record_id):
        return [r for r in self.service.list_records(item_id,"viewer") if r["id"]==record_id][0]

    def test_register_assignee_due_and_evidence(self):
        item=self._to_corrective(self._item())
        record=self._rectification(item["id"])
        self.assertEqual(record["assignee"],"张三"); self.assertEqual(record["due_at"],FUTURE)
        self.assertEqual(record["evidence"],"现场照片IMG_01.jpg")
        self.assertEqual(record["acceptance_status"],"pending")
        self.assertEqual(record["submitted_key_version"],item["key_version"])
        with self.assertRaises(ValidationError):
            self.service.add_record(item["id"],{"kind":"rectification","detail":"缺责任人","due_at":FUTURE},"investigator","investigator")
        with self.assertRaises(ValidationError):
            self.service.add_record(item["id"],{"kind":"rectification","detail":"缺到期时间","assignee":"张三"},"investigator","investigator")
        with self.assertRaises(ValidationError):
            self.service.add_record(item["id"],{"kind":"rectification","detail":"坏时间","assignee":"张三","due_at":"not-a-date"},"investigator","investigator")

    def test_overdue_boosts_priority_and_flags_list(self):
        overdue_item=self._to_corrective(self._item()); record=self._rectification(overdue_item["id"],due=PAST)
        clean_item=self._item()
        overdue_view=self.service.get_item(overdue_item["id"],"viewer"); clean_view=self.service.get_item(clean_item["id"],"viewer")
        self.assertEqual(overdue_view["overdue_rectifications"],1); self.assertTrue(overdue_view["has_overdue_rectifications"])
        self.assertGreater(overdue_view["priority"],clean_view["priority"])
        listed={i["id"]:i for i in self.service.list_items("viewer")}
        self.assertTrue(listed[overdue_item["id"]]["has_overdue_rectifications"]); self.assertFalse(listed[clean_item["id"]]["has_overdue_rectifications"])
        self.assertTrue(self._record(overdue_item["id"],record["id"])["overdue"])
        self.service.accept_record(overdue_item["id"],record["id"],"manager","safety_manager")
        accepted_view=self.service.get_item(overdue_item["id"],"viewer")
        self.assertEqual(accepted_view["overdue_rectifications"],0); self.assertFalse(accepted_view["has_overdue_rectifications"])

    def test_safety_manager_cannot_accept_own_submission(self):
        item=self._to_corrective(self._item())
        record=self._rectification(item["id"],actor="manager-a",role="safety_manager")
        with self.assertRaises(PermissionDenied):
            self.service.accept_record(item["id"],record["id"],"manager-a","safety_manager")
        accepted=self.service.accept_record(item["id"],record["id"],"manager-b","safety_manager")
        self.assertEqual(accepted["acceptance_status"],"accepted"); self.assertEqual(accepted["accepted_by"],"manager-b")
        self.assertEqual(accepted["status"],"closed"); self.assertTrue(accepted["acceptance_valid"])

    def test_missing_evidence_is_returned(self):
        item=self._to_corrective(self._item())
        record=self._rectification(item["id"],evidence=None)
        with self.assertRaises(ConflictError):
            self.service.accept_record(item["id"],record["id"],"manager","safety_manager")
        returned=self._record(item["id"],record["id"])
        self.assertEqual(returned["acceptance_status"],"returned"); self.assertIn("证据",returned["last_return_reason"])
        view=self.service.get_item(item["id"],"viewer")
        self.assertEqual(view["last_return"]["record_id"],record["id"]); self.assertIn("证据",view["last_return"]["reason"])
        self.assertTrue(any("退回" in blocker for blocker in view["blockers"]))

    def test_key_info_change_returns_submission(self):
        item=self._to_corrective(self._item())
        record=self._rectification(item["id"])
        current=self.service.get_item(item["id"],"viewer")
        self.service.update_item(item["id"],{"severity":"fatal"},current["version"],"investigator","investigator")
        with self.assertRaises(ConflictError):
            self.service.accept_record(item["id"],record["id"],"manager","safety_manager")
        returned=self._record(item["id"],record["id"])
        self.assertEqual(returned["acceptance_status"],"returned"); self.assertIn("变更",returned["last_return_reason"])

    def test_verification_requires_current_version_acceptance(self):
        item=self._to_corrective(self._item())
        record=self._rectification(item["id"])
        current=self.service.get_item(item["id"],"viewer")
        with self.assertRaises(ConflictError) as ctx:
            self.service.transition(item["id"],"verification",current["version"],"manager","safety_manager")
        self.assertIn("整改记录#",str(ctx.exception))
        self.assertTrue(self.service.get_item(item["id"],"viewer")["blockers"])
        self.service.accept_record(item["id"],record["id"],"manager","safety_manager")
        current=self.service.get_item(item["id"],"viewer")
        self.assertEqual(current["blockers"],[])
        updated=self.service.transition(item["id"],"verification",current["version"],"manager","safety_manager")
        self.assertEqual(updated["status"],"verification")

    def test_stale_acceptance_needs_resubmit_and_reacceptance(self):
        item=self._to_corrective(self._item())
        record=self._rectification(item["id"])
        self.service.accept_record(item["id"],record["id"],"manager","safety_manager")
        current=self.service.get_item(item["id"],"viewer")
        self.service.update_item(item["id"],{"quantity":9},current["version"],"investigator","investigator")
        view=self.service.get_item(item["id"],"viewer")
        self.assertTrue(view["blockers"])
        with self.assertRaises(ConflictError):
            self.service.transition(item["id"],"verification",view["version"],"manager","safety_manager")
        self.service.resubmit_record(item["id"],record["id"],{"evidence":"整改后照片IMG_02.jpg"},"investigator","investigator")
        self.service.accept_record(item["id"],record["id"],"manager","safety_manager")
        view=self.service.get_item(item["id"],"viewer")
        self.assertEqual(view["blockers"],[])
        done=self.service.transition(item["id"],"verification",view["version"],"manager","safety_manager")
        self.assertEqual(done["status"],"verification")

    def test_resubmit_restores_pending_and_keeps_return_note(self):
        item=self._to_corrective(self._item())
        record=self._rectification(item["id"],evidence=None)
        with self.assertRaises(ConflictError):
            self.service.accept_record(item["id"],record["id"],"manager","safety_manager")
        current=self.service.get_item(item["id"],"viewer")
        resubmitted=self.service.resubmit_record(item["id"],record["id"],{"evidence":"补充照片IMG_03.jpg"},"investigator","investigator")
        self.assertEqual(resubmitted["acceptance_status"],"pending")
        self.assertEqual(resubmitted["submitted_key_version"],current["key_version"])
        self.assertIn("证据",resubmitted["last_return_reason"])
        accepted=self.service.accept_record(item["id"],record["id"],"manager","safety_manager")
        self.assertEqual(accepted["acceptance_status"],"accepted"); self.assertTrue(accepted["acceptance_valid"])

    def test_manual_return_note_visible_in_detail_and_list(self):
        item=self._to_corrective(self._item())
        record=self._rectification(item["id"])
        with self.assertRaises(PermissionDenied):
            self.service.return_record(item["id"],record["id"],{"reason":"x"},"investigator","investigator")
        self.service.return_record(item["id"],record["id"],{"reason":"现场照片模糊"},"manager","safety_manager")
        view=self.service.get_item(item["id"],"viewer")
        self.assertEqual(view["last_return"]["reason"],"现场照片模糊")
        self.assertTrue(any("现场照片模糊" in blocker for blocker in view["blockers"]))
        listed={i["id"]:i for i in self.service.list_items("viewer")}
        self.assertEqual(listed[item["id"]]["last_return"]["reason"],"现场照片模糊")

    def test_update_item_version_role_and_state_guards(self):
        item=self._item()
        with self.assertRaises(ConflictError):
            self.service.update_item(item["id"],{"severity":"fatal"},99,"investigator","investigator")
        with self.assertRaises(PermissionDenied):
            self.service.update_item(item["id"],{"severity":"fatal"},item["version"],"reporter","reporter")
        updated=self.service.update_item(item["id"],{"severity":"fatal"},item["version"],"investigator","investigator")
        self.assertEqual(updated["severity"],"fatal")
        self.assertEqual(updated["key_version"],item["key_version"]+1); self.assertEqual(updated["version"],item["version"]+1)
        current=self._to_corrective(updated)
        record=self._rectification(current["id"])
        self.service.accept_record(current["id"],record["id"],"manager","safety_manager")
        current=self.service.get_item(current["id"],"viewer")
        in_verification=self.service.transition(current["id"],"verification",current["version"],"manager","safety_manager")
        with self.assertRaises(ConflictError):
            self.service.update_item(in_verification["id"],{"severity":"minor"},in_verification["version"],"investigator","investigator")

    def test_non_rectification_record_has_no_acceptance_flow(self):
        item=self._item()
        record=self.service.add_record(item["id"],{"kind":"evidence","detail":"现场照片"},"investigator","investigator")
        with self.assertRaises(ValidationError):
            self.service.accept_record(item["id"],record["id"],"manager","safety_manager")

    def test_full_ledger_workflow_to_close_and_audit(self):
        item=self._to_corrective(self._item())
        record=self._rectification(item["id"])
        self.service.accept_record(item["id"],record["id"],"manager","safety_manager")
        current=self.service.get_item(item["id"],"viewer")
        for target in ("verification","closed"):
            current=self.service.transition(current["id"],target,current["version"],"manager","safety_manager")
        self.assertEqual(current["status"],"closed")
        actions=[event["action"] for event in self.service.audit("viewer",current["id"])]
        self.assertIn("accept",actions); self.assertTrue(self.repo.verify_audit_chain())

if __name__=="__main__": unittest.main()
