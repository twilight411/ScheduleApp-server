"""
精灵周打分
"""
import uuid
from datetime import datetime, date, timezone

from sqlalchemy import String, Integer, Float, DateTime, Date, ForeignKey, Text, UniqueConstraint
from sqlalchemy import JSON
from app.models._types import GUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class SpiritWeeklyScore(Base):
    """精灵周打分 — 每用户每精灵每周一条"""
    __tablename__ = "spirit_weekly_scores"

    id: Mapped[uuid.UUID] = mapped_column(
        GUID, primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    spirit_code: Mapped[str] = mapped_column(String(20), nullable=False)
    week_start: Mapped[date] = mapped_column(Date, nullable=False)

    score: Mapped[float] = mapped_column(Float, nullable=False)
    design_score: Mapped[float] = mapped_column(Float, nullable=False)
    completion_score: Mapped[float] = mapped_column(Float, nullable=False)
    quality_score: Mapped[float] = mapped_column(Float, nullable=False)

    level: Mapped[str] = mapped_column(String(20), nullable=False)
    # flourishing / good / average / poor / withered

    intensity_at_scoring: Mapped[int] = mapped_column(Integer, nullable=False)
    # 打分时的强度快照

    task_stats: Mapped[dict] = mapped_column(JSON, nullable=False)
    # {planned, completed, cancelled, on_time}

    spirit_comment: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("user_id", "spirit_code", "week_start",
                         name="uq_spirit_score_user_spirit_week"),
    )
