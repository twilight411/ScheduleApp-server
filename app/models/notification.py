"""
通知系统 — 设备注册 + 通知设置 + 站内通知
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import String, Boolean, DateTime, ForeignKey, Text
from sqlalchemy import JSON
from app.models._types import GUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class UserDevice(Base):
    """推送设备注册"""
    __tablename__ = "user_devices"

    id: Mapped[uuid.UUID] = mapped_column(
        GUID, primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    device_token: Mapped[str] = mapped_column(String(500), nullable=False)
    platform: Mapped[str] = mapped_column(String(20), nullable=False)
    # ios / android / web

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )


class NotificationSetting(Base):
    """用户通知偏好设置"""
    __tablename__ = "notification_settings"

    id: Mapped[uuid.UUID] = mapped_column(
        GUID, primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="CASCADE"),
        unique=True, nullable=False,
    )

    daily_schedule_push: Mapped[bool] = mapped_column(Boolean, default=True)
    task_reminder_push: Mapped[bool] = mapped_column(Boolean, default=True)
    weekly_report_push: Mapped[bool] = mapped_column(Boolean, default=True)
    monthly_fruit_push: Mapped[bool] = mapped_column(Boolean, default=True)
    spirit_tip_push: Mapped[bool] = mapped_column(Boolean, default=True)
    sedentary_reminder: Mapped[bool] = mapped_column(Boolean, default=True)

    quiet_hours_start: Mapped[str] = mapped_column(String(5), default="22:00")
    quiet_hours_end: Mapped[str] = mapped_column(String(5), default="08:00")

    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )


class Notification(Base):
    """站内通知消息"""
    __tablename__ = "notifications"

    id: Mapped[uuid.UUID] = mapped_column(
        GUID, primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    type: Mapped[str] = mapped_column(String(50), nullable=False)
    # task_reminder / daily_schedule / weekly_report /
    # monthly_fruit / spirit_tip / sedentary / system

    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # 附加数据（如跳转链接）

    is_read: Mapped[bool] = mapped_column(Boolean, default=False)
    is_pushed: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)
