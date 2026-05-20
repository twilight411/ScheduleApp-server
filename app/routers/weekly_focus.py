"""
本周基调接口 — Sprint 1 + Sprint 4

端点:
  GET    /focus/weekly?week_start=YYYY-MM-DD  — 查询某周(默认本周)
  POST   /focus/weekly                        — 创建/更新某周基调 (upsert)
  DELETE /focus/weekly?week_start=YYYY-MM-DD  — 取消某周基调
  GET    /focus/presets                       — 列出预设主题
  GET    /focus/suggestion?week_start=YYYY-MM-DD  — Sprint 4: 基调推断建议
"""
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.auth import get_current_user
from app.models.user import User
from app.models.weekly_focus import WeeklyFocus
from app.schemas.common import success_response, error_response
from app.schemas.weekly_focus import WeeklyFocusUpsertRequest
from app.services.weekly_focus_service import (
    WeeklyFocusService,
    get_week_start,
)
from app.services.focus_suggestion_service import FocusSuggestionService


router = APIRouter(prefix="/focus", tags=["WeeklyFocus"])


def _parse_week_start(week_start: Optional[str]) -> date:
    if not week_start:
        return get_week_start()
    try:
        d = datetime.strptime(week_start, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(400, detail=error_response(
            "VALIDATION_ERROR", "日期格式错误,请使用 YYYY-MM-DD",
        ))
    if d.weekday() != 0:
        raise HTTPException(400, detail=error_response(
            "VALIDATION_ERROR", "week_start 必须是周一",
        ))
    return d


def _focus_to_dict(focus: WeeklyFocus) -> dict:
    return {
        "id": str(focus.id),
        "week_start": str(focus.week_start),
        "theme": focus.theme,
        "custom_label": focus.custom_label,
        "spirit_weights": focus.spirit_weights,
        "key_spirits": focus.key_spirits or [],
        "reason": focus.reason,
        "source": focus.source,
        "display_label": WeeklyFocusService.display_label(focus),
        "created_at": focus.created_at.isoformat() if focus.created_at else None,
        "updated_at": focus.updated_at.isoformat() if focus.updated_at else None,
    }


@router.get("/weekly")
async def get_weekly_focus(
    week_start: Optional[str] = Query(
        None, description="周一日期 YYYY-MM-DD, 缺省取本周"
    ),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    ws = _parse_week_start(week_start)
    svc = WeeklyFocusService(db)
    focus = await svc.get_focus(current_user.id, ws)

    if not focus:
        snapshot = await svc.get_focus_snapshot(current_user.id, ws)
        return success_response(data={
            "week_start": str(ws),
            "focus": None,
            "default_snapshot": snapshot,
            "message": "本周尚未设置基调",
        })

    return success_response(data={
        "week_start": str(ws),
        "focus": _focus_to_dict(focus),
    })


@router.post("/weekly")
async def upsert_weekly_focus(
    body: WeeklyFocusUpsertRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    svc = WeeklyFocusService(db)
    focus = await svc.upsert_focus(
        user_id=current_user.id,
        week_start=body.week_start,
        theme=body.theme,
        spirit_weights=body.spirit_weights,
        key_spirits=body.key_spirits,
        custom_label=body.custom_label,
        reason=body.reason,
        source=body.source,
    )
    return success_response(
        data=_focus_to_dict(focus),
        message="基调已保存",
    )


@router.delete("/weekly")
async def delete_weekly_focus(
    week_start: Optional[str] = Query(
        None, description="周一日期 YYYY-MM-DD, 缺省取本周"
    ),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    ws = _parse_week_start(week_start)
    svc = WeeklyFocusService(db)
    deleted = await svc.delete_focus(current_user.id, ws)
    if not deleted:
        raise HTTPException(404, detail=error_response(
            "RESOURCE_NOT_FOUND", "该周未设置基调",
        ))
    return success_response(message="已取消基调")


@router.get("/presets")
async def list_focus_presets(
    current_user: User = Depends(get_current_user),
):
    return success_response(data={
        "presets": WeeklyFocusService.list_presets(),
    })


# ========================================
#  Sprint 4: 基调推断建议
# ========================================

@router.get("/suggestion")
async def get_focus_suggestion(
    week_start: Optional[str] = Query(
        None, description="周一日期 YYYY-MM-DD, 缺省取本周"
    ),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    获取本周基调的智能推断建议。

    基于过去 2 周的任务分布、关键词和历史基调倾向综合推断。
    返回结构包含:
      - has_suggestion: 是否有建议
      - suggested_theme: 建议的主题
      - confidence: 置信度 (0-100)
      - reasons: 推荐理由列表
      - alternative_themes: 备选主题列表
      - warnings: 护栏警告 (过度聚焦/长期未设基调/长期忽略某方向)

    即使已设基调, warnings 仍会返回,用于提醒用户注意长期模式。
    """
    ws = _parse_week_start(week_start)
    svc = FocusSuggestionService(db)
    result = await svc.build_suggestion_response(current_user.id, ws)

    return success_response(data={
        "week_start": str(ws),
        **result,
    })