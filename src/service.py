from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from .domain import (ConflictError, PermissionDenied, ValidationError,
                     ensure_role, normalize_severity, parse_iso_datetime,
                     require_iso_datetime, require_number, require_text)
from .repository import Repository
from .rules import (ACCEPT_ROLES, AUDIT_ROLES, CREATE_ROLES, ENTITY,
                    KEY_INFO_EDITABLE_STATES, RECTIFICATION_KIND, RECORD_ROLES,
                    UPDATE_ROLES, VERIFICATION_STATE, VIEW_ROLES,
                    acceptance_blockers, acceptance_valid, completion_blockers,
                    escalation_required, priority_score,
                    response_deadline_hours, role_for_transition,
                    validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
        })
        return self.enrich(item)

    def update_item(self, item_id: int, payload: Dict[str, Any], expected_version: int,
                    actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, UPDATE_ROLES)
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        if item["status"] not in KEY_INFO_EDITABLE_STATES:
            raise ConflictError("验证或关闭阶段不能修改事故关键信息")
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        title = require_text(payload.get("title", item["title"]), "title", 200)
        description = require_text(payload.get("description", item["description"]), "description")
        severity = normalize_severity(payload.get("severity", item["severity"]))
        quantity = require_number(payload.get("quantity", item["quantity"]), "quantity")
        threshold = require_number(payload.get("threshold", item["threshold"]),
                                   "threshold", 0.000001)
        updated = self.repository.update_item(item_id, title, description, severity,
                                              quantity, threshold, expected_version)
        self.repository.append_audit("update", ENTITY, item_id, actor, {
            "from_version": item["version"], "key_version": updated["key_version"],
            "severity": severity, "quantity": quantity, "threshold": threshold,
        })
        return self.enrich(updated)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        item = self.repository.get_item(item_id)
        ledger = None
        if kind == RECTIFICATION_KIND:
            ledger = self._ledger_payload(payload, item)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor, ledger)
        audit_detail: Dict[str, Any] = {"record_id": record["id"], "kind": kind,
                                        "status": status}
        if ledger:
            audit_detail.update({"assignee": ledger["assignee"],
                                 "due_at": ledger["due_at"]})
        self.repository.append_audit("record", ENTITY, item_id, actor, audit_detail)
        return self.enrich_record(record, item)

    @staticmethod
    def _ledger_payload(payload: Dict[str, Any], item: Dict[str, Any]) -> Dict[str, Any]:
        assignee = require_text(payload.get("assignee"), "assignee", 100)
        due_at = require_iso_datetime(payload.get("due_at"), "due_at")
        evidence = payload.get("evidence")
        if evidence is not None:
            evidence = require_text(evidence, "evidence")
        return {"assignee": assignee, "due_at": due_at, "evidence": evidence,
                "acceptance_status": "pending",
                "submitted_key_version": item["key_version"]}

    def _rectification(self, item_id: int, record_id: int):
        item = self.repository.get_item(item_id)
        if item["status"] == "closed":
            raise ConflictError("事故已关闭，无法操作整改台账")
        record = self.repository.get_record(item_id, record_id)
        if record.get("kind") != RECTIFICATION_KIND:
            raise ValidationError("仅整改记录支持验收流程")
        return item, record

    def _auto_return(self, item: Dict[str, Any], record: Dict[str, Any],
                     reason: str, actor: str) -> None:
        self.repository.set_record_returned(item["id"], record["id"], reason, actor)
        self.repository.append_audit("return", ENTITY, item["id"], actor, {
            "record_id": record["id"], "reason": reason, "automatic": True,
        })

    def accept_record(self, item_id: int, record_id: int, actor: str,
                      role: str) -> Dict[str, Any]:
        ensure_role(role, ACCEPT_ROLES)
        actor = require_text(actor, "actor", 100)
        item, record = self._rectification(item_id, record_id)
        if record["created_by"] == actor:
            raise PermissionDenied("安全员不能验收本人提交的整改")
        if acceptance_valid(record, item["key_version"]):
            raise ConflictError("该整改已在当前版本下完成验收")
        if not record.get("evidence"):
            self._auto_return(item, record, "现场证据缺失", actor)
            raise ConflictError(f"整改记录#{record_id}缺少现场证据，已退回")
        if record.get("submitted_key_version") != item["key_version"]:
            self._auto_return(item, record, "事故关键信息已变更，需重新提交", actor)
            raise ConflictError(f"整改记录#{record_id}提交后事故关键信息已变更，已退回")
        updated = self.repository.set_record_accepted(item_id, record_id, actor,
                                                      item["key_version"])
        self.repository.append_audit("accept", ENTITY, item_id, actor, {
            "record_id": record_id, "key_version": item["key_version"],
        })
        return self.enrich_record(updated, item)

    def return_record(self, item_id: int, record_id: int, payload: Dict[str, Any],
                      actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, ACCEPT_ROLES)
        actor = require_text(actor, "actor", 100)
        reason = require_text(payload.get("reason"), "reason", 500)
        item, record = self._rectification(item_id, record_id)
        updated = self.repository.set_record_returned(item_id, record_id, reason, actor)
        self.repository.append_audit("return", ENTITY, item_id, actor, {
            "record_id": record_id, "reason": reason, "automatic": False,
        })
        return self.enrich_record(updated, item)

    def resubmit_record(self, item_id: int, record_id: int, payload: Dict[str, Any],
                        actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        item, record = self._rectification(item_id, record_id)
        if acceptance_valid(record, item["key_version"]):
            raise ConflictError("该整改已在当前版本下完成验收，无需重新提交")
        updates: Dict[str, Any] = {}
        if "detail" in payload:
            updates["detail"] = require_text(payload.get("detail"), "detail")
        if "assignee" in payload:
            updates["assignee"] = require_text(payload.get("assignee"), "assignee", 100)
        if "due_at" in payload:
            updates["due_at"] = require_iso_datetime(payload.get("due_at"), "due_at")
        if "evidence" in payload:
            updates["evidence"] = require_text(payload.get("evidence"), "evidence")
        updated = self.repository.resubmit_record(item_id, record_id, updates,
                                                  item["key_version"])
        self.repository.append_audit("resubmit", ENTITY, item_id, actor, {
            "record_id": record_id, "key_version": item["key_version"],
        })
        return self.enrich_record(updated, item)

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        if target == VERIFICATION_STATE:
            blockers += acceptance_blockers(self.repository.list_records(item_id),
                                            item["key_version"])
        if blockers:
            raise ConflictError("；".join(blockers))
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self.enrich(updated)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        item = self.repository.get_item(item_id)
        return [self.enrich_record(record, item)
                for record in self.repository.list_records(item_id)]

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    def enrich_record(self, record: Dict[str, Any],
                      item: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        result = dict(record)
        if record.get("kind") == RECTIFICATION_KIND:
            if item is None:
                item = self.repository.get_item(record["item_id"])
            result["acceptance_valid"] = acceptance_valid(record, item["key_version"])
            result["overdue"] = self._overdue(record, item,
                                              datetime.now(timezone.utc))
        return result

    def enrich(self, item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        records = self.repository.list_records(item["id"])
        now = datetime.now(timezone.utc)
        open_records = sum(1 for record in records if record["status"] == "open")
        overdue = [record for record in records
                   if self._overdue(record, item, now)]
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"],
            open_records, len(overdue))
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        result["open_records"] = open_records
        result["overdue_rectifications"] = len(overdue)
        result["has_overdue_rectifications"] = bool(overdue)
        result["blockers"] = self._blockers(item, records, open_records)
        result["last_return"] = self._last_return(records)
        return result

    @staticmethod
    def _overdue(record: Dict[str, Any], item: Dict[str, Any],
                 now: datetime) -> bool:
        if record.get("kind") != RECTIFICATION_KIND:
            return False
        if acceptance_valid(record, item["key_version"]):
            return False
        due = record.get("due_at")
        if not due:
            return False
        return parse_iso_datetime(due) < now

    @staticmethod
    def _blockers(item: Dict[str, Any], records: list, open_records: int) -> list:
        if item["status"] == "corrective_action":
            return acceptance_blockers(records, item["key_version"])
        if item["status"] == VERIFICATION_STATE:
            return completion_blockers("closed", open_records)
        return []

    @staticmethod
    def _last_return(records: list) -> Optional[Dict[str, Any]]:
        returned = [record for record in records if record.get("last_return_reason")]
        if not returned:
            return None
        latest = max(returned, key=lambda record: record.get("last_returned_at") or "")
        return {"record_id": latest["id"], "reason": latest["last_return_reason"],
                "returned_by": latest["last_returned_by"],
                "returned_at": latest["last_returned_at"]}
