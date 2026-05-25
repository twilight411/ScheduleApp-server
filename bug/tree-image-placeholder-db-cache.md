# Bug #006：生命树占位图 URL 被写入 DB 缓存且长期复用

**状态**：已修复（`tree_service.get_or_generate_weekly_tree_image` + 清理 `weekly_tree_images`）  
**发现日期**：2026-05-25  
**影响**：`GET /api/v1/tree/weekly/image`、`cached: true` 仍返回无效图

---

## 现象

1. 接口返回 200，但 `image_url` 为：

   `https://neeko-copilot.bytedance.net/api/text_to_image?...`

2. 日志：`tree_image_cache_hit`，`cached: true`。
3. Flutter 识别为占位 URL，不展示，用户以为「没生图」。
4. 同期 `fruits/image` 可返回 `byteimg.com` 真实即梦地址（说明即梦凭证正常，树图曾走 fallback）。

## 根因

1. **即梦失败或凭证缺失时**，`generate_tree_image()` 走 `_fallback_tree_image()`，返回 neeko 占位链接。
2. **旧缓存逻辑**将占位 URL 写入 `weekly_tree_images`，且 `score_fingerprint` 未变时一直 **`cache_hit`**。
3. 缓存命中条件未排除占位域名（仅检查 `[FALLBACK]` 前缀不够）。

典型日志：

```text
image_using_fallback  purpose=tree_image
tree_image_generated  image_url=https://neeko-copilot.bytedance.net/...
tree_image_cache_hit  week_start=2026-05-25
```

## 修复

1. **`TreeService._is_invalid_cached_tree_url()`**：`neeko-copilot`、`text_to_image` 视为无效，不命中缓存，触发重新生图。
2. **`generate_tree_image`**：若 API 返回仍是占位 URL，降级为 fallback 但不作为「成功缓存」长期复用（结合 flush 逻辑）。
3. **数据清理**（一次性）：

```sql
DELETE FROM weekly_tree_images
WHERE image_url LIKE '%neeko%' OR image_url LIKE '%text_to_image%';
```

脚本：`.deploy/clear_tree_cache_and_test.py`

## 验证

清理后 `GET /tree/weekly/image?refresh=true` → `cached: false`，`image_url` 含 `byteimg.com`。

## 部署检查清单

- [ ] `JIYUN_ACCESS_KEY_ID` / `JIYUN_SECRET_ACCESS_KEY` 在服务器 `.env` 已配置
- [ ] `image_provider=jiyun`
- [ ] 发布后抽查日志无大量 `image_using_fallback`

## 相关

- [tree-weekly-image-500-spirit-meta.md](./tree-weekly-image-500-spirit-meta.md)
