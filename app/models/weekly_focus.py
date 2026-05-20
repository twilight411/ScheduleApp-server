"""
本周基调 (Weekly Focus) — Sprint 1

每用户每周可设置一个基调:
  - theme: 预设主题或 custom
  - spirit_weights: 5 精灵的权重 mult, 默认 1.0, 范围 [0.5, 2.0]
  - key_spirits: 本周的 1-2 个重点精灵
  - reason: 设置原因 (例: 周六考试 / 项目上线 / 病假休养)

与 IntensityTemplate 的根本区别:
  - IntensityTemplate 应用后直接写入 SpiritIntensity.base_intensity (长期偏好)
  - WeeklyFocus 是本周临时, 不修改 base_intensity, 只在打分/编排时作为乘数生效

后续 Sprint 2 中, ScoringService 会读取本周的 WeeklyFocus 来调整:
  - design_score 的 expected_count 期望任务数 (重点精灵期望任务更多)
  - final_score 的基调放大 (重点精灵得分波动放大, 次要精灵收敛)
  - overall_score 的精灵加权
"""
import uuid
from datetime import datetime, date, timezone

from sqlalchemy import String, DateTime, Date, ForeignKey, Text, UniqueConstraint, JSON
from app.models._types import GUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


# ====================================================================
#  常量 — 校验时复用
# ====================================================================

VALID_THEMES = {
    "exam_prep",       # 备考冲刺 (光精灵主导)
    "project_sprint",  # 项目冲刺 (光精灵主导, 但保留健康底线)
    "recovery",        # 休整恢复 (土壤 + 水精灵主导)
    "balanced",        # 平衡发展 (五维 1.0)
    "social",          # 社交月 (空气精灵主导)
    "creative",        # 兴趣深耕 (营养精灵主导)
    "custom",          # 用户自定义
}

VALID_SOURCES = {"manual", "from_chat", "from_template"}

# spirit_weights 范围 — 与设计文档锁定
MIN_WEIGHT = 0.5
MAX_WEIGHT = 2.0
DEFAULT_WEIGHT = 1.0

# key_spirits 上限 — 一周最多 2 个重点, 防止"哪都是重点"失去意义
MAX_KEY_SPIRITS = 2


class WeeklyFocus(Base):
    """用户本周基调 — 每用户每周一条"""
    __tablename__ = "weekly_focus"

    id: Mapped[uuid.UUID] = mapped_column(
        GUID, primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    week_start: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    # 必须是周一; 由 schema 层校验

    theme: Mapped[str] = mapped_column(String(30), nullable=False, default="balanced")
    # exam_prep / project_sprint / recovery / balanced / social / creative / custom

    custom_label: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # 用户自填的展示名, theme=custom 时使用, 例如 "备考周"

    spirit_weights: Mapped[dict] = mapped_column(JSON, nullable=False)
    # {"light": 1.8, "water": 0.6, "soil": 1.0, "air": 0.7, "nutrition": 0.9}
    # 每个精灵的权重 mult, 范围 [0.5, 2.0], 5 个 key 必填

    key_spirits: Mapped[list] = mapped_column(JSON, default=list)
    # ["light"] 或 ["light", "soil"], 最多 2 个

    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 用户设置原因, 例如 "周六期末考试"

    source: Mapped[str] = mapped_column(String(20), default="manual")
    # manual / from_chat / from_template

    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        UniqueConstraint("user_id", "week_start", name="uq_weekly_focus_user_week"),
    )
