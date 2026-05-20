"""
通知接口 — 设备注册 / 通知设置 / 站内消息历史 / 标记已读

[P1 修复]
  - 删除 router 层手动 commit（4 处）—— get_db() 已经在请求结束 commit
  - 双重 commit 在 SQLAlchemy 不报错，但会触发不必要的事务边界变化，
    在 SQLite 上偶尔诱发"another transaction in progress"
"""
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.auth import get_current_user
from app.models.user import User
from app.schemas.common import success_response, error_response
from app.services.notification_service import NotificationService

router = APIRouter(prefix="/notifications", tags=["Notifications"])


# ===== Schemas =====

class RegisterDeviceRequest(BaseModel):
    device_token: str = Field(min_length=1, max_length=500)
    platform: str = Field(pattern=r"^(ios|android|web)$")


class UpdateSettingsRequest(BaseModel):
    daily_schedule_push: Optional[bool] = None
    task_reminder_push: Optional[bool] = None
    weekly_report_push: Optional[bool] = None
    monthly_fruit_push: Optional[bool] = None
    spirit_tip_push: Optional[bool] = None
    sedentary_reminder: Optional[bool] = None
    quiet_hours_start: Optional[str] = Field(None, pattern=r"^\d{2}:\d{2}$")
    quiet_hours_end: Optional[str] = Field(None, pattern=r"^\d{2}:\d{2}$")


class MarkReadRequest(BaseModel):
    notification_ids: list[uuid.UUID] = Field(min_length=1, max_length=100)


# ========================================
#  设备注册
# ========================================

@router.post("/register-device")
async def register_device(
    body: RegisterDeviceRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """注册推送设备。"""
    svc = NotificationService(db)
    try:
        device = await svc.register_device(
            user_id=current_user.id,
            device_token=body.device_token,
            platform=body.platform,
        )
        # ❌ 旧代码：await db.commit() — 由 get_db() 统一处理
    except ValueError as e:
        raise HTTPException(400, detail=error_response("VALIDATION_ERROR", str(e)))

    return success_response(
        data={
            "device_id": str(device.id),
            "platform": device.platform,
            "is_active": device.is_active,
        },
        message="设备注册成功",
    )


# ========================================
#  通知设置
# ========================================

@router.get("/settings")
async def get_notification_settings(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """获取通知设置（不存在则返回默认值）"""
    svc = NotificationService(db)
    settings = await svc.get_settings(current_user.id)
    # ❌ 旧代码：await db.commit() — 即便有新建默认设置，由 get_db() commit

    return success_response(data={
        "daily_schedule_push": settings.daily_schedule_push,
        "task_reminder_push": settings.task_reminder_push,
        "weekly_report_push": settings.weekly_report_push,
        "monthly_fruit_push": settings.monthly_fruit_push,
        "spirit_tip_push": settings.spirit_tip_push,
        "sedentary_reminder": settings.sedentary_reminder,
        "quiet_hours_start": settings.quiet_hours_start,
        "quiet_hours_end": settings.quiet_hours_end,
    })


@router.patch("/settings")
async def update_notification_settings(
    body: UpdateSettingsRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """更新通知设置（只传需要修改的字段）"""
    svc = NotificationService(db)
    updates = body.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(400, detail=error_response(
            "VALIDATION_ERROR", "至少需要更新一个字段"
        ))

    settings = await svc.update_settings(current_user.id, updates)
    # ❌ 旧代码：await db.commit()

    return success_response(
        data={
            "daily_schedule_push": settings.daily_schedule_push,
            "task_reminder_push": settings.task_reminder_push,
            "weekly_report_push": settings.weekly_report_push,
            "monthly_fruit_push": settings.monthly_fruit_push,
            "spirit_tip_push": settings.spirit_tip_push,
            "sedentary_reminder": settings.sedentary_reminder,
            "quiet_hours_start": settings.quiet_hours_start,
            "quiet_hours_end": settings.quiet_hours_end,
        },
        message="通知设置已更新",
    )


# ========================================
#  站内消息历史
# ========================================

@router.get("/history")
async def get_history(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    type: Optional[str] = Query(None, description="通知类型过滤"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """站内消息历史（分页）"""
    svc = NotificationService(db)
    result = await svc.get_history(
        user_id=current_user.id,
        page=page,
        page_size=page_size,
        type_filter=type,
    )
    return success_response(data=result)


# ========================================
#  标记已读
# ========================================

@router.post("/mark-read")
async def mark_read(
    body: MarkReadRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """标记指定通知为已读"""
    svc = NotificationService(db)
    count = await svc.mark_read(current_user.id, body.notification_ids)
    # ❌ 旧代码：await db.commit()

    return success_response(
        data={"marked_count": count},
        message=f"已标记 {count} 条为已读",
    )