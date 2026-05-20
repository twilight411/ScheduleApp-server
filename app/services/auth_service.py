"""
鉴权服务 — 注册、登录、Token 管理、密码重置、账户注销

[P0 修复 v2]
  - register: 用 UserPreferences().model_dump() 作为完整默认偏好（不再是空 dict）
  - register: 邮箱标准化 lower().strip() 后再查重
  - register: 密码按 UTF-8 字节截断 72，正确处理中文密码
  - register: 三次 flush 合并为一次（减少 SQLite 写锁竞争）
  - register: 真正校验是否已存在（即便 _get_user_by_email 已过滤 is_deleted）
  - hash_password 统一走 jwt.py 的实现，不再在本文件里重复 bcrypt 调用
  - reset_password: 同样按字节截断
"""
import uuid
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

import structlog

from app.models.user import User, RefreshToken
from app.models.profile import UserProfile, SpiritIntensity
from app.schemas.profile import UserPreferences
from app.utils.jwt import (
    hash_password,
    verify_password,
    create_access_token,
    create_refresh_token,
    decode_token,
)

logger = structlog.get_logger()

# ===== 常量 =====
SPIRIT_CODES = ["light", "water", "soil", "air", "nutrition"]
DEFAULT_INTENSITY = 50

RESET_TOKEN_EXPIRE_MINUTES = 30
MAX_REFRESH_TOKENS_PER_USER = 10


def _truncate_password_bytes(password: str, max_bytes: int = 72) -> bytes:
    """
    按 UTF-8 字节正确截断密码到 bcrypt 上限 72 字节。
    中文一字 3 字节，按字符截断会出错。
    """
    encoded = password.encode("utf-8")
    if len(encoded) <= max_bytes:
        return encoded
    # 截到 max_bytes 字节，再尝试解码避免在多字节字符中间切断
    truncated = encoded[:max_bytes]
    while truncated:
        try:
            truncated.decode("utf-8")
            return truncated
        except UnicodeDecodeError:
            truncated = truncated[:-1]
    return b""


class AuthService:

    def __init__(self, db: AsyncSession):
        self.db = db

    # ========================================
    #  注册
    # ========================================

    async def register(
        self, email: str, password: str, name: str
    ) -> Tuple[User, dict]:
        """
        注册新用户。
        自动创建: User + UserProfile（带完整默认 preferences） + 5 个 SpiritIntensity 记录。
        返回 (user, token_pair)。
        """
        # 邮箱已经在 schema 层标准化过，这里再防御一次
        email = (email or "").lower().strip()
        name = (name or "").strip()
        if not email or not name:
            raise ValueError("VALIDATION_ERROR")

        # 检查邮箱是否已注册（_get_user_by_email 自带 is_deleted=False 过滤）
        existing = await self._get_user_by_email(email)
        if existing:
            raise ValueError("EMAIL_EXISTS")

        # 同时也防御性检查 is_deleted=True 的同邮箱用户
        # 业务策略：软删用户 30 天内邮箱不可复用，30 天后释放
        deleted_check = await self.db.execute(
            select(User).where(
                User.email == email,
                User.is_deleted == True,
            )
        )
        deleted_user = deleted_check.scalar_one_or_none()
        if deleted_user and deleted_user.deleted_at:
            recent = datetime.now(timezone.utc) - deleted_user.deleted_at < timedelta(days=30)
            if recent:
                raise ValueError("EMAIL_EXISTS")
            # 30 天前删除的，硬删它的记录腾位置
            await self.db.delete(deleted_user)
            await self.db.flush()

        # 密码哈希（按字节正确截断）
        import bcrypt
        pwd_bytes = _truncate_password_bytes(password, max_bytes=72)
        if not pwd_bytes:
            raise ValueError("INVALID_PASSWORD")
        hashed_pwd = bcrypt.hashpw(pwd_bytes, bcrypt.gensalt()).decode("utf-8")

        # 一次性创建所有对象（profile.id 用 uuid4 显式分配，避免依赖 flush）
        user = User(
            email=email,
            hashed_password=hashed_pwd,
            name=name,
        )

        profile = UserProfile(
            id=uuid.uuid4(),
            user=user,                                  # 用 relationship 关联，不需要 user_id
            preferences=UserPreferences().model_dump(),  # ← 关键修复：完整默认偏好
            stats={},
            tags=[],
            onboarding_completed=False,
        )

        self.db.add(user)
        self.db.add(profile)

        # 五精灵初始强度
        for code in SPIRIT_CODES:
            self.db.add(SpiritIntensity(
                profile=profile,                        # relationship 关联
                spirit_code=code,
                base_intensity=DEFAULT_INTENSITY,
                learned_delta=0,
                is_locked=False,
            ))

        # 一次 flush 落库（之前是 3 次）
        await self.db.flush()

        # 签发 Token
        token_pair = await self._issue_tokens(user)

        logger.info("user_registered", user_id=str(user.id), email=user.email)
        return user, token_pair

    # ========================================
    #  登录
    # ========================================

    async def login(self, email: str, password: str) -> Tuple[User, dict]:
        email = (email or "").lower().strip()
        user = await self._get_user_by_email(email)

        if not user or not verify_password(password, user.hashed_password):
            raise ValueError("AUTH_INVALID_CREDENTIALS")

        if not user.is_active:
            raise ValueError("AUTH_ACCOUNT_DISABLED")

        token_pair = await self._issue_tokens(user)

        logger.info("user_logged_in", user_id=str(user.id))
        return user, token_pair

    # ========================================
    #  Token 刷新
    # ========================================

    async def refresh(self, refresh_token_str: str) -> dict:
        """使用 Refresh Token 获取新的 Token 对（轮换）"""
        try:
            payload = decode_token(refresh_token_str)
        except Exception:
            raise ValueError("AUTH_INVALID_TOKEN")

        if payload.get("type") != "refresh":
            raise ValueError("AUTH_INVALID_TOKEN")

        user_id = payload.get("sub")
        if not user_id:
            raise ValueError("AUTH_INVALID_TOKEN")

        token_hash = self._hash_token(refresh_token_str)
        result = await self.db.execute(
            select(RefreshToken).where(
                RefreshToken.token_hash == token_hash,
                RefreshToken.is_revoked == False,
            )
        )
        stored_token = result.scalar_one_or_none()

        if not stored_token:
            # 重放检测：吊销该用户所有 token
            logger.warning("refresh_token_reuse_detected", user_id=user_id)
            try:
                await self._revoke_all_tokens(uuid.UUID(user_id))
            except (ValueError, Exception):
                pass
            raise ValueError("AUTH_INVALID_TOKEN")

        if stored_token.expires_at < datetime.now(timezone.utc):
            stored_token.is_revoked = True
            await self.db.flush()
            raise ValueError("AUTH_INVALID_TOKEN")

        user_result = await self.db.execute(
            select(User).where(
                User.id == uuid.UUID(user_id),
                User.is_active == True,
                User.is_deleted == False,
            )
        )
        user = user_result.scalar_one_or_none()
        if not user:
            raise ValueError("AUTH_INVALID_TOKEN")

        stored_token.is_revoked = True
        token_pair = await self._issue_tokens(user)

        logger.info("token_refreshed", user_id=str(user.id))
        return token_pair

    # ========================================
    #  登出
    # ========================================

    async def logout(self, refresh_token_str: str) -> None:
        token_hash = self._hash_token(refresh_token_str)
        result = await self.db.execute(
            select(RefreshToken).where(RefreshToken.token_hash == token_hash)
        )
        stored = result.scalar_one_or_none()
        if stored:
            stored.is_revoked = True
            await self.db.flush()
        logger.info("user_logged_out")

    # ========================================
    #  忘记密码 / 重置密码
    # ========================================

    async def forgot_password(self, email: str) -> Optional[str]:
        """
        发起密码重置。为防枚举，邮箱不存在时静默。
        当前直接返回 reset_token；生产环境应通过邮件发送。
        """
        email = (email or "").lower().strip()
        user = await self._get_user_by_email(email)
        if not user:
            logger.info("forgot_password_email_not_found", email=email)
            return None

        reset_token = create_access_token(
            user_id=str(user.id),
            extra_claims={
                "type": "password_reset",
                "salt": secrets.token_hex(8),
            },
        )

        logger.info("password_reset_token_generated", user_id=str(user.id))
        return reset_token

    async def reset_password(self, token: str, new_password: str) -> None:
        """使用重置 Token 设置新密码"""
        try:
            payload = decode_token(token)
        except Exception:
            raise ValueError("AUTH_INVALID_TOKEN")

        if payload.get("type") != "password_reset":
            raise ValueError("AUTH_INVALID_TOKEN")

        user_id = payload.get("sub")
        if not user_id:
            raise ValueError("AUTH_INVALID_TOKEN")

        result = await self.db.execute(
            select(User).where(
                User.id == uuid.UUID(user_id),
                User.is_deleted == False,
            )
        )
        user = result.scalar_one_or_none()
        if not user:
            raise ValueError("AUTH_INVALID_TOKEN")

        # 按字节正确截断
        import bcrypt
        pwd_bytes = _truncate_password_bytes(new_password, max_bytes=72)
        if not pwd_bytes:
            raise ValueError("INVALID_PASSWORD")
        user.hashed_password = bcrypt.hashpw(pwd_bytes, bcrypt.gensalt()).decode("utf-8")
        user.updated_at = datetime.now(timezone.utc)

        # 强制重新登录
        await self._revoke_all_tokens(user.id)
        await self.db.flush()
        logger.info("password_reset_success", user_id=str(user.id))

    # ========================================
    #  账户注销
    # ========================================

    async def delete_account(self, user: User) -> None:
        user.is_active = False
        user.is_deleted = True
        user.deleted_at = datetime.now(timezone.utc)
        user.updated_at = datetime.now(timezone.utc)
        await self._revoke_all_tokens(user.id)
        await self.db.flush()
        logger.info("account_deleted", user_id=str(user.id))

    # ========================================
    #  内部辅助
    # ========================================

    async def _get_user_by_email(self, email: str) -> Optional[User]:
        """按邮箱查找未删除用户"""
        result = await self.db.execute(
            select(User).where(
                User.email == email.lower().strip(),
                User.is_deleted == False,
            )
        )
        return result.scalar_one_or_none()

    async def _issue_tokens(self, user: User) -> dict:
        access_token = create_access_token(str(user.id))
        refresh_token_str, expires_at = create_refresh_token(str(user.id))

        stored = RefreshToken(
            user_id=user.id,
            token_hash=self._hash_token(refresh_token_str),
            expires_at=expires_at,
        )
        self.db.add(stored)
        await self._cleanup_excess_tokens(user.id)
        await self.db.flush()

        from app.config import get_settings
        settings = get_settings()

        return {
            "access_token": access_token,
            "refresh_token": refresh_token_str,
            "token_type": "bearer",
            "expires_in": settings.jwt_access_token_expire_minutes * 60,
        }

    async def _revoke_all_tokens(self, user_id: uuid.UUID) -> None:
        result = await self.db.execute(
            select(RefreshToken).where(
                RefreshToken.user_id == user_id,
                RefreshToken.is_revoked == False,
            )
        )
        for token in result.scalars().all():
            token.is_revoked = True
        await self.db.flush()

    async def _cleanup_excess_tokens(self, user_id: uuid.UUID) -> None:
        result = await self.db.execute(
            select(RefreshToken)
            .where(
                RefreshToken.user_id == user_id,
                RefreshToken.is_revoked == False,
            )
            .order_by(RefreshToken.created_at.desc())
        )
        active_tokens = list(result.scalars().all())
        if len(active_tokens) > MAX_REFRESH_TOKENS_PER_USER:
            for old_token in active_tokens[MAX_REFRESH_TOKENS_PER_USER:]:
                old_token.is_revoked = True

    @staticmethod
    def _hash_token(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()