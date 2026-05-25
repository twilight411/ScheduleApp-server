# Bug 记录（后端 ScheduleApp-server）

本目录记录 **生产后端**（`/app/spirit-scheduler`）已确认的问题、根因与修复说明。  
**Flutter 客户端**见主仓 `schedule_app_flutter/bugs-doc/`。

## 本次会话相关（任务 / 周报）

| 编号 | 文件 | 状态 | 摘要 |
|------|------|------|------|
| 001 | [weekly-report-task-count-zero.md](./weekly-report-task-count-zero.md) | 已修复 | 周报计划任务数为 0；改为按父任务统计 |
| 002 | [task-deadline-not-synced.md](./task-deadline-not-synced.md) | 已修复 | `deadline` 未落库、PATCH 500、日期错一天 |
| 003 | [task-completion-checkbox-not-synced.md](./task-completion-checkbox-not-synced.md) | 已修复 | 日历勾选未上传；`PATCH /tasks/{id}/completion` |
| 005 | [weekly-report-stale-cache.md](./weekly-report-stale-cache.md) | 已修复 | 周报 DB 缓存不刷新；`regenerate` 忽略 `week_start` |

## 其它已记录

| 文件 | 摘要 |
|------|------|
| [auth-account-username-login.md](./auth-account-username-login.md) | 账号登录 / `account` 字段 |
| [tree-image-generation-fallback.md](./tree-image-generation-fallback.md) | 生命树生图降级 |
| [tree-image-placeholder-db-cache.md](./tree-image-placeholder-db-cache.md) | 占位图 DB 缓存 |
| [tree-weekly-image-500-spirit-meta.md](./tree-weekly-image-500-spirit-meta.md) | 周树图 500 / spirit meta |
| [weekly-tree-image-no-cache.md](./weekly-tree-image-no-cache.md) | 周树图无缓存策略 |

## 部署

```bash
cd /app/spirit-scheduler
bash scripts/deploy_backend.sh main
systemctl restart spirit-scheduler
```

仓库：[twilight411/ScheduleApp-server](https://github.com/twilight411/ScheduleApp-server)
