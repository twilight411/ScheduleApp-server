"""
报告系统 — 周报 + 周行为摘要 + 月度果实 + 月度摘要
"""
import uuid
from datetime import datetime, date, timezone

from sqlalchemy import String, Float, DateTime, Date, ForeignKey, Text, UniqueConstraint
from sqlalchemy import JSON
from app.models._types import GUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class WeeklyReport(Base):
    """周报"""
    __tablename__ = "weekly_reports"

    id: Mapped[uuid.UUID] = mapped_column(
        GUID, primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    week_start: Mapped[date] = mapped_column(Date, nullable=False)
    week_end: Mapped[date] = mapped_column(Date, nullable=False)

    headline: Mapped[str] = mapped_column(Text, nullable=False)
    overall_score: Mapped[float] = mapped_column(Float, nullable=False)
    vs_last_week: Mapped[float | None] = mapped_column(Float, nullable=True)

    stats: Mapped[dict] = mapped_column(JSON, nullable=False)
    tree_data: Mapped[dict] = mapped_column(JSON, nullable=False)
    analysis: Mapped[dict] = mapped_column(JSON, nullable=False)
    next_week_suggestions: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("user_id", "week_start", name="uq_weekly_report_user_week"),
    )


class WeeklySummary(Base):
    """周行为摘要 — 用于 Context Engineering"""
    __tablename__ = "weekly_summaries"

    id: Mapped[uuid.UUID] = mapped_column(
        GUID, primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    week_start: Mapped[date] = mapped_column(Date, nullable=False)
    narrative: Mapped[str] = mapped_column(Text, nullable=False)
    # AI 生成的行为摘要文本

    key_events: Mapped[dict] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("user_id", "week_start", name="uq_weekly_summary_user_week"),
    )


class MonthlyFruit(Base):
    """月度果实"""
    __tablename__ = "monthly_fruits"

    id: Mapped[uuid.UUID] = mapped_column(
        GUID, primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    month: Mapped[str] = mapped_column(String(7), nullable=False)
    # "2024-01"

    fruit_type: Mapped[str] = mapped_column(String(50), nullable=False)
    fruit_name: Mapped[str] = mapped_column(String(100), nullable=False)
    fruit_rarity: Mapped[str] = mapped_column(String(20), nullable=False)

    overall_score: Mapped[float] = mapped_column(Float, nullable=False)
    weekly_scores: Mapped[dict] = mapped_column(JSON, nullable=False)

    spirit_monthly: Mapped[dict] = mapped_column(JSON, nullable=False)
    best_spirit: Mapped[str | None] = mapped_column(String(20), nullable=True)
    weakest_spirit: Mapped[str | None] = mapped_column(String(20), nullable=True)
    awards: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    monthly_narrative: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("user_id", "month", name="uq_monthly_fruit_user_month"),
    )


class MonthlyDigest(Base):
    """月度摘要 — Context Engineering"""
    __tablename__ = "monthly_digests"

    id: Mapped[uuid.UUID] = mapped_column(
        GUID, primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    month: Mapped[str] = mapped_column(String(7), nullable=False)
    narrative: Mapped[str] = mapped_column(Text, nullable=False)
    key_metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("user_id", "month", name="uq_monthly_digest_user_month"),
    )
