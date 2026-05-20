"""
定时任务 — 每月1号 10:00 为所有用户生成月度果实

Sprint D 增强:
  - 果实生成后推送通知（含果实 emoji 和描述）
  - 统计面板增强

注册名: app.jobs.monthly_fruit.generate_all_fruits
触发: celery_app.conf.beat_schedule["monthly_fruit_gen"]

生成的是上一个月的果实（例如2月1号生成1月的果实）。
"""
import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.jobs.celery_app import celery_app
from app.database import async_session_factory
from app.models.user import User
from app.services.fruit_service import FruitService, get_fruit_type
from app.services.notification_service import NotificationService

import structlog

logger = structlog.get_logger()


@celery_app.task(name="app.jobs.monthly_fruit.generate_all_fruits")
def generate_all_fruits():
    """入口"""
    asyncio.run(_generate_all())


async def _generate_all():
    """遍历所有活跃用户，生成上月果实 + 推送通知"""
    async with async_session_factory() as session:
        # 上一个月
        today = datetime.now(timezone.utc).date()
        first_of_month = today.replace(day=1)
        last_month_end = first_of_month - timedelta(days=1)
        month_str = last_month_end.strftime("%Y-%m")

        result = await session.execute(
            select(User.id).where(
                User.is_active == True,
                User.is_deleted == False,
            )
        )
        user_ids = [row[0] for row in result.all()]

        logger.info(
            "monthly_fruit_gen_start",
            user_count=len(user_ids),
            month=month_str,
        )

        success = 0
        errors = 0
        notified = 0

        for uid in user_ids:
            try:
                svc = FruitService(session)
                fruit = await svc.generate_monthly_fruit(uid, month_str)
                await session.commit()
                success += 1

                # Sprint D: 推送果实通知
                try:
                    notif_svc = NotificationService(session)
                    fruit_info = get_fruit_type(
                        fruit.overall_score if hasattr(fruit, "overall_score") else 0
                    )
                    emoji = fruit_info.get("emoji", "🌰")
                    name = fruit_info.get("name", "种子")
                    rarity = fruit_info.get("rarity", "common")

                    rarity_label = {
                        "legendary": "传说级",
                        "epic": "史诗级",
                        "rare": "稀有",
                        "common": "",
                    }.get(rarity, "")

                    await notif_svc.create_notification(
                        user_id=uid,
                        type="monthly_fruit",
                        title=f"{emoji} 月度果实已成熟！",
                        body=f"你获得了{rarity_label}{name}！点击查看果实详情和成长记忆~",
                        data={
                            "month": month_str,
                            "fruit_type": fruit_info.get("fruit", "seed"),
                            "fruit_name": name,
                            "rarity": rarity,
                        },
                        push=True,
                    )
                    await session.commit()
                    notified += 1
                except Exception as notif_err:
                    await session.rollback()
                    logger.warning(
                        "monthly_fruit_notification_error",
                        user_id=str(uid),
                        error=str(notif_err),
                    )

            except Exception as e:
                await session.rollback()
                errors += 1
                logger.error(
                    "monthly_fruit_user_error",
                    user_id=str(uid),
                    month=month_str,
                    error=str(e),
                )

        logger.info(
            "monthly_fruit_gen_done",
            success=success,
            errors=errors,
            notified=notified,
            month=month_str,
        )