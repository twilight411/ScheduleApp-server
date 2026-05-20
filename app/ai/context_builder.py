"""
Context Builder — Context Engineering 模块

职责:
  - 为各种 LLM 调用场景组装丰富的上下文
  - 从用户画像、行为统计、日程状态、精灵强度中提取关键信息
  - 为协商引擎提供冲突上下文、各精灵的利益诉求背景
  - 控制 Context 大小，避免 token 浪费

设计原则:
  - 每个 build_* 方法返回一个结构化 dict，调用方按需序列化为 prompt 片段
  - 信息分层：核心信息（必须包含）> 辅助信息（空间允许时包含）> 背景信息（可省略）
"""
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.user import User
from app.models.profile import UserProfile, SpiritIntensity
from app.models.task import Task, SubTask
from app.models.conversation import Conversation

import structlog

logger = structlog.get_logger()

SPIRIT_NAMES = {
    "light": "光精灵", "water": "水精灵", "soil": "土壤精灵",
    "air": "空气精灵", "nutrition": "营养精灵",
}
SPIRIT_EMOJIS = {
    "light": "💡", "water": "💧", "soil": "🌱",
    "air": "💨", "nutrition": "✨",
}
SPIRIT_DOMAINS = {
    "light": "工作与学习", "water": "休闲与娱乐", "soil": "健康与运动",
    "air": "社交与人际", "nutrition": "兴趣与成长",
}


class ContextBuilder:
    """为 LLM 调用构建上下文"""

    def __init__(self, db: AsyncSession):
        self.db = db

    # ========================================
    #  协商上下文（核心方法）
    # ========================================

    async def build_negotiation_context(
        self,
        user_id: uuid.UUID,
        conflicting_tasks: list[dict],
        date_range: tuple[date, date],
    ) -> dict:
        """
        构建协商引擎所需的完整上下文。
        返回结构化 dict，协商引擎各步骤按需使用。
        """
        # 并行加载所有数据
        user_ctx = await self._load_user_basics(user_id)
        intensity_ctx = await self._load_spirit_intensities(user_id)
        schedule_ctx = await self._load_schedule_context(user_id, date_range)
        behavior_ctx = await self._load_behavior_stats(user_id)

        # 按精灵分组任务
        spirit_tasks = {}
        for task in conflicting_tasks:
            spirit = task.get("spirit", task.get("primary_spirit", "light"))
            spirit_tasks.setdefault(spirit, []).append(task)

        # 计算每个精灵的"谈判权重"
        spirit_weights = self._calculate_negotiation_weights(
            intensity_ctx, spirit_tasks, behavior_ctx
        )

        return {
            "user": user_ctx,
            "intensities": intensity_ctx,
            "schedule": schedule_ctx,
            "behavior": behavior_ctx,
            "spirit_tasks": spirit_tasks,
            "spirit_weights": spirit_weights,
            "date_range": {
                "start": str(date_range[0]),
                "end": str(date_range[1]),
            },
        }

    # ========================================
    #  主持人 Prompt 上下文
    # ========================================

    def build_orchestrator_prompt_context(self, negotiation_ctx: dict) -> str:
        """
        将协商上下文转为主持人 LLM 的 prompt 片段。
        主持人需要全局视角来调停冲突。
        """
        lines = []

        # 用户偏好摘要
        user = negotiation_ctx.get("user", {})
        lines.append(f"## 用户信息")
        lines.append(f"- 作息: {user.get('wake_time', '07:00')} ~ {user.get('sleep_time', '23:00')}")
        lines.append(f"- 高效时段: {', '.join(user.get('peak_hours', []))}")
        lines.append(f"- 精力模式: {user.get('energy_pattern', 'balanced')}")
        lines.append("")

        # 精灵强度
        lines.append("## 各精灵设定强度")
        for code, data in negotiation_ctx.get("intensities", {}).items():
            name = SPIRIT_NAMES.get(code, code)
            intensity = data.get("effective", 50)
            lines.append(f"- {name}: {intensity}/100")
        lines.append("")

        # 争议任务
        lines.append("## 需要协调的任务")
        for spirit, tasks in negotiation_ctx.get("spirit_tasks", {}).items():
            name = SPIRIT_NAMES.get(spirit, spirit)
            emoji = SPIRIT_EMOJIS.get(spirit, "")
            for t in tasks:
                lines.append(
                    f"- {emoji}{name}: 「{t.get('title', '?')}」"
                    f" {t.get('duration_minutes', 60)}分钟"
                    f" 优先级{t.get('priority', 'medium')}"
                )
        lines.append("")

        # 日程占用情况
        sched = negotiation_ctx.get("schedule", {})
        if sched.get("busy_summary"):
            lines.append("## 当前日程占用")
            for day_info in sched["busy_summary"]:
                lines.append(f"- {day_info}")
            lines.append("")

        # 谈判权重
        weights = negotiation_ctx.get("spirit_weights", {})
        if weights:
            lines.append("## 精灵谈判权重（越高越应被优先满足）")
            for code, w in sorted(weights.items(), key=lambda x: -x[1]):
                lines.append(f"- {SPIRIT_NAMES.get(code, code)}: {w:.1f}")
            lines.append("")

        # 行为洞察
        behavior = negotiation_ctx.get("behavior", {})
        if behavior.get("insights"):
            lines.append("## 用户行为洞察")
            for insight in behavior["insights"][:3]:
                lines.append(f"- {insight}")

        return "\n".join(lines)

    # ========================================
    #  精灵 Prompt 上下文
    # ========================================

    def build_spirit_claim_context(
        self,
        spirit_code: str,
        negotiation_ctx: dict,
    ) -> str:
        """为某个精灵构建协商诉求的 prompt 上下文"""
        lines = []

        # 该精灵的任务
        tasks = negotiation_ctx.get("spirit_tasks", {}).get(spirit_code, [])
        intensity = negotiation_ctx.get("intensities", {}).get(
            spirit_code, {}
        ).get("effective", 50)
        weight = negotiation_ctx.get("spirit_weights", {}).get(spirit_code, 50)

        lines.append(f"## 你的强度设定: {intensity}/100")
        lines.append(f"## 你的谈判权重: {weight:.1f}")
        lines.append(f"## 你负责的任务:")
        for t in tasks:
            deadline_str = ""
            if t.get("deadline"):
                deadline_str = f" deadline={t['deadline']}"
            lines.append(
                f"  - 「{t.get('title', '?')}」"
                f" {t.get('duration_minutes', 60)}分钟"
                f" 优先级{t.get('priority', 'medium')}"
                f"{deadline_str}"
            )
        lines.append("")

        # 可用时间
        sched = negotiation_ctx.get("schedule", {})
        if sched.get("available_summary"):
            lines.append("## 可用时间段:")
            for slot in sched["available_summary"][:8]:
                lines.append(f"  - {slot}")
        lines.append("")

        # 行为数据（该精灵相关）
        behavior = negotiation_ctx.get("behavior", {})
        spirit_stats = behavior.get("by_spirit", {}).get(spirit_code, {})
        if spirit_stats:
            rate = spirit_stats.get("completion_rate", 0)
            lines.append(f"## 用户历史: 该领域完成率 {rate:.0%}")

        return "\n".join(lines)

    # ========================================
    #  内部数据加载
    # ========================================

    async def _load_user_basics(self, user_id: uuid.UUID) -> dict:
        """加载用户基本偏好"""
        result = await self.db.execute(
            select(UserProfile).where(UserProfile.user_id == user_id)
        )
        profile = result.scalar_one_or_none()
        if not profile:
            return {}

        prefs = profile.preferences or {}
        return {
            "wake_time": prefs.get("wake_time", "07:00"),
            "sleep_time": prefs.get("sleep_time", "23:00"),
            "peak_hours": prefs.get("peak_hours", []),
            "energy_pattern": prefs.get("energy_pattern", "balanced"),
            "max_continuous_work_minutes": prefs.get("max_continuous_work_minutes", 120),
            "social_importance": prefs.get("social_importance", "medium"),
            "tags": profile.tags or [],
        }

    async def _load_spirit_intensities(self, user_id: uuid.UUID) -> dict:
        """加载五精灵强度"""
        result = await self.db.execute(
            select(UserProfile).where(UserProfile.user_id == user_id)
        )
        profile = result.scalar_one_or_none()
        if not profile:
            return {}

        result2 = await self.db.execute(
            select(SpiritIntensity).where(
                SpiritIntensity.profile_id == profile.id
            )
        )
        intensities = {}
        for si in result2.scalars().all():
            effective = min(100, max(0, si.base_intensity + (si.learned_delta or 0)))
            intensities[si.spirit_code] = {
                "base": si.base_intensity,
                "learned_delta": si.learned_delta or 0,
                "effective": effective,
                "is_locked": si.is_locked,
            }
        return intensities

    async def _load_schedule_context(
        self, user_id: uuid.UUID, date_range: tuple[date, date]
    ) -> dict:
        """加载日程占用情况摘要"""
        from app.models.schedule import Schedule

        result = await self.db.execute(
            select(Schedule).where(
                Schedule.user_id == user_id,
                Schedule.date >= date_range[0],
                Schedule.date <= date_range[1],
            ).order_by(Schedule.date)
        )

        busy_summary = []
        available_summary = []

        for sched in result.scalars().all():
            items = sched.items or []
            if items:
                total_min = 0
                slots = []
                for it in items:
                    start = it.get("time_start", "")
                    end = it.get("time_end", "")
                    title = it.get("title", "?")
                    spirit = it.get("spirit", "")
                    emoji = SPIRIT_EMOJIS.get(spirit, "")
                    slots.append(f"{start}-{end} {emoji}{title}")
                    try:
                        sh, sm = map(int, start.split(":"))
                        eh, em = map(int, end.split(":"))
                        total_min += (eh * 60 + em) - (sh * 60 + sm)
                    except (ValueError, AttributeError):
                        pass

                busy_summary.append(
                    f"{sched.date}: {len(items)}项, 共{total_min}分钟"
                )
            else:
                available_summary.append(f"{sched.date}: 全天空闲")

        # 日期范围内没有日程记录的天数也是空闲
        existing_dates = set()
        for s in busy_summary:
            existing_dates.add(s.split(":")[0])
        current = date_range[0]
        while current <= date_range[1]:
            if str(current) not in existing_dates:
                available_summary.append(f"{current}: 全天空闲")
            current += timedelta(days=1)

        return {
            "busy_summary": busy_summary,
            "available_summary": sorted(available_summary),
        }

    async def _load_behavior_stats(self, user_id: uuid.UUID) -> dict:
        """加载用户行为统计（最近30天）"""
        since = datetime.now(timezone.utc) - timedelta(days=30)

        # 查询最近30天的任务统计
        result = await self.db.execute(
            select(
                Task.primary_spirit,
                Task.status,
                func.count(Task.id),
            )
            .where(
                Task.user_id == user_id,
                Task.created_at >= since,
            )
            .group_by(Task.primary_spirit, Task.status)
        )

        by_spirit = {}
        for spirit, status, count in result.all():
            if spirit not in by_spirit:
                by_spirit[spirit] = {"total": 0, "completed": 0}
            by_spirit[spirit]["total"] += count
            if status == "completed":
                by_spirit[spirit]["completed"] += count

        # 计算完成率
        for spirit, stats in by_spirit.items():
            total = stats["total"]
            stats["completion_rate"] = (
                stats["completed"] / total if total > 0 else 0
            )

        # 生成洞察
        insights = []
        for spirit, stats in sorted(
            by_spirit.items(), key=lambda x: -x[1]["total"]
        ):
            name = SPIRIT_NAMES.get(spirit, spirit)
            rate = stats["completion_rate"]
            if rate < 0.5 and stats["total"] >= 3:
                insights.append(
                    f"{name}领域完成率偏低({rate:.0%})，安排时间时需留更多buffer"
                )
            elif rate > 0.9 and stats["total"] >= 5:
                insights.append(
                    f"{name}领域表现优秀({rate:.0%})，可以适当增加任务量"
                )

        return {
            "by_spirit": by_spirit,
            "insights": insights,
        }

    # ========================================
    #  谈判权重计算
    # ========================================

    def _calculate_negotiation_weights(
        self,
        intensities: dict,
        spirit_tasks: dict,
        behavior: dict,
    ) -> dict:
        """
        计算每个精灵的谈判权重。
        权重越高，协商中越应被优先满足。

        权重 = 强度(40%) + 任务紧急度(30%) + 历史欠债(30%)
        """
        weights = {}

        for code in ["light", "water", "soil", "air", "nutrition"]:
            # 强度分
            effective = intensities.get(code, {}).get("effective", 50)
            intensity_score = effective / 100

            # 任务紧急度（有 deadline 的、高优先级的加分）
            tasks = spirit_tasks.get(code, [])
            urgency_score = 0
            if tasks:
                for t in tasks:
                    if t.get("priority") == "high":
                        urgency_score += 0.4
                    elif t.get("priority") == "medium":
                        urgency_score += 0.2
                    else:
                        urgency_score += 0.1
                    if t.get("deadline"):
                        urgency_score += 0.3
                urgency_score = min(1.0, urgency_score / max(1, len(tasks)))

            # 历史欠债（完成率越低，越应该被优先安排）
            spirit_stats = behavior.get("by_spirit", {}).get(code, {})
            completion_rate = spirit_stats.get("completion_rate", 0.5)
            debt_score = 1.0 - completion_rate

            # 加权
            weight = (
                intensity_score * 0.4
                + urgency_score * 0.3
                + debt_score * 0.3
            ) * 100

            weights[code] = round(weight, 1)

        return weights

    # ========================================
    #  自由群聊上下文
    # ========================================

    async def build_profile_context(self, user_id: uuid.UUID) -> str:
        """构建自由群聊所需的用户画像上下文"""
        user_ctx = await self._load_user_basics(user_id)
        intensity_ctx = await self._load_spirit_intensities(user_id)

        lines = []

        # 用户基本信息
        if user_ctx:
            lines.append("## 用户偏好")
            lines.append(f"- 作息: {user_ctx.get('wake_time', '07:00')} ~ {user_ctx.get('sleep_time', '23:00')}")
            lines.append(f"- 高效时段: {', '.join(user_ctx.get('peak_hours', ['未设置']))}")
            if user_ctx.get('tags'):
                lines.append(f"- 标签: {', '.join(user_ctx.get('tags', []))}")

        # 精灵强度
        if intensity_ctx:
            lines.append("\n## 各精灵强度")
            for code, data in intensity_ctx.items():
                name = SPIRIT_NAMES.get(code, code)
                intensity = data.get("effective", 50)
                lines.append(f"- {name}: {intensity}/100")

        return "\n".join(lines) if lines else "用户信息暂未设置"