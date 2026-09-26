# 工伤事故调查与纠正措施

记录工伤经过、伤害、现场和证人，维护调查、纠正措施、验证与关闭流程。整改以**待验收台账**管理：登记责任人、到期时间和现场证据，逾期自动抬高事故优先级；安全员分权验收，证据缺失或事故关键信息变更将退回，进入验证前必须持有当前版本下的有效验收。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限、验收有效性与验证闸口规则。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链。
- `src/service.py`：权限检查、整改台账用例、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：列表/台账演示页（逾期、阻断、退回说明可视化）。
- `tests/`：完整流程、规则和失败测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8311
```

默认端口为`8311`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

事故：

- `GET /health`
- `GET /api/items`（列表含 `priority/base_priority/overdue/blocked/block_reasons/latest_return_note/rectification_overdue_count`）
- `POST /api/items`
- `GET /api/items/{id}`（详情另含 `rectifications` 台账明细）
- `PATCH /api/items/{id}`：修改事故关键信息（title/description/severity/quantity/threshold），必须带 `expected_version`；修改成功版本+1，此前验收自动失效
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/audit`

整改待验收台账（仅在 `corrective_action`、`verification` 阶段开放）：

- `GET /api/items/{id}/rectifications`
- `POST /api/items/{id}/rectifications`：登记 `owner`（责任人）、`due_at`（ISO 8601到期时间）、`detail`、可选 `evidence`
- `POST /api/items/{id}/rectifications/{rid}/submit`：责任人提交现场证据（`evidence`必填），记录提交人及提交时事故版本
- `POST /api/items/{id}/rectifications/{rid}/accept`：安全员验收
- `POST /api/items/{id}/rectifications/{rid}/return`：安全员退回，`note` 必填

## 验收与闸口规则

- 仅 `safety_manager` 可验收，且**验收人不能是该整改的提交人**。
- 验收时若证据缺失，或提交版本早于事故当前版本（关键信息被改过），系统强制退回并写入退回说明，验收接口返回 409。
- 事故由 `corrective_action` 进入 `verification` 前：必须至少有一条整改，且每条整改在**当前事故版本**下有有效验收（`accepted_version >= item.version`），否则 409 阻断；阻断原因可在列表 `block_reasons` 与详情中查看，最近退回说明见 `latest_return_note`。
- 整改到期（`due_at` 早于当前UTC时间）且未取得当前版本有效验收即逾期：事故优先级 +3（封顶10），列表 `overdue=true` 并给出逾期整改数；验收通过后回落。

允许角色：reporter, investigator, safety_manager, viewer。严重度越高、伤害指数越大或未关闭措施越多，优先级越高；严重事故必须在4小时内启动调查。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
