"""
日程表 — 每个用户每天一条记录
"""
import uuid
from datetime import datetime, date, timezone

from sqlalchemy import Integer, DateTime, Date, ForeignKey, UniqueConstraint
from sqlalchemy import JSON
from app.models._types import GUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Schedule(Base):
    """日程表"""
    __tablename__ = "schedules"

    id: Mapped[uuid.UUID] = mapped_column(
        GUID, primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    date: Mapped[date] = mapped_column(Date, nullable=False)

    # [N4 修复] 实际存储的是 list[dict]，类型标注从 Mapped[dict] 改为 Mapped[list]
    items: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # List[ScheduleItem] — 每个 item 是一个 dict，结构见 schemas/schedule.py

    version: Mapped[int] = mapped_column(Integer, default=1)
    # 乐观锁版本号

    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )

    __table_args__ = (
        UniqueConstraint("user_id", "date", name="uq_schedule_user_date"),
    )