# Bug #004：生命树生图接口超时 / 仅占位图

**状态**：已修复（服务器部署 `spirit-scheduler 5(1)` + 即梦 `.env` + `pip install volcengine`）  
**发现日期**：2026-05-25  
**影响**：`GET /api/v1/tree/weekly/image`、Flutter 植物页顶部生命树图

---

## 现象

1. 请求 `GET /tree/weekly/image` 耗时 **1～6 分钟**，最终返回 200，但 `image_url` 为：
   `https://neeko-copilot.bytedance.net/api/text_to_image?...`
2. 日志反复出现 `image_timeout` → `image_all_retries_failed` → `image_using_fallback`。
3. 用户看到的图像是**通用占位图**，不是按本周五维得分生成的生命树。
4. 曾出现 `SPIRIT_META` 导致 **500**（旧版 `tree.py` 在 router 层访问 `svc.SPIRIT_META`）。

## 根因

### 1. 生产环境未配置即梦生图

服务器 `/app/spirit-scheduler/.env` **缺少**：

- `IMAGE_PROVIDER=jiyun`
- `JIYUN_ACCESS_KEY_ID` / `JIYUN_SECRET_ACCESS_KEY`

默认走 OpenAI 兼容生图 → 无有效 Key → 每次 **45s×2 次** 超时后 fallback。

本地 `spirit-scheduler 5(1)` 配好即梦 + `volcengine` 后，**约 11s** 可返回 `byteimg.com` 真实 URL。

### 2. 未安装 `volcengine` Python 包

即使写入 `.env`，`import volcengine` 失败也会立刻 fallback（日志 `jiyun_api_error: No module named 'volcengine'`）。

### 3. 历史 `tree.py` 实现错误

`/weekly/image` 手写拼 `branches` 并访问 `TreeService.SPIRIT_META`（类上不存在）→ 500。  
应改为 `build_tree_data(..., include_narrative=False)` + `generate_tree_image()`。

### 4. 部署版本不一致

曾只上传 `.deploy/ScheduleApp-server-staging` 零散补丁，**未**同步完整 `spirit-scheduler 5(1)` 与即梦配置，与本地可生图环境不一致。

## 修复

| 项 | 说明 |
|----|------|
| `tree.py` | 使用 `build_tree_data` + `generate_tree_image`，去掉 `SPIRIT_META` |
| `tree_service.py` | 生图接口 `include_narrative=False`，减少 LLM 等待 |
| 服务器 `.env` | 合并 `IMAGE_PROVIDER=jiyun` 与 `JIYUN_*` |
| 依赖 | `/opt/miniconda/bin/pip install volcengine` |
| 整包部署 | `spirit-scheduler 5(1)/spirit-scheduler 4` → `/app/spirit-scheduler` |

### 验证（2026-05-25）

```http
GET /api/v1/tree/weekly/image
Authorization: Bearer <token>
```

- 约 **10s** 返回 200
- `image_url` 含 `byteimg.com`，**非** `neeko-copilot`

## 关联

- Flutter：`schedule_app_flutter/bugs-doc/plant-tree-image-api.md`
- 本地测试脚本：`.deploy/test_local_image_gen.py`
