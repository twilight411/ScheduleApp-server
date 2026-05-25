# Bug 记录

本目录记录生产环境已确认的问题、根因与修复说明，便于回溯与 Code Review。

| 编号 | 文件 | 状态 | 摘要 |
|------|------|------|------|
| 001 | [weekly-report-task-count-zero.md](./weekly-report-task-count-zero.md) | 已修复 | AI 周报 `stats.total_tasks_planned` 恒为 0 |
| 003 | [task-completion-checkbox-not-synced.md](./task-completion-checkbox-not-synced.md) | 已修复 | 日历勾选 100% 未上传服务器 |
| 002 | [task-deadline-not-synced.md](./task-deadline-not-synced.md) | 已修复 | 创建/更新任务 `deadline` 未落库，PATCH 500，月历日期错一天 |
| — | （Flutter `bugs-doc` #002） | 已修复 | 完成度依赖 PATCH 任务成功；见主仓 `schedule_app_flutter/bugs-doc/task-completion-not-synced.md` |

修复代码合并到 `main` 后，请在服务器执行 `GIT_DEPLOY.md` 中的日常发布流程并重新生成周报验证。
