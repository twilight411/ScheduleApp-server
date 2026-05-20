"""
本周基调服务 — Sprint 1

职责:
  - 查询/创建/更新某周的 WeeklyFocus
  - 提供"获取当前周基调"的快捷方法 (给 ScoringService 使用)
  - 内置主题 → spirit_weights 的预设映射
  - 主题 → 中文展示名的转换

外部调用接口:
  - get_or_default_weights(user_id, week_start) → 给 ScoringService 用,
    任何精灵打分都先问一次"这周我的权重是多少"; 未设基调返回全 1.0
"""
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.weekly_focus import WeeklyFocus, DEFAULT_WEIGHT

import structlog
logger = structlog.get_logger()


SPIRIT_CODES = ["light", "water", "soil", "air", "nutrition"]


# ====================================================================
#  预设主题 — theme → 权重模板 + 中文展示 + 描述 + 默认 key_spirits
# ====================================================================

THEME_PRESETS: dict[str, dict] = {
    "exam_prep": {
        "label": "备考冲刺",
        "description": "学习权重显著抬升,娱乐/社交收敛",
        "icon": "📚",
        "spirit_weights": {
            "light": 1.8, "water": 0.6, "soil": 0.9,
            "air": 0.7, "nutrition": 0.7,
        },
        "key_spirits": ["light"],
    },
    "project_sprint": {
        "label": "项目冲刺",
        "description": "工作权重高,关注健康底线",
        "icon": "🚀",
        "spirit_weights": {
            "light": 1.6, "water": 0.7, "soil": 1.0,
            "air": 0.8, "nutrition": 0.7,
        },
        "key_spirits": ["light"],
    },
    "recovery": {
        "label": "休整恢复",
        "description": "健康和休闲优先,工作压力调低",
        "icon": "🌿",
        "spirit_weights": {
            "light": 0.6, "water": 1.4, "soil": 1.6,
            "air": 0.9, "nutrition": 1.1,
        },
        "key_spirits": ["soil", "water"],
    },
    "social": {
        "label": "社交月",
        "description": "人际投入抬升,其他维度均衡",
        "icon": "🤝",
        "spirit_weights": {
            "light": 0.9, "water": 1.0, "soil": 0.9,
            "air": 1.7, "nutrition": 0.9,
        },
        "key_spirits": ["air"],
    },
    "creative": {
        "label": "兴趣深耕",
        "description": "兴趣爱好优先,留更多沉浸时间",
        "icon": "🎨",
        "spirit_weights": {
            "light": 0.9, "water": 0.9, "soil": 0.9,
            "air": 0.8, "nutrition": 1.7,
        },
        "key_spirits": ["nutrition"],
    },
    "balanced": {
        "label": "平衡发展",
        "description": "五维均衡,不偏不倚",
        "icon": "⚖️",
        "spirit_weights": {
            "light": 1.0, "water": 1.0, "soil": 1.0,
            "air": 1.0, "nutrition": 1.0,
        },
        "key_spirits": [],
    },
}


THEME_LABEL_MAP = {k: v["label"] for k, v in THEME_PRESETS.items()}


def get_week_start(d: Optional[date] = None) -> date:
    """获取某天所在周的周一; d 为 None 时取今天 (UTC)"""
    d = d or datetime.now(timezone.utc).date()
    return d - timedelta(days=d.weekday())


class WeeklyFocusService:

    def __init__(self, db: AsyncSession):
        self.db = db

    # ========================================
    #  查询
    # ========================================

    async def get_focus(
        self, user_id: uuid.UUID, week_start: date
    ) -> Optional[WeeklyFocus]:
        """获取指定周的基调; 不存在返回 None"""
        result = await self.db.execute(
            select(WeeklyFocus).where(
                WeeklyFocus.user_id == user_id,
                WeeklyFocus.week_start == week_start,
            )
        )
        return result.scalar_one_or_none()

    async def get_current_focus(
        self, user_id: uuid.UUID
    ) -> Optional[WeeklyFocus]:
        """获取本周基调"""
        return await self.get_focus(user_id, get_week_start())

    async def get_or_default_weights(
        self, user_id: uuid.UUID, week_start: Optional[date] = None
    ) -> dict[str, float]:
        """
        获取某周的 spirit_weights; 未设置时返回全 1.0 默认值。

        ScoringService 调用此方法时不需要关心是否存在记录:
          weights = await focus_svc.get_or_default_weights(uid, ws)
          mult = weights[spirit_code]
        """
        ws = week_start or get_week_start()
        focus = await self.get_focus(user_id, ws)
        if focus and focus.spirit_weights:
            return {
                c: float(focus.spirit_weights.get(c, DEFAULT_WEIGHT))
                for c in SPIRIT_CODES
            }
        return {c: DEFAULT_WEIGHT for c in SPIRIT_CODES}

    async def get_focus_snapshot(
        self, user_id: uuid.UUID, week_start: Optional[date] = None
    ) -> dict:
        """
        给 ScoringService 在打分时一次性拿全所需信息的快照。
        返回 (即使未设基调也返回默认值):
          {
            "theme": "exam_prep" or None,
            "weights": {...},
            "key_spirits": [...],
            "label": "备考冲刺" or "平衡发展",
          }
        """
        ws = week_start or get_week_start()
        focus = await self.get_focus(user_id, ws)
        if focus:
            return {
                "theme": focus.theme,
                "weights": await self.get_or_default_weights(user_id, ws),
                "key_spirits": list(focus.key_spirits or []),
                "label": self.display_label(focus),
            }
        return {
            "theme": None,
            "weights": {c: DEFAULT_WEIGHT for c in SPIRIT_CODES},
            "key_spirits": [],
            "label": "未设基调",
        }

    # ========================================
    #  写入 (upsert)
    # ========================================

    async def upsert_focus(
        self,
        user_id: uuid.UUID,
        week_start: date,
        theme: str,
        spirit_weights: dict[str, float],
        key_spirits: list[str],
        custom_label: Optional[str] = None,
        reason: Optional[str] = None,
        source: str = "manual",
    ) -> WeeklyFocus:
        """
        创建或更新某周的基调。
        同 (user_id, week_start) 已有记录则覆盖, 否则新建。
        """
        existing = await self.get_focus(user_id, week_start)

        if existing:
            existing.theme = theme
            existing.custom_label = custom_label
            existing.spirit_weights = spirit_weights
            existing.key_spirits = key_spirits
            existing.reason = reason
            existing.source = source
            existing.updated_at = datetime.now(timezone.utc)
            await self.db.flush()
            logger.info(
                "weekly_focus_updated",
                user_id=str(user_id),
                week=str(week_start),
                theme=theme,
                source=source,
            )
            return existing

        focus = WeeklyFocus(
            user_id=user_id,
            week_start=week_start,
            theme=theme,
            custom_label=custom_label,
            spirit_weights=spirit_weights,
            key_spirits=key_spirits,
            reason=reason,
            source=source,
        )
        self.db.add(focus)
        await self.db.flush()
        logger.info(
            "weekly_focus_created",
            user_id=str(user_id),
            week=str(week_start),
            theme=theme,
            source=source,
        )
        return focus

    async def delete_focus(
        self, user_id: uuid.UUID, week_start: date
    ) -> bool:
        """删除某周基调,前端取消设置时调用"""
        result = await self.db.execute(
            delete(WeeklyFocus).where(
                WeeklyFocus.user_id == user_id,
                WeeklyFocus.week_start == week_start,
            )
        )
        deleted = (result.rowcount or 0) > 0
        if deleted:
            logger.info(
                "weekly_focus_deleted",
                user_id=str(user_id), week=str(week_start),
            )
        return deleted

    # ========================================
    #  辅助
    # ========================================

    @staticmethod
    def display_label(focus: WeeklyFocus) -> str:
        """生成给前端的展示名"""
        if focus.theme == "custom":
            return focus.custom_label or "自定义"
        return THEME_LABEL_MAP.get(focus.theme, focus.theme)

    @staticmethod
    def list_presets() -> list[dict]:
        """列出预设主题,供前端选择 UI"""
        return [
            {
                "theme": theme,
                "label": preset["label"],
                "description": preset["description"],
                "icon": preset["icon"],
                "spirit_weights": preset["spirit_weights"],
                "key_spirits": preset["key_spirits"],
            }
            for theme, preset in THEME_PRESETS.items()
        ]

    @staticmethod
    def get_preset(theme: str) -> Optional[dict]:
        """获取单个预设,用于前端"应用此模板"时一键填充"""
        return THEME_PRESETS.get(theme)
