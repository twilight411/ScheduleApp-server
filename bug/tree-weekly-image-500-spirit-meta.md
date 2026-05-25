# Bug #005：生命树生图接口 500（`TreeService` 无 `SPIRIT_META`）

**状态**：已修复（热更新 `app/routers/tree.py` + `app/services/tree_service.py`）  
**发现日期**：2026-05-25  
**影响**：`GET /api/v1/tree/weekly/image`、Flutter 植物状态卡片 AI 生图

---

## 现象

1. 用户多次进入植物页，**月度果实**可正常生成（`GET /fruits/image` 200）。
2. **植物状态生命树**始终为本地样例图，或加载后无变化。
3. 服务器日志（用户 IP 如 `223.104.122.37`）：

```text
GET /api/v1/tree/weekly?week_start=2026-05-25 → 200
GET /api/v1/tree/weekly/image?week_start=2026-05-25 → 500
error: 'TreeService' object has no attribute 'SPIRIT_META'
  meta = svc.SPIRIT_META.get(score.spirit_code, {})
```

4. 用脚本直连 API：`tree/weekly` 200，`tree/weekly/image` **500**；果实接口 200。

## 根因

服务器 `/app/spirit-scheduler/app/routers/tree.py` 仍为**旧版实现**（与本地 `spirit-scheduler 4` 不一致）：

- 在 router 内手写 `branches` 循环，访问 **`svc.SPIRIT_META`**、**`svc.BRANCH_COLORS`**（实例属性）。
- 当前 `TreeService` 仅在模块级定义 **`SPIRIT_META`**、**`BRANCH_COLORS`**，无 `self.SPIRIT_META`。
- 未使用 `build_tree_data()` + `get_or_generate_weekly_tree_image()` 的新流程。

**为何部署后仍出现**：`deploy_spirit4_to_server.py` 全量 tar 部署时，曾出现其它文件 MD5 一致但 **`tree.py` 未正确覆盖**（或之后被旧文件回写）。对比命令：

```bash
grep get_or_generate /app/spirit-scheduler/app/routers/tree.py  # 修复后应有匹配
```

## 修复

1. 使用 `spirit-scheduler 4` 中正确版本：
   - `tree.py`：`build_tree_data` → `get_or_generate_weekly_tree_image`，支持 `refresh=true`
   - `tree_service.py`：模块级 `SPIRIT_META`、`WeeklyTreeImage` 表缓存、占位 URL 不命中缓存
2. 热更新脚本：`.deploy/hotfix_tree_router.py`（上传 4 个文件后 `systemctl restart spirit-scheduler`）。

## 验证

```http
GET /api/v1/tree/weekly/image?week_start=2026-05-25&refresh=true
Authorization: Bearer <token>
→ 200，image_url 为 byteimg.com 即梦 CDN
```

## 相关

- [tree-image-placeholder-db-cache.md](./tree-image-placeholder-db-cache.md)（500 修复后仍可能命中旧占位缓存）
- Flutter：[plant-tree-image-cache-blocks-retry.md](../../../schedule_app_flutter/bugs-doc/plant-tree-image-cache-blocks-retry.md)
