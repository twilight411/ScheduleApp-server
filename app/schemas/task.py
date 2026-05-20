"""
任务 Schemas — 创建、更新、状态流转、Chat-to-Task
"""
import uuid
from typing import Optional
from datetime import datetime
from pydantic import BaseModel, Field


class TaskCreateRequest(BaseModel):
    """创建任务 — 支持自然语言输入，后端自动解析"""
    user_input: Optional[str] = Field(None, max_length=2000, description="自然语言输入")
    title: Optional[str] = Field(None, max_length=500, description="任务标题（如 user_input 为空则必填）")
    primary_spirit: Optional[str] = Field(None, description="主精灵代码")
    deadline: Optional[str] = Field(None, description="截止时间 ISO 格式")
    estimated_hours: Optional[float] = Field(None, ge=0.1, le=100)
    priority: str = Field("medium", description="high/medium/low")
    is_recurring: bool = False
    recurrence_pattern: Optional[str] = None
    # 如果提供 user_input，后端先调用 task_parser 解析，再创建任务
    auto_decompose: bool = Field(False, description="创建后自动拆解为子任务")


class TaskUpdateRequest(BaseModel):
    """更新任务"""
    title: Optional[str] = Field(None, max_length=500)
    primary_spirit: Optional[str] = None
    deadline: Optional[str] = None
    estimated_hours: Optional[float] = None
    priority: Optional[str] = None
    is_recurring: Optional[bool] = None
    recurrence_pattern: Optional[str] = None


class TaskCompleteRequest(BaseModel):
    """完成任务"""
    feedback: Optional[str] = Field(None, description="easy / just_right / hard")
    note: Optional[str] = Field(None, max_length=500, description="完成备注")


class TaskCancelRequest(BaseModel):
    """取消任务"""
    reason: Optional[str] = Field(None, max_length=500)


class TaskRescheduleRequest(BaseModel):
    """改期"""
    new_start: str = Field(..., description="新开始时间 ISO 格式")
    new_end: str = Field(..., description="新结束时间 ISO 格式")
    reason: Optional[str] = Field(None, max_length=500)


class ChatTaskCreateRequest(BaseModel):
    """从对话创建任务"""
    suggestion_id: str = Field(..., description="Chat-to-Task 建议 ID")
    session_id: Optional[str] = None
    title: str
    spirit: str
    date: Optional[str] = None
    time_start: Optional[str] = None
    time_end: Optional[str] = None
    duration_minutes: Optional[int] = None
    priority: str = "medium"


class ChatTaskResolveRequest(BaseModel):
    """解决对话任务冲突"""
    task_id: str
    conflict_resolution: str  # replace / reschedule / cancel
    new_start: Optional[str] = None
    new_end: Optional[str] = None


class BatchCompleteRequest(BaseModel):
    """批量完成"""
    task_ids: list[str]
    feedback: Optional[str] = None


# ====================================================================
#  输出
# ====================================================================

class SubTaskOut(BaseModel):
    id: str
    task_id: str
    spirit: str
    title: str
    duration_minutes: int
    scheduled_start: Optional[str] = None
    scheduled_end: Optional[str] = None
    status: str = "pending"
    priority: str = "medium"
    spirit_tip: Optional[str] = None
    suggested_time: Optional[str] = None


class TaskOut(BaseModel):
    id: str
    title: str
    raw_input: Optional[str] = None
    primary_spirit: str
    secondary_spirits: list = Field(default_factory=list)
    deadline: Optional[str] = None
    estimated_hours: Optional[float] = None
    priority: str = "medium"
    is_recurring: bool = False
    recurrence_pattern: Optional[str] = None
    status: str = "pending"
    source: str = "manual"
    created_at: Optional[str] = None
    subtasks: list[SubTaskOut] = Field(default_factory=list)