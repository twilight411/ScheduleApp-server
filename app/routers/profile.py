"""
用户画像接口 — Onboarding 引导 + 偏好设置 + 精灵强度

Onboarding 三阶段:
  POST /profile/onboarding/stage1  — 注册即问（作息/衔接/拆分/年度关键词）
  POST /profile/onboarding/stage2  — 精灵强度引导（五精灵 ABC 选项）
  POST /profile/onboarding/stage3  — 冲突处理偏好（首次冲突时弹出）
  POST /profile/onboarding         — 一次性提交（兼容旧接口 + 支持全量提交）
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.auth import get_current_user
from app.models.user import User
from app.schemas.common import success_response, error_response
from app.schemas.profile import (
    OnboardingRequest,
    OnboardingStage1,
    OnboardingStage2,
    OnboardingStage3,
    PreferencesUpdateRequest,
    IntensityUpdateRequest,
    BatchIntensityUpdateRequest,
    SpiritIntensityOut,
    ProfileOut,
)
from app.services.profile_service import ProfileService
from app.services.intensity_service import IntensityService, SPIRIT_NAMES

router = APIRouter(prefix="/profile", tags=["Profile"])


def _profile_to_out(profile) -> dict:
    """将 UserProfile ORM 对象转为响应字典"""
    intensities = []
    for si in (profile.spirit_intensities or []):
        intensities.append({
            "spirit_code": si.spirit_code,
            "spirit_name": SPIRIT_NAMES.get(si.spirit_code, si.spirit_code),
            "base_intensity": si.base_intensity,
            "learned_delta": si.learned_delta,
            "effective_intensity": si.effective_intensity,
            "is_locked": si.is_locked,
        })

    return {
        "preferences": profile.preferences or {},
        "tags": profile.tags or [],
        "onboarding_completed": profile.onboarding_completed,
        "spirit_intensities": intensities,
    }


# ========================================
#  画像读取
# ========================================

@router.get("")
async def get_profile(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """获取完整画像（含偏好 + 精灵强度）"""
    svc = ProfileService(db)
    profile = await svc.get_profile(current_user.id)
    if not profile:
        raise HTTPException(
            status_code=404,
            detail=error_response("RESOURCE_NOT_FOUND", "用户画像不存在"),
        )
    return success_response(data=_profile_to_out(profile))


# ========================================
#  偏好更新
# ========================================

@router.patch("")
async def update_preferences(
    body: PreferencesUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """增量更新偏好设置"""
    svc = ProfileService(db)
    new_prefs = body.model_dump(exclude_unset=True)
    profile = await svc.update_preferences(current_user.id, new_prefs)
    return success_response(
        data=_profile_to_out(profile),
        message="偏好已更新",
    )


# ========================================
#  Onboarding — 分阶段提交
# ========================================

@router.post("/onboarding/stage1")
async def onboarding_stage1(
    body: OnboardingStage1,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Stage 1 — 注册即问

    Q1 chronotype:      early_bird / standard / night_owl
    Q2 task_transition:  tight / comfortable / loose
    Q3 chunk_style:      ant / balanced / sprint
    Q4 annual_keyword:   breakthrough / repair / explore / stable
    """
    svc = ProfileService(db)
    profile = await svc.process_onboarding_stage1(current_user.id, body)
    return success_response(
        data=_profile_to_out(profile),
        message="基础画像已设置",
    )


@router.post("/onboarding/stage2")
async def onboarding_stage2(
    body: OnboardingStage2,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Stage 2 — 精灵强度引导

    每个精灵选 high / mid / low:
      light_intensity:     学业工作
      water_intensity:     休闲娱乐
      soil_intensity:      身心健康
      air_intensity:       社交互动
      nutrition_intensity: 兴趣成长
    """
    svc = ProfileService(db)
    profile = await svc.process_onboarding_stage2(current_user.id, body)
    return success_response(
        data=_profile_to_out(profile),
        message="精灵强度已设置",
    )


@router.post("/onboarding/stage3")
async def onboarding_stage3(
    body: OnboardingStage3,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Stage 3 — 冲突处理偏好（首次冲突时弹出）

    conflict_strategy: auto_defer / ask / auto_trim
    """
    svc = ProfileService(db)
    profile = await svc.process_onboarding_stage3(current_user.id, body)
    return success_response(
        data=_profile_to_out(profile),
        message="冲突处理偏好已设置",
    )


@router.post("/onboarding")
async def onboarding_full(
    body: OnboardingRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    一次性提交 Onboarding（兼容旧接口 + 支持新字段全量提交）。
    支持部分提交 — 只会处理非 null 的字段。
    """
    svc = ProfileService(db)
    profile = await svc.process_onboarding(current_user.id, body)
    return success_response(
        data=_profile_to_out(profile),
        message="引导设置完成",
    )


# ========================================
#  精灵强度管理
# ========================================

@router.get("/intensity")
async def get_intensities(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """获取所有精灵强度"""
    svc = IntensityService(db)
    intensities = await svc.get_intensities(current_user.id)
    return success_response(data=[
        {
            "spirit_code": si.spirit_code,
            "spirit_name": SPIRIT_NAMES.get(si.spirit_code, si.spirit_code),
            "base_intensity": si.base_intensity,
            "learned_delta": si.learned_delta,
            "effective_intensity": si.effective_intensity,
            "is_locked": si.is_locked,
        }
        for si in intensities
    ])


@router.patch("/intensity")
async def update_intensity(
    body: IntensityUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """更新单个精灵强度"""
    svc = IntensityService(db)
    try:
        si = await svc.update_intensity(
            current_user.id,
            body.spirit_code,
            body.base_intensity,
            body.is_locked,
        )
        return success_response(data={
            "spirit_code": si.spirit_code,
            "base_intensity": si.base_intensity,
            "effective_intensity": si.effective_intensity,
            "is_locked": si.is_locked,
        })
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error_response("VALIDATION_ERROR", str(e)),
        )


@router.patch("/intensity/batch")
async def batch_update_intensities(
    body: BatchIntensityUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """批量更新精灵强度 — {"intensities": {"light": 80, "soil": 60}}"""
    svc = IntensityService(db)
    try:
        updated = await svc.batch_update_intensities(current_user.id, body.intensities)
        return success_response(data=[
            {
                "spirit_code": si.spirit_code,
                "base_intensity": si.base_intensity,
                "effective_intensity": si.effective_intensity,
            }
            for si in updated
        ])
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error_response("VALIDATION_ERROR", str(e)),
        )


@router.post("/intensity/template")
async def apply_template(
    body: dict,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """应用强度模板"""
    template_id = body.get("template_id")
    if not template_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error_response("VALIDATION_ERROR", "缺少 template_id"),
        )
    svc = IntensityService(db)
    try:
        updated = await svc.apply_template(current_user.id, uuid.UUID(template_id))
        return success_response(data=[
            {
                "spirit_code": si.spirit_code,
                "base_intensity": si.base_intensity,
                "effective_intensity": si.effective_intensity,
            }
            for si in updated
        ])
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error_response("VALIDATION_ERROR", str(e)),
        )


@router.get("/intensity/templates")
async def get_templates(
    db: AsyncSession = Depends(get_db),
):
    """获取所有可用的强度模板"""
    svc = IntensityService(db)
    templates = await svc.get_templates()
    return success_response(data=[
        {
            "id": str(t.id),
            "name": t.name,
            "description": t.description,
            "icon": t.icon,
            "intensities": t.intensities,
        }
        for t in templates
    ])


# ========================================
#  洞察（预留）
# ========================================

@router.get("/insights")
async def get_insights(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """获取个性化洞察（Phase 5+ 实现）"""
    return success_response(
        data={"insights": [], "generated_at": None},
        message="洞察功能将在后续版本上线",
    )
