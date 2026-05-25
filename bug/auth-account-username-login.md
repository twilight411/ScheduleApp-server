# Bug #004：登录接口仅支持邮箱，无法使用用户名

**状态**：已修复（`app/schemas/auth.py`、`app/routers/auth.py`、`app/services/auth_service.py`）  
**发现日期**：2026-05-25  
**影响**：`POST /api/v1/auth/login`、`POST /api/v1/auth/register`、Flutter 账号登录页

---

## 现象

1. Flutter 账号登录页填写用户名「雅」+ 密码，请求返回 **422**，提示缺少 `email` 或「不是合法邮箱」。
2. 客户端曾用 `雅@schedule.dev` 拼接邮箱以兼容旧后端，与「用户名登录」产品需求不符。
3. 注册接口同样要求 `EmailStr`，无法直接注册纯中文/短用户名。

## 根因

线上 `ScheduleApp-server-staging` 初版鉴权 Schema 使用 **Pydantic `EmailStr`**：

- `LoginRequest` 仅字段 `email`
- `RegisterRequest` 仅字段 `email`
- `AuthService.login()` / `register()` 按邮箱查 `users.email` 列

Flutter 已改为传 `account`，但旧后端校验失败。

## 修复

1. **Schema**：`RegisterRequest.account`、`LoginRequest.account`（可选兼容 `email`）。
2. **Service**：`login(account, password)` / `register(account, ...)`，账号写入 `users.email` 列（历史列名未改，语义为登录名）。
3. **响应**：`TokenResponse` 增加 `user_id`、`account`、`nickname`（便于 Flutter 写入本地用户 ID）。
4. **部署注意**：发布时必须覆盖 `app/schemas/auth.py`、`app/routers/auth.py`、`app/services/auth_service.py`，勿与旧 tar 包混用。

## 验证

```http
POST /api/v1/auth/login
{ "account": "雅", "password": "123456" }
→ 200，返回 access_token
```

## 相关

- Flutter：[schedule_app_flutter/bugs-doc/account-login-remove-email-fallback.md](../../../schedule_app_flutter/bugs-doc/account-login-remove-email-fallback.md)
