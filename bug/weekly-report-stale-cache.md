# Bug #005：AI 周报生成后不再随任务更新

**状态**：已修复（`reports.py` + `report_service.py` 统计逻辑，见 #001）  
**发现日期**：2026-05-25  
**影响**：`GET /api/v1/reports/weekly`、`POST /api/v1/reports/weekly/regenerate`

---

## 现象

1. 用户首次打开植物页生成周报后，再创建/完成任务，周报仍显示「0 任务」或旧文案。
2. `GET /reports/weekly?week_start=...` 第二次起**很快**返回，内容不变。
3. `POST /reports/weekly/regenerate` 未带 `week_start` 时，只重算「当前自然周」，与 App 切周查看不一致。

## 根因

### 1. 服务端持久化缓存

`ReportService.generate_weekly_report(..., force=False)`：

- 若 DB 已有该 `user_id + week_start` 的 `WeeklyReport`，**直接返回**，不重新拉任务、不调 LLM。

任务与完成度变更后，除非 `force=True`，否则 stats / analysis 均为历史快照。

### 2. 统计口径导致「首次生成就写成 0 任务」（见 #001）

初版只统计 `scheduled_start` 落在本周的 **SubTask**。无排期子任务 → `stats.total_tasks_planned=0` → AI prompt 为「本周 0 件事」。  
该错误内容被缓存后，用户误以为「刷新也没用」。

### 3. Regenerate 接口周次参数

旧版 `POST /weekly/regenerate` 写死「今天所在周一」，忽略客户端传入的 `week_start`。

## 修复

| 项 | 说明 |
|----|------|
| `GET /reports/weekly?refresh=true` | 强制 `generate_weekly_report(..., force=True)` |
| `POST /reports/weekly/regenerate?week_start=YYYY-MM-DD` | 按指定周重算 |
| `_calculate_weekly_stats` | 见 [Bug #001](./weekly-report-task-count-zero.md) |

## AI 是否收到任务数据？

**会。** `generate_weekly_report` 流程：

1. `calculate_all_spirits` / `build_tree_data`
2. `_calculate_weekly_stats` → 写入 `stats`（计划数、完成数、按精灵等）
3. `_collect_quality_notes` → 部分完成备注
4. `_generate_analysis` → LLM prompt 包含上述 `behavior_lines` 与 `stats`

此前问题在于 **stats 为 0** + **返回旧缓存**，不是「没发给 AI」。

## 验证

1. 创建任务并同步完成度（见 Flutter `task-completion-not-synced.md`）
2. `GET /reports/weekly?week_start=<周一>&refresh=true` 或 `POST .../regenerate?week_start=<周一>`
3. 响应 `stats.total_tasks_planned` / `total_tasks_completed` 与当周任务一致

## 关联

- Flutter：`schedule_app_flutter/bugs-doc/plant-week-report-cache.md`
- Bug #001：统计为 0 的根因
