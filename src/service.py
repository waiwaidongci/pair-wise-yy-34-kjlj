from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .domain import (ConflictError, PermissionDenied, NotFoundError,
                     ensure_role, normalize_severity, parse_due_at,
                     require_number, require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, RECT_ACCEPT_ROLES,
                    RECT_REGISTER_ROLES, RECT_SUBMIT_ROLES,
                    RECORD_ROLES, ENTITY, VIEW_ROLES, acceptance_is_current,
                    completion_blockers, escalation_required,
                    is_rectification_overdue, priority_score,
                    response_deadline_hours, role_for_transition,
                    validate_transition, verification_blockers)

RECTIFICATION_STATES = ("corrective_action", "verification")


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    def _require_rectification_phase(self, item: Dict[str, Any]) -> None:
        if item["status"] not in RECTIFICATION_STATES:
            raise ConflictError("整改台账仅在纠正措施/验证阶段开放")

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

    def update_item(self, item_id: int, payload: Dict[str, Any], actor: str,
                    role: str) -> Dict[str, Any]:
        """修改事故关键信息；任何关键信息变更都产生新版本，使既有验收失效。"""
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        expected_version = payload.get("expected_version")
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ConflictError("expected_version必须是正整数")
        item = self.repository.get_item(item_id)
        if item["status"] == "closed":
            raise ConflictError("事故已关档，不能修改关键信息")
        fields: Dict[str, Any] = {}
        if "title" in payload:
            fields["title"] = require_text(payload.get("title"), "title", 200)
        if "description" in payload:
            fields["description"] = require_text(payload.get("description"), "description")
        if "severity" in payload:
            fields["severity"] = normalize_severity(payload.get("severity"))
        if "quantity" in payload:
            fields["quantity"] = require_number(payload.get("quantity"), "quantity")
        if "threshold" in payload:
            fields["threshold"] = require_number(payload.get("threshold"), "threshold", 0.000001)
        if not fields:
            raise ConflictError("没有可更新的关键信息字段")
        changed = {key: fields[key] for key in fields if fields[key] != item.get(key)}
        if not changed:
            return self.enrich(item)
        updated = self.repository.update_item_fields(item_id, changed, expected_version, actor)
        self.repository.append_audit("item_update", ENTITY, item_id, actor, {
            "changed": list(changed.keys()),
            "from_version": item["version"], "to_version": updated["version"],
        })
        # 关键信息变更使旧验收失效：已验收整改自动退回并记录退回说明
        note = "事故关键信息已修改，原验收自动失效，需在当前版本下重新提交验收"
        for rect in self.repository.list_rectifications(item_id):
            if acceptance_is_current(rect, item["version"]):
                self.repository.update_rectification(rect["id"], {
                    "status": "returned", "last_return_note": note,
                    "returned_by": "system", "returned_at": self._now(),
                })
                self.repository.append_audit("rect_return", ENTITY, item_id, "system", {
                    "rectification_id": rect["id"], "reason": note, "auto": True,
                })
        return self.enrich(updated, with_rectifications=True)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ConflictError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        return record

    # ---------------- 整改待验收台账 ----------------

    def register_rectification(self, item_id: int, payload: Dict[str, Any],
                               actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, RECT_REGISTER_ROLES)
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        self._require_rectification_phase(item)
        owner = require_text(payload.get("owner"), "owner", 100)
        due_at = parse_due_at(payload.get("due_at"))
        detail = require_text(payload.get("detail", ""), "detail", 2000) \
            if str(payload.get("detail", "")).strip() else ""
        evidence = payload.get("evidence")
        if evidence is not None:
            evidence = require_text(evidence, "evidence", 1000)
        rect = self.repository.add_rectification(item_id, owner, due_at, detail,
                                                 evidence, actor)
        self.repository.append_audit("rect_register", ENTITY, item_id, actor, {
            "rectification_id": rect["id"], "owner": owner, "due_at": due_at,
        })
        return self._rect_view(rect, item["version"])

    def submit_rectification(self, item_id: int, rect_id: int, payload: Dict[str, Any],
                             actor: str, role: str) -> Dict[str, Any]:
        """整改责任人提交现场证据，进入待验收。"""
        ensure_role(role, RECT_SUBMIT_ROLES)
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        self._require_rectification_phase(item)
        rect = self._rect_of_item(rect_id, item_id)
        if rect["status"] == "accepted":
            raise ConflictError("整改已验收通过，无需重复提交")
        evidence = payload.get("evidence", rect["evidence"])
        evidence = require_text(evidence, "evidence", 1000)
        updates = {
            "evidence": evidence, "status": "submitted", "submitted_by": actor,
            "submitted_at": self._now(),
            "submitted_version": item["version"],
        }
        if payload.get("owner") is not None:
            updates["owner"] = require_text(payload.get("owner"), "owner", 100)
        if payload.get("due_at") is not None:
            updates["due_at"] = parse_due_at(payload.get("due_at"))
        if str(payload.get("detail", "")).strip():
            updates["detail"] = require_text(payload.get("detail"), "detail", 2000)
        rect = self.repository.update_rectification(rect_id, updates)
        self.repository.append_audit("rect_submit", ENTITY, item_id, actor, {
            "rectification_id": rect_id, "submitted_version": item["version"],
        })
        return self._rect_view(rect, item["version"])

    def accept_rectification(self, item_id: int, rect_id: int, payload: Dict[str, Any],
                             actor: str, role: str) -> Dict[str, Any]:
        """安全员验收；不能验收本人提交的整改；证据缺失或版本失配强制退回。"""
        ensure_role(role, RECT_ACCEPT_ROLES)
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        self._require_rectification_phase(item)
        rect = self._rect_of_item(rect_id, item_id)
        if rect["status"] not in ("submitted", "returned"):
            raise ConflictError("整改尚未提交，不能验收")
        # 分权：安全员不能验收本人提交的整改
        if rect.get("submitted_by") and actor == rect["submitted_by"]:
            raise PermissionDenied("不能验收本人提交的整改")
        note = ""
        if not rect.get("evidence"):
            note = "证据缺失：未登记现场证据，退回补正"
        elif rect.get("submitted_version") is None or rect["submitted_version"] < item["version"]:
            note = "版本失配：事故关键信息已修改，需基于当前版本重新提交并验收"
        if note:
            rect = self.repository.update_rectification(rect_id, {
                "status": "returned", "last_return_note": note,
                "returned_by": actor, "returned_at": self._now(),
            })
            self.repository.append_audit("rect_return", ENTITY, item_id, actor, {
                "rectification_id": rect_id, "reason": note, "auto": True,
            })
            raise ConflictError(note)
        rect = self.repository.update_rectification(rect_id, {
            "status": "accepted", "accepted_by": actor,
            "accepted_at": self._now(), "accepted_version": item["version"],
        })
        self.repository.append_audit("rect_accept", ENTITY, item_id, actor, {
            "rectification_id": rect_id, "accepted_version": item["version"],
        })
        return self._rect_view(rect, item["version"])

    def return_rectification(self, item_id: int, rect_id: int, payload: Dict[str, Any],
                             actor: str, role: str) -> Dict[str, Any]:
        """安全员主动退回，必须填写退回说明。"""
        ensure_role(role, RECT_ACCEPT_ROLES)
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        self._require_rectification_phase(item)
        rect = self._rect_of_item(rect_id, item_id)
        if rect["status"] not in ("submitted", "accepted", "returned"):
            raise ConflictError("整改尚未提交，不能退回")
        note = require_text(payload.get("note"), "note", 1000)
        rect = self.repository.update_rectification(rect_id, {
            "status": "returned", "last_return_note": note,
            "returned_by": actor, "returned_at": self._now(),
        })
        self.repository.append_audit("rect_return", ENTITY, item_id, actor, {
            "rectification_id": rect_id, "reason": note, "auto": False,
        })
        return self._rect_view(rect, item["version"])

    def list_rectifications(self, item_id: int, role: str) -> List[Dict[str, Any]]:
        self._view(role)
        item = self.repository.get_item(item_id)
        return [self._rect_view(rect, item["version"])
                for rect in self.repository.list_rectifications(item_id)]

    # ---------------- 状态机 ----------------

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ConflictError("expected_version必须是正整数")
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        if blockers:
            raise ConflictError("；".join(blockers))
        # 进入验证前：整改必须有当前版本下的有效验收
        if target == "verification":
            reasons = self._block_reasons(
                self.repository.list_rectifications(item_id), item["version"])
            if reasons:
                text = "；".join(
                    f"整改#{entry['rectification_id']} {entry['reason']}"
                    if entry["rectification_id"] is not None else entry["reason"]
                    for entry in reasons)
                raise ConflictError("存在阻断：" + text)
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        # 转换成功后版本已+1；验收闸口锚定的是转换前版本，
        # 因此转换响应中的整改视图仍以转换前版本衡量，避免误报版本失效。
        return self.enrich(
            self.repository.get_item(item_id),
            with_rectifications=True, view_version=item["version"])

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        item = self.repository.get_item(item_id)
        return self.enrich(item, with_rectifications=True)

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        items = self.repository.list_items(status)
        rects_by_item: Dict[int, list] = {}
        for rect in self.repository.list_rectifications():
            rects_by_item.setdefault(rect["item_id"], []).append(rect)
        return [self.enrich(item, rects_by_item.get(item["id"], [])) for item in items]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    # ---------------- 汇总/视图 ----------------

    def _rect_of_item(self, rect_id: int, item_id: int) -> Dict[str, Any]:
        rect = self.repository.get_rectification(rect_id)
        if rect["item_id"] != item_id:
            raise NotFoundError("整改不属于该事故")
        return rect

    @staticmethod
    def _now() -> str:
        from .audit import utc_now
        return utc_now()

    @staticmethod
    def _block_reasons(rects: List[Dict[str, Any]],
                       item_version: int) -> List[Dict[str, Any]]:
        """阻断原因：一条整改都没有也算阻断。"""
        if not rects:
            return [{"rectification_id": None, "reason": "尚未登记整改措施"}]
        return [{"rectification_id": rect["id"], "reason": reason}
                for rect, reason in verification_blockers(rects, item_version)]

    def _rect_view(self, rect: Dict[str, Any], item_version: int) -> Dict[str, Any]:
        result = dict(rect)
        result["acceptance_current"] = acceptance_is_current(rect, item_version)
        result["overdue"] = is_rectification_overdue(rect, item_version)
        return result

    def enrich(self, item: Dict[str, Any],
               rects: Optional[List[Dict[str, Any]]] = None,
               with_rectifications: bool = False,
               view_version: Optional[int] = None) -> Dict[str, Any]:
        result = dict(item)
        if rects is None and with_rectifications:
            rects = self.repository.list_rectifications(item["id"])
        rects = rects or []
        version = item["version"] if view_version is None else view_version
        views = [self._rect_view(rect, version) for rect in rects]
        # 阻断原因只服务于"能否进入验证"这一决策；进入验证/关档后不再追溯。
        # 逾期是整改台账的实时状态，但关档后不再对外抬优先级。
        gate_passed = item["status"] in ("verification", "closed")
        if item["status"] == "closed":
            for view in views:
                view["overdue"] = False
        overdue_count = sum(1 for view in views if view["overdue"])
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"],
            self.repository.open_record_count(item["id"]), overdue_count)
        result["base_priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"],
            self.repository.open_record_count(item["id"]), 0)
        result["overdue"] = overdue_count > 0
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        result["rectification_count"] = len(views)
        result["rectification_overdue_count"] = overdue_count
        block_pairs = [] if gate_passed else self._block_reasons(rects, version)
        result["block_reasons"] = block_pairs
        result["blocked"] = len(block_pairs) > 0
        latest_note = None
        for rect in sorted(
                rects,
                key=lambda r: r.get("returned_at") or "", reverse=True):
            if rect.get("returned_at"):
                latest_note = {
                    "rectification_id": rect["id"],
                    "note": rect.get("last_return_note"),
                    "returned_by": rect.get("returned_by"),
                    "returned_at": rect.get("returned_at"),
                }
                break
        result["latest_return_note"] = latest_note
        if with_rectifications:
            result["rectifications"] = views
        return result
