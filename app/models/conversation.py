"""
对话记录 + 对话中识别的任务建议
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import String, Integer, Float, DateTime, ForeignKey, Text, Date
from sqlalchemy import JSON
from app.models._types import GUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Conversation(Base):
    """对话记录"""
    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(
        GUID, primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    spirit_code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # null for negotiation sessions

    session_type: Mapped[str] = mapped_column(String(20), nullable=False)
    # chat / negotiation / decompose

    messages: Mapped[dict] = mapped_column(JSON, default=list)
    # List[Message]

    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )


class ChatTaskSuggestion(Base):
    """对话中 AI 识别到的任务建议（临时态）"""
    __tablename__ = "chat_task_suggestions"

    id: Mapped[uuid.UUID] = mapped_column(
        GUID, primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("conversations.id"), nullable=True,
    )

    title: Mapped[str] = mapped_column(Text, nullable=False)
    spirit: Mapped[str] = mapped_column(String(20), nullable=False)
    suggested_date: Mapped[datetime | None] = mapped_column(Date, nullable=True)
    time_start: Mapped[str | None] = mapped_column(String(5), nullable=True)
    time_end: Mapped[str | None] = mapped_column(String(5), nullable=True)
    duration_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    priority: Mapped[str] = mapped_column(String(10), default="medium")
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    source_quote: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(String(20), default="pending")
    # pending / accepted / rejected / expired

    task_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("tasks.id"), nullable=True,
    )
    # 如果被接受，关联到创建的任务

    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
