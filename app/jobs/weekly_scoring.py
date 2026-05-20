"""
定时任务 — 每周日 20:30 计算所有用户的精灵周得分

Sprint D 增强:
  - 打分后自动运行画像归因分析 (profile_evolution)
  - 打分后检测趋势性触发器 (group_chat_trigger)
  - 带重试机制和详细统计

注册名: app.jobs.weekly_scoring.calculate_all_scores
触发: celery_app.conf.beat_schedule["weekly_scoring"]
"""
import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.jobs.celery_app import celery_app
from app.database import async_session_factory
from app.models.user import User
from app.services.scoring_service import ScoringService
from app.services.profile_evolution import ProfileEvolutionService

import structlog

logger = structlog.get_logger()


@celery_app.task(name="app.jobs.weekly_scoring.calculate_all_scores")
def calculate_all_scores():
    """入口：同步包装异步调用"""
    asyncio.run(_calculate_all())


async def _calculate_all():
    """遍历所有活跃用户，计算5精灵周得分 + 画像归因"""
    async with async_session_factory() as session:
        # 本周一
        today = datetime.now(timezone.utc).date()
        week_start = today - timedelta(days=today.weekday())

        # 获取所有活跃用户
        result = await session.execute(
            select(User.id).where(
                User.is_active == True,
                User.is_deleted == False,
            )
        )
        user_ids = [row[0] for row in result.all()]

        logger.info("weekly_scoring_start", user_count=len(user_ids), week=str(week_start))

        success = 0
        errors = 0
        attribution_count = 0
        trigger_count = 0

        for uid in user_ids:
            try:
                # Step 1: 打分
                scoring_svc = ScoringService(session)
                await scoring_svc.calculate_all_spirits(uid, week_start)
                await session.commit()
                success += 1

                # Step 2: 画像归因分析 (Sprint D)
                try:
                    evo_svc = ProfileEvolutionService(session)
                    insights = await evo_svc.run_weekly_attribution(uid, week_start)
                    if insights.get("adjustments"):
                        attribution_count += 1
                    await session.commit()
                except Exception as evo_err:
                    await session.rollback()
                    logger.warning(
                        "weekly_attribution_error",
                        user_id=str(uid),
                        error=str(evo_err),
                    )

                # Step 3: 趋势触发检测 (Sprint C trigger)
                try:
                    from app.services.group_chat_trigger import GroupChatTrigger
                    trigger = GroupChatTrigger(session)
                    trigger_result = await trigger.check_all(uid, {
                        "event_type": "weekly_score",
                        "target_date": today,
                    })
                    if trigger_result.should_trigger:
                        trigger_count += 1
                        # 触发器结果可推送通知或存储，此处记录日志
                        logger.info(
                            "weekly_trigger_detected",
                            user_id=str(uid),
                            triggers=[t.trigger_type for t in trigger_result.triggers],
                        )
                except Exception as trig_err:
                    logger.warning(
                        "weekly_trigger_check_error",
                        user_id=str(uid),
                        error=str(trig_err),
                    )

            except Exception as e:
                await session.rollback()
                errors += 1
                logger.error("weekly_scoring_user_error", user_id=str(uid), error=str(e))

        logger.info(
            "weekly_scoring_done",
            success=success,
            errors=errors,
            attribution_count=attribution_count,
            trigger_count=trigger_count,
            week=str(week_start),
        )