"""
生命树接口 — 周树数据 + 历史趋势

[P1 修复] 删除 router 层手动 commit
"""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.auth import get_current_user
from app.models.user import User
from app.schemas.common import success_response, error_response
from app.services.tree_service import TreeService

router = APIRouter(prefix="/tree", tags=["Tree"])


@router.get("/weekly")
async def get_weekly_tree(
    week_start: str = Query(None, description="周一日期 YYYY-MM-DD"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    获取周生命树数据。
    如该周尚未打分，会自动触发打分。
    """
    svc = TreeService(db)

    if not week_start:
        today = datetime.now(timezone.utc).date()
        ws = today - timedelta(days=today.weekday())
    else:
        try:
            ws = datetime.strptime(week_start, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(400, detail=error_response(
                "VALIDATION_ERROR", "日期格式错误"
            ))

    tree_data = await svc.build_tree_data(current_user.id, ws)
    # ❌ 旧代码：await db.commit() — get_db() 统一处理

    return success_response(data=tree_data)


@router.get("/history")
async def get_tree_history(
    months: int = Query(3, ge=1, le=12, description="查看最近N个月"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """生命树历史趋势"""
    svc = TreeService(db)
    history = await svc.get_tree_history(current_user.id, months)
    return success_response(data={"history": history, "months": months})


@router.get("/weekly/image")
async def get_weekly_tree_image(
    week_start: str = Query(None, description="周一日期 YYYY-MM-DD"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    获取周生命树图像。
    根据用户本周五个维度的得分生成个性化的生命树图像。
    树的形态取决于本周用户的得分情况。
    """
    svc = TreeService(db)

    if not week_start:
        today = datetime.now(timezone.utc).date()
        ws = today - timedelta(days=today.weekday())
    else:
        try:
            ws = datetime.strptime(week_start, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(400, detail=error_response(
                "VALIDATION_ERROR", "日期格式错误"
            ))

    scores = await svc.scoring_svc.get_week_scores(current_user.id, ws)
    if not scores:
        scores = await svc.scoring_svc.calculate_all_spirits(current_user.id, ws)

    branches = []
    for score in scores:
        meta = svc.SPIRIT_META.get(score.spirit_code, {})
        level = score.level
        color = svc.BRANCH_COLORS.get(
            score.spirit_code, {}
        ).get(level, "#9E9E9E")

        branches.append({
            "spirit_code": score.spirit_code,
            "spirit_name": meta.get("name", score.spirit_code),
            "spirit_emoji": meta.get("emoji", ""),
            "position": meta.get("position", "left"),
            "score": score.score,
            "level": level,
            "color": color,
            "intensity": score.intensity_at_scoring,
            "comment": score.spirit_comment or "",
        })

    overall = await svc.scoring_svc.get_overall_score(current_user.id, ws)
    tree_health = svc._get_tree_health(overall)
    season = await svc._get_season_label(current_user.id, ws, overall)

    image_url = await svc.generate_tree_image(
        branches=branches,
        overall=overall,
        tree_health=tree_health,
        season=season,
        user_id=current_user.id,
    )

    return success_response(data={
        "week_start": str(ws),
        "overall_score": overall,
        "tree_health": tree_health,
        "season_label": season,
        "image_url": image_url,
    })