"""
日程调度算法 (Module 3) — 时间槽生成、贪心分配、冲突检测、健康规则
纯算法层，不依赖数据库。
"""
import uuid
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta, time
from enum import Enum
from typing import Optional

import structlog

logger = structlog.get_logger()


# ====================================================================
#  数据结构
# ====================================================================

@dataclass
class TimeSlot:
    """一段可用的时间"""
    start: datetime
    end: datetime

    @property
    def duration_minutes(self) -> int:
        return int((self.end - self.start).total_seconds() / 60)

    def overlaps(self, other: "TimeSlot") -> bool:
        return self.start < other.end and other.start < self.end

    def contains(self, other: "TimeSlot") -> bool:
        return self.start <= other.start and self.end >= other.end

    def split_at(self, used_start: datetime, used_end: datetime) -> list["TimeSlot"]:
        """从当前时间槽中移除 [used_start, used_end]，返回剩余片段"""
        remaining = []
        if self.start < used_start:
            remaining.append(TimeSlot(self.start, used_start))
        if used_end < self.end:
            remaining.append(TimeSlot(used_end, self.end))
        return remaining


@dataclass
class SubTaskInput:
    """调度器的子任务输入"""
    id: str
    task_id: str
    title: str
    spirit: str
    duration_minutes: int
    priority: str = "medium"
    deadline: Optional[datetime] = None
    suggested_time: Optional[str] = None  # morning/afternoon/evening
    dependencies: list = field(default_factory=list)
    is_fixed: bool = False
    fixed_start: Optional[datetime] = None
    fixed_end: Optional[datetime] = None


@dataclass
class ScheduledItem:
    """排定结果"""
    id: str
    subtask_id: str
    task_id: str
    title: str
    slot: TimeSlot
    spirit: str
    priority: str
    is_fixed: bool = False
    is_recurring: bool = False
    spirit_tip: str = ""


class ConflictType(str, Enum):
    TIME_OVERLAP = "time_overlap"
    HEALTH_VIOLATION = "health_violation"
    DEADLINE_MISS = "deadline_miss"


@dataclass
class Conflict:
    type: ConflictType
    items: list  # involved ScheduledItem or SubTaskInput
    description: str
    severity: str = "medium"  # high / medium / low
    suggestion: str = ""


# ====================================================================
#  精灵-时间偏好
# ====================================================================

SPIRIT_TIME_PREF = {
    "light":     {"prefer": ["morning", "afternoon"], "avoid": ["evening"]},
    "water":     {"prefer": ["afternoon", "evening"], "avoid": ["morning"]},
    "soil":      {"prefer": ["morning", "afternoon"], "avoid": []},
    "air":       {"prefer": ["evening"],              "avoid": ["morning"]},
    "nutrition": {"prefer": ["afternoon", "evening"], "avoid": []},
}


# ====================================================================
#  时间槽生成
# ====================================================================

def generate_available_slots(
    date_range: tuple[date, date],
    user_profile: dict,
    blocked_items: list[ScheduledItem] = None,
) -> dict[date, list[TimeSlot]]:
    """
    根据用户作息生成每天的可用时间槽。
    Returns: {date: [TimeSlot, ...]}
    """
    wake_time = _parse_time(user_profile.get("wake_time", "07:00"))
    sleep_time = _parse_time(user_profile.get("sleep_time", "23:00"))
    meal_times = user_profile.get("meal_times", ["07:30", "12:00", "18:30"])
    meal_duration = 30

    blocked = blocked_items or []
    start_date, end_date = date_range

    result = {}
    current = start_date
    while current <= end_date:
        day_start = datetime.combine(current, wake_time)
        day_end = datetime.combine(current, sleep_time)

        slots = [TimeSlot(day_start, day_end)]

        # 排除用餐时间
        for mt_str in meal_times:
            mt = _parse_time(mt_str)
            meal_start = datetime.combine(current, mt)
            meal_end = meal_start + timedelta(minutes=meal_duration)
            slots = _subtract_from_slots(slots, meal_start, meal_end)

        # 排除已有固定事件
        for item in blocked:
            if item.slot.start.date() == current:
                slots = _subtract_from_slots(slots, item.slot.start, item.slot.end)

        # 过滤太短的时间槽
        slots = [s for s in slots if s.duration_minutes >= 15]

        result[current] = slots
        current += timedelta(days=1)

    return result


def _subtract_from_slots(
    slots: list[TimeSlot], remove_start: datetime, remove_end: datetime
) -> list[TimeSlot]:
    new_slots = []
    remove = TimeSlot(remove_start, remove_end)
    for slot in slots:
        if slot.overlaps(remove):
            new_slots.extend(slot.split_at(remove_start, remove_end))
        else:
            new_slots.append(slot)
    return new_slots


# ====================================================================
#  任务排序
# ====================================================================

def sort_subtasks(subtasks: list[SubTaskInput]) -> list[SubTaskInput]:
    priority_order = {"high": 0, "medium": 1, "low": 2}

    def sort_key(st: SubTaskInput):
        if st.is_fixed:
            return (-1, datetime.min, 0)
        return (
            priority_order.get(st.priority, 1),
            st.deadline or datetime.max,
            -st.duration_minutes,
        )

    return sorted(subtasks, key=sort_key)


# ====================================================================
#  贪心分配
# ====================================================================

def allocate_schedule(
    subtasks: list[SubTaskInput],
    available_slots: dict[date, list[TimeSlot]],
    user_profile: dict,
) -> tuple[list[ScheduledItem], list[dict]]:
    sorted_tasks = sort_subtasks(subtasks)
    scheduled = []
    unscheduled = []

    remaining_slots = {d: list(slots) for d, slots in available_slots.items()}

    for st in sorted_tasks:
        if st.is_fixed and st.fixed_start and st.fixed_end:
            item = ScheduledItem(
                id=str(uuid.uuid4()),
                subtask_id=st.id,
                task_id=st.task_id,
                title=st.title,
                slot=TimeSlot(st.fixed_start, st.fixed_end),
                spirit=st.spirit,
                priority=st.priority,
                is_fixed=True,
            )
            scheduled.append(item)
            _consume_slot(remaining_slots, st.fixed_start, st.fixed_end)
            continue

        best = _find_best_slot(st, remaining_slots, user_profile)
        if best:
            slot_start, slot_end = best
            item = ScheduledItem(
                id=str(uuid.uuid4()),
                subtask_id=st.id,
                task_id=st.task_id,
                title=st.title,
                slot=TimeSlot(slot_start, slot_end),
                spirit=st.spirit,
                priority=st.priority,
            )
            scheduled.append(item)
            _consume_slot(remaining_slots, slot_start, slot_end)
        else:
            unscheduled.append({
                "subtask_id": st.id,
                "task_id": st.task_id,
                "title": st.title,
                "reason": "没有足够的可用时间槽",
                "duration_needed": st.duration_minutes,
            })

    return scheduled, unscheduled


def _find_best_slot(
    subtask: SubTaskInput,
    remaining_slots: dict[date, list[TimeSlot]],
    user_profile: dict,
) -> Optional[tuple[datetime, datetime]]:
    """
    评分选最佳时间槽：
    0.35 高效时段 | 0.25 精灵偏好 | 0.25 deadline | 0.15 suggested_time
    """
    peak_hours = user_profile.get("peak_hours", ["09:00-11:00", "15:00-17:00"])
    candidates = []

    for day in sorted(remaining_slots.keys()):
        for slot in remaining_slots[day]:
            if slot.duration_minutes < subtask.duration_minutes:
                continue

            score = 0.0
            slot_hour = slot.start.hour
            tod = _get_time_of_day(slot_hour)

            if _in_peak_hours(slot.start.time(), peak_hours):
                score += 0.35

            spirit_pref = SPIRIT_TIME_PREF.get(subtask.spirit, {})
            if tod in spirit_pref.get("prefer", []):
                score += 0.25
            elif tod in spirit_pref.get("avoid", []):
                score -= 0.15

            if subtask.deadline:
                proposed_end = slot.start + timedelta(minutes=subtask.duration_minutes)
                if proposed_end <= subtask.deadline:
                    days_before = (subtask.deadline - proposed_end).days
                    score += max(0, 0.25 - days_before * 0.03)
                else:
                    score -= 0.5

            if subtask.suggested_time and tod == subtask.suggested_time:
                score += 0.15

            proposed_end = slot.start + timedelta(minutes=subtask.duration_minutes)
            candidates.append((slot.start, proposed_end, score))

    if not candidates:
        return None

    best = max(candidates, key=lambda x: x[2])
    return (best[0], best[1])


def _consume_slot(
    remaining_slots: dict[date, list[TimeSlot]],
    used_start: datetime,
    used_end: datetime,
):
    day = used_start.date()
    if day not in remaining_slots:
        return
    new_slots = _subtract_from_slots(remaining_slots[day], used_start, used_end)
    # 加10分钟缓冲
    padded_end = used_end + timedelta(minutes=10)
    new_slots = _subtract_from_slots(new_slots, used_end, padded_end)
    remaining_slots[day] = [s for s in new_slots if s.duration_minutes >= 15]


# ====================================================================
#  冲突检测
# ====================================================================

def detect_conflicts(items: list[ScheduledItem]) -> list[Conflict]:
    conflicts = []
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if items[i].slot.overlaps(items[j].slot):
                conflicts.append(Conflict(
                    type=ConflictType.TIME_OVERLAP,
                    items=[items[i], items[j]],
                    description=(
                        f"「{items[i].title}」({_fmt_time(items[i].slot)}) "
                        f"与「{items[j].title}」({_fmt_time(items[j].slot)}) 时间冲突"
                    ),
                    severity="high",
                    suggestion=f"建议将「{items[j].title}」移到其他时间段",
                ))
    return conflicts


def check_health_rules(
    items: list[ScheduledItem],
    user_profile: dict,
) -> list[Conflict]:
    conflicts = []
    max_continuous = user_profile.get("max_continuous_work_minutes", 120)
    exercise_target = user_profile.get("daily_exercise_target_minutes", 30)

    by_day: dict[date, list[ScheduledItem]] = {}
    for item in items:
        d = item.slot.start.date()
        by_day.setdefault(d, []).append(item)

    for day, day_items in by_day.items():
        day_items.sort(key=lambda x: x.slot.start)

        # 连续工作检查
        work_items = [it for it in day_items if it.spirit == "light"]
        for i in range(len(work_items) - 1):
            gap = (work_items[i + 1].slot.start - work_items[i].slot.end).total_seconds() / 60
            combined = work_items[i].slot.duration_minutes + work_items[i + 1].slot.duration_minutes
            if gap < 15 and combined > max_continuous:
                conflicts.append(Conflict(
                    type=ConflictType.HEALTH_VIOLATION,
                    items=[work_items[i], work_items[i + 1]],
                    description=f"{day} 连续工作 {combined} 分钟超过上限",
                    severity="medium",
                    suggestion="建议插入15分钟休息",
                ))

        # 每日运动检查
        exercise_min = sum(it.slot.duration_minutes for it in day_items if it.spirit == "soil")
        if exercise_min == 0:
            conflicts.append(Conflict(
                type=ConflictType.HEALTH_VIOLATION,
                items=[],
                description=f"{day} 没有安排运动",
                severity="low",
                suggestion=f"建议每天至少运动 {exercise_target} 分钟",
            ))

        # 每日总时长检查
        total_h = sum(it.slot.duration_minutes for it in day_items) / 60
        max_daily = user_profile.get("max_daily_work_hours", 10)
        if total_h > max_daily:
            conflicts.append(Conflict(
                type=ConflictType.HEALTH_VIOLATION,
                items=day_items,
                description=f"{day} 任务总时长 {total_h:.1f}h 超过 {max_daily}h",
                severity="high",
                suggestion="建议将部分任务移到其他天",
            ))

    return conflicts


# ====================================================================
#  完整流水线
# ====================================================================

def run_scheduling_pipeline(
    subtasks: list[SubTaskInput],
    date_range: tuple[date, date],
    user_profile: dict,
    existing_items: list[ScheduledItem] = None,
) -> dict:
    """
    完整调度流水线：生成时间槽 → 排序 → 分配 → 冲突检测 → 健康检查
    """
    existing = existing_items or []

    available = generate_available_slots(date_range, user_profile, existing)
    scheduled, unscheduled = allocate_schedule(subtasks, available, user_profile)

    all_items = existing + scheduled
    conflicts = detect_conflicts(all_items)
    health_warnings = check_health_rules(all_items, user_profile)

    by_spirit = {}
    total_hours = 0
    for item in scheduled:
        by_spirit[item.spirit] = by_spirit.get(item.spirit, 0) + 1
        total_hours += item.slot.duration_minutes / 60

    _attach_spirit_tips(scheduled)

    return {
        "scheduled": scheduled,
        "unscheduled": unscheduled,
        "conflicts": conflicts,
        "health_warnings": health_warnings,
        "stats": {
            "total_scheduled": len(scheduled),
            "total_unscheduled": len(unscheduled),
            "total_hours": round(total_hours, 1),
            "by_spirit": by_spirit,
            "has_conflicts": len(conflicts) > 0,
            "health_warning_count": len(health_warnings),
        },
    }


# ====================================================================
#  精灵提示
# ====================================================================

SPIRIT_TIPS = {
    "light": {"morning": "上午精力最好，全力冲刺！", "afternoon": "下午保持专注", "evening": "晚上适合简单任务"},
    "water": {"morning": "早起也要放松~", "afternoon": "午后小憩最好", "evening": "晚上是黄金放松时间"},
    "soil": {"morning": "晨练精力充沛！", "afternoon": "下午运动晚上睡得香", "evening": "晚间运动别太剧烈"},
    "air": {"morning": "早上见面好心情", "afternoon": "下午茶适合社交", "evening": "晚上聚会最有氛围！"},
    "nutrition": {"morning": "清晨灵感充沛", "afternoon": "午后创意时光", "evening": "安静夜晚沉浸爱好"},
}


def _attach_spirit_tips(items: list[ScheduledItem]):
    for item in items:
        if not item.spirit_tip:
            tod = _get_time_of_day(item.slot.start.hour)
            item.spirit_tip = SPIRIT_TIPS.get(item.spirit, {}).get(tod, "加油！")


# ====================================================================
#  辅助
# ====================================================================

def _parse_time(time_str: str) -> time:
    parts = time_str.split(":")
    return time(int(parts[0]), int(parts[1]))


def _in_peak_hours(t: time, peak_ranges: list[str]) -> bool:
    for pr in peak_ranges:
        parts = pr.split("-")
        if len(parts) == 2:
            s = _parse_time(parts[0])
            e = _parse_time(parts[1])
            if s <= t <= e:
                return True
    return False


def _get_time_of_day(hour: int) -> str:
    if 6 <= hour < 12:
        return "morning"
    elif 12 <= hour < 18:
        return "afternoon"
    return "evening"


def _fmt_time(slot: TimeSlot) -> str:
    return f"{slot.start.strftime('%H:%M')}-{slot.end.strftime('%H:%M')}"
