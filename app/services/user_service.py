"""
用户服务 — 用户信息 CRUD + 头像上传
"""
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.schemas.user import UserUpdateRequest


class UserService:

    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_by_id(self, user_id: uuid.UUID) -> Optional[User]:
        result = await self.db.execute(
            select(User).where(User.id == user_id, User.is_deleted == False)
        )
        return result.scalar_one_or_none()

    async def get_by_email(self, email: str) -> Optional[User]:
        result = await self.db.execute(
            select(User).where(User.email == email, User.is_deleted == False)
        )
        return result.scalar_one_or_none()

    async def update_user(self, user: User, data: UserUpdateRequest) -> User:
        """更新用户基本信息"""
        update_data = data.model_dump(exclude_unset=True)
        if not update_data:
            return user

        for field, value in update_data.items():
            setattr(user, field, value)

        user.updated_at = datetime.now(timezone.utc)
        await self.db.flush()
        return user

    async def update_avatar(self, user: User, avatar_url: str) -> User:
        """更新用户头像 URL"""
        user.avatar_url = avatar_url
        user.updated_at = datetime.now(timezone.utc)
        await self.db.flush()
        return user

    async def soft_delete(self, user: User) -> None:
        """软删除用户（30天后硬删）"""
        user.is_deleted = True
        user.is_active = False
        user.deleted_at = datetime.now(timezone.utc)
        await self.db.flush()
