"""
日程 Schemas — 生成/调整/交换/冲突检测/时间槽推荐
"""
from typing import Optional
from pydantic import BaseModel, Field


class ScheduleGenerateRequest(BaseModel):
    """AI 生成日程"""
    start_date: str = Field(..., description="起始日期 YYYY-MM-DD")
    end_date: str = Field(..., description="结束日期 YYYY-MM-DD")
    task_ids: Optional[list[str]] = None
    include_recurring: bool = True
    regenerate: bool = False


class ScheduleAdjustRequest(BaseModel):
    """手动调整日程项时间"""
    date: str
    item_id: str
    new_start: str = Field(..., pattern=r"^\d{2}:\d{2}$")
    new_end: str = Field(..., pattern=r"^\d{2}:\d{2}$")
    version: int = 0


class ScheduleSwapRequest(BaseModel):
    """交换两个日程项的时间"""
    date: str
    item_id_1: str
    item_id_2: str
    version: int = 0


class CheckConflictsRequest(BaseModel):
    """冲突检测请求"""
    start_date: str
    end_date: str


class SuggestSlotRequest(BaseModel):
    """为新任务推荐时间槽"""
    duration_minutes: int = Field(60, ge=15, le=480)
    spirit: str = "light"
    date: Optional[str] = None
    priority: str = "medium"