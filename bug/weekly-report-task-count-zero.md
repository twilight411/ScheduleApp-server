# Bug #001：AI 周报任务统计为 0

**状态**：已修复（`app/services/report_service.py`）  
**发现日期**：2026-05-25  
**影响**：`GET/POST /api/v1/reports/weekly*`、`stats.total_tasks_planned` / `total_tasks_completed`、Flutter 植物页「任务统计：完成 0 / 计划 0」

---

## 现象

1. 用户已在 App 中创建任务（`POST /api/v1/tasks` 返回 200，数据库 `tasks` 表有记录）。
2. 点击「重新生成周报」后，周报文案像「空周/过渡」（例如 headline「这一周在安静中过渡」）。
3. API 返回的 `stats` 中：
   - `total_tasks_planned`: **0**
   - `total_tasks_completed`: **0**
4. 服务器日志显示 **确实调用了 LLM**（`purpose=weekly_analysis`），并非写死模板；模型输入为「本周安排了 0 件事」，因此生成内容空洞。

## 根因

`ReportService._calculate_weekly_stats()` **仅统计**满足以下条件的 **子任务（SubTask）**：

```python
SubTask.scheduled_start != None
AND scheduled_start 落在 [week_start, week_end]
```

而当前 Flutter 创建任务链路（`/tasks` + `auto_decompose`）常见数据形态为：

| 字段 | 典型值 |
|------|--------|
| `Task.deadline` | `null`（未写入 UI 选择的截止时间） |
| `SubTask.scheduled_start` | `null`（AI 拆解后未自动排期） |
| `Task.created_at` | 有值（创建时间在本周） |

因此即使用户有 3 个父任务、10+ 子任务，统计查询结果仍为 **空集** → `total_tasks_planned = 0` → AI 周报按「无任务」生成。

**说明**：周报不是预设文案；是 **统计口径过窄** 导致喂给模型的数据为 0。

## 验证方式（修复前）

```bash
# 登录后
curl -H "Authorization: Bearer <token>" \
  "http://47.118.28.102:8000/api/v1/reports/weekly?week_start=2026-05-25"
# 观察 data.stats.total_tasks_planned == 0，但 GET /tasks 有条目
```

数据库（`/app/spirit-scheduler/spirit.db`）可查：`tasks` 有记录，`subtasks.scheduled_start` 多为 NULL。

## 解决方案

扩展 `_calculate_weekly_stats` 的纳入规则，在 **同一用户、同一自然周** 内，子任务满足 **任一** 条件即计入「计划」：

1. **已排期**：`scheduled_start` 落在 `[week_start 00:00, week_end 23:59:59]`（原逻辑保留）。
2. **未排期 + 有 deadline**：`scheduled_start IS NULL` 且父任务 `deadline` 落在本周。
3. **未排期 + 无 deadline**：`scheduled_start IS NULL` 且父任务 `deadline IS NULL`，且父任务 `created_at` 落在本周（覆盖 App 刚创建、尚未排期的任务）。

同时：

- 完成判定：`status == "completed"` **或** `completion_percent >= 100`（与打分/子任务完成接口一致）。
- `most_productive_day/hour`：完成子任务若无 `scheduled_start`，回退使用父任务 `deadline` 作为参考时间。

### 修改文件

- `app/services/report_service.py` — `_calculate_weekly_stats()`
- 新增 import：`or_`、`selectinload`

### 部署后验证

1. 服务器 `git pull` + `systemctl restart spirit-scheduler`
2. `POST /api/v1/reports/weekly/regenerate?week_start=<本周周一>`
3. 确认 `stats.total_tasks_planned` ≥ 实际子任务数（未排期但本周创建的任务应计入）
4. App 植物页重新生成周报，「任务统计」行应显示非 0

## 后续建议（非本 Bug 范围）

| 项 | 说明 |
|----|------|
| 创建任务写 `deadline` | `POST /tasks` 应持久化 Flutter 传入的 `deadline`，便于日历与按 deadline 统计 |
| 子任务排期 | 拆解后为子任务写入 `scheduled_start`，统计与周报会更准确 |
| 登录后拉任务 | Flutter `TaskProvider` 在登录成功后应 `fetchRemoteTasks()`，避免仅显示空本地缓存 |

---

## 关联

- 仓库：[ScheduleApp-server](https://github.com/twilight411/ScheduleApp-server)
- 生产目录：`/app/spirit-scheduler`
- Flutter 展示：`schedule_app_flutter/lib/utils/weekly_report_formatter.dart`（读取 `stats.total_tasks_planned`）
