# 化工装置变更与工艺安全管理

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8310`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：演示页面（装置/变更操作、实施前基线录入、回退恢复清单逐项恢复与复核、审计时间线）。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8310
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `unit`：装置运行状态；`change`：变更申请；`action_item`：风险控制行动项。
- `recovery_item`：回退恢复清单项（系统在回退时按基线快照自动生成，不能经 API 手动创建）。

## 回退恢复闭环

投用后效果不对时，回退不能只改状态，必须逐项恢复并复核：

1. **实施前保存基线**：`implement` 动作要求提交 `recovery_owner`（恢复负责人）以及
   `parameters`（关键工艺参数：名称、原值、单位）和 `isolations`（隔离措施：名称、原状），
   两者至少一项。系统同时快照受影响装置当时的运行状态，存入变更的 `baseline`。
2. **回退生成清单**：`rollback` 时按快照逐项生成 `recovery_item`（待恢复），每项记录应恢复到的原值。
3. **逐项恢复确认**：恢复负责人本人填写 `result`（恢复结果）后，清单项进入“待复核”；
   他人复核后才变为“已确认”。**恢复人本人不能自审**。
4. **停机/冻结加严**：装置当前仍为 `shutdown` 或 `frozen` 时，每项必须由
   **未参加本次实施**（不是 `implemented_by`）的**安全员（safety）**复核。
5. **驳回重做**：复核不通过可 `reject_restore` 退回“待恢复”，由恢复人重新填写。
6. **全部完成才能关闭**：`close` 时校验所有清单项均为“已确认”，否则返回未完成项清单并拒绝关闭。

恢复项状态：`pending`（待恢复）→ `restored`（待复核）→ `confirmed`（已确认）。

恢复清单项动作：

- `restore`：`{"result": "恢复结果描述"}`，仅该清单指定的恢复负责人可执行。
- `confirm_restore`：独立复核确认；角色与独立性规则见上。
- `reject_restore`：`{"reason": "驳回原因"}`。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`及`?change_id=`等数据字段过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `GET /api/entities/<id>/recovery-items`：读取某变更的回退恢复清单。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录，可用`?entity_id=`过滤。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

风险分级和投产规则用于流程演示，不替代HAZOP、LOPA、法定许可和现场安全审查。
