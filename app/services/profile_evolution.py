"""
用户画像自进化服务 (Sprint D) — periph.txt #11

两个核心机制:

1. 实时反馈环 (Real-time Feedback Loop)
   - 任务完成 → 该精灵 learned_delta +2
   - 任务取消 → 该精灵 learned_delta -1
   - 任务延期 → 该精灵 learned_delta -0.5
   - 连续完成（streak ≥3）→ 额外 +1 bonus
   - 连续取消（streak ≥3）→ 额外 -2 惩罚 + 触发关怀提示

2. 周期性归因分析 (Weekly Attribution Analysis)
   - 每周打分后自动运行
   - 分析哪些行为模式与得分变化相关
   - 更新用户画像偏好（peak_hours、buffer_minutes 等）
   - 调整精灵优先级建议

调用点:
  - task_service: 任务状态变更时 → on_task_event()
  - weekly_scoring job: 打分后 → run_weekly_attribution()

设计原则:
  - 渐进式调整: 单次变化不超过 ±3, 累计 delta 限制在 [-20, +20]
  - 保护锁定: is_locked=True 的精灵不受自动调整
  - 可解释: 每次调整都记录原因到 event_service
"""
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.profile import UserProfile, SpiritIntensity
from app.models.task import Task, SubTask, TaskEvent
from app.models.score import SpiritWeeklyScore
from app.services.event_service import EventService

import structlog

logger = structlog.get_logger()

SPIRIT_CODES = ["light", "water", "soil", "air", "nutrition"]

# ====================================================================
#  调整参数
# ====================================================================

# 实时反馈环参数
DELTA_ON_COMPLETE = 2.0       # 完成一个任务 +2
DELTA_ON_CANCEL = -1.0        # 取消一个任务 -1
DELTA_ON_RESCHEDULE = -0.5    # 延期一个任务 -0.5
STREAK_BONUS = 1.0            # 连续完成 ≥3 次，额外 +1
STREAK_PENALTY = -2.0         # 连续取消 ≥3 次，额外 -2

# 单次调整上限
MAX_SINGLE_ADJUSTMENT = 3.0

# learned_delta 累计上下限
LEARNED_DELTA_MIN = -20.0
LEARNED_DELTA_MAX = 20.0

# 周归因分析：画像调整阈值
ATTRIBUTION_MIN_TASKS = 5     # 至少有 5 个任务才做归因


class ProfileEvolutionService:
    """用户画像自进化服务"""

    def __init__(self, db: AsyncSession):
        self.db = db
        self.event_svc = EventService(db)

    # ========================================
    #  1. 实时反馈环
    # ========================================

    async def on_task_event(
        self,
        user_id: uuid.UUID,
        event_type: str,
        spirit_code: str,
        task_id: Optional[uuid.UUID] = None,
    ):
        """
        任务状态变更时调用。

        event_type:
          - task_completed: 完成
          - task_cancelled: 取消
          - task_rescheduled: 延期
          - subtask_completed: 子任务完成
          - subtask_cancelled: 子任务取消
        """
        if spirit_code not in SPIRIT_CODES:
            return

        # 确定调整值
        delta = self._calc_event_delta(event_type)
        if delta == 0:
            return

        # 检查连续行为（streak）
        streak_delta = await self._check_streak(user_id, spirit_code, event_type)
        total_delta = delta + streak_delta

        # 限幅
        total_delta = max(-MAX_SINGLE_ADJUSTMENT, min(MAX_SINGLE_ADJUSTMENT, total_delta))

        # 应用到 learned_delta
        applied = await self._apply_learned_delta(user_id, spirit_code, total_delta)

        if applied:
            await self.event_svc.record_event(user_id, "profile_evolution", {
                "type": "realtime_feedback",
                "spirit": spirit_code,
                "event_type": event_type,
                "delta": total_delta,
                "streak_delta": streak_delta,
                "task_id": str(task_id) if task_id else None,
            })

            logger.info(
                "profile_evolution_applied",
                user_id=str(user_id),
                spirit=spirit_code,
                event=event_type,
                delta=total_delta,
            )

    @staticmethod
    def _calc_event_delta(event_type: str) -> float:
        """根据事件类型计算基础 delta"""
        mapping = {
            "task_completed": DELTA_ON_COMPLETE,
            "subtask_completed": DELTA_ON_COMPLETE * 0.5,
            "task_cancelled": DELTA_ON_CANCEL,
            "subtask_cancelled": DELTA_ON_CANCEL * 0.5,
            "task_rescheduled": DELTA_ON_RESCHEDULE,
        }
        return mapping.get(event_type, 0)

    async def _check_streak(
        self, user_id: uuid.UUID, spirit_code: str, event_type: str
    ) -> float:
        """检查连续行为并返回额外 delta"""
        # 获取该精灵最近 5 次事件
        is_positive = event_type in ("task_completed", "subtask_completed")
        target_events = (
            ["task_completed", "subtask_completed"]
            if is_positive
            else ["task_cancelled", "subtask_cancelled"]
        )

        week_ago = datetime.now(timezone.utc) - timedelta(days=7)
        result = await self.db.execute(
            select(TaskEvent).where(
                TaskEvent.user_id == user_id,
                TaskEvent.event_type.in_(target_events),
                TaskEvent.created_at >= week_ago,
            ).order_by(TaskEvent.created_at.desc()).limit(5)
        )
        recent_events = list(result.scalars().all())

        # 从这些事件中筛选同精灵的
        same_spirit_count = 0
        for evt in recent_events:
            detail = evt.detail or {}
            if detail.get("spirit") == spirit_code:
                same_spirit_count += 1

        if same_spirit_count >= 3:
            if is_positive:
                return STREAK_BONUS
            else:
                return STREAK_PENALTY

        return 0

    async def _apply_learned_delta(
        self, user_id: uuid.UUID, spirit_code: str, delta: float
    ) -> bool:
        """将 delta 应用到 SpiritIntensity.learned_delta"""
        profile = await self._get_profile(user_id)
        if not profile:
            return False

        result = await self.db.execute(
            select(SpiritIntensity).where(
                SpiritIntensity.profile_id == profile.id,
                SpiritIntensity.spirit_code == spirit_code,
            )
        )
        si = result.scalar_one_or_none()
        if not si:
            return False

        # 锁定的精灵不调整
        if si.is_locked:
            logger.debug("intensity_locked_skip", spirit=spirit_code)
            return False

        # 应用并限幅
        new_delta = si.learned_delta + delta
        si.learned_delta = max(LEARNED_DELTA_MIN, min(LEARNED_DELTA_MAX, new_delta))
        si.updated_at = datetime.now(timezone.utc)
        await self.db.flush()
        return True

    # ========================================
    #  2. 周期性归因分析
    # ========================================

    async def run_weekly_attribution(
        self,
        user_id: uuid.UUID,
        week_start: date,
    ) -> dict:
        """
        每周打分后运行的归因分析。

        分析内容:
          1. 各精灵完成率 vs 得分变化 → 调整 learned_delta
          2. 高效时段准确率 → 建议调整 peak_hours
          3. 任务时长偏好 → 建议调整 chunk_style
          4. 精灵优先级 → 根据实际使用频率建议调整

        返回分析结果（可用于周报中的"行为洞察"部分）
        """
        week_end = week_start + timedelta(days=6)
        insights = {
            "week_start": str(week_start),
            "adjustments": [],
            "suggestions": [],
        }

        # 加载本周得分
        scores = await self._load_week_scores(user_id, week_start)
        if not scores:
            return insights

        # 加载本周任务数据
        task_stats = await self._load_week_task_stats(user_id, week_start, week_end)

        # --- 2a. 完成率归因 → learned_delta 微调 ---
        for spirit_code, stats in task_stats.items():
            score_obj = scores.get(spirit_code)
            if not score_obj:
                continue

            completion_rate = (
                stats["completed"] / stats["total"]
                if stats["total"] > 0 else 0
            )

            # 完成率高(≥80%) + 得分好(≥70) → 正向强化 +1
            if completion_rate >= 0.8 and score_obj.score >= 70:
                await self._apply_learned_delta(user_id, spirit_code, 1.0)
                insights["adjustments"].append({
                    "spirit": spirit_code,
                    "delta": 1.0,
                    "reason": f"完成率{completion_rate:.0%}且得分{score_obj.score}，正向强化",
                })

            # 完成率低(<40%) + 得分差(<50) → 降低期望 -1
            elif completion_rate < 0.4 and score_obj.score < 50 and stats["total"] >= 3:
                await self._apply_learned_delta(user_id, spirit_code, -1.0)
                insights["adjustments"].append({
                    "spirit": spirit_code,
                    "delta": -1.0,
                    "reason": f"完成率{completion_rate:.0%}且得分{score_obj.score}，降低期望",
                })

        # --- 2b. 高效时段分析 ---
        productive_hours = await self._analyze_productive_hours(
            user_id, week_start, week_end
        )
        if productive_hours:
            insights["suggestions"].append({
                "type": "peak_hours",
                "current_best": productive_hours,
                "suggestion": f"本周你在 {productive_hours} 效率最高，建议调整高效时段",
            })

        # --- 2c. 任务时长偏好分析 ---
        chunk_pref = self._analyze_chunk_preference(task_stats)
        if chunk_pref:
            insights["suggestions"].append({
                "type": "chunk_style",
                "detected": chunk_pref,
                "suggestion": f"你倾向于 {chunk_pref} 风格的任务块",
            })

        # --- 2d. 精灵使用频率 → 优先级建议 ---
        usage_rank = sorted(
            task_stats.items(),
            key=lambda x: x[1]["total"],
            reverse=True,
        )
        if len(usage_rank) >= 3:
            top_spirits = [code for code, _ in usage_rank[:3]]
            insights["suggestions"].append({
                "type": "spirit_priority",
                "usage_rank": top_spirits,
                "suggestion": f"本周最活跃的领域: {', '.join(top_spirits)}",
            })

        # 记录归因事件
        await self.event_svc.record_event(user_id, "profile_evolution", {
            "type": "weekly_attribution",
            "week_start": str(week_start),
            "adjustments": insights["adjustments"],
            "suggestion_count": len(insights["suggestions"]),
        })

        logger.info(
            "weekly_attribution_completed",
            user_id=str(user_id),
            week=str(week_start),
            adjustments=len(insights["adjustments"]),
            suggestions=len(insights["suggestions"]),
        )

        return insights

    # ========================================
    #  归因分析 — 内部方法
    # ========================================

    async def _load_week_scores(
        self, user_id: uuid.UUID, week_start: date
    ) -> dict[str, SpiritWeeklyScore]:
        """加载本周各精灵得分"""
        result = await self.db.execute(
            select(SpiritWeeklyScore).where(
                SpiritWeeklyScore.user_id == user_id,
                SpiritWeeklyScore.week_start == week_start,
            )
        )
        return {s.spirit_code: s for s in result.scalars().all()}

    async def _load_week_task_stats(
        self, user_id: uuid.UUID, start: date, end: date
    ) -> dict[str, dict]:
        """加载本周各精灵的任务统计"""
        start_dt = datetime.combine(start, datetime.min.time())
        end_dt = datetime.combine(end, datetime.max.time())

        result = await self.db.execute(
            select(Task).where(
                Task.user_id == user_id,
                Task.created_at >= start_dt,
                Task.created_at <= end_dt,
            )
        )
        tasks = list(result.scalars().all())

        # 补充：也包括本周内状态变更过的任务
        result2 = await self.db.execute(
            select(Task).where(
                Task.user_id == user_id,
                Task.updated_at >= start_dt,
                Task.updated_at <= end_dt,
                Task.status.in_(["completed", "cancelled"]),
            )
        )
        updated_tasks = list(result2.scalars().all())

        # 合并去重
        task_ids = {t.id for t in tasks}
        for t in updated_tasks:
            if t.id not in task_ids:
                tasks.append(t)

        stats: dict[str, dict] = {
            code: {"total": 0, "completed": 0, "cancelled": 0, "avg_duration": 0, "durations": []}
            for code in SPIRIT_CODES
        }

        for task in tasks:
            spirit = task.primary_spirit
            if spirit not in stats:
                continue
            stats[spirit]["total"] += 1
            if task.status == "completed":
                stats[spirit]["completed"] += 1
            elif task.status == "cancelled":
                stats[spirit]["cancelled"] += 1
            if task.estimated_hours:
                stats[spirit]["durations"].append(task.estimated_hours * 60)

        # 计算平均时长
        for code, s in stats.items():
            if s["durations"]:
                s["avg_duration"] = sum(s["durations"]) / len(s["durations"])

        return stats

    async def _analyze_productive_hours(
        self, user_id: uuid.UUID, start: date, end: date
    ) -> Optional[str]:
        """分析本周最高效的时段"""
        start_dt = datetime.combine(start, datetime.min.time())
        end_dt = datetime.combine(end, datetime.max.time())

        # 统计完成的子任务的时段分布
        result = await self.db.execute(
            select(SubTask).where(
                SubTask.status == "completed",
                SubTask.scheduled_start != None,
                SubTask.scheduled_start >= start_dt,
                SubTask.scheduled_start <= end_dt,
            )
        )
        subtasks = list(result.scalars().all())

        if len(subtasks) < ATTRIBUTION_MIN_TASKS:
            return None

        # 按小时统计完成数
        hour_counts: dict[int, int] = {}
        for st in subtasks:
            if st.scheduled_start:
                h = st.scheduled_start.hour
                hour_counts[h] = hour_counts.get(h, 0) + 1

        if not hour_counts:
            return None

        # 找出 top 2 小时
        sorted_hours = sorted(hour_counts.items(), key=lambda x: -x[1])
        top_hours = [h for h, _ in sorted_hours[:2]]
        top_hours.sort()

        return f"{top_hours[0]:02d}:00-{top_hours[-1]+1:02d}:00"

    @staticmethod
    def _analyze_chunk_preference(task_stats: dict) -> Optional[str]:
        """根据任务时长分布判断 chunk 偏好"""
        all_durations = []
        for stats in task_stats.values():
            all_durations.extend(stats.get("durations", []))

        if len(all_durations) < 3:
            return None

        avg = sum(all_durations) / len(all_durations)

        if avg <= 45:
            return "蚂蚁搬家 (ant)"
        elif avg <= 150:
            return "稳扎稳打 (balanced)"
        else:
            return "暴力通关 (sprint)"

    # ========================================
    #  3. 自动调整画像偏好（可选执行）
    # ========================================

    async def auto_adjust_preferences(
        self,
        user_id: uuid.UUID,
        insights: dict,
    ) -> list[str]:
        """
        根据归因分析的建议，自动微调画像偏好。
        只调整置信度高的参数，返回调整说明列表。
        """
        profile = await self._get_profile(user_id)
        if not profile:
            return []

        prefs = profile.preferences or {}
        changes = []

        for suggestion in insights.get("suggestions", []):
            stype = suggestion.get("type")

            if stype == "peak_hours" and suggestion.get("current_best"):
                # 只有当建议与当前设置差异较大时才调整
                current_peaks = prefs.get("peak_hours", [])
                best = suggestion["current_best"]
                if best not in " ".join(current_peaks):
                    # 不自动改，只记录建议
                    changes.append(f"建议将高效时段调整为包含 {best}")

            if stype == "chunk_style" and suggestion.get("detected"):
                detected = suggestion["detected"]
                current = prefs.get("chunk_style", "balanced")
                short_detected = detected.split("(")[1].rstrip(")") if "(" in detected else detected
                if short_detected != current:
                    changes.append(f"检测到偏好 {detected}，当前设置为 {current}")

        if changes:
            logger.info(
                "preference_adjustment_suggestions",
                user_id=str(user_id),
                suggestions=changes,
            )

        return changes

    # ========================================
    #  辅助
    # ========================================

    async def _get_profile(self, user_id: uuid.UUID) -> Optional[UserProfile]:
        result = await self.db.execute(
            select(UserProfile).where(UserProfile.user_id == user_id)
        )
        return result.scalar_one_or_none()