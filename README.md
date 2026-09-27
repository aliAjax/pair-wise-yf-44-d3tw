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
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8310
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `unit`：装置运行状态；`change`：变更申请；`action_item`：风险控制行动项；`restore_item`：回退恢复清单项。

## 回退恢复流程

- `implement` 时必须提交`key_parameters`（关键工艺参数）和`restoration_owner`（恢复负责人），可选`isolation_measures`（隔离措施）；系统自动快照装置当前状态，一并存入变更的`baseline`。
- `rollback` 时按基线自动生成`restore_item`清单：装置状态一项、每个工艺参数一项、每条隔离措施一项，初始状态均为`pending`。
- 恢复人逐项执行`restore`（必填`result`恢复结果）使清单项变为`restored`；复核不通过可用`reopen`打回。
- 装置仍处于`shutdown`或`frozen`时，每项必须经`review`复核为`verified`才能关闭变更；复核人须为安全员角色、未参加实施（非`implemented_by`/`commissioned_by`），且不能是恢复人本人。
- 清单全部完成前`close`会被拒绝；装置已恢复`operating`时，`restored`即视为完成。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤，其他查询参数按`data`字段等值过滤（如`?change_id=`）。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

风险分级和投产规则用于流程演示，不替代HAZOP、LOPA、法定许可和现场安全审查。
