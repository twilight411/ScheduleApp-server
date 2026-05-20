"""
基调推断服务 — Sprint 4

职责: 当用户周一打开 app 还没设基调时, 根据过去 2 周的行为给出建议,
       同时检测"过度聚焦""长期忽略某方向"等护栏警告。

核心:
  1. suggest_for_week(user_id, week_start) → 推断本周适合什么基调
  2. check_over_focus_warnings(user_id, week_start) → 检测护栏问题
  3. build_suggestion_response(user_id, week_start) → 一次返回完整结构

推断算法 (三信号融合):
  - 活跃度分布 (60%): 过去 2 周各精灵的任务数 + 完成数加权
  - 关键词匹配 (30%): 任务标题里的暗示词
  - 历史延续    (10%): 用户最近习惯的 theme 倾向

设计原则:
  - 不持久化, 按需计算 (避免新表与新迁移)
  - 纯规则, 不调 LLM (响应快、透明、易测)
  - 数据不足时返回 None, 不硬塞建议
  - 给出"为什么推荐"的可读理由
"""
import re
import uuid
from collections import Counter
from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.task import Task, SubTask
from app.models.score import SpiritWeeklyScore
from app.services.weekly_focus_service import (
    WeeklyFocusService, THEME_PRESETS, SPIRIT_CODES,
)

import structlog

logger = structlog.get_logger()


# ====================================================================
#  关键词词典
# ====================================================================

THEME_KEYWORDS: dict[str, list[str]] = {
    "exam_prep": [
        "考试", "复习", "刷题", "试卷", "笔试", "面试", "考研", "考公",
        "考证", "测验", "答题", "题目", "exam", "quiz", "review",
    ],
    "project_sprint": [
        "项目", "上线", "deadline", "ddl", "汇报", "提交", "迭代",
        "需求", "排期", "评审", "发布", "milestone", "sprint", "ship",
    ],
    "recovery": [
        "休息", "放松", "调整", "睡觉", "休假", "假期", "调休",
        "散步", "冥想", "瑜伽", "rest", "chill", "relax", "nap",
    ],
    "social": [
        "聚餐", "约饭", "约会", "聚会", "朋友", "聊天", "见面",
        "聚", "饭局", "dinner", "lunch", "meetup", "party",
    ],
    "creative": [
        "练琴", "画画", "写作", "弹琴", "唱歌", "拍照", "练习",
        "创作", "学吉他", "学钢琴", "做菜", "烘焙", "手工", "diy",
        "draw", "paint", "compose", "practice", "craft",
    ],
}

SPIRIT_TO_PRIMARY_THEMES: dict[str, list[str]] = {
    "light":     ["exam_prep", "project_sprint"],
    "water":     ["recovery"],
    "soil":      ["recovery"],
    "air":       ["social"],
    "nutrition": ["creative"],
}

LOOKBACK_WEEKS = 2
MIN_TASKS_FOR_SUGGESTION = 5
MIN_CONFIDENCE_TO_SHOW = 50
ALT_THEME_THRESHOLD = 10
KEYWORD_SATURATION = 3

OVER_FOCUS_WEEKS_THRESHOLD = 3
OVER_FOCUS_HEALTH_AVG = 55
NO_FOCUS_WEEKS_THRESHOLD = 4
NEGLECT_SPIRIT_WEEKS = 4


# ====================================================================
#  纯函数 — 信号计算 (便于单测)
# ====================================================================

def compute_activity_distribution(
    tasks: list, subtasks: list
) -> dict[str, float]:
    task_count: Counter = Counter()
    subtask_count: Counter = Counter()

    for t in tasks:
        code = getattr(t, "primary_spirit", None)
        if code in SPIRIT_CODES:
            task_count[code] += 1

    for st in subtasks:
        code = getattr(st, "spirit", None)
        if code in SPIRIT_CODES:
            subtask_count[code] += 1

    activity_raw: dict[str, float] = {}
    for code in SPIRIT_CODES:
        activity_raw[code] = (
            task_count.get(code, 0) * 0.4
            + subtask_count.get(code, 0) * 0.6
        )

    total = sum(activity_raw.values())
    if total <= 0:
        return {code: 1.0 / len(SPIRIT_CODES) for code in SPIRIT_CODES}

    return {code: round(activity_raw[code] / total, 3) for code in SPIRIT_CODES}


def scan_keywords(tasks: list, subtasks: list) -> dict[str, int]:
    hits: Counter = Counter()
    texts: list[str] = []
    for t in tasks:
        title = getattr(t, "title", None) or ""
        raw = getattr(t, "raw_input", None) or ""
        texts.append((title + " " + raw).lower())
    for st in subtasks:
        title = getattr(st, "title", None) or ""
        texts.append(title.lower())

    for text in texts:
        if not text.strip():
            continue
        for theme, kws in THEME_KEYWORDS.items():
            for kw in kws:
                if kw.lower() in text:
                    hits[theme] += 1
                    break

    return dict(hits)


def score_themes_by_activity(activity: dict[str, float]) -> dict[str, float]:
    light = activity.get("light", 0)
    water = activity.get("water", 0)
    soil = activity.get("soil", 0)
    air = activity.get("air", 0)
    nutrition = activity.get("nutrition", 0)

    scores: dict[str, float] = {}

    if light >= 0.45:
        s = min(1.0, max(0.0, (light - 0.25) * 2))
        scores["exam_prep"] = s
        scores["project_sprint"] = s

    health_focus = soil + water
    if health_focus >= 0.40:
        s = min(1.0, max(0.0, (health_focus - 0.20) * 2))
        scores["recovery"] = s

    if air >= 0.25:
        s = min(1.0, max(0.0, (air - 0.10) * 2.5))
        scores["social"] = s

    if nutrition >= 0.20:
        s = min(1.0, max(0.0, (nutrition - 0.10) * 3))
        scores["creative"] = s

    spread = max(activity.values()) - min(activity.values())
    if spread <= 0.20:
        scores["balanced"] = max(0.0, 1.0 - spread * 3)

    return scores


def score_themes_by_keywords(hits: dict[str, int]) -> dict[str, float]:
    return {
        theme: min(hits.get(theme, 0) / KEYWORD_SATURATION, 1.0)
        for theme in THEME_KEYWORDS
    }


def merge_theme_scores(
    activity_scores: dict[str, float],
    keyword_scores: dict[str, float],
    history_bonus: dict[str, float],
    *,
    w_activity: float = 0.6,
    w_keyword: float = 0.3,
    w_history: float = 0.1,
) -> dict[str, float]:
    all_themes = set(THEME_PRESETS.keys()) | set(activity_scores) | set(keyword_scores) | set(history_bonus)
    final: dict[str, float] = {}
    for theme in all_themes:
        score = (
            activity_scores.get(theme, 0) * w_activity
            + keyword_scores.get(theme, 0) * w_keyword
            + history_bonus.get(theme, 0) * w_history
        )
        final[theme] = round(score * 100, 1)
    return final


# ====================================================================
#  FocusSuggestionService
# ====================================================================

class FocusSuggestionService:

    def __init__(self, db: AsyncSession):
        self.db = db
        self.focus_svc = WeeklyFocusService(db)

    async def build_suggestion_response(
        self,
        user_id: uuid.UUID,
        week_start: date,
    ) -> dict:
        warnings = await self.check_over_focus_warnings(user_id, week_start)

        existing = await self.focus_svc.get_focus(user_id, week_start)
        if existing:
            return {
                "has_suggestion": False,
                "reason_if_none": "本周已设基调",
                "suggested_theme": None,
                "label": "",
                "icon": "",
                "confidence": 0,
                "reasons": [],
                "alternative_themes": [],
                "warnings": warnings,
            }

        suggestion = await self.suggest_for_week(user_id, week_start)
        if not suggestion:
            return {
                "has_suggestion": False,
                "reason_if_none": "数据不足或无明显倾向",
                "suggested_theme": None,
                "label": "",
                "icon": "",
                "confidence": 0,
                "reasons": [],
                "alternative_themes": [],
                "warnings": warnings,
            }

        suggestion["warnings"] = warnings
        suggestion["has_suggestion"] = True
        suggestion["reason_if_none"] = None
        return suggestion

    async def suggest_for_week(
        self,
        user_id: uuid.UUID,
        week_start: date,
    ) -> Optional[dict]:
        lookback_start = week_start - timedelta(weeks=LOOKBACK_WEEKS)
        tasks, subtasks = await self._load_recent_activity(
            user_id, lookback_start, week_start
        )

        if len(subtasks) < MIN_TASKS_FOR_SUGGESTION:
            logger.info(
                "suggestion_skip_insufficient_data",
                user_id=str(user_id), subtask_count=len(subtasks),
            )
            return None

        activity = compute_activity_distribution(tasks, subtasks)
        keyword_hits = scan_keywords(tasks, subtasks)

        activity_scores = score_themes_by_activity(activity)
        keyword_scores = score_themes_by_keywords(keyword_hits)
        history_bonus = await self._compute_history_bonus(user_id, week_start)

        final = merge_theme_scores(activity_scores, keyword_scores, history_bonus)

        if not final:
            return None

        ranked = sorted(final.items(), key=lambda kv: -kv[1])
        top_theme, top_conf = ranked[0]

        if top_conf < MIN_CONFIDENCE_TO_SHOW:
            logger.info(
                "suggestion_skip_low_confidence",
                user_id=str(user_id), top=top_theme, conf=top_conf,
            )
            return None

        alternative_themes = []
        for theme, conf in ranked[1:3]:
            if top_conf - conf <= ALT_THEME_THRESHOLD and conf >= 40:
                preset = THEME_PRESETS.get(theme, {})
                alternative_themes.append({
                    "theme": theme,
                    "label": preset.get("label", theme),
                    "icon": preset.get("icon", ""),
                    "confidence": int(round(conf)),
                })

        reasons = self._build_reasons(
            top_theme, activity, keyword_hits, history_bonus,
        )

        preset = THEME_PRESETS.get(top_theme, {})
        return {
            "suggested_theme": top_theme,
            "label": preset.get("label", top_theme),
            "icon": preset.get("icon", ""),
            "confidence": int(round(top_conf)),
            "reasons": reasons,
            "alternative_themes": alternative_themes,
        }

    def _build_reasons(
        self,
        theme: str,
        activity: dict[str, float],
        keyword_hits: dict[str, int],
        history_bonus: dict[str, float],
    ) -> list[str]:
        reasons = []
        spirit_name_map = {
            "light": "学习/工作", "water": "娱乐", "soil": "健康",
            "air": "社交", "nutrition": "兴趣爱好",
        }

        if theme in ("exam_prep", "project_sprint"):
            pct = activity.get("light", 0) * 100
            if pct >= 45:
                reasons.append(f"过去 2 周 {pct:.0f}% 的任务集中在学习/工作")
        elif theme == "recovery":
            pct = (activity.get("soil", 0) + activity.get("water", 0)) * 100
            if pct >= 40:
                reasons.append(f"过去 2 周 {pct:.0f}% 的任务在健康和休闲方向")
        elif theme == "social":
            pct = activity.get("air", 0) * 100
            if pct >= 25:
                reasons.append(f"过去 2 周社交类任务占比 {pct:.0f}%")
        elif theme == "creative":
            pct = activity.get("nutrition", 0) * 100
            if pct >= 20:
                reasons.append(f"过去 2 周兴趣类任务占比 {pct:.0f}%")
        elif theme == "balanced":
            reasons.append("过去 2 周五个方向的任务量比较均衡")

        hits = keyword_hits.get(theme, 0)
        if hits >= 2:
            kw_sample = []
            for kw in THEME_KEYWORDS.get(theme, [])[:3]:
                kw_sample.append(kw)
            kw_str = "、".join(kw_sample[:3])
            reasons.append(f"任务标题里出现『{kw_str}』等关键词共 {hits} 次")

        if history_bonus.get(theme, 0) > 0.5:
            preset = THEME_PRESETS.get(theme, {})
            reasons.append(f"上周也是「{preset.get('label', theme)}」基调")

        if not reasons:
            reasons.append("综合任务结构和近期习惯的整体倾向")

        return reasons

    async def check_over_focus_warnings(
        self,
        user_id: uuid.UUID,
        week_start: date,
    ) -> list[dict]:
        warnings = []

        cutoff = week_start - timedelta(weeks=6)
        result = await self.db.execute(
            select(SpiritWeeklyScore).where(
                SpiritWeeklyScore.user_id == user_id,
                SpiritWeeklyScore.week_start >= cutoff,
                SpiritWeeklyScore.week_start < week_start,
            ).order_by(SpiritWeeklyScore.week_start)
        )
        scores = list(result.scalars().all())
        if not scores:
            return warnings

        by_week: dict[date, list[SpiritWeeklyScore]] = {}
        for s in scores:
            by_week.setdefault(s.week_start, []).append(s)

        weeks_sorted = sorted(by_week.keys(), reverse=True)

        warnings.extend(self._detect_over_focus(by_week, weeks_sorted))
        warnings.extend(self._detect_no_focus(by_week, weeks_sorted))
        warnings.extend(self._detect_neglected_spirit(by_week, weeks_sorted))

        return warnings

    @staticmethod
    def _detect_over_focus(
        by_week: dict[date, list[SpiritWeeklyScore]],
        weeks_sorted: list[date],
    ) -> list[dict]:
        if len(weeks_sorted) < OVER_FOCUS_WEEKS_THRESHOLD:
            return []

        recent = weeks_sorted[:OVER_FOCUS_WEEKS_THRESHOLD]
        themes = []
        for ws in recent:
            scores_w = by_week.get(ws, [])
            if not scores_w:
                themes.append(None)
                continue
            themes.append(scores_w[0].focus_at_scoring)

        if all(t == themes[0] and t in ("exam_prep", "project_sprint", "creative") for t in themes):
            health_scores = []
            for ws in recent:
                for s in by_week.get(ws, []):
                    if s.spirit_code in ("soil", "water"):
                        health_scores.append(s.score)
            if health_scores:
                avg_health = sum(health_scores) / len(health_scores)
                if avg_health < OVER_FOCUS_HEALTH_AVG:
                    preset_label = THEME_PRESETS.get(themes[0], {}).get("label", themes[0])
                    return [{
                        "type": "over_focus",
                        "severity": "medium",
                        "message": (
                            f"你已经连续 {OVER_FOCUS_WEEKS_THRESHOLD} 周以「{preset_label}」为重点, "
                            f"健康和休闲均分仅 {avg_health:.0f},"
                            f"要不要这周给自己留一段休整时间?"
                        ),
                        "suggested_alternative": "recovery",
                        "evidence": {
                            "consecutive_weeks": OVER_FOCUS_WEEKS_THRESHOLD,
                            "current_theme": themes[0],
                            "health_avg": round(avg_health, 1),
                        },
                    }]
        return []

    @staticmethod
    def _detect_no_focus(
        by_week: dict[date, list[SpiritWeeklyScore]],
        weeks_sorted: list[date],
    ) -> list[dict]:
        if len(weeks_sorted) < NO_FOCUS_WEEKS_THRESHOLD:
            return []

        recent = weeks_sorted[:NO_FOCUS_WEEKS_THRESHOLD]
        all_none = True
        for ws in recent:
            scores_w = by_week.get(ws, [])
            if scores_w and scores_w[0].focus_at_scoring:
                all_none = False
                break

        if all_none:
            return [{
                "type": "no_focus_too_long",
                "severity": "low",
                "message": (
                    f"已经 {NO_FOCUS_WEEKS_THRESHOLD} 周没有为自己设过本周方向了, "
                    f"花 30 秒选个基调能让周报和月度果实更懂你哦。"
                ),
                "suggested_alternative": None,
                "evidence": {"consecutive_weeks_no_focus": NO_FOCUS_WEEKS_THRESHOLD},
            }]
        return []

    @staticmethod
    def _detect_neglected_spirit(
        by_week: dict[date, list[SpiritWeeklyScore]],
        weeks_sorted: list[date],
    ) -> list[dict]:
        if len(weeks_sorted) < NEGLECT_SPIRIT_WEEKS:
            return []

        recent = weeks_sorted[:NEGLECT_SPIRIT_WEEKS]
        spirit_name_map = {
            "light": "学习/工作", "water": "娱乐", "soil": "健康",
            "air": "社交", "nutrition": "兴趣爱好",
        }

        out = []
        for code in SPIRIT_CODES:
            all_low = True
            for ws in recent:
                weights = [
                    float(s.focus_weight or 1.0)
                    for s in by_week.get(ws, []) if s.spirit_code == code
                ]
                if not weights or weights[0] > 0.7:
                    all_low = False
                    break
            if all_low:
                name = spirit_name_map.get(code, code)
                out.append({
                    "type": "neglected_spirit",
                    "severity": "low",
                    "message": (
                        f"「{name}」已经连续 {NEGLECT_SPIRIT_WEEKS} 周被你主动收敛, "
                        f"如果不是有意为之, 可以考虑让它回到平衡。"
                    ),
                    "suggested_alternative": None,
                    "evidence": {
                        "spirit_code": code,
                        "consecutive_low_weeks": NEGLECT_SPIRIT_WEEKS,
                    },
                })
        return out

    async def _load_recent_activity(
        self,
        user_id: uuid.UUID,
        window_start: date,
        window_end: date,
    ) -> tuple[list, list]:
        ws_dt = datetime.combine(window_start, datetime.min.time())
        we_dt = datetime.combine(window_end, datetime.max.time())

        t_result = await self.db.execute(
            select(Task).where(
                Task.user_id == user_id,
                Task.created_at >= ws_dt,
                Task.created_at <= we_dt,
            )
        )
        tasks = list(t_result.scalars().all())

        st_result = await self.db.execute(
            select(SubTask).join(Task).where(
                and_(
                    Task.user_id == user_id,
                    SubTask.scheduled_start != None,
                    SubTask.scheduled_start >= ws_dt,
                    SubTask.scheduled_start <= we_dt,
                )
            )
        )
        subtasks = list(st_result.scalars().all())

        return tasks, subtasks

    async def _compute_history_bonus(
        self,
        user_id: uuid.UUID,
        week_start: date,
    ) -> dict[str, float]:
        bonus: dict[str, float] = {}

        cutoff = week_start - timedelta(weeks=3)
        result = await self.db.execute(
            select(SpiritWeeklyScore).where(
                SpiritWeeklyScore.user_id == user_id,
                SpiritWeeklyScore.week_start >= cutoff,
                SpiritWeeklyScore.week_start < week_start,
            ).order_by(SpiritWeeklyScore.week_start.desc())
        )
        scores = list(result.scalars().all())

        weekly_themes: list[Optional[str]] = []
        seen_weeks: set[date] = set()
        for s in scores:
            if s.week_start in seen_weeks:
                continue
            seen_weeks.add(s.week_start)
            weekly_themes.append(s.focus_at_scoring)

        if len(weekly_themes) >= 1 and weekly_themes[0]:
            bonus[weekly_themes[0]] = bonus.get(weekly_themes[0], 0) + 0.7
        if len(weekly_themes) >= 2 and weekly_themes[1]:
            bonus[weekly_themes[1]] = bonus.get(weekly_themes[1], 0) + 0.3

        if (len(weekly_themes) >= 3 and weekly_themes[0]
                and weekly_themes[0] == weekly_themes[1] == weekly_themes[2]
                and weekly_themes[0] in ("exam_prep", "project_sprint", "creative")):
            theme = weekly_themes[0]
            bonus[theme] = max(-0.3, bonus.get(theme, 0) - 1.0)

        for t in list(bonus.keys()):
            bonus[t] = max(-0.5, min(1.0, bonus[t]))

        return bonus