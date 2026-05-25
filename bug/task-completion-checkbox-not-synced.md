# Bug #003：日历勾选 100% 未通知服务器

**状态**：已修复  
**发现日期**：2026-05-25  
**影响**：日历勾选、完成度刷新后丢失、周报完成数不更新

---

## 现象

用户在日历点勾选框，界面立刻变为 100%，但：

- 服务器 `subtasks.completion_percent` 仍为 0
- 重新登录或拉列表后变回未完成
- 重新生成周报仍显示完成 0

## 根因

1. **完成度存在子任务上**，接口为 `PATCH /tasks/subtasks/{id}/completion`。
2. Flutter `syncSubtaskCompletion` / `_syncCompletionToServer` 在 **`primarySubtaskId` 为空时直接 return**，不请求服务器。
3. `primarySubtaskId` 在以下情况为空：本地缓存旧数据、创建尚未回写子任务 id、列表未带子任务等。
4. 勾选只改本地 `completionPercent`，用户以为已同步。

## 解决方案

### 后端

新增 `PATCH /api/v1/tasks/{task_id}/completion`：

- 按**父任务 id** 更新，无需客户端知道子任务 id
- 将该任务下**全部子任务**写成同一 `completion_percent`（0/100 勾选）
- 同步父任务 `status`

### Flutter

- `syncSubtaskCompletion`：优先调 `PATCH /tasks/{id}/completion`；旧服 404 时回退子任务接口，必要时 `GET /tasks/{id}` 解析 `primarySubtaskId`
- `_syncCompletionToServer`：仅要求 `task.id`，不再要求 `primarySubtaskId`

## 验证

1. 部署后勾选某任务 → 服务器对应子任务 `completion_percent=100`
2. 杀进程重开，仍为 100%
3. 重新生成周报，`total_tasks_completed` 增加

## 关联

- [weekly-report-task-count-zero.md](./weekly-report-task-count-zero.md)
- [task-deadline-not-synced.md](./task-deadline-not-synced.md)
