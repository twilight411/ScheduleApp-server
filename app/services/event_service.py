"""
事件服务 — 记录用户行为事件（任务操作流水）
为用户画像学习系统提供数据基础
"""
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.task import TaskEvent


class EventService:

    def __init__(self, db: AsyncSession):
        self.db = db

    async def record_event(
        self,
        user_id: uuid.UUID,
        event_type: str,
        event_data: dict,
    ) -> TaskEvent:
        """
        记录一个行为事件。

        event_type 枚举:
        - task_created / task_started / task_paused
        - task_completed / task_cancelled / task_rescheduled
        - subtask_completed / profile_updated / onboarding_completed
        - schedule_generated / negotiation_completed
        """
        event = TaskEvent(
            user_id=user_id,
            event_type=event_type,
            event_data=event_data,
        )
        self.db.add(event)
        await self.db.flush()
        return event

    async def record(
        self,
        user_id: uuid.UUID,
        event_type: str,
        entity_type: str = None,
        entity_id=None,
        detail: dict = None,
    ) -> TaskEvent:
        """
        兼容调用方式 — schedule_service 等模块使用此签名。
        将扩展参数合并到 event_data 后委托给 record_event。
        """
        event_data = {}
        if entity_type:
            event_data["entity_type"] = entity_type
        if entity_id:
            event_data["entity_id"] = str(entity_id)
        if detail:
            event_data.update(detail)
        return await self.record_event(user_id, event_type, event_data)

    async def get_user_events(
        self,
        user_id: uuid.UUID,
        event_type: str = None,
        days: int = 30,
        limit: int = 100,
    ) -> list[TaskEvent]:
        """获取用户的行为事件"""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        query = (
            select(TaskEvent)
            .where(TaskEvent.user_id == user_id, TaskEvent.created_at >= cutoff)
            .order_by(TaskEvent.created_at.desc())
            .limit(limit)
        )
        if event_type:
            query = query.where(TaskEvent.event_type == event_type)

        result = await self.db.execute(query)
        return list(result.scalars().all())

    async def cleanup_old_events(self, retention_days: int = 90) -> int:
        """清理过期事件（定时任务调用）"""
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        result = await self.db.execute(
            delete(TaskEvent).where(TaskEvent.created_at < cutoff)
        )
        await self.db.flush()
        return result.rowcount