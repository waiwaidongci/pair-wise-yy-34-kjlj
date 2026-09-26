import tempfile, unittest
from pathlib import Path
from datetime import datetime, timedelta, timezone
from src.domain import ConflictError, PermissionDenied
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSITION_ROLES


def iso(offset_hours):
    return (datetime.now(timezone.utc) + timedelta(hours=offset_hours)).replace(microsecond=0).isoformat()


class FailureTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.repo=Repository(str(Path(self.tmp.name)/"test.db")); self.service=Service(self.repo)
        self.item=self.service.create_item({"title":"failure item","description":"failure scenarios","severity":'serious',"quantity":5,"threshold":10,"external_ref":"FAIL-1"},"creator",'reporter')
    def tearDown(self): self.repo.close(); self.tmp.cleanup()

    def _to_corrective(self):
        current=self.service.get_item(self.item["id"],"viewer")
        for target in (STATES[1], STATES[2]):
            current=self.service.transition(current["id"],target,current["version"],"reviewer",TRANSITION_ROLES[target][0])
        return self.service.get_item(current["id"],"viewer")

    def test_permission_version_duplicate_and_invariant(self):
        with self.assertRaises(PermissionDenied): self.service.transition(self.item["id"],STATES[1],1,"attacker","viewer")
        with self.assertRaises(ConflictError): self.service.transition(self.item["id"],STATES[1],99,"reviewer",TRANSITION_ROLES[STATES[1]][0])
        payload={"kind":"action","detail":"same reference","status":"open","external_ref":"DUP-1"}
        self.service.add_record(self.item["id"],payload,"recorder",'investigator')
        with self.assertRaises(ConflictError): self.service.add_record(self.item["id"],payload,"recorder",'investigator')
        current=self._to_corrective()
        # 没有任何整改：进入验证被阻断，且能看到阻断原因
        detail=self.service.get_item(current["id"],"viewer")
        self.assertTrue(detail["blocked"])
        self.assertEqual(detail["block_reasons"][0]["reason"],"尚未登记整改措施")
        with self.assertRaises(ConflictError): self.service.transition(current["id"],STATES[3],current["version"],"reviewer",TRANSITION_ROLES[STATES[3]][0])

    def test_self_acceptance_forbidden(self):
        current=self._to_corrective()
        rect=self.service.register_rectification(current["id"],{"owner":"李四","due_at":iso(24)},"sm",'safety_manager')
        self.service.submit_rectification(current["id"],rect["id"],{"evidence":"照片X"},"sm",'safety_manager')
        with self.assertRaises(PermissionDenied):
            self.service.accept_rectification(current["id"],rect["id"],{},"sm",'safety_manager')

    def test_missing_evidence_is_returned(self):
        current=self._to_corrective()
        rect=self.service.register_rectification(current["id"],{"owner":"王五","due_at":iso(24),"evidence":None},"inv",'investigator')
        # 不补证据直接提交
        self.service.submit_rectification(current["id"],rect["id"],{"evidence":"临时说明"},"inv",'investigator')
        self.service.return_rectification(current["id"],rect["id"],{"note":"证据不合格"},"sm",'safety_manager')
        self.service.submit_rectification(current["id"],rect["id"],{},"inv",'investigator')
        view=self.service.list_rectifications(current["id"],"viewer")[0]
        self.assertEqual(view["status"],"submitted"); self.assertTrue(view["evidence"])
        # 人为清掉证据后验收，应被系统强制退回
        self.repo.update_rectification(rect["id"],{"evidence":None})
        with self.assertRaises(ConflictError):
            self.service.accept_rectification(current["id"],rect["id"],{},"sm",'safety_manager')
        view=self.service.list_rectifications(current["id"],"viewer")[0]
        self.assertEqual(view["status"],"returned")
        self.assertIn("证据缺失",view["last_return_note"])

    def test_key_info_change_invalidates_acceptance(self):
        current=self._to_corrective()
        rect=self.service.register_rectification(current["id"],{"owner":"赵六","due_at":iso(24)},"inv",'investigator')
        self.service.submit_rectification(current["id"],rect["id"],{"evidence":"照片Y"},"inv",'investigator')
        accepted=self.service.accept_rectification(current["id"],rect["id"],{},"sm",'safety_manager')
        self.assertTrue(accepted["acceptance_current"])
        # 事故关键信息被修改 -> 版本抬升 -> 验收失效，进入验证被阻断
        updated=self.service.update_item(current["id"],{"severity":'fatal',"expected_version":current["version"]},"inv2",'investigator')
        self.assertEqual(updated["version"],current["version"]+1)
        detail=self.service.get_item(updated["id"],"viewer")
        self.assertTrue(detail["blocked"])
        self.assertIn("退回",detail["block_reasons"][0]["reason"])
        self.assertIsNotNone(detail["latest_return_note"])
        self.assertIn("关键信息",detail["latest_return_note"]["note"])
        with self.assertRaises(ConflictError):
            self.service.transition(updated["id"],STATES[3],updated["version"],"reviewer",TRANSITION_ROLES[STATES[3]][0])
        # 重新提交并在当前版本验收后可放行
        self.service.submit_rectification(updated["id"],rect["id"],{"evidence":"照片Z"},"inv",'investigator')
        self.service.accept_rectification(updated["id"],rect["id"],{},"sm",'safety_manager')
        detail=self.service.get_item(updated["id"],"viewer")
        self.assertFalse(detail["blocked"])

    def test_overdue_raises_priority_and_flags_list(self):
        current=self._to_corrective()
        base_priority=self.service.get_item(current["id"],"viewer")["priority"]
        rect=self.service.register_rectification(current["id"],{"owner":"孙七","due_at":iso(-2)},"inv",'investigator')
        listing=self.service.list_items("viewer")[0]
        self.assertTrue(listing["overdue"]); self.assertEqual(listing["priority"],min(10,base_priority+3))
        self.assertEqual(listing["rectification_overdue_count"],1)
        # 验收通过后逾期标记消失、优先级回落
        self.service.submit_rectification(current["id"],rect["id"],{"evidence":"照片W"},"inv",'investigator')
        self.service.accept_rectification(current["id"],rect["id"],{},"sm",'safety_manager')
        listing=self.service.list_items("viewer")[0]
        self.assertFalse(listing["overdue"]); self.assertEqual(listing["priority"],base_priority)
if __name__=="__main__": unittest.main()
