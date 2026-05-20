"""
AI 服务 Schemas — 解析、精灵对话、任务拆解
"""
import uuid
from typing import Optional
from pydantic import BaseModel, Field


class ParseRequest(BaseModel):
    """自然语言 → 结构化任务解析请求"""
    user_input: str = Field(..., min_length=1, max_length=2000, description="用户的自然语言输入")


class SpiritChatRequest(BaseModel):
    """单精灵对话请求"""
    message: str = Field(..., min_length=1, max_length=2000, description="用户消息")
    session_id: Optional[str] = Field(None, description="会话 ID（续接对话时传入）")


class DecomposeRequest(BaseModel):
    """手动触发任务拆解请求"""
    task_id: uuid.UUID = Field(..., description="要拆解的任务 ID")


class NegotiateRequest(BaseModel):
    """发起精灵协商请求"""
    task_ids: list[str] = Field(default_factory=list, description="相关任务 ID 列表")
    date: Optional[str] = Field(None, description="目标日期 YYYY-MM-DD")
    trigger_reason: Optional[str] = Field(None, description="触发原因")


class NegotiateResolveRequest(BaseModel):
    """用户介入协商"""
    negotiation_id: str = Field(..., description="协商会话 ID")
    decision: str = Field(..., description="用户决策")
    details: Optional[dict] = None


class SuggestSlotRequest(BaseModel):
    """为新任务推荐时间槽"""
    duration_minutes: int = Field(60, ge=15, le=480)
    spirit: str = Field("light", description="精灵代码")
    date: Optional[str] = Field(None, description="目标日期 YYYY-MM-DD")
    priority: str = Field("medium", description="优先级 high/medium/low")
