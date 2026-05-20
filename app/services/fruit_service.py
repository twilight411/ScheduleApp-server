"""
月度果实服务 — 聚合 4-5 周得分 → 果实类型 + 趣味奖项 + AI 月度叙述

Sprint C: 升级 AI 月度叙述 Prompt（periph.txt #9 风格）
  - 果实描述个性化，基于具体行为
  - 像游戏 RPG 获得道具时的描述
  - 每个果实独一无二，带有"成长记忆"

果实类型体系:
  90-100 → golden_apple  (金苹果, legendary)
  80-89  → crystal_grape (水晶葡萄, epic)
  65-79  → sunshine_orange (阳光橙, rare)
  50-64  → green_apple   (青苹果, common)
  0-49   → seed          (种子, common)

趣味奖项池:
  最佳劳模、全勤之星、逆袭王者、稳如泰山、
  最需关爱、被遗忘的、大起大落
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

# ====================================================================
#  果实类型映射
# ====================================================================

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

# ====================================================================
#  趣味奖项池
# ====================================================================

AWARD_POOL = [
    {"name": "最佳劳模", "condition": "completed_most", "emoji": "🏆"},
    {"name": "全勤之星", "condition": "highest_completion_rate", "emoji": "⭐"},
    {"name": "逆袭王者", "condition": "biggest_improvement", "emoji": "📈"},
    {"name": "稳如泰山", "condition": "most_stable", "emoji": "🪨"},
    {"name": "最需关爱", "condition": "lowest_score", "emoji": "💝"},
    {"name": "被遗忘的", "condition": "zero_tasks", "emoji": "😢"},
    {"name": "大起大落", "condition": "most_volatile", "emoji": "🎢"},
]


def get_fruit_type(score: float) -> dict:
    """根据月均分获取果实类型"""
    for ft in FRUIT_TYPES:
        if ft["min"] <= score <= ft["max"]:
            return ft
    return FRUIT_TYPES[-1]


class FruitService:

    def __init__(self, db: AsyncSession):
        self.db = db

    # ========================================
    #  生成月度果实
    # ========================================

    async def generate_monthly_fruit(
        self,
        user_id: uuid.UUID,
        month: str,
    ) -> MonthlyFruit:
        """
        生成月度果实。

        Args:
            month: "YYYY-MM" 格式
        """
        # 幂等
        existing = await self.get_fruit(user_id, month)
        if existing:
            return existing

        # 1. 获取该月所有周的精灵得分
        week_starts = self._get_month_week_starts(month)
        all_scores = await self._load_month_scores(user_id, week_starts)

        if not all_scores:
            logger.info("no_scores_for_month", user_id=str(user_id), month=month)
            # 无数据也生成一个种子果实
            return await self._create_empty_fruit(user_id, month)

        # 2. 聚合各精灵月度数据
        spirit_monthly = self._aggregate_spirit_monthly(all_scores, week_starts)

        # 3. 计算月均分 + 周分趋势
        weekly_overall_scores = self._calc_weekly_overalls(all_scores, week_starts)
        overall_score = (
            sum(weekly_overall_scores) / len(weekly_overall_scores)
            if weekly_overall_scores else 0
        )
        overall_score = round(overall_score, 1)

        # 4. 确定果实类型
        fruit_info = get_fruit_type(overall_score)

        # 5. 最佳/最弱精灵
        spirit_avgs = {
            code: data.get("avg_score", 0)
            for code, data in spirit_monthly.items()
        }
        best_spirit = max(spirit_avgs, key=spirit_avgs.get) if spirit_avgs else None
        weakest_spirit = min(spirit_avgs, key=spirit_avgs.get) if spirit_avgs else None

        # 6. 趣味奖项
        awards = self._calculate_awards(spirit_monthly, all_scores, week_starts)

        # 7. AI 月度叙述
        narrative = await self._generate_narrative(
            month, overall_score, fruit_info, spirit_monthly, awards
        )

        # 8. 存储
        fruit = MonthlyFruit(
            user_id=user_id,
            month=month,
            fruit_type=fruit_info["fruit"],
            fruit_name=fruit_info["name"],
            fruit_rarity=fruit_info["rarity"],
            overall_score=overall_score,
            weekly_scores=weekly_overall_scores,
            spirit_monthly=spirit_monthly,
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

    # ========================================
    #  查询
    # ========================================

    async def get_fruit(
        self, user_id: uuid.UUID, month: str
    ) -> Optional[MonthlyFruit]:
        result = await self.db.execute(
            select(MonthlyFruit).where(
                MonthlyFruit.user_id == user_id,
                MonthlyFruit.month == month,
            )
        )
        return result.scalar_one_or_none()

    async def get_latest_fruit(
        self, user_id: uuid.UUID
    ) -> Optional[MonthlyFruit]:
        result = await self.db.execute(
            select(MonthlyFruit)
            .where(MonthlyFruit.user_id == user_id)
            .order_by(desc(MonthlyFruit.month))
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def get_collection(
        self, user_id: uuid.UUID
    ) -> list[MonthlyFruit]:
        """获取用户所有历史果实（果实墙）"""
        result = await self.db.execute(
            select(MonthlyFruit)
            .where(MonthlyFruit.user_id == user_id)
            .order_by(desc(MonthlyFruit.month))
        )
        return list(result.scalars().all())

    # ========================================
    #  数据聚合
    # ========================================

    async def _load_month_scores(
        self,
        user_id: uuid.UUID,
        week_starts: list[date],
    ) -> list[SpiritWeeklyScore]:
        """加载该月所有周的精灵得分"""
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
        self,
        all_scores: list[SpiritWeeklyScore],
        week_starts: list[date],
    ) -> dict:
        """聚合各精灵的月度数据"""
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
                }
                continue

            scores_vals = [s.score for s in spirit_scores]
            total_tasks = sum(s.task_stats.get("planned", 0) for s in spirit_scores)
            completed_tasks = sum(s.task_stats.get("completed", 0) for s in spirit_scores)

            # 趋势：对比前半月和后半月
            mid = len(scores_vals) // 2
            if mid > 0 and len(scores_vals) > 1:
                first_half = sum(scores_vals[:mid]) / mid
                second_half = sum(scores_vals[mid:]) / (len(scores_vals) - mid)
                diff = second_half - first_half
                trend = "up" if diff > 5 else ("down" if diff < -5 else "stable")
            else:
                trend = "stable"

            spirit_monthly[code] = {
                "avg_score": round(sum(scores_vals) / len(scores_vals), 1),
                "trend": trend,
                "total_tasks": total_tasks,
                "completed_tasks": completed_tasks,
                "best_week_score": round(max(scores_vals), 1),
                "worst_week_score": round(min(scores_vals), 1),
            }

        return spirit_monthly

    def _calc_weekly_overalls(
        self,
        all_scores: list[SpiritWeeklyScore],
        week_starts: list[date],
    ) -> list[float]:
        """计算每周的加权总分"""
        overalls = []
        for ws in week_starts:
            week_scores = [s for s in all_scores if s.week_start == ws]
            if not week_scores:
                continue

            total_w = 0
            weighted = 0
            for s in week_scores:
                w = max(1, s.intensity_at_scoring)
                weighted += s.score * w
                total_w += w

            overalls.append(round(weighted / total_w, 1) if total_w else 0)

        return overalls

    # ========================================
    #  趣味奖项
    # ========================================

    def _calculate_awards(
        self,
        spirit_monthly: dict,
        all_scores: list[SpiritWeeklyScore],
        week_starts: list[date],
    ) -> list[dict]:
        """计算趣味奖项"""
        awards = []

        # 每个精灵的汇总指标
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
            }

        # 最佳劳模 — 完成任务数最多
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

        # 全勤之星 — 完成率最高
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

        # 稳如泰山 — 周分数波动最小（至少有2周数据）
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

        # 大起大落 — 波动最大
        if stable_candidates:
            most_volatile = max(stable_candidates, key=lambda c: spirit_data[c]["volatility"])
            if spirit_data[most_volatile]["volatility"] > 20:
                awards.append({
                    "award_name": "大起大落",
                    "spirit_code": most_volatile,
                    "reason": f"本月状态起伏明显",
                    "emoji": "🎢",
                })

        # 最需关爱 — 月均分最低
        lowest_code = min(SPIRIT_CODES, key=lambda c: spirit_data[c]["avg_score"])
        if spirit_data[lowest_code]["avg_score"] < 50 and spirit_data[lowest_code]["total"] > 0:
            awards.append({
                "award_name": "最需关爱",
                "spirit_code": lowest_code,
                "reason": f"月均分仅{spirit_data[lowest_code]['avg_score']}",
                "emoji": "💝",
            })

        # 被遗忘的 — 整月任务数为 0
        for code in SPIRIT_CODES:
            if spirit_data[code]["total"] == 0:
                awards.append({
                    "award_name": "被遗忘的",
                    "spirit_code": code,
                    "reason": f"整月没有安排任何任务",
                    "emoji": "😢",
                })

        return awards

    # ========================================
    #  AI 叙述
    # ========================================

    async def _generate_narrative(
        self,
        month: str,
        overall_score: float,
        fruit_info: dict,
        spirit_monthly: dict,
        awards: list[dict],
    ) -> str:
        """
        LLM 生成月度叙述 — Sprint C 升级版

        Prompt 策略 (periph.txt #9):
          - RPG 道具获得感：像游戏里拿到稀有道具的描述
          - 果实带有"成长记忆"：基于用户本月具体行为
          - 120-180 字，分两段：果实描述 + 成长回顾
          - 个性化：根据最佳/最弱精灵和奖项定制
        """
        spirit_lines = []
        for code in SPIRIT_CODES:
            data = spirit_monthly.get(code, {})
            name = SPIRIT_NAMES.get(code, code)
            spirit_lines.append(
                f"- {name}: 均分{data.get('avg_score', 0)}, "
                f"趋势{data.get('trend', '?')}, "
                f"完成{data.get('completed_tasks', 0)}/{data.get('total_tasks', 0)}任务"
            )

        awards_str = ", ".join(
            f"{a['emoji']}{a['award_name']}({SPIRIT_NAMES.get(a['spirit_code'], '')})"
            for a in awards[:4]
        ) if awards else "无"

        external_prompt = load_prompt("monthly_fruit")

        if external_prompt:
            system = external_prompt
        else:
            system = f"""你是精灵日程系统的果实铸造师。每个月，用户的生命树会根据表现结出一颗独特的果实。

## 你的任务
根据用户本月数据，写一段果实叙述（120-180字，分两段）。

## 第一段：果实描述（RPG 道具风格）
- 像游戏里获得稀有道具时的描述文字
- 描述果实的外观、质地、光泽
- 融入用户最突出的行为特征（如"表面刻着每一次准时完成的细纹"）
- 果实品质: {fruit_info['name']}({fruit_info['rarity']})

## 第二段：成长回顾（温暖朋友视角）
- 用一两句话回顾这个月的亮点
- 如果有明显短板，温和地提一句
- 用"你"而不是"该用户"

## 限制
- 不要用列表或条列格式
- 不要超过 180 字
- 直接输出文字，不要 JSON"""

        user_prompt = f"""月份：{month}
月均分：{overall_score}，果实：{fruit_info['name']}{fruit_info['emoji']}({fruit_info['rarity']})

各精灵表现：
{chr(10).join(spirit_lines)}

获得奖项：{awards_str}"""

        result = await llm_client.complete(
            system=system,
            user=user_prompt,
            max_tokens=350,
            purpose="monthly_narrative",
        )

        if result and not result.startswith("[FALLBACK]"):
            return result.strip().strip('"')

        return self._fallback_narrative(
            month, overall_score, fruit_info, spirit_monthly
        )

    @staticmethod
    def _fallback_narrative(
        month: str, overall: float, fruit_info: dict, spirit_monthly: dict
    ) -> str:
        """降级叙述"""
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

        return (
            f"{month}的月度果实是{fruit_info['name']}{fruit_info['emoji']}。"
            f"{best_name}表现最佳（均分{best_avg}），"
            f"{'而' + worst_name + '需要更多关注（均分' + str(worst_avg) + '）。' if worst_avg < 60 else '整体表现不错！'}"
            f"下个月继续加油！"
        )

    # ========================================
    #  辅助
    # ========================================

    @staticmethod
    def _get_month_week_starts(month: str) -> list[date]:
        """获取某月包含的所有周一日期"""
        year, mon = int(month[:4]), int(month[5:7])
        first_day = date(year, mon, 1)
        if mon == 12:
            last_day = date(year + 1, 1, 1) - timedelta(days=1)
        else:
            last_day = date(year, mon + 1, 1) - timedelta(days=1)

        # 找到该月涉及的所有周一
        # 一个周只要有任何一天落在该月就算
        week_starts = []
        # 从该月第一天所在周的周一开始
        first_monday = first_day - timedelta(days=first_day.weekday())
        current = first_monday
        while current <= last_day:
            week_end = current + timedelta(days=6)
            # 该周与该月有交集
            if week_end >= first_day and current <= last_day:
                week_starts.append(current)
            current += timedelta(days=7)

        return week_starts

    async def _create_empty_fruit(
        self, user_id: uuid.UUID, month: str
    ) -> MonthlyFruit:
        """无数据时生成种子果实"""
        fruit_info = FRUIT_TYPES[-1]  # seed
        fruit = MonthlyFruit(
            user_id=user_id,
            month=month,
            fruit_type=fruit_info["fruit"],
            fruit_name=fruit_info["name"],
            fruit_rarity=fruit_info["rarity"],
            overall_score=0,
            weekly_scores=[],
            spirit_monthly={c: {
                "avg_score": 0, "trend": "stable",
                "total_tasks": 0, "completed_tasks": 0,
                "best_week_score": 0, "worst_week_score": 0,
            } for c in SPIRIT_CODES},
            best_spirit=None,
            weakest_spirit=None,
            awards=[],
            monthly_narrative="这个月还没有数据哦，下个月开始记录你的生活吧！🌱",
        )
        self.db.add(fruit)
        await self.db.flush()
        return fruit

    # ========================================
    #  AI 图像生成
    # ========================================

    async def generate_fruit_image(
        self,
        month: str,
        overall_score: float,
        fruit_info: dict,
        spirit_monthly: dict,
        best_spirit: str,
        awards: list[dict],
        user_id: uuid.UUID,
    ) -> str:
        """
        根据用户本月五个维度的得分生成月度果实图像。
        
        调用外部生图大模型API，使用 fruit_image.md 中的prompt模板。
        果实的形态取决于本月用户五个维度的得分，特别是最佳维度。
        """
        external_prompt = load_prompt("fruit_image")
        
        if not external_prompt:
            logger.warning("fruit_image_prompt_not_found")
            return await self._fallback_fruit_image()

        score_desc = []
        for code in SPIRIT_CODES:
            data = spirit_monthly.get(code, {})
            name = SPIRIT_NAMES.get(code, code)
            avg_score = data.get("avg_score", 0)
            score_desc.append(f"{name}: {avg_score}分")

        best_spirit_name = SPIRIT_NAMES.get(best_spirit, best_spirit) if best_spirit else "无"
        
        user_data = (
            f"【月度果实生成】\n"
            f"- 月份: {month}\n"
            f"- 月均分: {overall_score}\n"
            f"- 果实类型: {fruit_info['name']}({fruit_info['rarity']})\n"
            f"- 最佳维度: {best_spirit_name}\n"
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
        """图像生成不可用时的降级方案"""
        return "https://neeko-copilot.bytedance.net/api/text_to_image?prompt=minimalist%20magical%20fruit%20illustration%20cute%20dreamy%20healing%20style&image_size=square"