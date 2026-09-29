# 矿井应急避险与通风协调

这是一个只使用Python标准库和SQLite的模块化原型项目，默认端口为`8335`。领域对象包括矿井人员、气体传感、通风设备、逃生通道、避险硐室、事件和处置任务。`app.py`只负责参数解析、依赖组装和服务生命周期，业务状态机与约束集中在`src/rules.py`。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、领域异常、身份解析和实体数据结构。
- `src/rules.py`：状态机、角色权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务、乐观锁、审计和幂等键。
- `src/service.py`：用例编排、离线记录合并、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、JSON解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8335
```

服务启动时自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。初始化服务不需要单独命令，首次启动即可访问：

```bash
curl http://127.0.0.1:8335/health
```

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。

身份通过`X-User-Id`和`X-Role`请求头传入，角色和动作权限由规则引擎校验。## 核心流程

创建矿井事件、人员和设备记录后，依次执行撤离、搜救、通风恢复和事件关闭。`POST /api/offline-records` 用于合并现场离线记录，`source_id + record_id` 相同会幂等返回原记录。

## 联动单

气体报警后按事件和污染区域创建联动单（`POST /api/linkages`，字段`incident_id`、`area_code`）：同一事件和区域只保留一张活跃联动单，重复报警返回原单。创建时沿可用通道（未封锁）搜索并冻结受影响区域。对联动单执行`execute`动作后，系统按固定顺序执行四个步骤：

1. `isolate_passages`：封锁所有连接受影响区域的通道。
2. `start_fans`：恢复受影响区域内停运的通风设备；设备不可用（`out_of_service`）时该步留在待处理。
3. `occupy_refuges`：按受影响区域内失联/已定位人员数量占用避险硐室；容量不足时该步留在待处理。
4. `dispatch_tasks`：为受影响区域内每名失联人员创建并指派搜救任务（`dedupe_key`去重）。

某一步条件不满足时后续步骤本轮不再执行，已完成的步骤不会回滚；修复条件后再次`execute`即可续跑，重试不会重复占用硐室或重复派单。全部步骤完成后联动单自动变为`completed`，也可用`cancel`（需`reason`）取消。每步记录执行人（`executed_by`）和时间（`executed_at`），首页联动单面板可直接查看。

## 规则重点

- 活跃任务按 `dedupe_key` 防止重复派工。
- 气体读数按阈值计算`severity`。
- 事件关闭前必须没有失联或已定位人员、没有活跃任务，并且所有通风设备恢复运行。
- 事件存在联动单时，联动单必须全部完成或取消才能关闭事件；没有联动单的旧事件仍按原流程关闭。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

项目使用请求头模拟身份、SQLite单机持久化和简化状态机，适合原型演示和流程验证，不替代行业正式系统、设备控制系统或现场安全规程。
