"""
任务 Schemas — 创建、更新、状态流转、Chat-to-Task

Sprint 1 新增:
  - SubTaskCompletionUpdateRequest: 更新子任务连续完成度 (0/25/50/75/100)
  - SubTaskOut 增加 completion_percent / quality_note / user_feedback / self_reported_at 字段
"""
import uuid
from typing import Optional
from datetime import datetime
from pydantic import BaseModel, Field, field_validator


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
#  Sprint 1: 子任务完成度更新
# ====================================================================

# 离散完成度档位 — 前端 UI 用五档单选, 后端强校验
VALID_COMPLETION_PERCENTS = {0, 25, 50, 75, 100}
VALID_USER_FEEDBACK = {"easy", "just_right", "hard"}


class SubTaskCompletionUpdateRequest(BaseModel):
    """
    更新单个子任务的完成度。

    字段:
      - completion_percent: 必须是 0/25/50/75/100
      - quality_note: 可选, 部分完成时给周末 AI 的提示
      - user_feedback: 可选, easy/just_right/hard (沿用现有质量分维度)
      - auto_advance_status: True 时自动联动 status:
          completion_percent=100 → status='completed' (并写 actual_end=now)
          completion_percent in (25,50,75) → status='in_progress'
          completion_percent=0 → 不修改 status (避免一键归零误降级已完成的任务)
    """
    completion_percent: int = Field(..., description="0/25/50/75/100")
    quality_note: Optional[str] = Field(None, max_length=500)
    user_feedback: Optional[str] = Field(None)
    auto_advance_status: bool = Field(
        True, description="为 True 时根据完成度自动调整 status"
    )

    @field_validator("completion_percent")
    @classmethod
    def _percent_must_be_discrete(cls, v: int) -> int:
        if v not in VALID_COMPLETION_PERCENTS:
            raise ValueError(
                f"completion_percent 必须是 {sorted(VALID_COMPLETION_PERCENTS)} 之一, 收到 {v}"
            )
        return v

    @field_validator("user_feedback")
    @classmethod
    def _feedback_must_be_valid(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if v not in VALID_USER_FEEDBACK:
            raise ValueError(
                f"user_feedback 必须是 {sorted(VALID_USER_FEEDBACK)} 之一, 收到 {v!r}"
            )
        return v


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
    # ─── Sprint 1 新增字段 ───
    completion_percent: int = 0
    quality_note: Optional[str] = None
    user_feedback: Optional[str] = None
    self_reported_at: Optional[str] = None


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