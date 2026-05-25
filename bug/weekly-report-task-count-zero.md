# Bug #001：AI 周报任务统计为 0

**状态**：已修复（`app/services/report_service.py`）  
**发现日期**：2026-05-25  
**影响**：`GET/POST /api/v1/reports/weekly*`、`stats.total_tasks_planned` / `total_tasks_completed`、Flutter 植物页「任务统计：完成 0 / 计划 0」

---

## 现象

1. 用户已在 App 中创建任务（`POST /api/v1/tasks` 返回 200，数据库 `tasks` 表有记录）。
2. 点击「重新生成周报」后，周报文案像「空周/过渡」。
3. API 返回的 `stats` 中 `total_tasks_planned` / `total_tasks_completed` 为 **0**。
4. 服务器日志有真实 LLM 调用（`purpose=weekly_analysis`），但输入为「本周 0 件事」。

## 根因（历史）

初版 `_calculate_weekly_stats()` 只统计 **子任务 SubTask**，且要求 `scheduled_start` 落在本周。App 创建的任务通常无排期子任务 → 统计为空。

中间曾改为「子任务 + deadline/created_at 兜底」，条数偏多（一条父任务拆成多条子任务），与用户在日历里看到的 **任务条数** 不一致。

## 最终方案：直接统计父任务

与产品一致：**用户在 App 里创建的一条 = 周报里的一条任务**。

### 纳入本周的父任务（`tasks` 表）

满足 **任一** 即可：

1. `deadline` 落在 `[week_start, week_end]`
2. `deadline` 为空，且 `created_at` 落在本周（覆盖刚创建、尚未设截止时间的任务）

### 完成 / 取消

| 字段 | 规则 |
|------|------|
| `total_tasks_planned` | 本周纳入的父任务总数 |
| `total_tasks_completed` | `status == "completed"` |
| `total_tasks_cancelled` | `status == "cancelled"` |
| 预计工时 | `estimated_hours`，缺省按 1 小时/条 |
| 按精灵 | `primary_spirit` 分组 |

子任务仍用于打分、质量备注等其它链路；**周报顶部的任务个数不再子任务聚合**。

### 修改文件

- `app/services/report_service.py` — `_calculate_weekly_stats()`

### 部署后验证

1. `git pull` + `systemctl restart spirit-scheduler`
2. `POST /api/v1/reports/weekly/regenerate?week_start=<本周周一>`
3. 账号「雅」本周 3 条父任务时，应看到 `total_tasks_planned: 3`（不是 10+ 子任务）
4. App 重新生成周报，「任务统计：完成 x / 计划 3」

## 后续建议

| 项 | 说明 |
|----|------|
| 写 `deadline` | 创建任务时持久化 Flutter 传入的截止时间 |
| 父任务完成态 | 若只在子任务上勾选进度，可考虑同步更新父任务 `status` |
| 登录后拉任务 | Flutter 登录成功后 `fetchRemoteTasks()` |

---

## 关联

- 仓库：[ScheduleApp-server](https://github.com/twilight411/ScheduleApp-server)
- 生产：`/app/spirit-scheduler`
- Flutter：`schedule_app_flutter/lib/utils/weekly_report_formatter.dart`
