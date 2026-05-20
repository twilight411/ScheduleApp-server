"""
用户 Schemas — 用户信息输出 + 更新请求

注意：RegisterRequest / LoginRequest / RefreshRequest / ResetPasswordRequest
已统一移至 schemas/auth.py，此处不再重复定义。
"""
import uuid
from typing import Optional
from datetime import datetime
from pydantic import BaseModel, Field


class UserOut(BaseModel):
    """用户信息输出"""
    id: str
    email: str
    name: str
    avatar_url: Optional[str] = None
    timezone: str = "Asia/Shanghai"
    is_active: bool = True
    created_at: Optional[datetime] = None

    model_config = {"from_attributes": True}

    @classmethod
    def model_validate(cls, obj, **kwargs):
        """兼容 ORM 对象"""
        if hasattr(obj, "id") and isinstance(obj.id, uuid.UUID):
            data = {
                "id": str(obj.id),
                "email": obj.email,
                "name": obj.name,
                "avatar_url": obj.avatar_url,
                "timezone": obj.timezone,
                "is_active": obj.is_active,
                "created_at": obj.created_at,
            }
            return cls(**data)
        return super().model_validate(obj, **kwargs)


class UserUpdateRequest(BaseModel):
    """更新用户信息"""
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    timezone: Optional[str] = None