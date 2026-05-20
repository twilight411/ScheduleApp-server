"""
果实收藏接口 — 果实墙展示
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.auth import get_current_user
from app.models.user import User
from app.schemas.common import success_response, error_response
from app.services.fruit_service import FruitService, get_fruit_type

router = APIRouter(prefix="/fruits", tags=["Fruits"])


@router.get("/collection")
async def get_collection(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    果实收藏墙 — 返回用户所有历史月度果实。
    按月份倒序排列，附带果实 emoji 和描述。
    """
    svc = FruitService(db)
    fruits = await svc.get_collection(current_user.id)

    collection = []
    for f in fruits:
        # 用果实类型查找 emoji 和 description（DB 中未存储这两个展示字段）
        fruit_info = get_fruit_type(f.overall_score)
        collection.append({
            "month": f.month,
            "fruit_type": f.fruit_type,
            "fruit_name": f.fruit_name,
            "fruit_emoji": fruit_info.get("emoji", ""),
            "fruit_rarity": f.fruit_rarity,
            "fruit_description": fruit_info.get("description", ""),
            "overall_score": f.overall_score,
            "weekly_scores": f.weekly_scores,
            "spirit_monthly": f.spirit_monthly,
            "best_spirit": f.best_spirit,
            "weakest_spirit": f.weakest_spirit,
            "awards": f.awards,
            "monthly_narrative": f.monthly_narrative,
        })

    return success_response(data={
        "total": len(collection),
        "fruits": collection,
    })


@router.get("/image")
async def get_fruit_image(
    month: str = Query(None, description="月份 YYYY-MM"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    获取月度果实图像。
    根据用户本月五个维度的得分生成个性化的果实图像。
    果实的形态取决于本月用户的得分情况，特别是最佳维度。
    """
    svc = FruitService(db)

    if not month:
        from datetime import datetime
        today = datetime.now()
        month = f"{today.year}-{str(today.month).zfill(2)}"
    else:
        if len(month) != 7 or month[4] != "-":
            raise HTTPException(400, detail=error_response(
                "VALIDATION_ERROR", "月份格式错误，应为 YYYY-MM"
            ))

    fruit = await svc.get_fruit(current_user.id, month)
    if not fruit:
        fruit = await svc.generate_monthly_fruit(current_user.id, month)

    fruit_info = get_fruit_type(fruit.overall_score)

    image_url = await svc.generate_fruit_image(
        month=fruit.month,
        overall_score=fruit.overall_score,
        fruit_info=fruit_info,
        spirit_monthly=fruit.spirit_monthly,
        best_spirit=fruit.best_spirit,
        awards=fruit.awards or [],
        user_id=current_user.id,
    )

    return success_response(data={
        "month": fruit.month,
        "fruit_type": fruit.fruit_type,
        "fruit_name": fruit.fruit_name,
        "fruit_rarity": fruit.fruit_rarity,
        "overall_score": fruit.overall_score,
        "best_spirit": fruit.best_spirit,
        "image_url": image_url,
    })