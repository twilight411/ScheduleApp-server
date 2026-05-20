"""
鉴权接口 — 注册、登录、刷新、登出、忘记/重置密码、注销账户

[P0 修复 v2]
  - 完全改用 AuthService（之前是简化版内联实现，与 service 层重复且有 bug）
  - reset-password 接通真实实现（不再是 TODO）
  - 邮箱、密码、name 校验全部下沉到 schema 层（EmailStr）
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.auth import get_current_user
from app.models.user import User
from app.schemas.auth import (
    RegisterRequest,
    LoginRequest,
    TokenResponse,
    RefreshRequest,
    LogoutRequest,
    ForgotPasswordRequest,
    ResetPasswordRequest,
)
from app.schemas.common import success_response, error_response
from app.services.auth_service import AuthService

router = APIRouter(prefix="/auth", tags=["Auth"])


# ========================================
#  错误码映射
# ========================================

_ERROR_HTTP_MAP = {
    "EMAIL_EXISTS": (status.HTTP_409_CONFLICT, "EMAIL_EXISTS", "该邮箱已注册"),
    "AUTH_INVALID_CREDENTIALS": (status.HTTP_401_UNAUTHORIZED, "AUTH_INVALID_CREDENTIALS", "邮箱或密码错误"),
    "AUTH_ACCOUNT_DISABLED": (status.HTTP_403_FORBIDDEN, "AUTH_ACCOUNT_DISABLED", "账户已被禁用"),
    "AUTH_INVALID_TOKEN": (status.HTTP_401_UNAUTHORIZED, "AUTH_INVALID_TOKEN", "Token 无效或已过期"),
    "INVALID_PASSWORD": (status.HTTP_400_BAD_REQUEST, "VALIDATION_ERROR", "密码格式不合法"),
    "VALIDATION_ERROR": (status.HTTP_400_BAD_REQUEST, "VALIDATION_ERROR", "输入校验失败"),
}


def _raise_from_value_error(e: ValueError):
    code = str(e)
    http_status, err_code, msg = _ERROR_HTTP_MAP.get(
        code, (status.HTTP_400_BAD_REQUEST, "VALIDATION_ERROR", code)
    )
    raise HTTPException(status_code=http_status, detail=error_response(err_code, msg))


# ========================================
#  注册
# ========================================

@router.post("/register")
async def register(body: RegisterRequest, db: AsyncSession = Depends(get_db)):
    """
    注册新用户。

    自动创建：
      - User
      - UserProfile（含完整默认 preferences，避免后续接口拿到空值崩溃）
      - 5 个 SpiritIntensity（每个精灵默认 50）

    返回 access + refresh token。
    """
    svc = AuthService(db)
    try:
        user, tokens = await svc.register(body.email, body.password, body.name)
    except ValueError as e:
        _raise_from_value_error(e)

    return success_response(
        data=TokenResponse(**tokens).model_dump(),
        message="注册成功",
    )


# ========================================
#  登录
# ========================================

@router.post("/login")
async def login(body: LoginRequest, db: AsyncSession = Depends(get_db)):
    svc = AuthService(db)
    try:
        user, tokens = await svc.login(body.email, body.password)
    except ValueError as e:
        _raise_from_value_error(e)

    return success_response(data=TokenResponse(**tokens).model_dump())


# ========================================
#  刷新
# ========================================

@router.post("/refresh")
async def refresh_token(body: RefreshRequest, db: AsyncSession = Depends(get_db)):
    svc = AuthService(db)
    try:
        tokens = await svc.refresh(body.refresh_token)
    except ValueError as e:
        _raise_from_value_error(e)

    return success_response(data=TokenResponse(**tokens).model_dump())


# ========================================
#  登出
# ========================================

@router.post("/logout")
async def logout(body: LogoutRequest, db: AsyncSession = Depends(get_db)):
    svc = AuthService(db)
    await svc.logout(body.refresh_token)
    return success_response(message="已登出")


# ========================================
#  忘记密码 / 重置密码
# ========================================

@router.post("/forgot-password")
async def forgot_password(body: ForgotPasswordRequest, db: AsyncSession = Depends(get_db)):
    """
    发起密码重置。
    为防止用户枚举，无论邮箱是否存在均返回成功。
    开发环境下 reset_token 会写在响应里供调试；生产环境应改为发邮件。
    """
    svc = AuthService(db)
    reset_token = await svc.forgot_password(body.email)

    # 生产环境：reset_token 不应回显，应通过邮件发送。
    # 开发环境为了让前端能联调，可以暂时返回。
    from app.config import get_settings
    settings = get_settings()
    payload = {"sent": True}
    if settings.is_development and reset_token:
        payload["dev_reset_token"] = reset_token

    return success_response(
        data=payload,
        message="如果该邮箱已注册，重置邮件将在几分钟内发送",
    )


@router.post("/reset-password")
async def reset_password(body: ResetPasswordRequest, db: AsyncSession = Depends(get_db)):
    """
    使用 reset_token 重置密码。
    成功后该用户所有 refresh_token 被吊销，需要重新登录。
    """
    svc = AuthService(db)
    try:
        await svc.reset_password(body.token, body.new_password)
    except ValueError as e:
        _raise_from_value_error(e)

    return success_response(message="密码已重置，请重新登录")


# ========================================
#  注销账户
# ========================================

@router.delete("/account")
async def delete_account(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    注销账户 — 软删除，30天内可恢复，30天后由定时任务硬删。
    所有关联的 refresh_token 立即吊销。
    """
    svc = AuthService(db)
    await svc.delete_account(current_user)
    return success_response(message="账户已注销，30天内可联系客服恢复")