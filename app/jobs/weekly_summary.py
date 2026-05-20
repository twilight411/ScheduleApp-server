"""
定时任务 — 每周日 21:30 为所有用户生成行为摘要

注册名: app.jobs.weekly_summary.generate_all_summaries
触发: celery_app.conf.beat_schedule["weekly_summary_gen"]

行为摘要用于 Context Engineering（精灵对话/协商时的背景信息）。
比周报更轻量，不涉及 LLM 调用。
"""
import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.jobs.celery_app import celery_app
from app.database import async_session_factory
from app.models.user import User
from app.services.report_service import ReportService

import structlog

logger = structlog.get_logger()


@celery_app.task(name="app.jobs.weekly_summary.generate_all_summaries")
def generate_all_summaries():
    """入口"""
    asyncio.run(_generate_all())


async def _generate_all():
    """遍历所有活跃用户，生成行为摘要"""
    async with async_session_factory() as session:
        today = datetime.now(timezone.utc).date()
        week_start = today - timedelta(days=today.weekday())

        result = await session.execute(
            select(User.id).where(
                User.is_active == True,
                User.is_deleted == False,
            )
        )
        user_ids = [row[0] for row in result.all()]

        logger.info("weekly_summary_gen_start", user_count=len(user_ids), week=str(week_start))

        success = 0
        errors = 0
        for uid in user_ids:
            try:
                svc = ReportService(session)
                await svc.generate_weekly_summary(uid, week_start)
                await session.commit()
                success += 1
            except Exception as e:
                await session.rollback()
                errors += 1
                logger.error("weekly_summary_user_error", user_id=str(uid), error=str(e))

        logger.info(
            "weekly_summary_gen_done",
            success=success,
            errors=errors,
            week=str(week_start),
        )