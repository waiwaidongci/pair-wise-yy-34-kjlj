# 工伤事故调查与纠正措施

记录工伤经过、伤害、现场和证人，维护调查、纠正措施、验证与关闭流程。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则和失败测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8311
```

默认端口为`8311`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/update`，修改事故关键信息，必须提交`expected_version`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/records/{rid}/accept`，安全员验收整改
- `POST /api/items/{id}/records/{rid}/return`，安全员退回整改，必须填写`reason`
- `POST /api/items/{id}/records/{rid}/resubmit`，退回后重新提交整改
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/audit`

允许角色：reporter, investigator, safety_manager, viewer。严重度越高、伤害指数越大、未关闭措施越多或逾期整改越多，优先级越高；严重事故必须在4小时内启动调查。

## 整改待验收台账

`kind`为`rectification`的记录是整改台账条目，登记时必须提供责任人`assignee`和到期时间`due_at`（ISO 8601），现场证据`evidence`可后补。规则：

- 逾期未获有效验收的整改会抬高事故优先级，并在列表和详情中以`overdue_rectifications`标出。
- 只有safety_manager能验收，且不能验收本人提交的整改。
- 验收时证据缺失、或提交后事故关键信息变更过（`key_version`不一致），整改会被自动退回并记录原因。
- 事故进入`verification`前，所有整改必须持有当前`key_version`下的有效验收；关键信息变更会使既有验收失效，需重新提交并验收。
- 事故详情和列表提供`blockers`（当前阻断原因）和`last_return`（最近退回说明）；验证或关闭阶段不允许修改事故关键信息。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
