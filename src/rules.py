from __future__ import annotations
from .domain import ConflictError, ValidationError
TITLE='工伤事故调查与纠正措施'; ENTITY='事故'; ID_PREFIX='OI'
SEVERITIES=['minor', 'moderate', 'serious', 'fatal']; STATES=['reported', 'investigating', 'corrective_action', 'verification', 'closed']; TRANSITIONS={'reported': ['investigating'], 'investigating': ['corrective_action'], 'corrective_action': ['verification'], 'verification': ['closed'], 'closed': []}; TRANSITION_ROLES={'investigating': ['investigator'], 'corrective_action': ['investigator'], 'verification': ['safety_manager'], 'closed': ['safety_manager']}
CREATE_ROLES=set(['reporter', 'investigator']); RECORD_ROLES=set(['investigator', 'safety_manager']); AUDIT_ROLES=set(['safety_manager', 'viewer']); VIEW_ROLES=set(['reporter', 'investigator', 'safety_manager', 'viewer'])
ACCEPT_ROLES=set(['safety_manager']); UPDATE_ROLES=set(['investigator', 'safety_manager'])
RECTIFICATION_KIND='rectification'; ACCEPTANCE_STATES=['pending', 'accepted', 'returned']
VERIFICATION_STATE='verification'; KEY_INFO_EDITABLE_STATES=set(['reported', 'investigating', 'corrective_action'])
SEVERITY_WEIGHT={'minor': 1.0, 'moderate': 3.0, 'serious': 6.0, 'fatal': 9.0}; DEADLINE_HOURS={'minor': 72, 'moderate': 24, 'serious': 8, 'fatal': 4}; TERMINAL_STATES=set(['closed'])
def priority_score(severity,quantity=0.0,threshold=1.0,open_records=0,overdue_records=0):
    if severity not in SEVERITY_WEIGHT: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(0,min(10,int(round(SEVERITY_WEIGHT[severity]+min(4.0,ratio*4.0)+min(3.0,float(open_records))+min(4.0,2.0*float(overdue_records))))))
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
def acceptance_valid(record,key_version):
    return record.get("acceptance_status")=="accepted" and record.get("accepted_key_version")==key_version
def acceptance_blockers(records,key_version):
    blockers=[]
    for record in records:
        if record.get("kind")!=RECTIFICATION_KIND: continue
        if acceptance_valid(record,key_version): continue
        state=record.get("acceptance_status") or "pending"
        if state=="returned":
            reason=record.get("last_return_reason") or "未说明原因"
            blockers.append(f"整改记录#{record['id']}已退回（{reason}），需重新提交并验收")
        elif state=="accepted":
            blockers.append(f"整改记录#{record['id']}的验收因事故信息变更已失效，需重新提交并验收")
        else:
            blockers.append(f"整改记录#{record['id']}待验收")
    return blockers
def role_for_transition(target): return set(TRANSITION_ROLES.get(target,[]))
