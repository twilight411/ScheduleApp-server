"""
Redis 客户端 — 缓存和会话管理
"""
from typing import Optional

import redis.asyncio as aioredis

from app.config import get_settings

_redis_client: Optional[aioredis.Redis] = None


async def init_redis() -> aioredis.Redis:
    """初始化 Redis 连接"""
    global _redis_client
    settings = get_settings()
    _redis_client = aioredis.from_url(
        settings.redis_url,
        encoding="utf-8",
        decode_responses=True,
    )
    return _redis_client


async def get_redis() -> Optional[aioredis.Redis]:
    """获取 Redis 客户端实例"""
    return _redis_client


async def close_redis():
    """关闭 Redis 连接"""
    global _redis_client
    if _redis_client:
        await _redis_client.close()
        _redis_client = None
