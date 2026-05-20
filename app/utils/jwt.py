"""
JWT 工具 — 生成和验证 token
"""
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from passlib.context import CryptContext

from app.config import get_settings

settings = get_settings()
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    # 使用 bcrypt 库进行密码哈希
    import bcrypt
    password = password[:60]  # 截断密码长度，确保不超过 72 字节
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')


def verify_password(plain_password: str, hashed_password: str) -> bool:
    # 使用 bcrypt 库进行密码验证
    import bcrypt
    plain_password = plain_password[:60]  # 与 hash_password 保持一致，截断密码长度
    return bcrypt.checkpw(plain_password.encode('utf-8'), hashed_password.encode('utf-8'))


def create_access_token(user_id: str, extra_claims: Optional[dict] = None) -> str:
    """生成 access token"""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "type": "access",
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_access_token_expire_minutes),
        "jti": str(uuid.uuid4()),
    }
    if extra_claims:
        payload.update(extra_claims)
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def create_refresh_token(user_id: str) -> tuple[str, datetime]:
    """生成 refresh token，返回 (token, expires_at)"""
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=settings.jwt_refresh_token_expire_days)
    payload = {
        "sub": str(user_id),
        "type": "refresh",
        "iat": now,
        "exp": expires_at,
        "jti": str(uuid.uuid4()),
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, expires_at


def decode_token(token: str) -> dict:
    """
    解码 token，返回 payload。
    无效或过期时抛出 jwt.InvalidTokenError / jwt.ExpiredSignatureError
    """
    return jwt.decode(
        token,
        settings.jwt_secret,
        algorithms=[settings.jwt_algorithm],
    )
