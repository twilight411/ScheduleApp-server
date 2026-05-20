"""
对话服务 — 管理精灵对话会话、保存历史、处理 task_suggestion
"""
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Conversation, ChatTaskSuggestion


class ConversationService:

    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_or_create_session(
        self,
        user_id: uuid.UUID,
        spirit_code: str,
        session_id: uuid.UUID = None,
    ) -> Conversation:
        """获取或创建对话会话"""
        if session_id:
            result = await self.db.execute(
                select(Conversation).where(
                    Conversation.id == session_id,
                    Conversation.user_id == user_id,
                )
            )
            conv = result.scalar_one_or_none()
            if conv:
                return conv

        # 创建新会话
        conv = Conversation(
            user_id=user_id,
            spirit_code=spirit_code,
            session_type="chat",
            messages=[],
        )
        self.db.add(conv)
        await self.db.flush()
        return conv

    async def append_message(
        self,
        conversation: Conversation,
        role: str,
        content: str,
        extra: dict = None,
    ):
        """向对话追加消息"""
        msg = {
            "role": role,  # user / assistant / system
            "content": content,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if extra:
            msg.update(extra)

        messages = list(conversation.messages or [])
        messages.append(msg)
        conversation.messages = messages
        conversation.updated_at = datetime.now(timezone.utc)
        await self.db.flush()

    async def save_task_suggestion(
        self,
        user_id: uuid.UUID,
        session_id: uuid.UUID,
        spirit_code: str,
        suggestion: dict,
    ) -> Optional[ChatTaskSuggestion]:
        """保存 AI 识别到的任务建议"""
        if not suggestion.get("detected"):
            return None

        record = ChatTaskSuggestion(
            user_id=user_id,
            session_id=session_id,
            title=suggestion.get("title", ""),
            spirit=spirit_code,
            suggested_date=self._parse_date(suggestion.get("date")),
            time_start=suggestion.get("time_start"),
            time_end=suggestion.get("time_end"),
            duration_minutes=suggestion.get("duration_minutes"),
            priority="medium",
            confidence=suggestion.get("confidence", 0),
            source_quote=suggestion.get("source_quote", ""),
            status="pending",
        )
        self.db.add(record)
        await self.db.flush()
        return record

    @staticmethod
    def _parse_date(date_str):
        if not date_str:
            return None
        try:
            from datetime import date as date_type
            return datetime.strptime(str(date_str)[:10], "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return None
