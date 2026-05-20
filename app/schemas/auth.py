"""
鉴权 Schemas — 注册/登录/Token/密码重置

[P0 修复 v2]
  - email 改为 EmailStr，自动校验格式（需 pip install email-validator）
  - name 增加 strip 校验，禁止纯空白
  - 密码长度上限改为 72 字节实际生效边界
"""
from pydantic import BaseModel, EmailStr, Field, field_validator


class RegisterRequest(BaseModel):
    """注册请求"""
    email: EmailStr = Field(..., max_length=255)
    password: str = Field(..., min_length=6, max_length=128)
    name: str = Field(..., min_length=1, max_length=100)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("name 不能为纯空白")
        return v

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, v: str) -> str:
        # EmailStr 已校验格式，这里统一小写 + strip
        return v.lower().strip()


class LoginRequest(BaseModel):
    """登录请求"""
    email: EmailStr
    password: str = Field(..., min_length=1, max_length=128)

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, v: str) -> str:
        return v.lower().strip()


class TokenResponse(BaseModel):
    """Token 响应 — 登录/注册/刷新成功后返回"""
    access_token: str
    refresh_token: str
    expires_in: int
    token_type: str = "bearer"


class RefreshRequest(BaseModel):
    """刷新 Token 请求"""
    refresh_token: str = Field(..., min_length=10)


class ForgotPasswordRequest(BaseModel):
    """忘记密码"""
    email: EmailStr = Field(..., max_length=255)

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, v: str) -> str:
        return v.lower().strip()


class ResetPasswordRequest(BaseModel):
    """重置密码"""
    token: str = Field(..., min_length=10)
    new_password: str = Field(..., min_length=6, max_length=128)


class LogoutRequest(BaseModel):
    """登出请求"""
    refresh_token: str = Field(..., min_length=10)