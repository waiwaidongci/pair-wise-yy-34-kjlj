from __future__ import annotations
from .domain import ConflictError, ValidationError
TITLE='工伤事故调查与纠正措施'; ENTITY='事故'; ID_PREFIX='OI'
SEVERITIES=['minor', 'moderate', 'serious', 'fatal']; STATES=['reported', 'investigating', 'corrective_action', 'verification', 'closed']; TRANSITIONS={'reported': ['investigating'], 'investigating': ['corrective_action'], 'corrective_action': ['verification'], 'verification': ['closed'], 'closed': []}; TRANSITION_ROLES={'investigating': ['investigator'], 'corrective_action': ['investigator'], 'verification': ['safety_manager'], 'closed': ['safety_manager']}
CREATE_ROLES=set(['reporter', 'investigator']); RECORD_ROLES=set(['investigator', 'safety_manager']); AUDIT_ROLES=set(['safety_manager', 'viewer']); VIEW_ROLES=set(['reporter', 'investigator', 'safety_manager', 'viewer'])
RECT_STATUS=['pending', 'submitted', 'accepted', 'returned']
RECT_REGISTER_ROLES=set(['investigator', 'safety_manager'])
RECT_SUBMIT_ROLES=set(['investigator', 'safety_manager'])
RECT_ACCEPT_ROLES=set(['safety_manager'])
SEVERITY_WEIGHT={'minor': 1.0, 'moderate': 3.0, 'serious': 6.0, 'fatal': 9.0}; DEADLINE_HOURS={'minor': 72, 'moderate': 24, 'serious': 8, 'fatal': 4}; TERMINAL_STATES=set(['closed'])
OVERDUE_BOOST=3
def priority_score(severity,quantity=0.0,threshold=1.0,open_records=0,overdue_rectifications=0):
    if severity not in SEVERITY_WEIGHT: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    score=SEVERITY_WEIGHT[severity]+min(4.0,ratio*4.0)+min(3.0,float(open_records))
    if overdue_rectifications>0: score+=OVERDUE_BOOST
    return max(0,min(10,int(round(score))))
def response_deadline_hours(severity,quantity=0.0,threshold=1.0):
    if severity not in DEADLINE_HOURS: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(1,int(DEADLINE_HOURS[severity]/max(1.0,ratio)))
def escalation_required(severity,quantity=0.0,threshold=1.0):
    return severity==SEVERITIES[-1] or (threshold>0 and quantity>=threshold)
def can_transition(current,target): return target in TRANSITIONS.get(current,[])
def validate_transition(current,target):
    if current not in STATES or target not in STATES: raise ValidationError("未知状态")
    if not can_transition(current,target): raise ConflictError(f"不能从{current}转换到{target}")
def completion_blockers(target,open_records): return ["仍有未关闭事项"] if target in TERMINAL_STATES and open_records>0 else []
def role_for_transition(target): return set(TRANSITION_ROLES.get(target,[]))
def is_rectification_overdue(rect,item_version,now=None):
    """到期且没有当前版本下的有效验收，即视为逾期。"""
    from .domain import now_utc, parse_iso
    if acceptance_is_current(rect,item_version):
        return False
    if not rect.get("due_at"):
        return False
    current=now or now_utc()
    return parse_iso(rect["due_at"])<current
def acceptance_is_current(rect,item_version):
    """验收是否对事故当前版本仍然有效。"""
    return bool(rect.get("status")=='accepted' and rect.get("accepted_version") is not None
                and rect.get("accepted_version")>=item_version)
def verification_blockers(rects,item_version,now=None):
    """进入验证阶段前的整改阻断原因，返回[(rect, reason)]。"""
    blockers=[]
    for rect in rects:
        if not rect.get("evidence"):
            blockers.append((rect,"缺少现场证据"))
        elif not acceptance_is_current(rect,item_version):
            if rect.get("status")=='accepted':
                blockers.append((rect,"验收基于旧版本，事故关键信息已修改，需重新验收"))
            elif rect.get("status")=='returned':
                blockers.append((rect,"整改已被退回，待重新提交验收"))
            elif rect.get("status")=='submitted':
                blockers.append((rect,"整改待安全员验收"))
            else:
                blockers.append((rect,"整改尚未提交验收"))
    return blockers
