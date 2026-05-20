"""
精灵周打分服务 — Phase 5 核心

打分维度:
  1. design_score   (30%) — 任务设计合理度: 相对于精灵强度，安排的任务量是否合理
  2. completion_score(50%) — 完成度: 完成率 + 准时奖励 - 取消惩罚
  3. quality_score   (20%) — 质量: 用户反馈(easy/just_right/hard)

等级映射:
  90-100 → flourishing  (繁茂)
  70-89  → good         (良好)
  50-69  → average      (一般)
  30-49  → poor         (较差)
  0-29   → withered     (枯萎)
"""
import uuid
from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.score import SpiritWeeklyScore
from app.models.task import Task, SubTask
from app.services.intensity_service import IntensityService
from app.ai.spirits import get_spirit

import structlog

logger = structlog.get_logger()

SPIRIT_CODES = ["light", "water", "soil", "air", "nutrition"]

# 等级阈值
LEVEL_THRESHOLDS = [
    (90, "flourishing"),
    (70, "good"),
    (50, "average"),
    (30, "poor"),
    (0, "withered"),
]

# 用户反馈 → 质量分映射
FEEDBACK_SCORE_MAP = {
    "easy": 70,
    "just_right": 100,
    "hard": 85,
}


def _score_to_level(score: float) -> str:
    for threshold, level in LEVEL_THRESHOLDS:
        if score >= threshold:
            return level
    return "withered"


class ScoringService:

    def __init__(self, db: AsyncSession):
        self.db = db
        self.intensity_svc = IntensityService(db)

    # ========================================
    #  单精灵打分
    # ========================================

    async def calculate_spirit_score(
        self,
        user_id: uuid.UUID,
        spirit_code: str,
        week_start: date,
    ) -> SpiritWeeklyScore:
        """
        计算某个精灵在某周的得分。
        公式严格对照部署文档 §1.1。
        """
        week_end = week_start + timedelta(days=6)

        # 1. 获取有效强度
        intensity = await self.intensity_svc.get_effective_intensity(
            user_id, spirit_code
        )

        # 2. 获取该周该精灵的所有子任务
        subtasks = await self._get_week_subtasks(
            user_id, spirit_code, week_start, week_end
        )

        total_planned = len(subtasks)
        completed = [st for st in subtasks if st.status == "completed"]
        cancelled = [st for st in subtasks if st.status == "cancelled"]
        on_time = [
            st for st in completed
            if st.actual_end and st.scheduled_end and st.actual_end <= st.scheduled_end
        ]

        # 3. 维度一：设计合理度
        design_score = self._calc_design_score(intensity, total_planned)

        # 4. 维度二：完成度
        completion_score = self._calc_completion_score(
            total_planned, len(completed), len(on_time), len(cancelled)
        )

        # 5. 维度三：质量（用户反馈）
        quality_score = self._calc_quality_score(completed)

        # 6. 综合得分
        final_score = (
            completion_score * 0.50
            + design_score * 0.30
            + quality_score * 0.20
        )
        final_score = round(min(100, max(0, final_score)), 1)
        level = _score_to_level(final_score)

        # 7. 生成精灵点评
        details = {
            "design": round(design_score, 1),
            "completion": round(completion_score, 1),
            "quality": round(quality_score, 1),
            "total_planned": total_planned,
            "completed_count": len(completed),
            "cancelled_count": len(cancelled),
            "on_time_count": len(on_time),
            "intensity": intensity,
        }
        comment = await self._generate_comment(spirit_code, final_score, details)

        task_stats = {
            "planned": total_planned,
            "completed": len(completed),
            "cancelled": len(cancelled),
            "on_time": len(on_time),
        }

        score_record = SpiritWeeklyScore(
            user_id=user_id,
            spirit_code=spirit_code,
            week_start=week_start,
            score=final_score,
            design_score=round(design_score, 1),
            completion_score=round(completion_score, 1),
            quality_score=round(quality_score, 1),
            level=level,
            intensity_at_scoring=intensity,
            task_stats=task_stats,
            spirit_comment=comment,
        )

        return score_record

    # ========================================
    #  批量打分（一个用户5个精灵）
    # ========================================

    async def calculate_all_spirits(
        self,
        user_id: uuid.UUID,
        week_start: date,
    ) -> list[SpiritWeeklyScore]:
        """计算一个用户所有5个精灵的周得分"""
        scores = []
        for spirit_code in SPIRIT_CODES:
            # 检查是否已有记录（幂等性）
            existing = await self._get_existing_score(
                user_id, spirit_code, week_start
            )
            if existing:
                scores.append(existing)
                continue

            score = await self.calculate_spirit_score(
                user_id, spirit_code, week_start
            )
            self.db.add(score)
            scores.append(score)

        await self.db.flush()
        return scores

    # ========================================
    #  查询
    # ========================================

    async def get_week_scores(
        self, user_id: uuid.UUID, week_start: date
    ) -> list[SpiritWeeklyScore]:
        """获取某周的所有精灵得分"""
        result = await self.db.execute(
            select(SpiritWeeklyScore).where(
                SpiritWeeklyScore.user_id == user_id,
                SpiritWeeklyScore.week_start == week_start,
            ).order_by(SpiritWeeklyScore.spirit_code)
        )
        return list(result.scalars().all())

    async def get_last_week_scores(
        self, user_id: uuid.UUID, week_start: date
    ) -> list[SpiritWeeklyScore]:
        """获取上一周的得分（用于对比）"""
        last_week = week_start - timedelta(days=7)
        return await self.get_week_scores(user_id, last_week)

    async def get_spirit_score_history(
        self, user_id: uuid.UUID, spirit_code: str, weeks: int = 12
    ) -> list[SpiritWeeklyScore]:
        """获取某精灵的历史得分趋势"""
        cutoff = date.today() - timedelta(weeks=weeks)
        result = await self.db.execute(
            select(SpiritWeeklyScore).where(
                SpiritWeeklyScore.user_id == user_id,
                SpiritWeeklyScore.spirit_code == spirit_code,
                SpiritWeeklyScore.week_start >= cutoff,
            ).order_by(SpiritWeeklyScore.week_start)
        )
        return list(result.scalars().all())

    async def get_overall_score(
        self, user_id: uuid.UUID, week_start: date
    ) -> float:
        """计算加权总分（5精灵按强度加权）"""
        scores = await self.get_week_scores(user_id, week_start)
        if not scores:
            return 0

        total_weight = 0
        weighted_sum = 0
        for s in scores:
            weight = max(1, s.intensity_at_scoring)
            weighted_sum += s.score * weight
            total_weight += weight

        return round(weighted_sum / total_weight, 1) if total_weight > 0 else 0

    # ========================================
    #  打分子公式
    # ========================================

    @staticmethod
    def _calc_design_score(intensity: int, total_planned: int) -> float:
        """
        设计合理度：用户相对于精灵强度设定，安排了合理的任务量。
        强度 80 → 期望 ~6-8 个子任务/周
        强度 30 → 期望 ~2-3 个
        """
        expected_count = max(1, round(intensity / 100 * 8))

        if total_planned == 0:
            return 0.0

        ratio = total_planned / expected_count
        if ratio >= 0.7:
            return min(100.0, ratio * 100)
        return ratio * 100

    @staticmethod
    def _calc_completion_score(
        total: int, completed: int, on_time: int, cancelled: int
    ) -> float:
        """
        完成度 = 基础完成率 + 准时奖励 - 取消惩罚
        """
        if total == 0:
            return 0.0

        base_rate = completed / total
        on_time_bonus = (on_time / max(completed, 1)) * 0.15
        cancel_penalty = (cancelled / total) * 0.2

        raw = (base_rate + on_time_bonus - cancel_penalty) * 100
        return min(100.0, max(0.0, raw))

    @staticmethod
    def _calc_quality_score(completed_subtasks: list) -> float:
        """
        质量分：基于用户反馈。
        easy=70, just_right=100, hard=85
        无反馈默认 75。
        """
        feedbacks = [
            st.user_feedback for st in completed_subtasks
            if hasattr(st, "user_feedback") and st.user_feedback
        ]
        if not feedbacks:
            return 75.0

        total = sum(FEEDBACK_SCORE_MAP.get(f, 80) for f in feedbacks)
        return total / len(feedbacks)

    # ========================================
    #  精灵点评
    # ========================================

    async def _generate_comment(
        self, spirit_code: str, score: float, details: dict
    ) -> str:
        """调用精灵 Agent 生成个性化点评"""
        try:
            spirit = get_spirit(spirit_code)
            comment = await spirit.generate_comment(score, details)
            return comment
        except Exception as e:
            logger.warning("spirit_comment_fallback", spirit=spirit_code, error=str(e))
            # 降级：使用模板
            return self._fallback_comment(spirit_code, score, details)

    @staticmethod
    def _fallback_comment(spirit_code: str, score: float, details: dict) -> str:
        """LLM 不可用时的模板化点评"""
        planned = details.get("total_planned", 0)
        completed = details.get("completed_count", 0)

        if planned == 0:
            return "这周完全没有安排任务哦，下周记得给我派活！"
        if score >= 90:
            return f"太棒了！{completed}/{planned} 个任务完成，继续保持！"
        if score >= 70:
            return f"表现不错，{completed}/{planned} 完成。再接再厉！"
        if score >= 50:
            return f"中规中矩，{completed}/{planned} 完成。下周加油！"
        if score >= 30:
            return f"这周有点松懈，只完成了 {completed}/{planned}。需要更多关注。"
        return f"这周需要重点关注了，完成率太低。"

    # ========================================
    #  数据加载
    # ========================================

    async def _get_week_subtasks(
        self,
        user_id: uuid.UUID,
        spirit_code: str,
        week_start: date,
        week_end: date,
    ) -> list:
        """获取某精灵在某周的所有子任务"""
        week_start_dt = datetime.combine(week_start, datetime.min.time())
        week_end_dt = datetime.combine(week_end, datetime.max.time())

        result = await self.db.execute(
            select(SubTask).join(Task).where(
                and_(
                    Task.user_id == user_id,
                    SubTask.spirit == spirit_code,
                    SubTask.scheduled_start != None,
                    SubTask.scheduled_start >= week_start_dt,
                    SubTask.scheduled_start <= week_end_dt,
                )
            )
        )
        return list(result.scalars().all())

    async def _get_existing_score(
        self,
        user_id: uuid.UUID,
        spirit_code: str,
        week_start: date,
    ) -> Optional[SpiritWeeklyScore]:
        """检查是否已有打分记录（幂等性保护）"""
        result = await self.db.execute(
            select(SpiritWeeklyScore).where(
                SpiritWeeklyScore.user_id == user_id,
                SpiritWeeklyScore.spirit_code == spirit_code,
                SpiritWeeklyScore.week_start == week_start,
            )
        )
        return result.scalar_one_or_none()