# Bug 记录

本目录记录生产环境已确认的问题、根因与修复说明，便于回溯与 Code Review。

| 编号 | 文件 | 状态 | 摘要 |
|------|------|------|------|
| 001 | [weekly-report-task-count-zero.md](./weekly-report-task-count-zero.md) | 已修复 | AI 周报 `stats.total_tasks_planned` 恒为 0 |

修复代码合并到 `main` 后，请在服务器执行 `GIT_DEPLOY.md` 中的日常发布流程并重新生成周报验证。
