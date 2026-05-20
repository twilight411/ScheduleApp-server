"""
日程管理接口 — 查询 / AI生成 / 手动调整 / 手动增删改 / 冲突检测

原有端点（对接 ScheduleService）:
  GET  /schedule/today
  GET  /schedule/{date}
  GET  /schedule/week/{week_start}
  GET  /schedule/range
  POST /schedule/generate
  POST /schedule/adjust
  POST /schedule/swap
  POST /schedule/check-conflicts

新增端点（手动日程管理）:
  POST   /schedule/items            — 手动添加日程项
  PATCH  /schedule/items/{item_id}  — 编辑日程项
  DELETE /schedule/items/{item_id}  — 删除日程项
"""
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.auth import get_current_user
from app.models.user import User
from app.schemas.common import success_response, error_response
from app.schemas.schedule import (
    ScheduleGenerateRequest,
    ScheduleAdjustRequest,
    ScheduleSwapRequest,
    CheckConflictsRequest,
)
from app.services.schedule_service import ScheduleService

router = APIRouter(prefix="/schedule", tags=["Schedule"])


# ===== 新增 Schema =====

class ManualItemCreateRequest(BaseModel):
    date: str = Field(description="日期 YYYY-MM-DD")
    title: str = Field(min_length=1, max_length=200, description="事项标题")
    time_start: str = Field(pattern=r"^\d{2}:\d{2}$", description="开始时间 HH:MM")
    time_end: str = Field(pattern=r"^\d{2}:\d{2}$", description="结束时间 HH:MM")
    spirit: Optional[str] = Field(None, description="关联精灵（可选）")
    note: Optional[str] = Field(None, max_length=500, description="备注")
    is_fixed: bool = Field(True, description="是否固定时间（AI 排程时不移动）")


class ManualItemUpdateRequest(BaseModel):
    date: str = Field(description="日期 YYYY-MM-DD")
    title: Optional[str] = Field(None, min_length=1, max_length=200)
    time_start: Optional[str] = Field(None, pattern=r"^\d{2}:\d{2}$")
    time_end: Optional[str] = Field(None, pattern=r"^\d{2}:\d{2}$")
    spirit: Optional[str] = None
    note: Optional[str] = Field(None, max_length=500)
    priority: Optional[str] = None
    is_fixed: Optional[bool] = None
    version: int = Field(description="乐观锁版本号")


class DeleteItemRequest(BaseModel):
    date: str = Field(description="日期 YYYY-MM-DD")
    version: int = Field(description="乐观锁版本号")


# ===== 辅助 =====

VALID_SPIRITS = {"light", "water", "soil", "air", "nutrition"}


def _parse_date(date_str: str):
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        raise HTTPException(400, detail=error_response(
            "VALIDATION_ERROR", "日期格式错误，请使用 YYYY-MM-DD"
        ))


def _validate_time_range(start: str, end: str):
    if start >= end:
        raise HTTPException(400, detail=error_response(
            "VALIDATION_ERROR", "结束时间必须晚于开始时间"
        ))


# ========================================
#  查询
# ========================================

@router.get("/today")
async def get_today(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """今日日程"""
    svc = ScheduleService(db)
    today = datetime.now(timezone.utc).date()
    result = await svc.get_day_schedule(current_user.id, today)

    if not result:
        return success_response(data={
            "date": str(today),
            "items": [],
            "version": 0,
        })
    return success_response(data=result)


@router.get("/week/{week_start}")
async def get_week(
    week_start: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """某周日程（7天）"""
    svc = ScheduleService(db)
    ws = _parse_date(week_start)
    result = await svc.get_week_schedule(current_user.id, ws)
    return success_response(data=result)


@router.get("/range")
async def get_range(
    start: str = Query(..., description="开始日期"),
    end: str = Query(..., description="结束日期"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """日期范围日程"""
    svc = ScheduleService(db)
    s = _parse_date(start)
    e = _parse_date(end)
    if e < s:
        raise HTTPException(400, detail=error_response(
            "VALIDATION_ERROR", "结束日期不能早于开始日期"
        ))
    if (e - s).days > 31:
        raise HTTPException(400, detail=error_response(
            "VALIDATION_ERROR", "范围不能超过31天"
        ))
    result = await svc.get_range_schedule(current_user.id, s, e)
    return success_response(data=result)


@router.get("/{date}")
async def get_day(
    date: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """某日日程"""
    svc = ScheduleService(db)
    d = _parse_date(date)
    result = await svc.get_day_schedule(current_user.id, d)

    if not result:
        return success_response(data={
            "date": str(d),
            "items": [],
            "version": 0,
        })
    return success_response(data=result)


# ========================================
#  AI 生成
# ========================================

@router.post("/generate")
async def generate_schedule(
    body: ScheduleGenerateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    AI 生成日程。
    调用调度算法为指定日期范围排程。
    """
    svc = ScheduleService(db)
    s = _parse_date(body.start_date)
    e = _parse_date(body.end_date)

    if e < s:
        raise HTTPException(400, detail=error_response(
            "VALIDATION_ERROR", "结束日期不能早于开始日期"
        ))

    result = await svc.generate_schedule(
        user_id=current_user.id,
        start_date=s,
        end_date=e,
        task_ids=body.task_ids or None,
        include_recurring=body.include_recurring,
        regenerate=body.regenerate,
    )
    return success_response(data=result)


# ========================================
#  调整
# ========================================

@router.post("/adjust")
async def adjust_item(
    body: ScheduleAdjustRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """手动调整某个日程项的时间"""
    svc = ScheduleService(db)
    d = _parse_date(body.date)
    _validate_time_range(body.new_start, body.new_end)

    try:
        result = await svc.adjust_item(
            user_id=current_user.id,
            target_date=d,
            item_id=body.item_id,
            new_start=body.new_start,
            new_end=body.new_end,
            version=body.version,
        )
    except ValueError as e:
        code = 409 if "版本冲突" in str(e) else 400
        raise HTTPException(code, detail=error_response("SCHEDULE_ERROR", str(e)))

    return success_response(data=result)


@router.post("/swap")
async def swap_items(
    body: ScheduleSwapRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """交换两个日程项的时间"""
    svc = ScheduleService(db)
    d = _parse_date(body.date)

    try:
        result = await svc.swap_items(
            user_id=current_user.id,
            target_date=d,
            item_id_1=body.item_id_1,
            item_id_2=body.item_id_2,
            version=body.version,
        )
    except ValueError as e:
        code = 409 if "版本冲突" in str(e) else 400
        raise HTTPException(code, detail=error_response("SCHEDULE_ERROR", str(e)))

    return success_response(data=result)


# ========================================
#  冲突检测
# ========================================

@router.post("/check-conflicts")
async def check_conflicts(
    body: CheckConflictsRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """检查指定日期范围内的冲突（只读，不修改数据）"""
    svc = ScheduleService(db)
    s = _parse_date(body.start_date)
    e = _parse_date(body.end_date)

    result = await svc.check_conflicts(current_user.id, s, e)
    return success_response(data=result)


# ========================================
#  手动日程管理（新增）
# ========================================

@router.post("/items")
async def add_manual_item(
    body: ManualItemCreateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    手动添加日程项。
    source='manual'，可选关联精灵。
    手动项同样参与冲突检测和周报统计。
    """
    d = _parse_date(body.date)
    _validate_time_range(body.time_start, body.time_end)

    if body.spirit and body.spirit not in VALID_SPIRITS:
        raise HTTPException(400, detail=error_response(
            "VALIDATION_ERROR", f"无效的精灵代码: {body.spirit}"
        ))

    svc = ScheduleService(db)
    try:
        result = await svc.add_manual_item(
            user_id=current_user.id,
            target_date=d,
            title=body.title,
            time_start=body.time_start,
            time_end=body.time_end,
            spirit=body.spirit,
            note=body.note,
            is_fixed=body.is_fixed,
        )
    except ValueError as e:
        raise HTTPException(400, detail=error_response("SCHEDULE_ERROR", str(e)))

    return success_response(
        data=result,
        message="日程项已添加",
    )


@router.patch("/items/{item_id}")
async def update_schedule_item(
    item_id: str,
    body: ManualItemUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    编辑日程项（标题/时间/备注/精灵/优先级）。
    同时支持 AI 项和手动项。
    """
    d = _parse_date(body.date)

    if body.time_start and body.time_end:
        _validate_time_range(body.time_start, body.time_end)
    if body.spirit and body.spirit not in VALID_SPIRITS:
        raise HTTPException(400, detail=error_response(
            "VALIDATION_ERROR", f"无效的精灵代码: {body.spirit}"
        ))

    updates = body.model_dump(exclude_unset=True, exclude={"date", "version"})
    if not updates:
        raise HTTPException(400, detail=error_response(
            "VALIDATION_ERROR", "至少需要更新一个字段"
        ))

    svc = ScheduleService(db)
    try:
        result = await svc.update_item(
            user_id=current_user.id,
            target_date=d,
            item_id=item_id,
            updates=updates,
            version=body.version,
        )
    except ValueError as e:
        code = 409 if "版本冲突" in str(e) else 400
        raise HTTPException(code, detail=error_response("SCHEDULE_ERROR", str(e)))

    return success_response(
        data=result,
        message="日程项已更新",
    )


@router.delete("/items/{item_id}")
async def delete_schedule_item(
    item_id: str,
    body: DeleteItemRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    删除单个日程项。
    如果是 AI 项（有 subtask_id），关联子任务重置为 pending。
    """
    d = _parse_date(body.date)

    svc = ScheduleService(db)
    try:
        result = await svc.delete_item(
            user_id=current_user.id,
            target_date=d,
            item_id=item_id,
            version=body.version,
        )
    except ValueError as e:
        code = 409 if "版本冲突" in str(e) else 400
        raise HTTPException(code, detail=error_response("SCHEDULE_ERROR", str(e)))

    return success_response(
        data=result,
        message="日程项已删除",
    )