"""
日程服务 — 调度算法与数据库之间的桥梁
CRUD、日程生成、调整、冲突检测、手动日程管理
"""
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.schedule import Schedule
from app.models.task import Task, SubTask
from app.ai.scheduler import (
    SubTaskInput,
    ScheduledItem,
    TimeSlot,
    run_scheduling_pipeline,
    detect_conflicts as algo_detect_conflicts,
    check_health_rules,
    generate_available_slots,
)
from app.services.profile_service import ProfileService
from app.services.event_service import EventService

import structlog

logger = structlog.get_logger()


class ScheduleService:

    def __init__(self, db: AsyncSession):
        self.db = db
        self.profile_svc = ProfileService(db)
        self.event_svc = EventService(db)

    # ========================================
    #  查询
    # ========================================

    async def get_day_schedule(
        self, user_id: uuid.UUID, target_date: date
    ) -> Optional[dict]:
        """获取某天的日程"""
        result = await self.db.execute(
            select(Schedule).where(
                Schedule.user_id == user_id,
                Schedule.date == target_date,
            )
        )
        sched = result.scalar_one_or_none()
        if not sched:
            return None
        return {
            "date": str(sched.date),
            "items": sched.items or [],
            "version": sched.version,
        }

    async def get_week_schedule(
        self, user_id: uuid.UUID, week_start: date
    ) -> dict:
        """获取某周的日程（7天）"""
        week_end = week_start + timedelta(days=6)
        return await self.get_range_schedule(user_id, week_start, week_end)

    async def get_range_schedule(
        self, user_id: uuid.UUID, start: date, end: date
    ) -> dict:
        """获取日期范围内的日程"""
        result = await self.db.execute(
            select(Schedule).where(
                Schedule.user_id == user_id,
                Schedule.date >= start,
                Schedule.date <= end,
            ).order_by(Schedule.date)
        )
        schedules = list(result.scalars().all())

        days = {}
        for sched in schedules:
            days[str(sched.date)] = {
                "items": sched.items or [],
                "version": sched.version,
            }

        # 填充空天
        current = start
        while current <= end:
            key = str(current)
            if key not in days:
                days[key] = {"items": [], "version": 0}
            current += timedelta(days=1)

        return {
            "start": str(start),
            "end": str(end),
            "days": days,
        }

    # ========================================
    #  生成日程
    # ========================================

    async def generate_schedule(
        self,
        user_id: uuid.UUID,
        start_date: date,
        end_date: date,
        task_ids: list = None,
        include_recurring: bool = True,
        regenerate: bool = False,
    ) -> dict:
        """
        AI 生成日程：
        1. 获取待排子任务
        2. 获取用户画像
        3. 获取已有固定事件
        4. 运行调度算法
        5. 存入数据库
        """
        # 转换 task_ids 为 UUID 对象
        parsed_task_ids = None
        if task_ids:
            try:
                parsed_task_ids = [uuid.UUID(tid) for tid in task_ids]
            except (ValueError, AttributeError):
                raise ValueError("无效的 task_id 格式")

        # 1. 获取待排子任务
        subtask_inputs = await self._load_subtasks(
            user_id, parsed_task_ids, start_date, end_date
        )

        if not subtask_inputs:
            return {
                "schedule": {},
                "unscheduled": [],
                "conflicts": [],
                "warnings": [],
                "stats": {"total_scheduled": 0},
                "message": "没有待排的子任务",
            }

        # 2. 用户画像
        profile = await self.profile_svc.get_scheduling_params(user_id)

        # 3. 已有固定事件（如果不是重新生成）
        existing = []
        if not regenerate:
            existing = await self._load_existing_items(user_id, start_date, end_date)

        # 4. 运行调度
        result = run_scheduling_pipeline(
            subtasks=subtask_inputs,
            date_range=(start_date, end_date),
            user_profile=profile,
            existing_items=existing,
        )

        # 5. 存入数据库 + 更新子任务排定时间
        await self._save_schedule(user_id, result["scheduled"], start_date, end_date, regenerate)
        await self._update_subtask_times(result["scheduled"])

        # 6. 格式化输出
        return self._format_result(result, start_date, end_date)

    async def _load_subtasks(
        self,
        user_id: uuid.UUID,
        task_ids: list[uuid.UUID] = None,
        start_date: date = None,
        end_date: date = None,
    ) -> list[SubTaskInput]:
        """加载待排的子任务"""
        conditions = [
            Task.user_id == user_id,
            Task.status.in_(["pending", "in_progress"]),
        ]
        if task_ids:
            conditions.append(Task.id.in_(task_ids))

        result = await self.db.execute(
            select(Task)
            .where(and_(*conditions))
            .options(selectinload(Task.subtasks))
        )
        tasks = list(result.scalars().all())

        inputs = []
        for task in tasks:
            for st in task.subtasks:
                if st.status in ("completed", "cancelled"):
                    continue
                # 已有排定时间且是固定的，标记为 fixed
                is_fixed = st.is_fixed and st.scheduled_start and st.scheduled_end
                inputs.append(SubTaskInput(
                    id=str(st.id),
                    task_id=str(task.id),
                    title=st.title,
                    spirit=st.spirit,
                    duration_minutes=st.duration_minutes,
                    priority=st.priority if st.priority is not None else task.priority,
                    deadline=task.deadline,
                    suggested_time=st.suggested_time,
                    is_fixed=is_fixed,
                    fixed_start=st.scheduled_start if is_fixed else None,
                    fixed_end=st.scheduled_end if is_fixed else None,
                ))

        return inputs

    async def _load_existing_items(
        self, user_id: uuid.UUID, start: date, end: date
    ) -> list[ScheduledItem]:
        """加载已有的固定日程项（含手动项）"""
        result = await self.db.execute(
            select(Schedule).where(
                Schedule.user_id == user_id,
                Schedule.date >= start,
                Schedule.date <= end,
            )
        )
        existing = []
        for sched in result.scalars().all():
            for item in (sched.items or []):
                if item.get("is_fixed"):
                    try:
                        slot_start = datetime.fromisoformat(f"{sched.date}T{item['time_start']}")
                        slot_end = datetime.fromisoformat(f"{sched.date}T{item['time_end']}")
                        existing.append(ScheduledItem(
                            id=item["id"],
                            subtask_id=item.get("subtask_id", ""),
                            task_id=item.get("task_id", ""),
                            title=item.get("title", ""),
                            slot=TimeSlot(slot_start, slot_end),
                            spirit=item.get("spirit", "light"),
                            priority=item.get("priority", "medium"),
                            is_fixed=True,
                        ))
                    except (KeyError, ValueError):
                        continue
        return existing

    async def _save_schedule(
        self,
        user_id: uuid.UUID,
        items: list[ScheduledItem],
        start: date,
        end: date,
        regenerate: bool,
    ):
        """将排定结果存入数据库（按天分组）"""
        by_day: dict[date, list[dict]] = {}
        for item in items:
            d = item.slot.start.date()
            by_day.setdefault(d, []).append({
                "id": item.id,
                "subtask_id": item.subtask_id,
                "task_id": item.task_id,
                "title": item.title,
                "time_start": item.slot.start.strftime("%H:%M"),
                "time_end": item.slot.end.strftime("%H:%M"),
                "spirit": item.spirit,
                "priority": item.priority,
                "is_fixed": item.is_fixed,
                "is_recurring": item.is_recurring,
                "spirit_tip": item.spirit_tip,
                "status": "pending",
                "source": "ai",
                "note": None,
            })

        current = start
        while current <= end:
            day_items = by_day.get(current, [])
            # 按时间排序
            day_items.sort(key=lambda x: x["time_start"])

            # 查找已有记录
            result = await self.db.execute(
                select(Schedule).where(
                    Schedule.user_id == user_id,
                    Schedule.date == current,
                )
            )
            sched = result.scalar_one_or_none()

            if sched:
                if regenerate:
                    # 重新生成时保留手动项
                    manual_items = [
                        it for it in (sched.items or [])
                        if it.get("source") == "manual"
                    ]
                    merged = manual_items + day_items
                    merged.sort(key=lambda x: x["time_start"])
                    sched.items = merged
                else:
                    # 合并：保留已有项 + 新增项
                    existing_ids = {it.get("id") for it in (sched.items or [])}
                    merged = list(sched.items or [])
                    for new_item in day_items:
                        if new_item["id"] not in existing_ids:
                            merged.append(new_item)
                    merged.sort(key=lambda x: x["time_start"])
                    sched.items = merged
                sched.version += 1
                sched.updated_at = datetime.now(timezone.utc)
            else:
                if day_items:
                    sched = Schedule(
                        user_id=user_id,
                        date=current,
                        items=day_items,
                        version=1,
                    )
                    self.db.add(sched)

            current += timedelta(days=1)

        await self.db.flush()

    async def _update_subtask_times(self, items: list[ScheduledItem]):
        """更新子任务的排定时间"""
        for item in items:
            try:
                st_id = uuid.UUID(item.subtask_id)
            except (ValueError, AttributeError):
                continue
            result = await self.db.execute(
                select(SubTask).where(SubTask.id == st_id)
            )
            st = result.scalar_one_or_none()
            if st:
                st.scheduled_start = item.slot.start
                st.scheduled_end = item.slot.end
                if st.status == "pending":
                    st.status = "scheduled"
        await self.db.flush()

    def _format_result(self, result: dict, start: date, end: date) -> dict:
        """格式化调度结果为 API 响应"""
        # 按天分组
        schedule_by_day = {}
        for item in result["scheduled"]:
            d = str(item.slot.start.date())
            schedule_by_day.setdefault(d, []).append({
                "id": item.id,
                "subtask_id": item.subtask_id,
                "task_id": item.task_id,
                "title": item.title,
                "time_start": item.slot.start.strftime("%H:%M"),
                "time_end": item.slot.end.strftime("%H:%M"),
                "spirit": item.spirit,
                "priority": item.priority,
                "is_fixed": item.is_fixed,
                "spirit_tip": item.spirit_tip,
                "status": "pending",
                "source": "ai",
            })

        for d in schedule_by_day:
            schedule_by_day[d].sort(key=lambda x: x["time_start"])

        conflicts = [
            {"type": c.type.value, "description": c.description,
             "severity": c.severity, "suggestion": c.suggestion}
            for c in result["conflicts"]
        ]
        warnings = [
            {"type": w.type.value, "description": w.description,
             "severity": w.severity, "suggestion": w.suggestion}
            for w in result["health_warnings"]
        ]

        return {
            "schedule": schedule_by_day,
            "unscheduled": result["unscheduled"],
            "conflicts": conflicts,
            "warnings": warnings,
            "stats": result["stats"],
        }

    # ========================================
    #  调整
    # ========================================

    async def adjust_item(
        self,
        user_id: uuid.UUID,
        target_date: date,
        item_id: str,
        new_start: str,
        new_end: str,
        version: int,
    ) -> dict:
        """手动调整某个日程项的时间"""
        result = await self.db.execute(
            select(Schedule).where(
                Schedule.user_id == user_id,
                Schedule.date == target_date,
            )
        )
        sched = result.scalar_one_or_none()
        if not sched:
            raise ValueError("该日期没有日程")
        if sched.version != version:
            raise ValueError(f"版本冲突：当前版本 {sched.version}，请求版本 {version}")

        items = list(sched.items or [])
        found = False
        for item in items:
            if item.get("id") == item_id:
                item["time_start"] = new_start
                item["time_end"] = new_end
                found = True
                break

        if not found:
            raise ValueError("日程项不存在")

        items.sort(key=lambda x: x["time_start"])
        sched.items = items
        sched.version += 1
        sched.updated_at = datetime.now(timezone.utc)
        await self.db.flush()

        # 更新子任务排定时间
        for item in items:
            if item.get("id") == item_id:
                await self._sync_subtask_time(item, target_date)
                break

        return {
            "date": str(target_date),
            "items": items,
            "version": sched.version,
        }

    async def swap_items(
        self,
        user_id: uuid.UUID,
        target_date: date,
        item_id_1: str,
        item_id_2: str,
        version: int,
    ) -> dict:
        """交换两个日程项的时间"""
        result = await self.db.execute(
            select(Schedule).where(
                Schedule.user_id == user_id,
                Schedule.date == target_date,
            )
        )
        sched = result.scalar_one_or_none()
        if not sched:
            raise ValueError("该日期没有日程")
        if sched.version != version:
            raise ValueError(f"版本冲突")

        items = list(sched.items or [])
        idx1 = idx2 = None
        for i, item in enumerate(items):
            if item.get("id") == item_id_1:
                idx1 = i
            elif item.get("id") == item_id_2:
                idx2 = i

        if idx1 is None or idx2 is None:
            raise ValueError("日程项不存在")

        # 交换时间
        t1_start, t1_end = items[idx1]["time_start"], items[idx1]["time_end"]
        items[idx1]["time_start"] = items[idx2]["time_start"]
        items[idx1]["time_end"] = items[idx2]["time_end"]
        items[idx2]["time_start"] = t1_start
        items[idx2]["time_end"] = t1_end

        items.sort(key=lambda x: x["time_start"])
        sched.items = items
        sched.version += 1
        sched.updated_at = datetime.now(timezone.utc)
        await self.db.flush()

        # 同步子任务
        await self._sync_subtask_time(items[idx1], target_date)
        await self._sync_subtask_time(items[idx2], target_date)

        return {
            "date": str(target_date),
            "items": items,
            "version": sched.version,
        }

    # ========================================
    #  冲突检测
    # ========================================

    async def check_conflicts(
        self, user_id: uuid.UUID, start: date, end: date
    ) -> dict:
        """检查指定范围内的冲突（不修改数据）"""
        profile = await self.profile_svc.get_scheduling_params(user_id)
        items = await self._load_all_items(user_id, start, end)

        conflicts = algo_detect_conflicts(items)
        health = check_health_rules(items, profile)

        return {
            "conflicts": [
                {"type": c.type.value, "description": c.description,
                 "severity": c.severity, "suggestion": c.suggestion}
                for c in conflicts
            ],
            "health_warnings": [
                {"type": w.type.value, "description": w.description,
                 "severity": w.severity, "suggestion": w.suggestion}
                for w in health
            ],
            "total_issues": len(conflicts) + len(health),
        }

    async def suggest_slot(
        self, user_id: uuid.UUID, duration_minutes: int, spirit: str,
        target_date: date = None,
    ) -> list[dict]:
        """为新任务推荐可用时间槽"""
        profile = await self.profile_svc.get_scheduling_params(user_id)
        if not target_date:
            target_date = date.today()
        end_date = target_date + timedelta(days=3)

        existing = await self._load_all_items(user_id, target_date, end_date)
        available = generate_available_slots(
            (target_date, end_date), profile, existing
        )

        suggestions = []
        for day in sorted(available.keys()):
            for slot in available[day]:
                if slot.duration_minutes >= duration_minutes:
                    suggestions.append({
                        "date": str(day),
                        "time_start": slot.start.strftime("%H:%M"),
                        "time_end": (slot.start + timedelta(minutes=duration_minutes)).strftime("%H:%M"),
                        "available_minutes": slot.duration_minutes,
                    })
                    if len(suggestions) >= 5:
                        return suggestions

        return suggestions

    # ========================================
    #  手动日程管理
    # ========================================

    async def add_manual_item(
        self,
        user_id: uuid.UUID,
        target_date: date,
        title: str,
        time_start: str,
        time_end: str,
        spirit: str = None,
        note: str = None,
        is_fixed: bool = True,
    ) -> dict:
        """
        用户手动添加日程项。
        source='manual'，subtask_id=None，与 AI 项共存于 Schedule.items。
        手动项同样参与冲突检测、周报统计。
        """
        new_item = {
            "id": str(uuid.uuid4()),
            "subtask_id": None,
            "task_id": None,
            "title": title,
            "time_start": time_start,
            "time_end": time_end,
            "spirit": spirit or "light",
            "priority": "medium",
            "is_fixed": is_fixed,
            "is_recurring": False,
            "spirit_tip": None,
            "status": "pending",
            "source": "manual",
            "note": note,
        }

        result = await self.db.execute(
            select(Schedule).where(
                Schedule.user_id == user_id,
                Schedule.date == target_date,
            )
        )
        sched = result.scalar_one_or_none()

        if sched:
            items = list(sched.items or [])
            items.append(new_item)
            items.sort(key=lambda x: x["time_start"])
            sched.items = items
            sched.version += 1
            sched.updated_at = datetime.now(timezone.utc)
        else:
            sched = Schedule(
                user_id=user_id,
                date=target_date,
                items=[new_item],
                version=1,
            )
            self.db.add(sched)

        await self.db.flush()

        await self.event_svc.record(
            user_id=user_id,
            event_type="manual_schedule_add",
            entity_type="schedule",
            entity_id=sched.id,
            detail={
                "item_id": new_item["id"],
                "title": title,
                "date": str(target_date),
                "time": f"{time_start}-{time_end}",
            },
        )

        return {
            "date": str(target_date),
            "item": new_item,
            "items": sched.items,
            "version": sched.version,
        }

    async def update_item(
        self,
        user_id: uuid.UUID,
        target_date: date,
        item_id: str,
        updates: dict,
        version: int,
    ) -> dict:
        """
        编辑日程项（标题/时间/备注/精灵/优先级）。
        兼容 AI 项和手动项，均支持编辑。
        使用乐观锁防止并发冲突。
        """
        result = await self.db.execute(
            select(Schedule).where(
                Schedule.user_id == user_id,
                Schedule.date == target_date,
            )
        )
        sched = result.scalar_one_or_none()
        if not sched:
            raise ValueError("该日期没有日程")
        if sched.version != version:
            raise ValueError(f"版本冲突：当前版本 {sched.version}，请求版本 {version}")

        items = list(sched.items or [])
        found = False
        updated_item = None

        allowed_fields = {
            "title", "time_start", "time_end",
            "spirit", "note", "priority", "is_fixed",
        }

        for item in items:
            if item.get("id") == item_id:
                for key, value in updates.items():
                    if key in allowed_fields and value is not None:
                        item[key] = value
                found = True
                updated_item = item
                break

        if not found:
            raise ValueError("日程项不存在")

        items.sort(key=lambda x: x["time_start"])
        sched.items = items
        sched.version += 1
        sched.updated_at = datetime.now(timezone.utc)
        await self.db.flush()

        # AI 项改了时间 → 同步到子任务表
        if updated_item.get("subtask_id") and (
            "time_start" in updates or "time_end" in updates
        ):
            await self._sync_subtask_time(updated_item, target_date)

        return {
            "date": str(target_date),
            "item": updated_item,
            "items": items,
            "version": sched.version,
        }

    async def delete_item(
        self,
        user_id: uuid.UUID,
        target_date: date,
        item_id: str,
        version: int,
    ) -> dict:
        """
        删除单个日程项。
        - 手动项：直接删除
        - AI 项：删除并将关联子任务重置为 pending
        """
        result = await self.db.execute(
            select(Schedule).where(
                Schedule.user_id == user_id,
                Schedule.date == target_date,
            )
        )
        sched = result.scalar_one_or_none()
        if not sched:
            raise ValueError("该日期没有日程")
        if sched.version != version:
            raise ValueError(f"版本冲突：当前版本 {sched.version}，请求版本 {version}")

        items = list(sched.items or [])
        removed = None
        new_items = []
        for item in items:
            if item.get("id") == item_id:
                removed = item
            else:
                new_items.append(item)

        if not removed:
            raise ValueError("日程项不存在")

        sched.items = new_items
        sched.version += 1
        sched.updated_at = datetime.now(timezone.utc)
        await self.db.flush()

        # AI 项 → 重置关联子任务
        subtask_id = removed.get("subtask_id")
        if subtask_id:
            try:
                st_uuid = uuid.UUID(subtask_id)
                st_result = await self.db.execute(
                    select(SubTask).where(SubTask.id == st_uuid)
                )
                st = st_result.scalar_one_or_none()
                if st and st.status == "scheduled":
                    st.status = "pending"
                    st.scheduled_start = None
                    st.scheduled_end = None
                    await self.db.flush()
            except (ValueError, AttributeError):
                pass

        await self.event_svc.record(
            user_id=user_id,
            event_type="schedule_item_delete",
            entity_type="schedule",
            entity_id=sched.id,
            detail={
                "item_id": item_id,
                "title": removed.get("title", ""),
                "source": removed.get("source", "ai"),
            },
        )

        return {
            "date": str(target_date),
            "removed_item_id": item_id,
            "items": new_items,
            "version": sched.version,
        }

    # ========================================
    #  辅助
    # ========================================

    async def _load_all_items(
        self, user_id: uuid.UUID, start: date, end: date
    ) -> list[ScheduledItem]:
        """加载指定范围的所有日程项为 ScheduledItem"""
        result = await self.db.execute(
            select(Schedule).where(
                Schedule.user_id == user_id,
                Schedule.date >= start,
                Schedule.date <= end,
            )
        )
        items = []
        for sched in result.scalars().all():
            for it in (sched.items or []):
                try:
                    slot_start = datetime.fromisoformat(f"{sched.date}T{it['time_start']}")
                    slot_end = datetime.fromisoformat(f"{sched.date}T{it['time_end']}")
                    items.append(ScheduledItem(
                        id=it["id"],
                        subtask_id=it.get("subtask_id", ""),
                        task_id=it.get("task_id", ""),
                        title=it.get("title", ""),
                        slot=TimeSlot(slot_start, slot_end),
                        spirit=it.get("spirit", "light"),
                        priority=it.get("priority", "medium"),
                        is_fixed=it.get("is_fixed", False),
                    ))
                except (KeyError, ValueError):
                    continue
        return items

    async def _sync_subtask_time(self, item: dict, target_date: date):
        """同步日程调整到子任务表"""
        subtask_id = item.get("subtask_id")
        if not subtask_id:
            return
        try:
            st_uuid = uuid.UUID(subtask_id)
        except (ValueError, AttributeError):
            return
        result = await self.db.execute(
            select(SubTask).where(SubTask.id == st_uuid)
        )
        st = result.scalar_one_or_none()
        if st:
            st.scheduled_start = datetime.fromisoformat(f"{target_date}T{item['time_start']}")
            st.scheduled_end = datetime.fromisoformat(f"{target_date}T{item['time_end']}")
            await self.db.flush()

    # ========================================
    #  整天日程写入（Sprint B 新增）
    # ========================================

    async def save_day_schedule(
        self,
        user_id: uuid.UUID,
        target_date: date,
        items: list[dict],
    ):
        """
        直接保存某天的日程项列表（覆盖写入）。

        用于：
          - 协商引擎输出的日程写入
          - Chat-to-Task 创建的日程写入
          - 任何需要整体替换某天日程的场景

        与 _save_schedule 不同，此方法直接操作 dict 格式的 items，
        不需要 ScheduledItem 数据结构。
        """
        result = await self.db.execute(
            select(Schedule).where(
                Schedule.user_id == user_id,
                Schedule.date == target_date,
            )
        )
        sched = result.scalar_one_or_none()

        # 确保每个 item 有 id
        for item in items:
            if not item.get("id"):
                item["id"] = str(uuid.uuid4())

        # 按时间排序
        items.sort(key=lambda x: x.get("time_start", "99:99"))

        if sched:
            sched.items = items
            sched.version += 1
            sched.updated_at = datetime.now(timezone.utc)
        else:
            sched = Schedule(
                user_id=user_id,
                date=target_date,
                items=items,
                version=1,
            )
            self.db.add(sched)

        await self.db.flush()

        logger.info(
            "day_schedule_saved",
            user_id=str(user_id),
            date=str(target_date),
            item_count=len(items),
            version=sched.version,
        )

    async def merge_into_day_schedule(
        self,
        user_id: uuid.UUID,
        target_date: date,
        new_items: list[dict],
        replace_conflicts: bool = False,
    ) -> dict:
        """
        将新条目合并到某天的日程中。
        - replace_conflicts=False: 保留已有条目，仅追加不冲突的新条目
        - replace_conflicts=True:  新条目时间段冲突时替换旧条目

        返回合并后的完整日程。
        """
        existing = await self.get_day_schedule(user_id, target_date)
        current_items = existing.get("items", []) if existing else []

        if replace_conflicts:
            # 移除与新条目时间冲突的旧条目
            for new_item in new_items:
                ns = new_item.get("time_start", "")
                ne = new_item.get("time_end", "")
                current_items = [
                    it for it in current_items
                    if not _times_overlap(
                        it.get("time_start", ""), it.get("time_end", ""),
                        ns, ne,
                    )
                ]

        # 追加新条目
        existing_ids = {it.get("id") for it in current_items if it.get("id")}
        for item in new_items:
            if not item.get("id"):
                item["id"] = str(uuid.uuid4())
            if item["id"] not in existing_ids:
                current_items.append(item)

        # 排序并保存
        current_items.sort(key=lambda x: x.get("time_start", "99:99"))
        await self.save_day_schedule(user_id, target_date, current_items)

        return {
            "date": str(target_date),
            "items": current_items,
            "item_count": len(current_items),
        }


# ========================================
#  辅助函数
# ========================================

def _times_overlap(start_a: str, end_a: str, start_b: str, end_b: str) -> bool:
    """检查两个 HH:MM 格式的时间段是否重叠"""
    if not start_a or not end_a or not start_b or not end_b:
        return False
    try:
        a_s = int(start_a.replace(":", ""))
        a_e = int(end_a.replace(":", ""))
        b_s = int(start_b.replace(":", ""))
        b_e = int(end_b.replace(":", ""))
        return a_s < b_e and b_s < a_e
    except (ValueError, AttributeError):
        return False