"""
过期任务检查 + 对话建议过期

Sprint D 增强:
  - 逾期任务 → 通知用户 + 精灵关怀提示
  - 逾期事件 → 画像自进化反馈（on_task_event）
  - 任务状态更新后检测触发器

注册名:
  app.jobs.overdue_check.check_overdue_tasks
  app.jobs.overdue_check.expire_suggestions
"""
import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update

from app.jobs.celery_app import celery_app
from app.database import async_session_factory
from app.models.task import Task, SubTask
from app.models.conversation import ChatTaskSuggestion
from app.services.notification_service import NotificationService

import structlog
logger = structlog.get_logger()

SPIRIT_NAMES = {
    "light": "光精灵", "water": "水精灵", "soil": "土壤精灵",
    "air": "空气精灵", "nutrition": "营养精灵",
}
SPIRIT_EMOJIS = {
    "light": "💡", "water": "💧", "soil": "🌱",
    "air": "💨", "nutrition": "✨",
}


@celery_app.task(name="app.jobs.overdue_check.check_overdue_tasks")
def check_overdue_tasks():
    """每晚 22:00 检查今天未完成的已排定任务"""
    asyncio.run(_check_overdue())


async def _check_overdue():
    async with async_session_factory() as session:
        now = datetime.now(timezone.utc)

        # 找出逾期的子任务（带关联任务信息）
        result = await session.execute(
            select(SubTask, Task).join(
                Task, SubTask.task_id == Task.id
            ).where(
                SubTask.scheduled_end < now,
                SubTask.status.in_(["pending", "scheduled"]),
            )
        )
        overdue_pairs = result.all()

        if not overdue_pairs:
            logger.info("overdue_check_completed", overdue_count=0)
            return

        # 按用户分组处理
        by_user: dict[str, list] = {}
        for subtask, task in overdue_pairs:
            uid = str(task.user_id)
            by_user.setdefault(uid, []).append((subtask, task))

        overdue_total = 0
        notified = 0

        for uid_str, pairs in by_user.items():
            try:
                # 批量更新状态
                for subtask, task in pairs:
                    subtask.status = "overdue"
                    overdue_total += 1

                await session.flush()

                # Sprint D: 画像自进化 — 记录逾期事件
                try:
                    from app.services.profile_evolution import ProfileEvolutionService
                    import uuid as uuid_module
                    uid = uuid_module.UUID(uid_str)
                    evo_svc = ProfileEvolutionService(session)
                    for subtask, task in pairs:
                        await evo_svc.on_task_event(
                            user_id=uid,
                            event_type="task_rescheduled",  # 逾期视为需要延期
                            spirit_code=task.primary_spirit,
                            task_id=task.id,
                        )
                except Exception as evo_err:
                    logger.warning("overdue_evolution_error", error=str(evo_err))

                # Sprint D: 发送通知
                try:
                    import uuid as uuid_module
                    uid = uuid_module.UUID(uid_str)
                    notif_svc = NotificationService(session)

                    # 按精灵分组统计逾期数
                    spirit_counts: dict[str, int] = {}
                    for subtask, task in pairs:
                        spirit = task.primary_spirit
                        spirit_counts[spirit] = spirit_counts.get(spirit, 0) + 1

                    # 找主要逾期精灵
                    main_spirit = max(spirit_counts, key=spirit_counts.get) if spirit_counts else "light"
                    emoji = SPIRIT_EMOJIS.get(main_spirit, "📋")
                    name = SPIRIT_NAMES.get(main_spirit, "精灵")
                    total_overdue = sum(spirit_counts.values())

                    # 精灵关怀语
                    if total_overdue == 1:
                        body = f"{name}有 1 个任务还没完成，明天补上也不迟~"
                    elif total_overdue <= 3:
                        body = f"有 {total_overdue} 个任务逾期了，{name}建议你重新安排一下时间"
                    else:
                        body = f"有 {total_overdue} 个任务逾期，最近是不是太忙了？建议和精灵们聊聊重新规划"

                    await notif_svc.create_notification(
                        user_id=uid,
                        type="task_reminder",
                        title=f"{emoji} 有任务需要关注",
                        body=body,
                        data={
                            "overdue_count": total_overdue,
                            "main_spirit": main_spirit,
                            "spirit_counts": spirit_counts,
                        },
                        push=True,
                    )
                    notified += 1
                except Exception as notif_err:
                    logger.warning("overdue_notification_error", error=str(notif_err))

                await session.commit()

            except Exception as e:
                await session.rollback()
                logger.error("overdue_user_error", user_id=uid_str, error=str(e))

        logger.info(
            "overdue_check_completed",
            overdue_count=overdue_total,
            users_notified=notified,
        )


@celery_app.task(name="app.jobs.overdue_check.expire_suggestions")
def expire_suggestions():
    """每天凌晨 3:00 清理超过 24h 未处理的对话任务建议"""
    asyncio.run(_expire())


async def _expire():
    async with async_session_factory() as session:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        result = await session.execute(
            update(ChatTaskSuggestion)
            .where(
                ChatTaskSuggestion.status == "pending",
                ChatTaskSuggestion.created_at < cutoff,
            )
            .values(status="expired", resolved_at=datetime.now(timezone.utc))
        )
        await session.commit()
        logger.info("suggestions_expired", count=result.rowcount)