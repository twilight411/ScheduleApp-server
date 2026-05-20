"""
数据清理 — 每月执行
清理 90 天前的 task_events
"""
import asyncio

from app.jobs.celery_app import celery_app
from app.database import async_session_factory
from app.services.event_service import EventService

import structlog
logger = structlog.get_logger()


@celery_app.task(name="app.jobs.data_cleanup.cleanup_old_events")
def cleanup_old_events():
    asyncio.run(_cleanup())


async def _cleanup():
    async with async_session_factory() as session:
        try:
            svc = EventService(session)
            deleted = await svc.cleanup_old_events(retention_days=90)
            await session.commit()
            logger.info("data_cleanup_completed", events_deleted=deleted)
        except Exception as e:
            await session.rollback()
            logger.error("data_cleanup_failed", error=str(e))
            raise