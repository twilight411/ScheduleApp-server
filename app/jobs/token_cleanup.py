"""
过期 Token 清理 — 每天凌晨执行
清理已过期或已吊销的 refresh tokens
"""
import asyncio
from datetime import datetime, timezone

from sqlalchemy import delete

from app.jobs.celery_app import celery_app
from app.database import async_session_factory
from app.models.user import RefreshToken

import structlog
logger = structlog.get_logger()


@celery_app.task(name="app.jobs.token_cleanup.cleanup_expired_tokens")
def cleanup_expired_tokens():
    asyncio.run(_cleanup())


async def _cleanup():
    async with async_session_factory() as session:
        try:
            # 删除已过期的 token
            result = await session.execute(
                delete(RefreshToken).where(RefreshToken.expires_at < datetime.now(timezone.utc))
            )
            expired_count = result.rowcount

            # 删除已吊销的 token
            result = await session.execute(
                delete(RefreshToken).where(RefreshToken.is_revoked == True)
            )
            revoked_count = result.rowcount

            await session.commit()
            logger.info(
                "token_cleanup_completed",
                expired=expired_count,
                revoked=revoked_count,
            )
        except Exception as e:
            await session.rollback()
            logger.error("token_cleanup_failed", error=str(e))
            raise