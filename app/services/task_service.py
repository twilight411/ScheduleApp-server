"""
任务服务 — 完整的任务 CRUD + 状态机 + 拆解集成

Sprint C: 集成群聊触发器，创建任务后自动检测是否需要协商
"""
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.task import Task, SubTask, TaskEvent
from app.models.conversation import ChatTaskSuggestion
from app.services.event_service import EventService

import structlog
logger = structlog.get_logger()

# ===== 状态机定义 =====
VALID_TRANSITIONS = {
    "pending":     ["in_progress", "cancelled"],
    "in_progress": ["pending", "completed", "cancelled"],  # pending = pause
    "completed":   [],
    "cancelled":   ["pending"],  # 可以恢复
    "overdue":     ["in_progress", "completed", "cancelled"],
}

SUBTASK_TRANSITIONS = {
    "pending":      ["scheduled", "in_progress", "cancelled"],
    "scheduled":    ["in_progress", "cancelled"],
    "in_progress":  ["completed", "cancelled", "pending"],
    "completed":    [],
    "cancelled":    ["pending"],
    "overdue":      ["in_progress", "completed", "cancelled"],
}


class TaskService:

    def __init__(self, db: AsyncSession):
        self.db = db
        self.event_svc = EventService(db)

    # ========================================
    #  CRUD
    # ========================================

    async def create_task(
        self,
        user_id: uuid.UUID,
        parsed: dict,
        source: str = "parsed",
    ) -> Task:
        """
        从解析结果创建任务。
        parsed 是 TaskParser 输出的单个 task dict。
        """
        task = Task(
            user_id=user_id,
            raw_input=parsed.get("raw_fragment", ""),
            title=parsed["title"],
            primary_spirit=parsed["primary_spirit"],
            secondary_spirits=parsed.get("secondary_spirits", []),
            deadline=self._parse_deadline(parsed.get("deadline")),
            estimated_hours=parsed.get("estimated_hours"),
            priority=parsed.get("priority", "medium"),
            is_recurring=parsed.get("is_recurring", False),
            recurrence_pattern=parsed.get("recurrence_pattern"),
            status="pending",
            source=source,
        )
        self.db.add(task)
        await self.db.flush()

        await self.event_svc.record_event(user_id, "task_created", {
            "task_id": str(task.id),
            "title": task.title,
            "spirit": task.primary_spirit,
            "source": source,
        })

        return task

    async def check_trigger_after_create(
        self,
        user_id: uuid.UUID,
        task: Task,
    ) -> Optional[dict]:
        """
        Sprint C: 创建任务后检测群聊触发器。
        返回触发结果 dict 或 None。
        调用方可据此决定是否自动发起协商。
        """
        from app.services.group_chat_trigger import GroupChatTrigger

        try:
            trigger = GroupChatTrigger(self.db)
            result = await trigger.check_on_task_created(
                user_id=user_id,
                task_id=task.id,
                target_date=task.deadline.date() if task.deadline else None,
            )

            if result.should_trigger:
                logger.info(
                    "trigger_detected_after_task_create",
                    user_id=str(user_id),
                    task_id=str(task.id),
                    triggers=[t.trigger_type for t in result.triggers],
                )
                return {
                    "should_negotiate": True,
                    "severity": result.highest_severity,
                    "reason": result.primary_reason,
                    "involved_spirits": result.involved_spirits,
                    "trigger_types": [t.trigger_type for t in result.triggers],
                    "suggested_task_ids": result.suggested_task_ids,
                }
        except Exception as e:
            # 触发器检测失败不应阻塞任务创建
            logger.warning("trigger_check_failed", error=str(e))

        return None

    async def create_subtasks(
        self,
        task: Task,
        decomposed: dict,
    ) -> list[SubTask]:
        """
        从拆解结果创建子任务。
        decomposed 是 Spirit.decompose_task 的输出。
        """
        subtasks = []
        for i, st_data in enumerate(decomposed.get("subtasks", [])):
            st = SubTask(
                task_id=task.id,
                spirit=task.primary_spirit,
                title=st_data["title"],
                duration_minutes=st_data["duration_minutes"],
                suggested_time=st_data.get("suggested_time", "morning"),
                dependencies=st_data.get("dependencies", []),
                priority=st_data.get("priority", task.priority),
                spirit_tip=st_data.get("tips", ""),
                status="pending",
            )
            self.db.add(st)
            subtasks.append(st)

        await self.db.flush()
        return subtasks

    async def get_task(
        self, task_id: uuid.UUID, user_id: uuid.UUID
    ) -> Optional[Task]:
        """获取任务详情（含子任务）"""
        result = await self.db.execute(
            select(Task)
            .where(Task.id == task_id, Task.user_id == user_id)
            .options(selectinload(Task.subtasks))
        )
        return result.scalar_one_or_none()

    async def list_tasks(
        self,
        user_id: uuid.UUID,
        status: str = None,
        spirit: str = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[Task], int]:
        """分页获取任务列表"""
        conditions = [Task.user_id == user_id]
        if status:
            conditions.append(Task.status == status)
        if spirit:
            conditions.append(Task.primary_spirit == spirit)

        # 计数
        count_q = select(func.count(Task.id)).where(and_(*conditions))
        total = (await self.db.execute(count_q)).scalar() or 0

        # 查询
        query = (
            select(Task)
            .where(and_(*conditions))
            .options(selectinload(Task.subtasks))
            .order_by(Task.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        result = await self.db.execute(query)
        tasks = list(result.scalars().all())

        return tasks, total

    async def update_task(
        self, task: Task, updates: dict
    ) -> Task:
        """更新任务字段"""
        for field, value in updates.items():
            if hasattr(task, field) and value is not None:
                setattr(task, field, value)

        task.updated_at = datetime.now(timezone.utc)
        await self.db.flush()
        return task

    async def delete_task(
        self, task_id: uuid.UUID, user_id: uuid.UUID
    ) -> bool:
        """删除任务（级联删除子任务）"""
        task = await self.get_task(task_id, user_id)
        if not task:
            return False

        await self.event_svc.record_event(user_id, "task_deleted", {
            "task_id": str(task_id), "title": task.title,
        })

        await self.db.delete(task)
        await self.db.flush()
        return True

    # ========================================
    #  状态流转
    # ========================================

    async def start_task(self, task: Task) -> Task:
        """开始任务"""
        self._check_transition(task.status, "in_progress")
        task.status = "in_progress"
        task.updated_at = datetime.now(timezone.utc)
        await self.db.flush()

        await self.event_svc.record_event(task.user_id, "task_started", {
            "task_id": str(task.id),
        })
        return task

    async def pause_task(self, task: Task) -> Task:
        """暂停任务"""
        self._check_transition(task.status, "pending")
        task.status = "pending"
        task.updated_at = datetime.now(timezone.utc)
        await self.db.flush()

        await self.event_svc.record_event(task.user_id, "task_paused", {
            "task_id": str(task.id),
        })
        return task

    async def complete_task(
        self, task: Task, feedback: str = None
    ) -> Task:
        """完成任务"""
        self._check_transition(task.status, "completed")
        task.status = "completed"
        task.updated_at = datetime.now(timezone.utc)
        await self.db.flush()

        # 同时标记所有未完成子任务
        for st in task.subtasks:
            if st.status not in ("completed", "cancelled"):
                st.status = "completed"
                st.actual_end = datetime.now(timezone.utc)
                if feedback:
                    st.user_feedback = feedback

        await self.db.flush()

        await self.event_svc.record_event(task.user_id, "task_completed", {
            "task_id": str(task.id),
            "feedback": feedback,
        })
        return task

    async def cancel_task(
        self, task: Task, reason: str = None
    ) -> Task:
        """取消任务"""
        self._check_transition(task.status, "cancelled")
        task.status = "cancelled"
        task.updated_at = datetime.now(timezone.utc)
        await self.db.flush()

        for st in task.subtasks:
            if st.status not in ("completed", "cancelled"):
                st.status = "cancelled"

        await self.db.flush()

        await self.event_svc.record_event(task.user_id, "task_cancelled", {
            "task_id": str(task.id),
            "reason": reason,
        })
        return task

    async def reschedule_task(
        self,
        task: Task,
        new_start: datetime,
        new_end: datetime,
        reason: str = None,
    ) -> Task:
        """改期 — 更新所有子任务的排定时间"""
        task.updated_at = datetime.now(timezone.utc)

        # 简单策略：给第一个子任务设新时间
        if task.subtasks:
            first = sorted(task.subtasks, key=lambda s: s.created_at)[0]
            first.scheduled_start = new_start
            first.scheduled_end = new_end

        await self.db.flush()

        await self.event_svc.record_event(task.user_id, "task_rescheduled", {
            "task_id": str(task.id),
            "new_start": new_start.isoformat(),
            "new_end": new_end.isoformat(),
            "reason": reason,
        })
        return task

    async def batch_complete(
        self,
        user_id: uuid.UUID,
        task_ids: list[uuid.UUID],
        feedback: str = None,
    ) -> list[Task]:
        """批量完成任务"""
        completed = []
        for tid in task_ids:
            task = await self.get_task(tid, user_id)
            if task and task.status != "completed":
                try:
                    await self.complete_task(task, feedback)
                    completed.append(task)
                except ValueError:
                    continue
        return completed

    # ========================================
    #  从对话创建任务 (Chat-to-Task)
    # ========================================

    async def create_from_chat(
        self,
        user_id: uuid.UUID,
        suggestion_id: uuid.UUID,
        data: dict,
    ) -> tuple[Task, bool, list]:
        """
        从对话建议创建任务。
        返回 (task, has_conflict, conflicts)
        """
        # 获取建议记录
        result = await self.db.execute(
            select(ChatTaskSuggestion).where(
                ChatTaskSuggestion.id == suggestion_id,
                ChatTaskSuggestion.user_id == user_id,
                ChatTaskSuggestion.status == "pending",
            )
        )
        suggestion = result.scalar_one_or_none()
        if not suggestion:
            raise ValueError("建议不存在或已处理")

        # 创建任务
        task = Task(
            user_id=user_id,
            raw_input=suggestion.source_quote or data.get("title", ""),
            title=data["title"],
            primary_spirit=data["spirit"],
            priority=data.get("priority", "medium"),
            source="chat",
        )
        self.db.add(task)
        await self.db.flush()

        # 创建子任务
        st = SubTask(
            task_id=task.id,
            spirit=data["spirit"],
            title=data["title"],
            duration_minutes=data.get("duration_minutes", 60),
            scheduled_start=self._parse_datetime(data.get("date"), data.get("time_start")),
            scheduled_end=self._parse_datetime(data.get("date"), data.get("time_end")),
            status="scheduled",
            priority=data.get("priority", "medium"),
            is_fixed=True,
        )
        self.db.add(st)

        # 更新建议状态
        suggestion.status = "accepted"
        suggestion.task_id = task.id
        suggestion.resolved_at = datetime.now(timezone.utc)

        await self.db.flush()

        # TODO: Phase 3 实现冲突检测
        conflicts = []

        await self.event_svc.record_event(user_id, "task_created", {
            "task_id": str(task.id),
            "source": "chat",
            "suggestion_id": str(suggestion_id),
        })

        return task, len(conflicts) > 0, conflicts

    # ========================================
    #  辅助方法
    # ========================================

    @staticmethod
    def _check_transition(current: str, target: str):
        allowed = VALID_TRANSITIONS.get(current, [])
        if target not in allowed:
            raise ValueError(
                f"无法从 '{current}' 转换到 '{target}'。允许的转换: {allowed}"
            )

    @staticmethod
    def _parse_deadline(deadline_str) -> Optional[datetime]:
        if not deadline_str:
            return None
        if isinstance(deadline_str, datetime):
            return deadline_str
        try:
            return datetime.fromisoformat(str(deadline_str).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _parse_datetime(date_str: str = None, time_str: str = None) -> Optional[datetime]:
        if not date_str:
            return None
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            if time_str:
                h, m = time_str.split(":")
                dt = dt.replace(hour=int(h), minute=int(m))
            return dt
        except (ValueError, TypeError):
            return None