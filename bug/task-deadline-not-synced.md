# Bug #002：任务截止时间未落库 / 更新失败

**状态**：已修复（`app/routers/tasks.py`、`app/services/task_service.py`、`app/schemas/task.py`）  
**发现日期**：2026-05-25  
**部署日期**：2026-05-25（`47.118.28.102`，`systemctl restart spirit-scheduler`）  
**影响**：`POST /api/v1/tasks`、`PATCH /api/v1/tasks/{id}`、月历日期、AI 周报任务统计

---

## 现象

1. Flutter 选择 **5 月 26/27 日** 创建任务，服务器 `tasks.deadline` 仍为 **NULL**，子任务 `scheduled_start` / `scheduled_end` 均为 **NULL**。
2. 用户编辑任务时间后 `PATCH` 返回 **500 Internal Server Error**，日志类似：

```text
[SQL: UPDATE tasks SET deadline=?, updated_at=? WHERE tasks.id = ?]
[parameters: {'deadline': '2026-05-26T04:35:00.000Z', ...}]
```

3. `GET /api/v1/tasks` 返回的 `deadline` 为空；客户端读回后按「当前时间」显示，月历落在 **5 月 25 日**。
4. 与 [Bug #001 周报统计为 0](./weekly-report-task-count-zero.md) 叠加：无 `deadline`、无 `scheduled_start` 时，周报 `total_tasks_planned` 更容易为 0。

## 根因

### 1. 创建接口忽略客户端 `deadline`

`POST /tasks`（`create_task`）仅调用：

```python
parsed = await task_parser.parse(user_input=body.user_input, ...)
for task_data in parsed.get("tasks", []):
    task = await task_svc.create_task(..., task_data, ...)
```

**未**将 `TaskCreateRequest.deadline` / `title` / `primary_spirit` 合并进 `task_data`。  
当 `user_input` 为「测试」等短文本时，日志出现 `task_parser_fallback`，解析结果无截止时间 → 入库 `deadline=NULL`。

### 2. 更新接口未解析 ISO 字符串

`TaskUpdateRequest.deadline` 为 `str`，`TaskService.update_task` 直接：

```python
setattr(task, field, value)  # value 仍是 '2026-05-26T04:35:00.000Z'
```

ORM 列类型为 `DateTime`，SQLite 更新失败 → **500**，日期从未写入。

### 3. 无开始时间字段

任务表仅有 `deadline`，无 `start_at`。客户端若只传结束时间，或未传 `start_iso`，则子任务排期为空，前端无法从 API 还原开始日期。

## 验证方式（修复前）

```bash
# 登录后创建（带 deadline）
curl -X POST -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"user_input":"测试","title":"测试","deadline":"2026-05-27T03:00:00.000Z","auto_decompose":false}' \
  http://47.118.28.102:8000/api/v1/tasks

# 查库：deadline 仍为 NULL
sqlite3 /app/spirit-scheduler/spirit.db \
  "SELECT title, deadline FROM tasks ORDER BY created_at DESC LIMIT 3;"
```

`journalctl -u spirit-scheduler` 可见 `PATCH` 带 `deadline` 参数但 **500**。

## 解决方案

### Schema（`app/schemas/task.py`）

- `TaskCreateRequest` / `TaskUpdateRequest` 增加可选字段：`start_iso`、`end_iso`（`end_iso` 优先于 `deadline`）。

### 路由（`app/routers/tasks.py`）

- `_merge_client_create_fields()`：客户端 `title`、`primary_spirit`、`deadline`/`end_iso` **覆盖** NLP 解析结果。
- `_fallback_parsed_from_body()`：解析无任务时，用客户端字段构造单条任务。
- 创建并在子任务生成后：若提供 `start_iso`/`end_iso`，调用 `apply_task_schedule()`。

### 服务（`app/services/task_service.py`）

- `update_task()`：对 `deadline` 调用 `_parse_deadline()`；处理 `start_iso`/`end_iso` 并更新子任务排期。
- 新增 `apply_task_schedule()`：写入 `task.deadline` 与首个子任务的 `scheduled_start` / `scheduled_end`。

### 部署与验证脚本

- 本仓库外部署脚本：ScheduleApp 主仓 `.deploy/deploy_task_datetime_fix.py`（上传上述 3 个文件并重启服务）。

**修复后自动化验证结果**（2026-05-25）：

```text
POST /tasks → deadline 2026-05-27T03:00:00+00:00
              scheduled_start 2026-05-27T01:00:00+00:00
PATCH /tasks/{id} → 200，deadline 2026-05-28T04:00:00+00:00
```

## 修改文件一览

| 文件 | 变更 |
|------|------|
| `app/schemas/task.py` | `start_iso`、`end_iso` |
| `app/routers/tasks.py` | 合并客户端字段、fallback、排期 |
| `app/services/task_service.py` | `update_task` 解析、`apply_task_schedule` |

## 客户端配合

Flutter 需同时传 `start_iso`、`end_iso`、`deadline`，读回时使用 `subtasks[0].scheduled_*`。  
见主仓：`schedule_app_flutter/bugs-doc/task-date-not-synced-with-server.md`。

## 关联

- 仓库：[ScheduleApp-server](https://github.com/twilight411/ScheduleApp-server)
- 生产目录：`/app/spirit-scheduler`
- 关联 Bug：[weekly-report-task-count-zero.md](./weekly-report-task-count-zero.md)（统计口径；本 Bug 修复后 `deadline` 有值更易计入）
- 客户端：主仓 `schedule_app_flutter/bugs-doc/task-completion-not-synced.md` — `PATCH /tasks` 失败时旧版不会上传 `subtasks/.../completion`（App 已改为完成度单独同步）
