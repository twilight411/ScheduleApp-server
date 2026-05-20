"""
定时任务 — 每周日 21:00 为所有用户生成周报

Sprint D 增强:
  - 周报生成后自动推送通知
  - 包含 headline 作为通知标题
  - 带统计面板

注册名: app.jobs.weekly_report.generate_all_reports
触发: celery_app.conf.beat_schedule["weekly_report_gen"]

依赖: weekly_scoring 应在 20:30 先完成打分
"""
import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.jobs.celery_app import celery_app
from app.database import async_session_factory
from app.models.user import User
from app.services.report_service import ReportService
from app.services.notification_service import NotificationService

import structlog

logger = structlog.get_logger()


@celery_app.task(name="app.jobs.weekly_report.generate_all_reports")
def generate_all_reports():
    """入口"""
    asyncio.run(_generate_all())


async def _generate_all():
    """遍历所有活跃用户，生成周报 + 推送通知"""
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

        logger.info("weekly_report_gen_start", user_count=len(user_ids), week=str(week_start))

        success = 0
        errors = 0
        notified = 0

        for uid in user_ids:
            try:
                report_svc = ReportService(session)
                report = await report_svc.generate_weekly_report(uid, week_start)
                await session.commit()
                success += 1

                # Sprint D: 推送周报通知
                try:
                    notif_svc = NotificationService(session)
                    headline = report.headline if hasattr(report, "headline") else "本周周报已生成"
                    score = report.overall_score if hasattr(report, "overall_score") else 0

                    await notif_svc.create_notification(
                        user_id=uid,
                        type="weekly_report",
                        title=f"📊 {headline}",
                        body=f"本周总分 {score}，点击查看详细分析和生命树~",
                        data={
                            "week_start": str(week_start),
                            "overall_score": score,
                        },
                        push=True,
                    )
                    await session.commit()
                    notified += 1
                except Exception as notif_err:
                    await session.rollback()
                    logger.warning(
                        "weekly_report_notification_error",
                        user_id=str(uid),
                        error=str(notif_err),
                    )

            except Exception as e:
                await session.rollback()
                errors += 1
                logger.error("weekly_report_user_error", user_id=str(uid), error=str(e))

        logger.info(
            "weekly_report_gen_done",
            success=success,
            errors=errors,
            notified=notified,
            week=str(week_start),
        )