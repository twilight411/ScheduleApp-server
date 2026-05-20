"""
精灵周打分服务 — Sprint 2 重写

打分公式(三步走):

  Step 1: 原始三维分 (0-100)
    - completion_score (50%) — 连续完成度: Σ(percent_i/100)/total + 准时奖励 - 取消惩罚
    - design_score    (30%) — 任务设计合理度: 期望任务数受基调权重影响
    - quality_score   (20%) — 用户反馈(easy/just_right/hard), 用 completion_percent 加权

  Step 2: 基调放大 → final_score
    - 重点精灵 (mult > 1.0): 以 70 为锚点放大波动 (高分更高, 低分更低)
    - 次要精灵 (mult < 1.0): 收敛波动 + 整体小幅抬升 (容忍取舍)
    - 平衡 (mult = 1.0): 不动

  Step 3: 总分加权
    - overall = Σ(final_score_i × intensity_i × focus_mult_i) / Σ(intensity_i × focus_mult_i)

等级映射:
  90-100 → flourishing   |  70-89  → good       |  50-69  → average
  30-49  → poor          |  0-29   → withered

存储字段(SpiritWeeklyScore):
  - score              ← final_score (经过基调放大)
  - raw_score          ← Step 1 三维合成, 未经基调放大
  - design/completion/quality_score ← Step 1 三个分量
  - focus_weight       ← 该精灵本周的 mult (默认 1.0)
  - focus_at_scoring   ← 本周基调 theme 快照
  - display_score      ← score / 10, 给雷达图

Sprint 3:
  - LLM 对 quality_note 的 ±10 校准, 通过 _batch_calibrate_quality_notes 实现
  - 校准结果通过 quality_calibrations 参数传入 calculate_spirit_score
"""
import json
import uuid
from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.score import SpiritWeeklyScore
from app.models.task import Task, SubTask
from app.services.intensity_service import IntensityService
from app.services.weekly_focus_service import WeeklyFocusService
from app.ai.spirits import get_spirit
from app.ai.llm_client import llm_client
from app.utils.prompt_loader import load_prompt

import structlog

logger = structlog.get_logger()

SPIRIT_CODES = ["light", "water", "soil", "air", "nutrition"]

LEVEL_THRESHOLDS = [
    (90, "flourishing"),
    (70, "good"),
    (50, "average"),
    (30, "poor"),
    (0, "withered"),
]

FEEDBACK_SCORE_MAP = {
    "easy": 70,
    "just_right": 100,
    "hard": 85,
}
DEFAULT_QUALITY_SCORE_NO_FEEDBACK = 75.0

FOCUS_MAGNIFY_PIVOT = 70.0


def _score_to_level(score: float) -> str:
    for threshold, level in LEVEL_THRESHOLDS:
        if score >= threshold:
            return level
    return "withered"


def apply_focus_magnification(raw: float, focus_mult: float) -> float:
    if raw <= 0.0:
        return 0.0
    if focus_mult <= 0:
        return raw

    delta = raw - FOCUS_MAGNIFY_PIVOT

    if focus_mult > 1.0:
        amplifier = 1.0 + (focus_mult - 1.0) * 0.5
        final = FOCUS_MAGNIFY_PIVOT + delta * amplifier
    elif focus_mult < 1.0:
        amplifier = focus_mult
        boost = (1.0 - focus_mult) * 5.0
        final = FOCUS_MAGNIFY_PIVOT + delta * amplifier + boost
    else:
        final = raw

    return max(0.0, min(100.0, final))


def calc_completion_score(subtasks: list) -> float:
    total_planned = len(subtasks)
    if total_planned == 0:
        return 0.0

    total_completion_units = sum(
        (getattr(st, "completion_percent", 0) or 0) / 100.0 for st in subtasks
    )
    completion_rate = total_completion_units / total_planned

    attempted_pct_sum = sum(
        (getattr(st, "completion_percent", 0) or 0) for st in subtasks
        if (getattr(st, "completion_percent", 0) or 0) > 0
    )
    on_time_pct_sum = sum(
        (getattr(st, "completion_percent", 0) or 0) for st in subtasks
        if (getattr(st, "completion_percent", 0) or 0) > 0
        and getattr(st, "actual_end", None)
        and getattr(st, "scheduled_end", None)
        and st.actual_end <= st.scheduled_end
    )
    if attempted_pct_sum > 0:
        on_time_bonus = (on_time_pct_sum / attempted_pct_sum) * 0.15
    else:
        on_time_bonus = 0.0

    cancelled = sum(1 for st in subtasks if getattr(st, "status", None) == "cancelled")
    cancel_penalty = (cancelled / total_planned) * 0.2

    raw = (completion_rate + on_time_bonus - cancel_penalty) * 100
    return max(0.0, min(100.0, raw))


def calc_design_score(intensity: int, focus_mult: float, total_planned: int) -> float:
    base_expected = max(1, round(intensity / 100 * 8))
    expected_count = max(1, round(base_expected * focus_mult))

    if total_planned == 0 and expected_count > 0:
        deviation = 1.0
    else:
        ratio = total_planned / expected_count
        deviation = abs(1.0 - ratio)

    tolerance = 0.3 / max(focus_mult, 0.5)
    penalty_base = max(0.0, deviation - tolerance)
    design = 100.0 - penalty_base * 80.0
    return max(0.0, min(100.0, design))


def calc_quality_score(subtasks: list) -> float:
    weighted_sum = 0.0
    weight_sum = 0.0

    for st in subtasks:
        pct = getattr(st, "completion_percent", 0) or 0
        if pct == 0:
            continue
        fb = getattr(st, "user_feedback", None)
        if fb:
            score = float(FEEDBACK_SCORE_MAP.get(fb, 80))
        else:
            score = DEFAULT_QUALITY_SCORE_NO_FEEDBACK

        w = pct / 100.0
        weighted_sum += score * w
        weight_sum += w

    if weight_sum < 0.5:
        return DEFAULT_QUALITY_SCORE_NO_FEEDBACK

    return weighted_sum / weight_sum


def _apply_quality_calibrations(
    base_quality: float,
    subtasks: list,
    calibrations: dict[str, int],
) -> tuple[float, float]:
    if not calibrations:
        return base_quality, 0.0

    adjust_sum = 0.0
    weight_sum = 0.0
    for st in subtasks:
        pct = (getattr(st, "completion_percent", 0) or 0) / 100.0
        if pct == 0:
            continue
        sid = str(getattr(st, "id", ""))
        adj = calibrations.get(sid, 0) if sid else 0
        adjust_sum += float(adj) * pct
        weight_sum += pct

    if weight_sum == 0:
        return base_quality, 0.0

    weighted_adj = adjust_sum / weight_sum
    adjusted = max(0.0, min(100.0, base_quality + weighted_adj))
    return adjusted, weighted_adj


class ScoringService:

    def __init__(self, db: AsyncSession):
        self.db = db
        self.intensity_svc = IntensityService(db)
        self.focus_svc = WeeklyFocusService(db)

    async def calculate_spirit_score(
        self,
        user_id: uuid.UUID,
        spirit_code: str,
        week_start: date,
        focus_snapshot: Optional[dict] = None,
        quality_calibrations: Optional[dict[str, int]] = None,
    ) -> SpiritWeeklyScore:
        week_end = week_start + timedelta(days=6)

        intensity = await self.intensity_svc.get_effective_intensity(user_id, spirit_code)
        subtasks = await self._get_week_subtasks(user_id, spirit_code, week_start, week_end)

        if focus_snapshot is None:
            focus_snapshot = await self.focus_svc.get_focus_snapshot(user_id, week_start)
        focus_mult = float(focus_snapshot["weights"].get(spirit_code, 1.0))
        focus_theme = focus_snapshot.get("theme")

        completion_score = calc_completion_score(subtasks)
        design_score = calc_design_score(intensity, focus_mult, len(subtasks))
        quality_score = calc_quality_score(subtasks)

        quality_calibration_applied = 0.0
        if quality_calibrations:
            quality_score, quality_calibration_applied = _apply_quality_calibrations(
                quality_score, subtasks, quality_calibrations
            )

        raw_score = (
            completion_score * 0.50
            + design_score * 0.30
            + quality_score * 0.20
        )
        raw_score = round(max(0.0, min(100.0, raw_score)), 1)

        final_score = apply_focus_magnification(raw_score, focus_mult)
        final_score = round(final_score, 1)

        display_score = round(final_score / 10.0, 2)

        level = _score_to_level(final_score)
        total_planned = len(subtasks)
        completed_count = sum(1 for st in subtasks if (st.completion_percent or 0) == 100)
        partial_count = sum(1 for st in subtasks if 0 < (st.completion_percent or 0) < 100)
        cancelled_count = sum(1 for st in subtasks if st.status == "cancelled")
        on_time_count = sum(
            1 for st in subtasks
            if (st.completion_percent or 0) == 100
            and st.actual_end and st.scheduled_end
            and st.actual_end <= st.scheduled_end
        )

        task_stats = {
            "planned":   total_planned,
            "completed": completed_count,
            "partial":   partial_count,
            "cancelled": cancelled_count,
            "on_time":   on_time_count,
            "completion_units": round(
                sum((st.completion_percent or 0) / 100.0 for st in subtasks), 2
            ),
            "quality_calibration_applied": round(quality_calibration_applied, 2),
        }

        details = {
            "design":          round(design_score, 1),
            "completion":      round(completion_score, 1),
            "quality":         round(quality_score, 1),
            "raw_score":       raw_score,
            "final_score":     final_score,
            "focus_mult":      focus_mult,
            "focus_theme":     focus_theme,
            "is_key_spirit":   spirit_code in focus_snapshot.get("key_spirits", []),
            "total_planned":   total_planned,
            "completed_count": completed_count,
            "partial_count":   partial_count,
            "cancelled_count": cancelled_count,
            "on_time_count":   on_time_count,
            "intensity":       intensity,
        }
        comment = await self._generate_comment(spirit_code, final_score, details)

        score_record = SpiritWeeklyScore(
            user_id=user_id,
            spirit_code=spirit_code,
            week_start=week_start,
            score=final_score,
            raw_score=raw_score,
            design_score=round(design_score, 1),
            completion_score=round(completion_score, 1),
            quality_score=round(quality_score, 1),
            level=level,
            intensity_at_scoring=intensity,
            focus_weight=round(focus_mult, 2),
            display_score=display_score,
            focus_at_scoring=focus_theme,
            task_stats=task_stats,
            spirit_comment=comment,
        )

        return score_record

    async def calculate_all_spirits(
        self,
        user_id: uuid.UUID,
        week_start: date,
        calibrate_quality_notes: bool = True,
    ) -> list[SpiritWeeklyScore]:
        focus_snapshot = await self.focus_svc.get_focus_snapshot(user_id, week_start)

        quality_calibrations: dict[str, int] = {}
        if calibrate_quality_notes:
            try:
                quality_calibrations = await self._batch_calibrate_quality_notes(
                    user_id, week_start
                )
            except Exception as e:
                logger.warning(
                    "quality_note_calibration_failed_skipping",
                    user_id=str(user_id), week=str(week_start), error=str(e),
                )

        scores = []
        for spirit_code in SPIRIT_CODES:
            existing = await self._get_existing_score(user_id, spirit_code, week_start)
            if existing:
                scores.append(existing)
                continue

            score = await self.calculate_spirit_score(
                user_id, spirit_code, week_start,
                focus_snapshot=focus_snapshot,
                quality_calibrations=quality_calibrations,
            )
            self.db.add(score)
            scores.append(score)

        await self.db.flush()
        return scores

    async def _batch_calibrate_quality_notes(
        self,
        user_id: uuid.UUID,
        week_start: date,
    ) -> dict[str, int]:
        week_end = week_start + timedelta(days=6)
        ws_dt = datetime.combine(week_start, datetime.min.time())
        we_dt = datetime.combine(week_end, datetime.max.time())

        result = await self.db.execute(
            select(SubTask).join(Task).where(
                and_(
                    Task.user_id == user_id,
                    SubTask.scheduled_start != None,
                    SubTask.scheduled_start >= ws_dt,
                    SubTask.scheduled_start <= we_dt,
                    SubTask.quality_note != None,
                    SubTask.quality_note != "",
                    SubTask.completion_percent > 0,
                )
            )
        )
        candidates = list(result.scalars().all())
        if not candidates:
            return {}

        MAX_CALIBRATION_ITEMS = 30
        if len(candidates) > MAX_CALIBRATION_ITEMS:
            candidates = sorted(
                candidates,
                key=lambda st: 0 if 0 < (st.completion_percent or 0) < 100 else 1,
            )[:MAX_CALIBRATION_ITEMS]

        items = [
            {
                "subtask_id": str(st.id),
                "title": st.title,
                "completion_percent": st.completion_percent or 0,
                "user_feedback": st.user_feedback or "",
                "quality_note": st.quality_note or "",
            }
            for st in candidates
        ]

        external = load_prompt("quality_note_calibration")
        if external:
            system = external
        else:
            system = (
                "你是质量评估助手. 对每条 quality_note 输出 adjustment "
                "(-10/-5/0/+5/+10). 不确定就给 0. "
                "输出 JSON: {\"calibrations\": [{\"subtask_id\":..., \"adjustment\":..., \"reason\":...}]}"
            )

        user_msg = json.dumps({"items": items}, ensure_ascii=False)

        try:
            result = await llm_client.complete_json(
                system=system,
                user=user_msg,
                purpose="quality_note_calibration",
            )
        except Exception as e:
            logger.warning("quality_note_llm_call_failed", error=str(e))
            return {}

        if not result or not isinstance(result, dict):
            return {}

        calibrations = result.get("calibrations") or []
        if not isinstance(calibrations, list):
            return {}

        adjustments: dict[str, int] = {}
        for c in calibrations:
            if not isinstance(c, dict):
                continue
            sid = c.get("subtask_id")
            adj = c.get("adjustment")
            try:
                adj_int = int(adj)
            except (TypeError, ValueError):
                continue
            if sid and adj_int in (-10, -5, 0, 5, 10):
                adjustments[str(sid)] = adj_int

        logger.info(
            "quality_note_calibration_done",
            user_id=str(user_id), week=str(week_start),
            candidates=len(candidates), applied=len(adjustments),
        )
        return adjustments

    async def get_week_scores(self, user_id: uuid.UUID, week_start: date) -> list[SpiritWeeklyScore]:
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
        last_week = week_start - timedelta(days=7)
        return await self.get_week_scores(user_id, last_week)

    async def get_spirit_score_history(
        self, user_id: uuid.UUID, spirit_code: str, weeks: int = 12
    ) -> list[SpiritWeeklyScore]:
        cutoff = date.today() - timedelta(weeks=weeks)
        result = await self.db.execute(
            select(SpiritWeeklyScore).where(
                SpiritWeeklyScore.user_id == user_id,
                SpiritWeeklyScore.spirit_code == spirit_code,
                SpiritWeeklyScore.week_start >= cutoff,
            ).order_by(SpiritWeeklyScore.week_start)
        )
        return list(result.scalars().all())

    async def get_overall_score(self, user_id: uuid.UUID, week_start: date) -> float:
        scores = await self.get_week_scores(user_id, week_start)
        if not scores:
            return 0

        total_weight = 0
        weighted_sum = 0
        for s in scores:
            base_w = max(1, s.intensity_at_scoring)
            focus_w = float(s.focus_weight or 1.0)
            w = base_w * focus_w
            weighted_sum += s.score * w
            total_weight += w

        return round(weighted_sum / total_weight, 1) if total_weight > 0 else 0

    async def _generate_comment(
        self, spirit_code: str, score: float, details: dict
    ) -> str:
        try:
            spirit = get_spirit(spirit_code)
            comment = await spirit.generate_comment(score, details)
            return comment
        except Exception as e:
            logger.warning("spirit_comment_fallback", spirit=spirit_code, error=str(e))
            return self._fallback_comment(spirit_code, score, details)

    @staticmethod
    def _fallback_comment(spirit_code: str, score: float, details: dict) -> str:
        planned = details.get("total_planned", 0)
        completed = details.get("completed_count", 0)
        partial = details.get("partial_count", 0)
        is_key = details.get("is_key_spirit", False)
        focus_theme = details.get("focus_theme")

        completion_str = f"{completed}/{planned}"
        if partial > 0:
            completion_str = f"{completed}全做完+{partial}部分完成/共{planned}"

        if planned == 0:
            if is_key:
                return "这周你定了我做重点,但一个任务都没派给我,我也很无奈呀!"
            return "这周完全没有安排任务哦,下周记得给我派活!"

        if score >= 90:
            tag = "重点维度满分表现!" if is_key else ""
            return f"太棒了!{completion_str},{tag}继续保持!"
        if score >= 70:
            return f"表现不错,{completion_str}。再接再厉!"
        if score >= 50:
            if is_key:
                return f"本周是你的重点,{completion_str},还得加把劲。"
            return f"中规中矩,{completion_str}。"
        if score >= 30:
            return f"这周有点松懈,{completion_str}。需要更多关注。"

        if is_key:
            return f"这周你想以此为重点,但{completion_str},需要重新调整节奏。"
        return f"这周不在你的重点上,但完成率也太低了。"

    async def _get_week_subtasks(
        self, user_id: uuid.UUID, spirit_code: str, week_start: date, week_end: date
    ) -> list:
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
        self, user_id: uuid.UUID, spirit_code: str, week_start: date
    ) -> Optional[SpiritWeeklyScore]:
        result = await self.db.execute(
            select(SpiritWeeklyScore).where(
                SpiritWeeklyScore.user_id == user_id,
                SpiritWeeklyScore.spirit_code == spirit_code,
                SpiritWeeklyScore.week_start == week_start,
            )
        )
        return result.scalar_one_or_none()

    @staticmethod
    def _calc_design_score(intensity: int, total_planned: int) -> float:
        return calc_design_score(intensity, 1.0, total_planned)

    @staticmethod
    def _calc_completion_score(
        total: int, completed: int, on_time: int, cancelled: int
    ) -> float:
        if total == 0:
            return 0.0
        base_rate = completed / total
        on_time_bonus = (on_time / max(completed, 1)) * 0.15
        cancel_penalty = (cancelled / total) * 0.2
        raw = (base_rate + on_time_bonus - cancel_penalty) * 100
        return min(100.0, max(0.0, raw))

    @staticmethod
    def _calc_quality_score(completed_subtasks: list) -> float:
        feedbacks = [
            st.user_feedback for st in completed_subtasks
            if hasattr(st, "user_feedback") and st.user_feedback
        ]
        if not feedbacks:
            return DEFAULT_QUALITY_SCORE_NO_FEEDBACK
        total = sum(FEEDBACK_SCORE_MAP.get(f, 80) for f in feedbacks)
        return total / len(feedbacks)
