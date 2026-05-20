"""
系统接口 — 健康检查、版本信息
"""
from fastapi import APIRouter
from sqlalchemy import text

from app.config import get_settings
from app.database import async_session_factory
from app.utils.redis_client import get_redis

router = APIRouter(tags=["System"])


@router.get("/health")
async def health_check():
    """存活检查（无需鉴权）"""
    return {"status": "ok"}


@router.get("/health/ready")
async def readiness_check():
    """就绪检查 — 验证 DB 和 Redis 连通性"""
    checks = {"database": False, "redis": False}

    # 检查 PostgreSQL
    try:
        async with async_session_factory() as session:
            await session.execute(text("SELECT 1"))
            checks["database"] = True
    except Exception:
        pass

    # 检查 Redis
    try:
        redis = await get_redis()
        if redis:
            await redis.ping()
            checks["redis"] = True
    except Exception:
        pass

    all_ok = all(checks.values())
    return {
        "status": "ready" if all_ok else "degraded",
        "checks": checks,
    }


@router.get("/version")
async def version_info():
    """版本信息"""
    settings = get_settings()
    return {
        "app": settings.app_name,
        "version": settings.app_version,
        "env": settings.app_env,
    }
