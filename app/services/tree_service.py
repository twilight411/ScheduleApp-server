"""
生命树服务 — 将精灵周得分映射为可视化的树数据

Sprint C: 新增 AI 生成树叙述（periph.txt #8 风格）
  - 极简治愈插画风格的文字描述
  - 果实/叶片/根系根据行为个性化
  - 统一生态系统比喻

树结构（对应五精灵）:
  ✨ 营养精灵 — 树冠顶部 (top)
  💡 光精灵   — 左主枝 (left)
  💨 空气精灵 — 右主枝 (right)
  💧 水精灵   — 树干中段 (middle)
  🌱 土壤精灵 — 根部 (bottom)
"""
import uuid
from datetime import date, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.score import SpiritWeeklyScore
from app.services.scoring_service import ScoringService, _score_to_level
from app.ai.llm_client import llm_client
from app.ai.image_client import image_client
from app.utils.prompt_loader import load_prompt

import structlog

logger = structlog.get_logger()

# ====================================================================
#  常量映射
# ====================================================================

SPIRIT_META = {
    "light":     {"name": "光精灵",   "emoji": "💡", "position": "left"},
    "water":     {"name": "水精灵",   "emoji": "💧", "position": "middle"},
    "soil":      {"name": "土壤精灵", "emoji": "🌱", "position": "bottom"},
    "air":       {"name": "空气精灵", "emoji": "💨", "position": "right"},
    "nutrition": {"name": "营养精灵", "emoji": "✨", "position": "top"},
}

# 颜色映射（前端直接用 hex 值）
BRANCH_COLORS = {
    "light": {
        "flourishing": "#FFD700",
        "good":        "#FFC107",
        "average":     "#FFE082",
        "poor":        "#FFF9C4",
        "withered":    "#9E9E9E",
    },
    "water": {
        "flourishing": "#1E88E5",
        "good":        "#42A5F5",
        "average":     "#90CAF9",
        "poor":        "#BBDEFB",
        "withered":    "#9E9E9E",
    },
    "soil": {
        "flourishing": "#2E7D32",
        "good":        "#43A047",
        "average":     "#A5D6A7",
        "poor":        "#C8E6C9",
        "withered":    "#9E9E9E",
    },
    "air": {
        "flourishing": "#7E57C2",
        "good":        "#9575CD",
        "average":     "#B39DDB",
        "poor":        "#D1C4E9",
        "withered":    "#9E9E9E",
    },
    "nutrition": {
        "flourishing": "#FF7043",
        "good":        "#FF8A65",
        "average":     "#FFAB91",
        "poor":        "#FFCCBC",
        "withered":    "#9E9E9E",
    },
}

# 整棵树的健康度映射
TREE_HEALTH_MAP = [
    (85, "vibrant"),      # 生机勃勃
    (65, "healthy"),      # 健康
    (45, "tired"),        # 疲惫
    (25, "struggling"),   # 挣扎
    (0,  "withering"),    # 枯萎中
]

# 季节标签（基于趋势）
SEASON_LABELS = {
    "rising":  "成长期",
    "stable":  "稳定期",
    "falling": "调整期",
    "new":     "起步期",
}


class TreeService:

    def __init__(self, db: AsyncSession):
        self.db = db
        self.scoring_svc = ScoringService(db)

    # ========================================
    #  构建生命树数据
    # ========================================

    async def build_tree_data(
        self,
        user_id: uuid.UUID,
        week_start: date,
    ) -> dict:
        """
        构建一周的生命树数据。
        如果该周尚未打分，先触发打分。
        """
        # 获取或计算当周得分
        scores = await self.scoring_svc.get_week_scores(user_id, week_start)
        if not scores:
            scores = await self.scoring_svc.calculate_all_spirits(user_id, week_start)

        # 构建树枝
        branches = []
        for score in scores:
            meta = SPIRIT_META.get(score.spirit_code, {})
            level = score.level
            color = BRANCH_COLORS.get(
                score.spirit_code, {}
            ).get(level, "#9E9E9E")

            branches.append({
                "spirit_code": score.spirit_code,
                "spirit_name": meta.get("name", score.spirit_code),
                "spirit_emoji": meta.get("emoji", ""),
                "position": meta.get("position", "left"),
                "score": score.score,
                "level": level,
                "color": color,
                "intensity": score.intensity_at_scoring,
                "comment": score.spirit_comment or "",
            })

        # 总分
        overall = await self.scoring_svc.get_overall_score(user_id, week_start)
        overall_level = _score_to_level(overall)
        tree_health = self._get_tree_health(overall)

        # 季节标签（对比上周趋势）
        season = await self._get_season_label(user_id, week_start, overall)

        # 最弱精灵
        weakest = min(branches, key=lambda b: b["score"]) if branches else None
        weakest_suggestion = self._get_weakest_suggestion(weakest) if weakest else ""

        # 一句话总评
        summary_line = self._build_summary_line(branches, overall)

        # Sprint C: AI 生成树叙述（periph.txt #8 风格）
        tree_narrative = await self._generate_tree_narrative(
            branches, overall, tree_health, season
        )

        return {
            "week_start": str(week_start),
            "overall_score": overall,
            "overall_level": overall_level,
            "branches": branches,
            "tree_health": tree_health,
            "season_label": season,
            "weekly_summary_line": summary_line,
            "weakest_spirit": weakest["spirit_code"] if weakest else None,
            "weakest_suggestion": weakest_suggestion,
            "tree_narrative": tree_narrative,
        }

    # ========================================
    #  AI 生成树叙述 (Sprint C — periph.txt #8)
    # ========================================

    async def _generate_tree_narrative(
        self,
        branches: list[dict],
        overall: float,
        tree_health: str,
        season: str,
    ) -> str:
        """
        生成生命树的文字描述 — 极简治愈风格。

        风格要求 (periph.txt #8):
          - 像在描述一幅治愈系插画
          - 用植物生长的比喻描述各精灵状态
          - 50-100 字，温暖简洁
          - 每棵树都是独一无二的
        """
        # 尝试加载外部 prompt
        external_prompt = load_prompt("tree_narrative")

        branch_desc = []
        for b in branches:
            name = b.get("spirit_name", "")
            score = b.get("score", 0)
            level = b.get("level", "average")
            position = b.get("position", "")
            branch_desc.append(f"{name}({position}): {score}分, {level}")

        if external_prompt:
            system = external_prompt
        else:
            system = """你是精灵日程系统的生命树画师。请用极简治愈插画风格描述这棵生命树。

## 风格要求
- 像在描述一幅水彩画，温暖柔和
- 用植物生长的比喻：嫩芽、繁花、果实、枯叶、根系
- 50-100 字，一小段话，不要分行或列表
- 每棵树独一无二：根据各枝干（精灵）状态描绘不同画面
- 树的部位对应：根部=土壤精灵, 树干=水精灵, 左枝=光精灵, 右枝=空气精灵, 树冠=营养精灵

直接输出描述文字，不要 JSON 包装。"""

        health_map = {
            "vibrant": "生机勃勃", "healthy": "健康舒展",
            "tired": "有些疲倦", "struggling": "略显吃力",
            "withering": "需要呵护",
        }
        health_zh = health_map.get(tree_health, "平静")

        user_prompt = (
            f"生命树状态: {health_zh}, 季节: {season}, 总分: {overall}\n"
            f"各枝干:\n" + "\n".join(branch_desc)
        )

        result = await llm_client.complete(
            system=system,
            user=user_prompt,
            max_tokens=200,
            purpose="tree_narrative",
        )

        if result and not result.startswith("[FALLBACK]"):
            return result.strip().strip('"')

        # Fallback: 根据分数生成简单描述
        return self._fallback_tree_narrative(overall, tree_health, branches)

    @staticmethod
    def _fallback_tree_narrative(
        overall: float, health: str, branches: list[dict]
    ) -> str:
        """降级树叙述"""
        best = max(branches, key=lambda b: b["score"]) if branches else None
        worst = min(branches, key=lambda b: b["score"]) if branches else None

        if overall >= 85:
            return (
                f"你的生命树正值盛夏，枝繁叶茂。"
                f"{best['spirit_name'] if best else ''}那一枝开满了花，"
                f"整棵树在阳光下微微摇曳。"
            )
        elif overall >= 65:
            base = f"生命树的根基扎实，大部分枝干都在稳定生长。"
            if worst and worst["score"] < 50:
                return base + f"{worst['spirit_name']}那枝叶子有些稀疏，多浇浇水吧。"
            return base + "继续照料，花期不远。"
        elif overall >= 45:
            return (
                f"生命树在微风中轻轻摇晃，有些枝条正在蓄力。"
                f"给它一些时间和耐心，新芽会冒出来的。"
            )
        else:
            return "生命树正在经历一段安静的时光，根部在泥土中默默积蓄。每一刻休息都是为了下一次生长。"

    # ========================================
    #  历史趋势
    # ========================================

    async def get_tree_history(
        self,
        user_id: uuid.UUID,
        months: int = 3,
    ) -> list[dict]:
        """获取最近 N 个月的周树数据（用于趋势图）"""
        cutoff = date.today() - timedelta(weeks=months * 4 + 1)
        result = await self.db.execute(
            select(SpiritWeeklyScore).where(
                SpiritWeeklyScore.user_id == user_id,
                SpiritWeeklyScore.week_start >= cutoff,
            ).order_by(SpiritWeeklyScore.week_start)
        )
        all_scores = list(result.scalars().all())

        # 按周分组
        by_week: dict[date, list] = {}
        for s in all_scores:
            by_week.setdefault(s.week_start, []).append(s)

        history = []
        for ws in sorted(by_week.keys()):
            week_scores = by_week[ws]
            total_weight = 0
            weighted_sum = 0
            spirit_data = {}
            for s in week_scores:
                w = max(1, s.intensity_at_scoring)
                weighted_sum += s.score * w
                total_weight += w
                spirit_data[s.spirit_code] = {
                    "score": s.score,
                    "level": s.level,
                }

            overall = round(weighted_sum / total_weight, 1) if total_weight else 0
            history.append({
                "week_start": str(ws),
                "overall_score": overall,
                "overall_level": _score_to_level(overall),
                "spirits": spirit_data,
            })

        return history

    # ========================================
    #  辅助方法
    # ========================================

    @staticmethod
    def _get_tree_health(overall: float) -> str:
        for threshold, health in TREE_HEALTH_MAP:
            if overall >= threshold:
                return health
        return "withering"

    async def _get_season_label(
        self, user_id: uuid.UUID, week_start: date, current_overall: float
    ) -> str:
        """根据趋势判断季节标签"""
        last_scores = await self.scoring_svc.get_last_week_scores(user_id, week_start)
        if not last_scores:
            return SEASON_LABELS["new"]

        last_total = 0
        last_weight = 0
        for s in last_scores:
            w = max(1, s.intensity_at_scoring)
            last_total += s.score * w
            last_weight += w
        last_overall = last_total / last_weight if last_weight else 0

        diff = current_overall - last_overall
        if diff > 5:
            return SEASON_LABELS["rising"]
        elif diff < -5:
            return SEASON_LABELS["falling"]
        return SEASON_LABELS["stable"]

    @staticmethod
    def _get_weakest_suggestion(weakest: dict) -> str:
        """针对最弱精灵给出简单建议"""
        code = weakest.get("spirit_code", "")
        score = weakest.get("score", 0)

        suggestions = {
            "light": "下周试着安排更多学习/工作任务，并按时完成。",
            "water": "别忘了给自己安排一些放松时间，劳逸结合很重要！",
            "soil": "每天至少散步30分钟，运动是一切的基础。",
            "air": "约朋友出来聊聊天吧，社交也是生活的重要部分。",
            "nutrition": "尝试花点时间在你的兴趣爱好上，探索让人更快乐。",
        }
        if score < 30:
            return suggestions.get(code, "多关注这个领域吧！")
        return suggestions.get(code, "")

    @staticmethod
    def _build_summary_line(branches: list[dict], overall: float) -> str:
        """构建一句话总评"""
        if not branches:
            return "暂无数据"

        best = max(branches, key=lambda b: b["score"])
        worst = min(branches, key=lambda b: b["score"])

        if overall >= 85:
            return f"出色的一周！{best['spirit_name']}表现尤为突出 ✨"
        elif overall >= 65:
            return (
                f"{best['spirit_name']}表现不错"
                f"{'，但' + worst['spirit_name'] + '需要更多关注' if worst['score'] < 50 else '，继续保持！'}"
            )
        elif overall >= 45:
            return f"平稳的一周，{worst['spirit_name']}有些被忽略了，下周多关注哦。"
        else:
            return f"这周比较艰难，{worst['spirit_name']}尤其需要关注。打起精神来！"

    # ========================================
    #  AI 图像生成
    # ========================================

    async def generate_tree_image(
        self,
        branches: list[dict],
        overall: float,
        tree_health: str,
        season: str,
        user_id: uuid.UUID,
    ) -> str:
        """
        根据用户本周五个维度的得分生成周生命树图像。
        
        调用外部生图大模型API，使用 tree_image.md 中的prompt模板。
        树的形态取决于本周用户五个维度的得分。
        """
        external_prompt = load_prompt("tree_image")
        
        if not external_prompt:
            logger.warning("tree_image_prompt_not_found")
            return await self._fallback_tree_image()

        score_desc = []
        for b in branches:
            name = b.get("spirit_name", "")
            score = b.get("score", 0)
            score_desc.append(f"{name}: {score}分")

        health_map = {
            "vibrant": "生机勃勃", "healthy": "健康舒展",
            "tired": "有些疲倦", "struggling": "略显吃力",
            "withering": "需要呵护",
        }
        health_zh = health_map.get(tree_health, "平静")

        user_data = (
            f"【生命树状态】\n"
            f"- 整体健康度: {health_zh}\n"
            f"- 季节: {season}\n"
            f"- 总分: {overall}\n"
            f"\n【五维度得分】\n" + "\n".join(score_desc)
        )

        full_prompt = external_prompt + "\n\n" + user_data

        result = await image_client.generate(
            prompt=full_prompt,
            user_id=str(user_id),
            purpose="tree_image",
        )

        if result and not result.startswith("[FALLBACK]"):
            return result

        return await self._fallback_tree_image()

    @staticmethod
    async def _fallback_tree_image() -> str:
        """图像生成不可用时的降级方案"""
        return "https://neeko-copilot.bytedance.net/api/text_to_image?prompt=minimalist%20healing%20tree%20illustration%20cute%20dreamy%20style&image_size=square"