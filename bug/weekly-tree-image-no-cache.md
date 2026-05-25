# Bug #004：切换周时生命树 AI 生图重复调用

**状态**：已修复（`app/services/tree_service.py`、`app/routers/tree.py`、`app/models/report.py`）  
**发现日期**：2026-05-25  
**影响**：`GET /api/v1/tree/weekly/image`、即梦/火山生图 API 调用量、Flutter 植物页切周加载耗时

---

## 现象

1. 用户在植物页切换「上一周 / 下一周」时，生命树图片每次都要等待 **数十秒** 重新生成。
2. 即使 **本周任务与五维得分完全未变**，再次进入同一周仍会触发完整生图流程。
3. 快速在两周之间来回切换时体验极差，loading 转圈频繁出现。
4. 服务器日志每次请求均有 `generating_tree_image` / `jiyun_image_generation_success`，无缓存命中记录。

## 根因

`GET /tree/weekly/image` 在路由层 **无条件** 调用 `TreeService.generate_tree_image()`：

```python
image_url = await svc.generate_tree_image(
    branches=branches,
    overall=overall,
    tree_health=tree_health,
    season=season,
    user_id=current_user.id,
)
```

- 未持久化已生成的 `image_url`。
- 未根据五维得分 / 综合分 / 树态计算 **内容指纹**，无法在「状态未变」时短路返回。
- 每次 HTTP 请求都会走即梦异步任务（轮询约 3s × N 次），成本高、延迟大。

Flutter 端虽有会话内 `PlantWeekCache`，但 **杀进程、重装、换设备** 后仍会打到上述无缓存接口；且前端缓存与得分变化未绑定，无法作为权威层。

## 解决方案（后端）

### 1. 新增表 `weekly_tree_images`

**文件**：`app/models/report.py`

| 字段 | 说明 |
|------|------|
| `user_id` + `week_start` | 唯一约束，每用户每周一条 |
| `score_fingerprint` | 五维得分 + overall + tree_health + season 拼接指纹 |
| `image_url` | 已生成图片 URL |
| `updated_at` | 指纹变化后更新时间 |

### 2. 缓存读写逻辑

**文件**：`app/services/tree_service.py`

- `tree_image_score_fingerprint()` — 与客户端对齐的指纹算法。
- `get_or_generate_weekly_tree_image()` — 指纹命中且 URL 有效 → 直接返回 `(url, cached=True)`；否则生图并 upsert 记录。

### 3. 接口响应

**文件**：`app/routers/tree.py`

响应 `data` 增加 `"cached": true | false`，便于客户端区分「秒回缓存」与「新生成」。

### 修改文件一览

| 文件 | 变更 |
|------|------|
| `app/models/report.py` | 新增 `WeeklyTreeImage` 模型 |
| `app/models/__init__.py` | 导出 `WeeklyTreeImage` |
| `app/services/tree_service.py` | 指纹 + `get_or_generate_weekly_tree_image()` |
| `app/routers/tree.py` | 路由改用缓存方法，返回 `cached` 字段 |

## 部署

生产路径：`/app/spirit-scheduler`（`47.118.28.102`）

1. 上传上述文件并 `systemctl restart spirit-scheduler`。
2. 执行一次 `init_db()`（或 Alembic 迁移）创建 `weekly_tree_images` 表。
3. 验证：`sqlite3 spirit.db` 中可见表 `weekly_tree_images`。

## 部署后验证

1. 登录测试账号，请求 `GET /api/v1/tree/weekly/image?week_start=<本周周一>` — 首次较慢，`cached: false`。
2. **不修改任务**，再次请求同一 `week_start` — 应在 **1s 内** 返回，`cached: true`，日志为 `tree_image_cache_hit`。
3. 修改该周任务完成度导致得分变化后再次请求 — 应 `cached: false` 并更新库中 `score_fingerprint` 与 `image_url`。
4. App 植物页：同一周内切走再切回，配合 Flutter 内存缓存应几乎无 loading。

## 关联

- Flutter 侧会话缓存与指纹校验：`schedule_app_flutter/bugs-doc/weekly-tree-image-regenerates-on-week-switch.md`
- 生图客户端：`app/ai/image_client.py`（即梦 `jiyun` provider）
- 生产服务：`spirit-scheduler.service`，数据库 `spirit.db`
