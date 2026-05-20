"""
报告接口 — 周报、月度果实查询与生成

[P1 修复] 删除 router 层手动 commit，由 get_db() 统一处理
"""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.auth import get_current_user
from app.models.user import User
from app.schemas.common import success_response, error_response
from app.services.report_service import ReportService

router = APIRouter(prefix="/reports", tags=["Reports"])


def _parse_week_start(week_start: str) -> datetime:
    try:
        d = datetime.strptime(week_start, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        raise HTTPException(400, detail=error_response(
            "VALIDATION_ERROR", "日期格式错误，请使用 YYYY-MM-DD"
        ))
    if d.weekday() != 0:
        raise HTTPException(400, detail=error_response(
            "VALIDATION_ERROR", "week_start 必须是周一"
        ))
    return d


# ========================================
#  周报
# ========================================

@router.get("/weekly")
async def get_weekly_report(
    week_start: str = Query(None, description="周一日期 YYYY-MM-DD"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """获取周报。如果未生成则实时触发生成。"""
    svc = ReportService(db)

    if not week_start:
        today = datetime.now(timezone.utc).date()
        ws = today - timedelta(days=today.weekday())
    else:
        ws = _parse_week_start(week_start)

    report = await svc.get_report(current_user.id, ws)
    if not report:
        report = await svc.generate_weekly_report(current_user.id, ws)
        # ❌ 旧代码：await db.commit()

    return success_response(data={
        "week_start": str(report.week_start),
        "week_end": str(report.week_end),
        "headline": report.headline,
        "overall_score": report.overall_score,
        "vs_last_week": report.vs_last_week,
        "stats": report.stats,
        "tree": report.tree_data,
        "analysis": report.analysis,
        "next_week_suggestions": report.next_week_suggestions,
    })


@router.get("/weekly/latest")
async def get_latest_weekly(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """最新周报"""
    svc = ReportService(db)
    report = await svc.get_latest_report(current_user.id)
    if not report:
        raise HTTPException(404, detail=error_response(
            "RESOURCE_NOT_FOUND", "暂无周报"
        ))

    return success_response(data={
        "week_start": str(report.week_start),
        "week_end": str(report.week_end),
        "headline": report.headline,
        "overall_score": report.overall_score,
        "vs_last_week": report.vs_last_week,
        "stats": report.stats,
        "tree": report.tree_data,
        "analysis": report.analysis,
        "next_week_suggestions": report.next_week_suggestions,
    })


@router.post("/weekly/regenerate")
async def regenerate_weekly(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """强制重新生成本周周报"""
    svc = ReportService(db)
    today = datetime.now(timezone.utc).date()
    ws = today - timedelta(days=today.weekday())
    report = await svc.generate_weekly_report(current_user.id, ws, force=True)
    # ❌ 旧代码：await db.commit()

    return success_response(
        data={
            "week_start": str(report.week_start),
            "week_end": str(report.week_end),
            "headline": report.headline,
            "overall_score": report.overall_score,
        },
        message="周报已重新生成",
    )


# ========================================
#  月度果实
# ========================================

@router.get("/monthly")
async def get_monthly_report(
    month: str = Query(None, description="月份 YYYY-MM"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """获取月度果实"""
    from app.services.fruit_service import FruitService
    svc = FruitService(db)

    if not month:
        today = datetime.now(timezone.utc).date()
        first_of_month = today.replace(day=1)
        last_month = first_of_month - timedelta(days=1)
        month = last_month.strftime("%Y-%m")

    fruit = await svc.get_fruit(current_user.id, month)
    if not fruit:
        raise HTTPException(404, detail=error_response(
            "RESOURCE_NOT_FOUND", f"暂无 {month} 的月度果实"
        ))

    return success_response(data={
        "month": fruit.month,
        "fruit_type": fruit.fruit_type,
        "fruit_name": fruit.fruit_name,
        "fruit_rarity": fruit.fruit_rarity,
        "overall_score": fruit.overall_score,
        "weekly_scores": fruit.weekly_scores,
        "spirit_monthly": fruit.spirit_monthly,
        "best_spirit": fruit.best_spirit,
        "weakest_spirit": fruit.weakest_spirit,
        "awards": fruit.awards,
        "monthly_narrative": fruit.monthly_narrative,
    })


@router.get("/monthly/latest")
async def get_latest_monthly(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """最新月度果实"""
    from app.services.fruit_service import FruitService
    svc = FruitService(db)

    fruit = await svc.get_latest_fruit(current_user.id)
    if not fruit:
        raise HTTPException(404, detail=error_response(
            "RESOURCE_NOT_FOUND", "暂无月度果实"
        ))

    return success_response(data={
        "month": fruit.month,
        "fruit_type": fruit.fruit_type,
        "fruit_name": fruit.fruit_name,
        "fruit_rarity": fruit.fruit_rarity,
        "overall_score": fruit.overall_score,
        "weekly_scores": fruit.weekly_scores,
        "spirit_monthly": fruit.spirit_monthly,
        "best_spirit": fruit.best_spirit,
        "weakest_spirit": fruit.weakest_spirit,
        "awards": fruit.awards,
        "monthly_narrative": fruit.monthly_narrative,
    })