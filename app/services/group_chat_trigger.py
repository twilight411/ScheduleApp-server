"""
群聊触发器 (Sprint C) — 自动检测需要发起多精灵协商的场景

8 种触发场景（来自 periph.txt #6）:
  1. 时间冲突型    — 同一时段有 ≥2 个精灵的任务竞争
  2. 结构性失衡型  — 连续 N 天某精灵占比过高 / 某精灵完全缺席
  3. 趋势性失衡型  — 某精灵得分连续下降 ≥2 周
  4. 情绪型协商    — 用户频繁延期/取消同一精灵的任务
  5. 资源争抢型    — 高优先级任务突然涌入，需重新分配
  6. Deadline 临近型 — 多个任务同时逼近 deadline
  7. 健康守护型    — 连续 N 天无运动或严重缺乏休息
  8. 用户主动触发  — 用户在对话中表达纠结/选择困难

调用方式:
  trigger = GroupChatTrigger(db)
  result = await trigger.check_all(user_id)
  if result.should_trigger:
      # 启动协商引擎 ...

集成点:
  - task_service.create_task()     — 创建任务后检测
  - schedule_service 日程调整后    — 调整日程后检测
  - weekly_scoring job             — 每周打分后检测趋势
  - 精灵对话 chat()               — 检测情绪型触发
"""
import uuid
from datetime import date, datetime, timedelta, timezone
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.task import Task, SubTask, TaskEvent
from app.models.schedule import Schedule
from app.models.score import SpiritWeeklyScore
from app.services.profile_service import ProfileService

import structlog

logger = structlog.get_logger()

SPIRIT_CODES = ["light", "water", "soil", "air", "nutrition"]
SPIRIT_NAMES = {
    "light": "光精灵", "water": "水精灵", "soil": "土壤精灵",
    "air": "空气精灵", "nutrition": "营养精灵",
}


# ====================================================================
#  触发结果数据结构
# ====================================================================

@dataclass
class TriggerResult:
    """单个触发器的检测结果"""
    triggered: bool = False
    trigger_type: str = ""
    severity: str = "low"       # low / medium / high
    reason: str = ""
    involved_spirits: list = field(default_factory=list)
    involved_task_ids: list = field(default_factory=list)
    context: dict = field(default_factory=dict)


@dataclass
class TriggerCheckResult:
    """所有触发器的汇总结果"""
    should_trigger: bool = False
    triggers: list[TriggerResult] = field(default_factory=list)
    highest_severity: str = "low"
    primary_reason: str = ""
    involved_spirits: list = field(default_factory=list)
    suggested_task_ids: list = field(default_factory=list)

    def add(self, result: TriggerResult):
        if result.triggered:
            self.triggers.append(result)
            self.should_trigger = True
            # 更新最高严重级
            severity_rank = {"low": 0, "medium": 1, "high": 2}
            if severity_rank.get(result.severity, 0) > severity_rank.get(self.highest_severity, 0):
                self.highest_severity = result.severity
                self.primary_reason = result.reason
            # 合并精灵和任务 ID
            for s in result.involved_spirits:
                if s not in self.involved_spirits:
                    self.involved_spirits.append(s)
            self.suggested_task_ids.extend(result.involved_task_ids)


# ====================================================================
#  触发器主类
# ====================================================================

class GroupChatTrigger:
    """群聊触发器 — 检测是否需要发起精灵协商"""

    def __init__(self, db: AsyncSession):
        self.db = db
        self.profile_svc = ProfileService(db)

    # ========================================
    #  统一入口
    # ========================================

    async def check_all(
        self,
        user_id: uuid.UUID,
        context: dict = None,
    ) -> TriggerCheckResult:
        """
        运行所有触发器检测。

        context 可选字段:
          - new_task_id: 刚创建的任务 ID（用于聚焦检测）
          - target_date: 目标日期
          - event_type:  触发来源 (task_created / schedule_adjusted / weekly_score / chat)
          - user_message: 用户对话内容（用于情绪型检测）
        """
        ctx = context or {}
        result = TriggerCheckResult()

        target_date = ctx.get("target_date", date.today())

        # 获取用户冲突处理偏好
        profile = await self.profile_svc.get_profile(user_id)
        conflict_strategy = "ask"
        if profile and profile.preferences:
            conflict_strategy = profile.preferences.get("conflict_strategy", "ask")

        # auto_defer 模式下不触发协商，直接系统处理
        if conflict_strategy == "auto_defer":
            logger.debug("trigger_skipped_auto_defer", user_id=str(user_id))
            return result

        # 依次检测 8 种场景
        result.add(await self._check_time_conflict(user_id, target_date, ctx))
        result.add(await self._check_structural_imbalance(user_id, target_date))
        result.add(await self._check_trend_decline(user_id))
        result.add(await self._check_emotional_avoidance(user_id))
        result.add(await self._check_resource_surge(user_id, ctx))
        result.add(await self._check_deadline_cluster(user_id))
        result.add(await self._check_health_neglect(user_id, target_date))
        result.add(self._check_user_hesitation(ctx))

        if result.should_trigger:
            logger.info(
                "group_chat_triggered",
                user_id=str(user_id),
                trigger_count=len(result.triggers),
                severity=result.highest_severity,
                types=[t.trigger_type for t in result.triggers],
            )

        return result

    async def check_on_task_created(
        self, user_id: uuid.UUID, task_id: uuid.UUID, target_date: date = None
    ) -> TriggerCheckResult:
        """创建任务后的快速检测（只检测相关子集）"""
        ctx = {
            "new_task_id": str(task_id),
            "target_date": target_date or date.today(),
            "event_type": "task_created",
        }
        result = TriggerCheckResult()
        result.add(await self._check_time_conflict(user_id, ctx["target_date"], ctx))
        result.add(await self._check_resource_surge(user_id, ctx))
        result.add(await self._check_deadline_cluster(user_id))
        return result

    async def check_on_schedule_change(
        self, user_id: uuid.UUID, target_date: date
    ) -> TriggerCheckResult:
        """日程调整后的快速检测"""
        ctx = {"target_date": target_date, "event_type": "schedule_adjusted"}
        result = TriggerCheckResult()
        result.add(await self._check_time_conflict(user_id, target_date, ctx))
        result.add(await self._check_structural_imbalance(user_id, target_date))
        return result

    # ========================================
    #  1. 时间冲突型
    # ========================================

    async def _check_time_conflict(
        self, user_id: uuid.UUID, target_date: date, ctx: dict
    ) -> TriggerResult:
        """检测同一时段是否有多精灵任务竞争"""
        # 查看目标日期前后 3 天的日程
        start = target_date
        end = target_date + timedelta(days=2)

        result_db = await self.db.execute(
            select(Schedule).where(
                Schedule.user_id == user_id,
                Schedule.date >= start,
                Schedule.date <= end,
            )
        )
        schedules = list(result_db.scalars().all())

        conflicts = []
        for sched in schedules:
            items = sched.items or []
            # 检测时间重叠
            for i in range(len(items)):
                for j in range(i + 1, len(items)):
                    a_start = items[i].get("time_start", "")
                    a_end = items[i].get("time_end", "")
                    b_start = items[j].get("time_start", "")
                    b_end = items[j].get("time_end", "")

                    if _times_overlap(a_start, a_end, b_start, b_end):
                        spirits = {items[i].get("spirit", ""), items[j].get("spirit", "")}
                        if len(spirits) >= 2:
                            conflicts.append({
                                "date": str(sched.date),
                                "items": [items[i].get("title"), items[j].get("title")],
                                "spirits": list(spirits),
                            })

        if conflicts:
            all_spirits = list({s for c in conflicts for s in c["spirits"]})
            return TriggerResult(
                triggered=True,
                trigger_type="time_conflict",
                severity="high",
                reason=f"发现 {len(conflicts)} 处时间冲突，涉及 {', '.join(SPIRIT_NAMES.get(s, s) for s in all_spirits)}",
                involved_spirits=all_spirits,
                context={"conflicts": conflicts[:5]},
            )

        return TriggerResult()

    # ========================================
    #  2. 结构性失衡型
    # ========================================

    async def _check_structural_imbalance(
        self, user_id: uuid.UUID, target_date: date
    ) -> TriggerResult:
        """检测连续多天某精灵占比过高或某精灵完全缺席"""
        start = target_date - timedelta(days=2)
        end = target_date + timedelta(days=2)

        result_db = await self.db.execute(
            select(Schedule).where(
                Schedule.user_id == user_id,
                Schedule.date >= start,
                Schedule.date <= end,
            )
        )

        # 统计每天各精灵占比
        spirit_total_minutes: dict[str, int] = {s: 0 for s in SPIRIT_CODES}
        total_minutes = 0
        absent_spirits = set(SPIRIT_CODES)

        for sched in result_db.scalars().all():
            for item in (sched.items or []):
                spirit = item.get("spirit", "light")
                duration = _estimate_item_duration(item)
                spirit_total_minutes[spirit] = spirit_total_minutes.get(spirit, 0) + duration
                total_minutes += duration
                absent_spirits.discard(spirit)

        if total_minutes < 60:
            # 日程太少，不做判断
            return TriggerResult()

        # 检测占比失衡（单精灵超过 60%）
        imbalanced = []
        for spirit, mins in spirit_total_minutes.items():
            ratio = mins / total_minutes if total_minutes > 0 else 0
            if ratio > 0.60:
                imbalanced.append((spirit, ratio))

        # 检测完全缺席（3 天以上）
        if absent_spirits and total_minutes > 120:
            absent_names = [SPIRIT_NAMES.get(s, s) for s in absent_spirits]
            return TriggerResult(
                triggered=True,
                trigger_type="structural_imbalance",
                severity="medium",
                reason=f"{', '.join(absent_names)} 最近完全没有安排任务",
                involved_spirits=list(absent_spirits),
            )

        if imbalanced:
            spirit, ratio = imbalanced[0]
            name = SPIRIT_NAMES.get(spirit, spirit)
            return TriggerResult(
                triggered=True,
                trigger_type="structural_imbalance",
                severity="medium",
                reason=f"{name}占比达 {ratio:.0%}，生活节奏失衡",
                involved_spirits=[spirit],
            )

        return TriggerResult()

    # ========================================
    #  3. 趋势性失衡型
    # ========================================

    async def _check_trend_decline(self, user_id: uuid.UUID) -> TriggerResult:
        """检测某精灵得分连续下降 ≥2 周"""
        three_weeks_ago = date.today() - timedelta(weeks=3)

        result_db = await self.db.execute(
            select(SpiritWeeklyScore).where(
                SpiritWeeklyScore.user_id == user_id,
                SpiritWeeklyScore.week_start >= three_weeks_ago,
            ).order_by(SpiritWeeklyScore.week_start)
        )
        scores = list(result_db.scalars().all())

        # 按精灵分组，检测连续下降
        by_spirit: dict[str, list[float]] = {}
        for s in scores:
            by_spirit.setdefault(s.spirit_code, []).append(s.score)

        declining = []
        for spirit, score_list in by_spirit.items():
            if len(score_list) >= 3:
                # 连续下降：每周都比上周低
                is_declining = all(
                    score_list[i] > score_list[i + 1]
                    for i in range(len(score_list) - 1)
                )
                if is_declining:
                    drop = score_list[0] - score_list[-1]
                    declining.append((spirit, drop))

        if declining:
            worst_spirit, drop = max(declining, key=lambda x: x[1])
            name = SPIRIT_NAMES.get(worst_spirit, worst_spirit)
            return TriggerResult(
                triggered=True,
                trigger_type="trend_decline",
                severity="medium" if drop < 20 else "high",
                reason=f"{name}得分连续下降，已累计下降 {drop:.0f} 分",
                involved_spirits=[worst_spirit],
                context={"drop": drop},
            )

        return TriggerResult()

    # ========================================
    #  4. 情绪型协商 — 频繁延期/取消
    # ========================================

    async def _check_emotional_avoidance(self, user_id: uuid.UUID) -> TriggerResult:
        """检测用户是否频繁延期/取消同一精灵的任务"""
        week_ago = datetime.now(timezone.utc) - timedelta(days=7)

        result_db = await self.db.execute(
            select(TaskEvent).where(
                TaskEvent.user_id == user_id,
                TaskEvent.created_at >= week_ago,
                TaskEvent.event_type.in_(["task_cancelled", "task_rescheduled", "subtask_cancelled"]),
            )
        )
        events = list(result_db.scalars().all())

        if len(events) < 3:
            return TriggerResult()

        # 统计每个精灵被取消/延期的次数
        spirit_avoidance: dict[str, int] = {}
        for evt in events:
            detail = evt.detail or {}
            spirit = detail.get("spirit", "")
            if spirit:
                spirit_avoidance[spirit] = spirit_avoidance.get(spirit, 0) + 1

        # 某精灵被取消/延期 ≥3 次
        for spirit, count in spirit_avoidance.items():
            if count >= 3:
                name = SPIRIT_NAMES.get(spirit, spirit)
                return TriggerResult(
                    triggered=True,
                    trigger_type="emotional_avoidance",
                    severity="medium",
                    reason=f"最近一周{name}相关任务被取消/延期 {count} 次，可能存在回避",
                    involved_spirits=[spirit],
                    context={"avoidance_count": count},
                )

        return TriggerResult()

    # ========================================
    #  5. 资源争抢型 — 高优任务突然涌入
    # ========================================

    async def _check_resource_surge(
        self, user_id: uuid.UUID, ctx: dict
    ) -> TriggerResult:
        """检测是否有大量高优先级任务同时存在"""
        result_db = await self.db.execute(
            select(Task).where(
                Task.user_id == user_id,
                Task.status.in_(["pending", "in_progress"]),
                Task.priority == "high",
            )
        )
        high_tasks = list(result_db.scalars().all())

        if len(high_tasks) >= 4:
            spirits = list({t.primary_spirit for t in high_tasks})
            task_ids = [str(t.id) for t in high_tasks]
            return TriggerResult(
                triggered=True,
                trigger_type="resource_surge",
                severity="high",
                reason=f"当前有 {len(high_tasks)} 个高优先级任务同时待处理",
                involved_spirits=spirits,
                involved_task_ids=task_ids,
                context={"high_task_count": len(high_tasks)},
            )

        return TriggerResult()

    # ========================================
    #  6. Deadline 临近型
    # ========================================

    async def _check_deadline_cluster(self, user_id: uuid.UUID) -> TriggerResult:
        """检测多个任务的 deadline 集中在近 3 天内"""
        now = datetime.now(timezone.utc)
        soon = now + timedelta(days=3)

        result_db = await self.db.execute(
            select(Task).where(
                Task.user_id == user_id,
                Task.status.in_(["pending", "in_progress"]),
                Task.deadline != None,
                Task.deadline <= soon,
                Task.deadline >= now,
            )
        )
        urgent_tasks = list(result_db.scalars().all())

        if len(urgent_tasks) >= 3:
            spirits = list({t.primary_spirit for t in urgent_tasks})
            task_ids = [str(t.id) for t in urgent_tasks]
            return TriggerResult(
                triggered=True,
                trigger_type="deadline_cluster",
                severity="high",
                reason=f"{len(urgent_tasks)} 个任务的截止日期在未来 3 天内",
                involved_spirits=spirits,
                involved_task_ids=task_ids,
            )

        return TriggerResult()

    # ========================================
    #  7. 健康守护型
    # ========================================

    async def _check_health_neglect(
        self, user_id: uuid.UUID, target_date: date
    ) -> TriggerResult:
        """检测连续多天无运动或严重缺乏休息"""
        start = target_date - timedelta(days=3)

        result_db = await self.db.execute(
            select(Schedule).where(
                Schedule.user_id == user_id,
                Schedule.date >= start,
                Schedule.date <= target_date,
            )
        )

        days_no_exercise = 0
        days_no_rest = 0
        total_days = 0

        for sched in result_db.scalars().all():
            total_days += 1
            items = sched.items or []
            has_exercise = any(it.get("spirit") == "soil" for it in items)
            has_rest = any(it.get("spirit") == "water" for it in items)

            if not has_exercise:
                days_no_exercise += 1
            if not has_rest:
                days_no_rest += 1

        if total_days < 2:
            return TriggerResult()

        involved = []
        reasons = []

        if days_no_exercise >= 3:
            involved.append("soil")
            reasons.append(f"连续 {days_no_exercise} 天没有运动")

        if days_no_rest >= 3:
            involved.append("water")
            reasons.append(f"连续 {days_no_rest} 天没有安排休息")

        if involved:
            return TriggerResult(
                triggered=True,
                trigger_type="health_neglect",
                severity="medium",
                reason="；".join(reasons),
                involved_spirits=involved,
            )

        return TriggerResult()

    # ========================================
    #  8. 用户主动触发 — 对话中的纠结
    # ========================================

    @staticmethod
    def _check_user_hesitation(ctx: dict) -> TriggerResult:
        """
        从用户对话内容中检测选择困难/纠结情绪。
        关键词匹配（轻量级，不调 LLM）。
        """
        message = ctx.get("user_message", "")
        if not message:
            return TriggerResult()

        hesitation_keywords = [
            "纠结", "选择困难", "不知道该", "来不及", "太多了",
            "怎么安排", "冲突了", "时间不够", "顾不过来",
            "取舍", "放弃哪个", "都很重要", "优先级",
        ]

        matched = [kw for kw in hesitation_keywords if kw in message]
        if len(matched) >= 1:
            return TriggerResult(
                triggered=True,
                trigger_type="user_hesitation",
                severity="low",
                reason=f"用户表达了选择困难（匹配: {', '.join(matched)}）",
                context={"matched_keywords": matched},
            )

        return TriggerResult()


# ====================================================================
#  模块级辅助函数
# ====================================================================

def _times_overlap(start_a: str, end_a: str, start_b: str, end_b: str) -> bool:
    """检查两个 HH:MM 时间段是否重叠"""
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


def _estimate_item_duration(item: dict) -> int:
    """估算日程项时长（分钟）"""
    ts = item.get("time_start", "")
    te = item.get("time_end", "")
    if ts and te:
        try:
            sh, sm = map(int, ts.split(":"))
            eh, em = map(int, te.split(":"))
            return max(0, (eh * 60 + em) - (sh * 60 + sm))
        except (ValueError, IndexError):
            pass
    return 60  # 默认 1 小时