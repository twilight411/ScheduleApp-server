"""
任务 + 子任务 + 行为事件流水
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import String, Integer, Float, Boolean, DateTime, ForeignKey, Text
from sqlalchemy import JSON
from app.models._types import GUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Task(Base):
    """主任务"""
    __tablename__ = "tasks"

    id: Mapped[uuid.UUID] = mapped_column(
        GUID, primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    raw_input: Mapped[str | None] = mapped_column(Text, nullable=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    primary_spirit: Mapped[str] = mapped_column(String(20), nullable=False)
    secondary_spirits: Mapped[dict] = mapped_column(JSON, default=list)
    deadline: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    estimated_hours: Mapped[float | None] = mapped_column(Float, nullable=True)
    priority: Mapped[str] = mapped_column(String(10), default="medium")
    # high / medium / low

    is_recurring: Mapped[bool] = mapped_column(Boolean, default=False)
    recurrence_pattern: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # daily / weekdays / weekly / every_monday / ...

    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    # pending / in_progress / completed / cancelled / overdue

    source: Mapped[str] = mapped_column(String(20), default="manual")
    # manual / parsed / chat

    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )

    user: Mapped["User"] = relationship(back_populates="tasks")
    subtasks: Mapped[list["SubTask"]] = relationship(
        back_populates="task", cascade="all, delete-orphan"
    )


class SubTask(Base):
    """子任务（精灵拆解后的可执行单元）"""
    __tablename__ = "subtasks"

    id: Mapped[uuid.UUID] = mapped_column(
        GUID, primary_key=True, default=uuid.uuid4
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    spirit: Mapped[str] = mapped_column(String(20), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    suggested_time: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # morning / afternoon / evening / night

    dependencies: Mapped[dict] = mapped_column(JSON, default=list)
    # list of subtask UUIDs

    scheduled_start: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    scheduled_end: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    actual_start: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    actual_end: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    status: Mapped[str] = mapped_column(String(20), default="pending")
    # pending / scheduled / in_progress / completed / cancelled / overdue

    # ─────────────── Sprint 1: 连续完成度 ───────────────
    completion_percent: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 0/25/50/75/100 五档离散值, 由前端选择
    # status='completed' 时默认 100; 部分完成时可设为 25/50/75
    # 与 status 是两件事: 一个任务可以 status='in_progress' + completion_percent=60

    self_reported_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 用户上次更新完成度的时间, 用于周末分析"是当天勾的还是事后补的"

    quality_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 可选, 部分完成时的简短说明 (例: "读完了一半, 后半还要再花一次")
    # 周末 LLM 分析时作为重要信号: 解释了"为什么没完成"
    # ─────────────── end Sprint 1 ───────────────

    user_feedback: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # easy / just_right / hard

    spirit_tip: Mapped[str | None] = mapped_column(Text, nullable=True)

    is_recurring_instance: Mapped[bool] = mapped_column(Boolean, default=False)
    instance_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    priority: Mapped[str] = mapped_column(String(10), default="medium")
    is_fixed: Mapped[bool] = mapped_column(Boolean, default=False)
    # 固定事件不可被调度器移动

    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    task: Mapped["Task"] = relationship(back_populates="subtasks")


class TaskEvent(Base):
    """行为事件流水 — 记录任务生命周期的所有事件，用于学习"""
    __tablename__ = "task_events"

    id: Mapped[uuid.UUID] = mapped_column(
        GUID, primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    # task_created / task_started / task_paused / task_completed
    # task_cancelled / task_rescheduled / subtask_completed / ...

    event_data: Mapped[dict] = mapped_column(JSON, nullable=False)
    # 包含 task_id, subtask_id, reason, feedback 等上下文数据

    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)
