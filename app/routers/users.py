"""
用户信息接口 — GET/PATCH /users/me, POST /users/me/avatar
"""
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.auth import get_current_user
from app.models.user import User
from app.schemas.common import success_response, error_response
from app.schemas.user import UserOut, UserUpdateRequest
from app.services.user_service import UserService
from app.services.file_service import FileService
from app.services.event_service import EventService

router = APIRouter(prefix="/users", tags=["Users"])


@router.get("/me")
async def get_me(current_user: User = Depends(get_current_user)):
    """获取当前用户信息"""
    return success_response(data=UserOut.model_validate(current_user).model_dump())


@router.patch("/me")
async def update_me(
    body: UserUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """更新用户信息（name, timezone）"""
    svc = UserService(db)
    updated = await svc.update_user(current_user, body)

    # 记录事件
    evt_svc = EventService(db)
    await evt_svc.record_event(
        current_user.id,
        "profile_updated",
        {"fields": list(body.model_dump(exclude_unset=True).keys())},
    )

    return success_response(data=UserOut.model_validate(updated).model_dump())


@router.post("/me/avatar")
async def upload_avatar(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    上传头像。
    支持 JPEG/PNG/WebP/GIF，最大 5MB。
    """
    file_svc = FileService(db)
    user_svc = UserService(db)

    try:
        content = await file.read()
        avatar_url = await file_svc.save_avatar(
            user_id=current_user.id,
            file_content=content,
            content_type=file.content_type or "image/jpeg",
            filename=file.filename or "avatar.jpg",
        )
        updated = await user_svc.update_avatar(current_user, avatar_url)
        return success_response(
            data={"avatar_url": avatar_url},
            message="头像上传成功",
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error_response("VALIDATION_ERROR", str(e)),
        )
