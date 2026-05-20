"""
月度果实服务 — 聚合 4-5 周得分 → 果实类型 + 趣味奖项 + AI 月度叙述

Sprint C: 升级 AI 月度叙述 Prompt（periph.txt #9 风格）
  - 果实描述个性化，基于具体行为
  - 像游戏 RPG 获得道具时的描述
  - 每个果实独一无二，带有"成长记忆"

Sprint 3 增量:
  - 月度总分公式: Σ(weekly_overall × focus_intensity) / Σ(focus_intensity)
    (基调鲜明的周比"混过去的周"对月度影响更大)
  - spirit_monthly 新增 focused_weeks / key_weeks_avg 字段
  - spirit_monthly["_meta"] 加 theme_history / week_focus_intensities
  - 3 个新奖项: 聚焦达人 / 节奏切换大师 / 平衡守护者
  - AI 月度叙述 + 生图 prompt 传入 theme_history

果实类型体系:
  90-100 → golden_apple  (金苹果, legendary)
  80-89  → crystal_grape (水晶葡萄, epic)
  65-79  → sunshine_orange (阳光橙, rare)
  50-64  → green_apple   (青苹果, common)
  0-49   → seed          (种子, common)

趣味奖项池:
  最佳劳模、全勤之星、逆袭王者、稳如泰山、
  最需关爱、被遗忘的、大起大落、聚焦达人、
  节奏切换大师、平衡守护者
"""
import uuid
import statistics
from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.report import MonthlyFruit
from app.models.score import SpiritWeeklyScore
from app.ai.llm_client import llm_client
from app.ai.image_client import image_client
from app.utils.prompt_loader import load_prompt

import structlog

logger = structlog.get_logger()

SPIRIT_CODES = ["light", "water", "soil", "air", "nutrition"]
SPIRIT_NAMES = {
    "light": "光精灵", "water": "水精灵", "soil": "土壤精灵",
    "air": "空气精灵", "nutrition": "营养精灵",
}

KEY_SPIRIT_WEIGHT_THRESHOLD = 1.3

KEY_FOCUSED_WEEKS_FOR_AWARD = 3
KEY_AVG_SCORE_FOR_AWARD = 75
THEME_SWITCH_COUNT_FOR_AWARD = 2
BALANCE_GUARDIAN_MIN_AVG = 70

FRUIT_TYPES = [
    {
        "min": 90, "max": 100,
        "fruit": "golden_apple", "name": "金苹果",
        "emoji": "🍎✨", "rarity": "legendary",
        "description": "这个月你的表现堪称完美！五个精灵都很满意。",
    },
    {
        "min": 80, "max": 89,
        "fruit": "crystal_grape", "name": "水晶葡萄",
        "emoji": "🍇", "rarity": "epic",
        "description": "丰收的季节！你在多个领域都有出色表现。",
    },
    {
        "min": 65, "max": 79,
        "fruit": "sunshine_orange", "name": "阳光橙",
        "emoji": "🍊", "rarity": "rare",
        "description": "稳步前行的一个月，继续保持！",
    },
    {
        "min": 50, "max": 64,
        "fruit": "green_apple", "name": "青苹果",
        "emoji": "🍏", "rarity": "common",
        "description": "还在成长中，下个月会更好的。",
    },
    {
        "min": 0, "max": 49,
        "fruit": "seed", "name": "种子",
        "emoji": "🌰", "rarity": "common",
        "description": "每颗种子都有发芽的潜力，下个月重新出发！",
    },
]

AWARD_POOL = [
    {"name": "最佳劳模", "condition": "completed_most", "emoji": "🏆"},
    {"name": "全勤之星", "condition": "highest_completion_rate", "emoji": "⭐"},
    {"name": "逆袭王者", "condition": "biggest_improvement", "emoji": "📈"},
    {"name": "稳如泰山", "condition": "most_stable", "emoji": "🪨"},
    {"name": "最需关爱", "condition": "lowest_score", "emoji": "💝"},
    {"name": "被遗忘的", "condition": "zero_tasks", "emoji": "😢"},
    {"name": "大起大落", "condition": "most_volatile", "emoji": "🎢"},
    {"name": "聚焦达人", "condition": "key_spirit_consistency", "emoji": "🎯"},
    {"name": "节奏切换大师", "condition": "theme_switching_mastery", "emoji": "🌊"},
    {"name": "平衡守护者", "condition": "balanced_excellence", "emoji": "⚖️"},
]


def get_fruit_type(score: float) -> dict:
    for ft in FRUIT_TYPES:
        if ft["min"] <= score <= ft["max"]:
            return ft
    return FRUIT_TYPES[-1]


class FruitService:

    def __init__(self, db: AsyncSession):
        self.db = db

    async def generate_monthly_fruit(
        self, user_id: uuid.UUID, month: str,
    ) -> MonthlyFruit:
        existing = await self.get_fruit(user_id, month)
        if existing:
            return existing

        week_starts = self._get_month_week_starts(month)
        all_scores = await self._load_month_scores(user_id, week_starts)

        if not all_scores:
            return await self._create_empty_fruit(user_id, month)

        spirit_monthly = self._aggregate_spirit_monthly(all_scores, week_starts)

        weekly_overall_scores = self._calc_weekly_overalls(all_scores, week_starts)
        week_focus_intensities = self._calc_week_focus_intensities(
            all_scores, week_starts
        )
        overall_score = self._calc_month_overall(
            weekly_overall_scores, week_focus_intensities
        )

        theme_history = self._extract_theme_history(all_scores, week_starts)

        fruit_info = get_fruit_type(overall_score)

        spirit_avgs = {
            code: data.get("avg_score", 0)
            for code, data in spirit_monthly.items()
        }
        best_spirit = max(spirit_avgs, key=spirit_avgs.get) if spirit_avgs else None
        weakest_spirit = min(spirit_avgs, key=spirit_avgs.get) if spirit_avgs else None

        awards = self._calculate_awards(
            spirit_monthly, all_scores, week_starts, theme_history
        )

        narrative = await self._generate_narrative(
            month, overall_score, fruit_info, spirit_monthly, awards,
            theme_history=theme_history,
        )

        spirit_monthly_with_meta = dict(spirit_monthly)
        spirit_monthly_with_meta["_meta"] = {
            "theme_history": theme_history,
            "week_focus_intensities": [
                round(x, 2) for x in week_focus_intensities
            ],
        }

        fruit = MonthlyFruit(
            user_id=user_id,
            month=month,
            fruit_type=fruit_info["fruit"],
            fruit_name=fruit_info["name"],
            fruit_rarity=fruit_info["rarity"],
            overall_score=overall_score,
            weekly_scores=weekly_overall_scores,
            spirit_monthly=spirit_monthly_with_meta,
            best_spirit=best_spirit,
            weakest_spirit=weakest_spirit,
            awards=awards,
            monthly_narrative=narrative,
        )
        self.db.add(fruit)
        await self.db.flush()

        logger.info(
            "monthly_fruit_generated",
            user_id=str(user_id),
            month=month,
            fruit=fruit_info["fruit"],
            score=overall_score,
        )
        return fruit

    async def get_fruit(self, user_id: uuid.UUID, month: str) -> Optional[MonthlyFruit]:
        result = await self.db.execute(
            select(MonthlyFruit).where(
                MonthlyFruit.user_id == user_id,
                MonthlyFruit.month == month,
            )
        )
        return result.scalar_one_or_none()

    async def get_latest_fruit(self, user_id: uuid.UUID) -> Optional[MonthlyFruit]:
        result = await self.db.execute(
            select(MonthlyFruit)
            .where(MonthlyFruit.user_id == user_id)
            .order_by(desc(MonthlyFruit.month))
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def get_collection(self, user_id: uuid.UUID) -> list[MonthlyFruit]:
        result = await self.db.execute(
            select(MonthlyFruit)
            .where(MonthlyFruit.user_id == user_id)
            .order_by(desc(MonthlyFruit.month))
        )
        return list(result.scalars().all())

    async def _load_month_scores(
        self, user_id: uuid.UUID, week_starts: list[date],
    ) -> list[SpiritWeeklyScore]:
        if not week_starts:
            return []

        result = await self.db.execute(
            select(SpiritWeeklyScore).where(
                SpiritWeeklyScore.user_id == user_id,
                SpiritWeeklyScore.week_start.in_(week_starts),
            )
        )
        return list(result.scalars().all())

    def _aggregate_spirit_monthly(
        self, all_scores: list[SpiritWeeklyScore], week_starts: list[date],
    ) -> dict:
        spirit_monthly = {}

        for code in SPIRIT_CODES:
            spirit_scores = [s for s in all_scores if s.spirit_code == code]
            if not spirit_scores:
                spirit_monthly[code] = {
                    "avg_score": 0,
                    "trend": "stable",
                    "total_tasks": 0,
                    "completed_tasks": 0,
                    "best_week_score": 0,
                    "worst_week_score": 0,
                    "focused_weeks": 0,
                    "key_weeks_avg": 0,
                }
                continue

            scores_vals = [s.score for s in spirit_scores]
            total_tasks = sum(s.task_stats.get("planned", 0) for s in spirit_scores)
            completed_tasks = sum(s.task_stats.get("completed", 0) for s in spirit_scores)

            mid = len(scores_vals) // 2
            if mid > 0 and len(scores_vals) > 1:
                first_half = sum(scores_vals[:mid]) / mid
                second_half = sum(scores_vals[mid:]) / (len(scores_vals) - mid)
                diff = second_half - first_half
                trend = "up" if diff > 5 else ("down" if diff < -5 else "stable")
            else:
                trend = "stable"

            key_week_scores = [
                s.score for s in spirit_scores
                if float(s.focus_weight or 1.0) > KEY_SPIRIT_WEIGHT_THRESHOLD
            ]
            focused_weeks = len(key_week_scores)
            key_weeks_avg = (
                round(sum(key_week_scores) / focused_weeks, 1)
                if focused_weeks > 0 else 0
            )

            spirit_monthly[code] = {
                "avg_score": round(sum(scores_vals) / len(scores_vals), 1),
                "trend": trend,
                "total_tasks": total_tasks,
                "completed_tasks": completed_tasks,
                "best_week_score": round(max(scores_vals), 1),
                "worst_week_score": round(min(scores_vals), 1),
                "focused_weeks": focused_weeks,
                "key_weeks_avg": key_weeks_avg,
            }

        return spirit_monthly

    def _calc_weekly_overalls(
        self, all_scores: list[SpiritWeeklyScore], week_starts: list[date],
    ) -> list[float]:
        overalls = []
        for ws in week_starts:
            week_scores = [s for s in all_scores if s.week_start == ws]
            if not week_scores:
                continue

            total_w = 0
            weighted = 0
            for s in week_scores:
                base_w = max(1, s.intensity_at_scoring)
                focus_w = float(s.focus_weight or 1.0)
                w = base_w * focus_w
                weighted += s.score * w
                total_w += w

            overalls.append(round(weighted / total_w, 1) if total_w else 0)

        return overalls

    def _calc_week_focus_intensities(
        self, all_scores: list[SpiritWeeklyScore], week_starts: list[date],
    ) -> list[float]:
        intensities = []
        for ws in week_starts:
            week_scores = [s for s in all_scores if s.week_start == ws]
            if not week_scores:
                continue

            deviations = [
                abs(float(s.focus_weight or 1.0) - 1.0)
                for s in week_scores
            ]
            mean_dev = sum(deviations) / len(deviations) if deviations else 0
            intensities.append(round(mean_dev + 1.0, 3))

        return intensities

    @staticmethod
    def _calc_month_overall(
        weekly_overall_scores: list[float],
        week_focus_intensities: list[float],
    ) -> float:
        if not weekly_overall_scores:
            return 0.0

        n = min(len(weekly_overall_scores), len(week_focus_intensities))
        if n == 0:
            return 0.0

        total_w = sum(week_focus_intensities[:n])
        if total_w == 0:
            return round(sum(weekly_overall_scores[:n]) / n, 1)

        weighted = sum(
            s * w for s, w in zip(weekly_overall_scores[:n], week_focus_intensities[:n])
        )
        return round(weighted / total_w, 1)

    @staticmethod
    def _extract_theme_history(
        all_scores: list[SpiritWeeklyScore], week_starts: list[date],
    ) -> dict:
        themes_per_week = []
        theme_counts: dict[str, int] = {}
        for ws in week_starts:
            week_scores = [s for s in all_scores if s.week_start == ws]
            if not week_scores:
                themes_per_week.append(None)
                continue
            theme = week_scores[0].focus_at_scoring
            themes_per_week.append(theme)
            key = theme if theme else "(none)"
            theme_counts[key] = theme_counts.get(key, 0) + 1

        non_none_themes = {k: v for k, v in theme_counts.items() if k != "(none)"}
        dominant_theme = (
            max(non_none_themes, key=non_none_themes.get)
            if non_none_themes else None
        )

        weeks_with_focus = sum(1 for t in themes_per_week if t)
        weeks_without_focus = sum(1 for t in themes_per_week if not t)
        theme_switch_count = len(non_none_themes)

        return {
            "themes_per_week":     themes_per_week,
            "theme_counts":        theme_counts,
            "dominant_theme":      dominant_theme,
            "weeks_with_focus":    weeks_with_focus,
            "weeks_without_focus": weeks_without_focus,
            "theme_switch_count":  theme_switch_count,
        }

    def _calculate_awards(
        self,
        spirit_monthly: dict,
        all_scores: list[SpiritWeeklyScore],
        week_starts: list[date],
        theme_history: Optional[dict] = None,
    ) -> list[dict]:
        awards = []

        spirit_data = {}
        for code in SPIRIT_CODES:
            data = spirit_monthly.get(code, {})
            scores_vals = [
                s.score for s in all_scores if s.spirit_code == code
            ]
            spirit_data[code] = {
                "avg_score": data.get("avg_score", 0),
                "completed": data.get("completed_tasks", 0),
                "total": data.get("total_tasks", 0),
                "completion_rate": (
                    data.get("completed_tasks", 0) / max(data.get("total_tasks", 0), 1)
                ),
                "weekly_scores": scores_vals,
                "volatility": (
                    statistics.stdev(scores_vals) if len(scores_vals) >= 2 else 0
                ),
                "focused_weeks": data.get("focused_weeks", 0),
                "key_weeks_avg": data.get("key_weeks_avg", 0),
            }

        most_completed = max(
            SPIRIT_CODES,
            key=lambda c: spirit_data[c]["completed"],
        )
        if spirit_data[most_completed]["completed"] > 0:
            name = SPIRIT_NAMES[most_completed]
            count = spirit_data[most_completed]["completed"]
            awards.append({
                "award_name": "最佳劳模",
                "spirit_code": most_completed,
                "reason": f"完成了{count}个任务",
                "emoji": "🏆",
            })

        highest_rate_code = max(
            [c for c in SPIRIT_CODES if spirit_data[c]["total"] > 0],
            key=lambda c: spirit_data[c]["completion_rate"],
            default=None,
        )
        if highest_rate_code and spirit_data[highest_rate_code]["completion_rate"] >= 0.9:
            rate = spirit_data[highest_rate_code]["completion_rate"]
            awards.append({
                "award_name": "全勤之星",
                "spirit_code": highest_rate_code,
                "reason": f"完成率高达{rate:.0%}",
                "emoji": "⭐",
            })

        stable_candidates = [
            c for c in SPIRIT_CODES if len(spirit_data[c]["weekly_scores"]) >= 2
        ]
        if stable_candidates:
            most_stable = min(stable_candidates, key=lambda c: spirit_data[c]["volatility"])
            if spirit_data[most_stable]["volatility"] < 10:
                awards.append({
                    "award_name": "稳如泰山",
                    "spirit_code": most_stable,
                    "reason": f"每周表现波动极小",
                    "emoji": "🪨",
                })

        if stable_candidates:
            most_volatile = max(stable_candidates, key=lambda c: spirit_data[c]["volatility"])
            if spirit_data[most_volatile]["volatility"] > 20:
                awards.append({
                    "award_name": "大起大落",
                    "spirit_code": most_volatile,
                    "reason": f"本月状态起伏明显",
                    "emoji": "🎢",
                })

        lowest_code = min(SPIRIT_CODES, key=lambda c: spirit_data[c]["avg_score"])
        if spirit_data[lowest_code]["avg_score"] < 50 and spirit_data[lowest_code]["total"] > 0:
            awards.append({
                "award_name": "最需关爱",
                "spirit_code": lowest_code,
                "reason": f"月均分仅{spirit_data[lowest_code]['avg_score']}",
                "emoji": "💝",
            })

        for code in SPIRIT_CODES:
            if spirit_data[code]["total"] == 0:
                awards.append({
                    "award_name": "被遗忘的",
                    "spirit_code": code,
                    "reason": f"整月没有安排任何任务",
                    "emoji": "😢",
                })

        for code in SPIRIT_CODES:
            sd = spirit_data[code]
            if (sd["focused_weeks"] >= KEY_FOCUSED_WEEKS_FOR_AWARD
                    and sd["key_weeks_avg"] >= KEY_AVG_SCORE_FOR_AWARD):
                awards.append({
                    "award_name": "聚焦达人",
                    "spirit_code": code,
                    "reason": (
                        f"{sd['focused_weeks']}周定为重点, 重点周均分 {sd['key_weeks_avg']}"
                    ),
                    "emoji": "🎯",
                })

        if theme_history:
            switches = theme_history.get("theme_switch_count", 0)
            month_avg = (
                sum(spirit_data[c]["avg_score"] for c in SPIRIT_CODES) / 5
            )
            if switches >= THEME_SWITCH_COUNT_FOR_AWARD and month_avg >= 70:
                dominant = theme_history.get("dominant_theme")
                awards.append({
                    "award_name": "节奏切换大师",
                    "spirit_code": None,
                    "reason": (
                        f"本月在 {switches} 种基调间切换, 月均分 {month_avg:.1f}, "
                        f"主基调: {dominant or '混合'}"
                    ),
                    "emoji": "🌊",
                })

        if theme_history:
            weeks_with_focus = theme_history.get("weeks_with_focus", 0)
            if weeks_with_focus == 0:
                min_avg = min(spirit_data[c]["avg_score"] for c in SPIRIT_CODES)
                avg_of_all = (
                    sum(spirit_data[c]["avg_score"] for c in SPIRIT_CODES) / 5
                )
                if (avg_of_all >= BALANCE_GUARDIAN_MIN_AVG
                        and min_avg >= BALANCE_GUARDIAN_MIN_AVG - 10):
                    awards.append({
                        "award_name": "平衡守护者",
                        "spirit_code": None,
                        "reason": (
                            f"整月未设重点, 五维均分 {avg_of_all:.1f}, "
                            f"最低也有 {min_avg:.1f}"
                        ),
                        "emoji": "⚖️",
                    })

        return awards

    THEME_LABELS_ZH = {
        "exam_prep":      "备考冲刺",
        "project_sprint": "项目冲刺",
        "recovery":       "休整恢复",
        "social":         "社交月",
        "creative":       "兴趣深耕",
        "balanced":       "平衡发展",
        "custom":         "自定义",
    }

    async def _generate_narrative(
        self,
        month: str,
        overall_score: float,
        fruit_info: dict,
        spirit_monthly: dict,
        awards: list[dict],
        theme_history: Optional[dict] = None,
    ) -> str:
        spirit_lines = []
        for code in SPIRIT_CODES:
            data = spirit_monthly.get(code, {})
            name = SPIRIT_NAMES.get(code, code)
            extras = []
            fw = data.get("focused_weeks", 0)
            if fw > 0:
                extras.append(f"重点 {fw} 周, 重点周均 {data.get('key_weeks_avg', 0)}")
            extra_str = f" [{', '.join(extras)}]" if extras else ""
            spirit_lines.append(
                f"- {name}: 均分{data.get('avg_score', 0)}, "
                f"趋势{data.get('trend', '?')}, "
                f"完成{data.get('completed_tasks', 0)}/{data.get('total_tasks', 0)}任务{extra_str}"
            )

        awards_str = ", ".join(
            f"{a['emoji']}{a['award_name']}"
            + (f"({SPIRIT_NAMES.get(a['spirit_code'], '')})"
               if a.get('spirit_code') else "")
            for a in awards[:6]
        ) if awards else "无"

        focus_block = self._format_theme_history_for_prompt(theme_history)

        external_prompt = load_prompt("monthly_fruit")

        if external_prompt:
            system = external_prompt
        else:
            system = f"""你是精灵日程系统的果实铸造师。
根据用户本月数据, 写一段果实叙述 (120-180字, 分两段)。

第一段: 果实描述 (RPG 道具风格), 融入本月最突出的行为特征。
第二段: 成长回顾 (温暖朋友视角), 一两句话回顾亮点。

果实品质: {fruit_info['name']}({fruit_info['rarity']})
要求:
- 不要列表
- 不超过 180 字
- 不要用"基调""权重""精灵"等系统词
- 直接输出文字, 不要 JSON"""

        user_prompt = f"""月份: {month}
月均分: {overall_score}, 果实: {fruit_info['name']}{fruit_info['emoji']}({fruit_info['rarity']})
{focus_block}
各方向表现:
{chr(10).join(spirit_lines)}

获得奖项: {awards_str}"""

        result = await llm_client.complete(
            system=system,
            user=user_prompt,
            max_tokens=350,
            purpose="monthly_narrative",
        )

        if result and not result.startswith("[FALLBACK]"):
            return result.strip().strip('"')

        return self._fallback_narrative(
            month, overall_score, fruit_info, spirit_monthly,
            theme_history=theme_history,
        )

    @classmethod
    def _format_theme_history_for_prompt(
        cls, theme_history: Optional[dict]
    ) -> str:
        if not theme_history:
            return ""

        counts = theme_history.get("theme_counts") or {}
        if not counts:
            return ""

        weeks_with = theme_history.get("weeks_with_focus", 0)
        if weeks_with == 0:
            return "\n本月基调: 整月未设重点 (平衡发展)\n"

        parts = []
        for theme_key, count in sorted(counts.items(), key=lambda kv: -kv[1]):
            if theme_key == "(none)":
                if count > 0:
                    parts.append(f"未设基调 {count} 周")
                continue
            label = cls.THEME_LABELS_ZH.get(theme_key, theme_key)
            parts.append(f"{label} {count} 周")

        return f"\n本月基调: {', '.join(parts)}\n"

    @classmethod
    def _fallback_narrative(
        cls,
        month: str, overall: float, fruit_info: dict, spirit_monthly: dict,
        theme_history: Optional[dict] = None,
    ) -> str:
        best_code = max(
            SPIRIT_CODES,
            key=lambda c: spirit_monthly.get(c, {}).get("avg_score", 0),
        )
        worst_code = min(
            SPIRIT_CODES,
            key=lambda c: spirit_monthly.get(c, {}).get("avg_score", 0),
        )
        best_name = SPIRIT_NAMES.get(best_code, "")
        worst_name = SPIRIT_NAMES.get(worst_code, "")
        best_avg = spirit_monthly.get(best_code, {}).get("avg_score", 0)
        worst_avg = spirit_monthly.get(worst_code, {}).get("avg_score", 0)

        theme_prefix = ""
        if theme_history:
            dominant = theme_history.get("dominant_theme")
            weeks_with = theme_history.get("weeks_with_focus", 0)
            if dominant:
                label = cls.THEME_LABELS_ZH.get(dominant, dominant)
                theme_prefix = f"这是一个以{label}为主线的月份。"
            elif weeks_with == 0:
                theme_prefix = "这是一个没有刻意取舍的平衡月。"

        tail = (
            f"而{worst_name}需要更多关注 (均分{worst_avg})。"
            if worst_avg < 60 else "整体表现不错!"
        )

        return (
            f"{theme_prefix}"
            f"{month}的月度果实是{fruit_info['name']}{fruit_info['emoji']}。"
            f"{best_name}表现最佳 (均分{best_avg}), {tail}"
            f"下个月继续加油!"
        )

    @staticmethod
    def _get_month_week_starts(month: str) -> list[date]:
        year, mon = int(month[:4]), int(month[5:7])
        first_day = date(year, mon, 1)
        if mon == 12:
            last_day = date(year + 1, 1, 1) - timedelta(days=1)
        else:
            last_day = date(year, mon + 1, 1) - timedelta(days=1)

        week_starts = []
        first_monday = first_day - timedelta(days=first_day.weekday())
        current = first_monday
        while current <= last_day:
            week_end = current + timedelta(days=6)
            if week_end >= first_day and current <= last_day:
                week_starts.append(current)
            current += timedelta(days=7)

        return week_starts

    async def _create_empty_fruit(
        self, user_id: uuid.UUID, month: str
    ) -> MonthlyFruit:
        fruit_info = FRUIT_TYPES[-1]
        spirit_monthly = {c: {
            "avg_score": 0, "trend": "stable",
            "total_tasks": 0, "completed_tasks": 0,
            "best_week_score": 0, "worst_week_score": 0,
            "focused_weeks": 0, "key_weeks_avg": 0,
        } for c in SPIRIT_CODES}
        spirit_monthly["_meta"] = {
            "theme_history": {
                "themes_per_week": [],
                "theme_counts": {},
                "dominant_theme": None,
                "weeks_with_focus": 0,
                "weeks_without_focus": 0,
                "theme_switch_count": 0,
            },
            "week_focus_intensities": [],
        }
        fruit = MonthlyFruit(
            user_id=user_id,
            month=month,
            fruit_type=fruit_info["fruit"],
            fruit_name=fruit_info["name"],
            fruit_rarity=fruit_info["rarity"],
            overall_score=0,
            weekly_scores=[],
            spirit_monthly=spirit_monthly,
            best_spirit=None,
            weakest_spirit=None,
            awards=[],
            monthly_narrative="这个月还没有数据哦, 下个月开始记录你的生活吧! 🌱",
        )
        self.db.add(fruit)
        await self.db.flush()
        return fruit

    async def generate_fruit_image(
        self,
        month: str,
        overall_score: float,
        fruit_info: dict,
        spirit_monthly: dict,
        best_spirit: str,
        awards: list[dict],
        user_id: uuid.UUID,
        theme_history: Optional[dict] = None,
    ) -> str:
        external_prompt = load_prompt("fruit_image")

        if not external_prompt:
            return await self._fallback_fruit_image()

        score_desc = []
        for code in SPIRIT_CODES:
            data = spirit_monthly.get(code, {})
            name = SPIRIT_NAMES.get(code, code)
            avg_score = data.get("avg_score", 0)
            score_desc.append(f"{name}: {avg_score}分")

        best_spirit_name = SPIRIT_NAMES.get(best_spirit, best_spirit) if best_spirit else "无"

        focus_block = ""
        if theme_history:
            dominant = theme_history.get("dominant_theme")
            weeks_with = theme_history.get("weeks_with_focus", 0)
            counts = theme_history.get("theme_counts") or {}
            if weeks_with == 0:
                focus_block = "\n【月度基调】整月未设重点 (平衡月)"
            elif dominant:
                label = self.THEME_LABELS_ZH.get(dominant, dominant)
                count = counts.get(dominant, 0)
                non_none = sum(v for k, v in counts.items() if k != "(none)")
                if non_none > count:
                    focus_block = (
                        f"\n【月度基调】以{label}为主 ({count}周), 但存在切换 (混合月)"
                    )
                else:
                    focus_block = f"\n【月度基调】{label} {count}周"

        user_data = (
            f"【月度果实生成】\n"
            f"- 月份: {month}\n"
            f"- 月均分: {overall_score}\n"
            f"- 果实类型: {fruit_info['name']}({fruit_info['rarity']})\n"
            f"- 最佳维度: {best_spirit_name}"
            f"{focus_block}\n"
            f"\n【五维度得分】\n" + "\n".join(score_desc)
        )

        full_prompt = external_prompt + "\n\n" + user_data

        result = await image_client.generate(
            prompt=full_prompt,
            user_id=str(user_id),
            purpose="fruit_image",
        )

        if result and not result.startswith("[FALLBACK]"):
            return result

        return await self._fallback_fruit_image()

    @staticmethod
    async def _fallback_fruit_image() -> str:
        return "https://neeko-copilot.bytedance.net/api/text_to_image?prompt=minimalist%20magical%20fruit%20illustration%20cute%20dreamy%20healing%20style&image_size=square"
