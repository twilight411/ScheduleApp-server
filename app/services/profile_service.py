"""
用户画像服务 — 偏好管理、Onboarding 问卷处理、行为学习、标签推断

Onboarding 分三阶段:
  Stage 1 — 注册即问（作息/衔接/拆分/年度关键词）→ 写入 preferences
  Stage 2 — 进入主界面引导（五精灵强度 ABC）→ 写入 spirit_intensities
  Stage 3 — 首次冲突弹出（冲突处理偏好）→ 写入 preferences.conflict_strategy
"""
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.profile import UserProfile, SpiritIntensity
from app.schemas.profile import (
    UserPreferences,
    OnboardingRequest,
    OnboardingStage1,
    OnboardingStage2,
    OnboardingStage3,
)

import structlog

logger = structlog.get_logger()


# ===== 默认偏好值 =====
DEFAULT_PREFERENCES = UserPreferences().model_dump()

# ====================================================================
#  Stage 1 映射规则 — 基于弹出问题.docx
# ====================================================================

# Q1: 标准作息类型 → 作息参数
CHRONOTYPE_MAP = {
    "early_bird": {
        "chronotype": "early_bird",
        "wake_time": "06:00",
        "sleep_time": "22:30",
        "peak_hours": ["06:30-08:30", "09:00-11:00"],
        "energy_pattern": "morning",
    },
    "standard": {
        "chronotype": "standard",
        "wake_time": "07:30",
        "sleep_time": "23:30",
        "peak_hours": ["09:00-11:00", "14:00-16:00"],
        "energy_pattern": "balanced",
    },
    "night_owl": {
        "chronotype": "night_owl",
        "wake_time": "09:00",
        "sleep_time": "01:00",
        "peak_hours": ["14:00-16:00", "20:00-23:00"],
        "energy_pattern": "night",
    },
}

# Q2: 任务衔接偏好 → buffer_minutes
TASK_TRANSITION_MAP = {
    "tight": {
        "buffer_minutes_between_tasks": 5,
        "max_continuous_work_minutes": 150,
    },
    "comfortable": {
        "buffer_minutes_between_tasks": 15,
        "max_continuous_work_minutes": 120,
    },
    "loose": {
        "buffer_minutes_between_tasks": 30,
        "max_continuous_work_minutes": 90,
    },
}

# Q3: 大项目拆分风格
CHUNK_STYLE_MAP = {
    "ant": {"chunk_style": "ant"},          # 蚂蚁搬家: 30-60min 碎片
    "balanced": {"chunk_style": "balanced"},  # 稳扎稳打: 2-3h 阶段
    "sprint": {"chunk_style": "sprint"},      # 暴力通关: 半天以上
}

# Q4: 年度关键词 → 精灵优先级倾向 + 标签
ANNUAL_KEYWORD_MAP = {
    "breakthrough": {
        "annual_keyword": "breakthrough",
        "spirit_priority": ["light", "nutrition", "soil", "water", "air"],
        "_tags": ["goal_oriented", "career_focused"],
    },
    "repair": {
        "annual_keyword": "repair",
        "spirit_priority": ["soil", "water", "air", "nutrition", "light"],
        "_tags": ["health_conscious", "recovery_mode"],
    },
    "explore": {
        "annual_keyword": "explore",
        "spirit_priority": ["nutrition", "air", "water", "light", "soil"],
        "_tags": ["explorer", "social_seeker"],
    },
    "stable": {
        "annual_keyword": "stable",
        "spirit_priority": ["soil", "light", "water", "air", "nutrition"],
        "_tags": ["stability_seeker", "balanced"],
    },
}


# ====================================================================
#  Stage 2 映射规则 — 精灵强度 ABC → 数值
# ====================================================================

INTENSITY_LEVEL_MAP = {
    "high": 80,
    "mid": 50,
    "low": 25,
}


# ====================================================================
#  旧接口兼容映射（保留原有逻辑）
# ====================================================================

LEGACY_WORK_SCHEDULE_MAP = {
    "9to5": CHRONOTYPE_MAP["standard"],
    "flexible": CHRONOTYPE_MAP["standard"],
    "shift": CHRONOTYPE_MAP["early_bird"],
}

LEGACY_ENERGY_MAP = {
    "morning": CHRONOTYPE_MAP["early_bird"],
    "night": CHRONOTYPE_MAP["night_owl"],
    "balanced": CHRONOTYPE_MAP["standard"],
}

LEGACY_EXERCISE_MAP = {
    "daily": {"daily_exercise_target_minutes": 60},
    "sometimes": {"daily_exercise_target_minutes": 30},
    "rarely": {"daily_exercise_target_minutes": 15},
}

LEGACY_SOCIAL_MAP = {
    "daily": {"social_importance": "high"},
    "weekly": {"social_importance": "medium"},
    "monthly": {"social_importance": "low"},
    "rarely": {"social_importance": "low"},
}


class ProfileService:

    def __init__(self, db: AsyncSession):
        self.db = db

    # ========================================
    #  读取
    # ========================================

    async def get_profile(self, user_id: uuid.UUID) -> Optional[UserProfile]:
        """获取用户画像（含精灵强度）"""
        result = await self.db.execute(
            select(UserProfile)
            .where(UserProfile.user_id == user_id)
            .options(selectinload(UserProfile.spirit_intensities))
        )
        return result.scalar_one_or_none()

    # ========================================
    #  偏好更新
    # ========================================

    async def update_preferences(self, user_id: uuid.UUID, new_prefs: dict) -> UserProfile:
        """更新偏好设置 — 增量合并"""
        profile = await self.get_profile(user_id)
        if not profile:
            raise ValueError("用户画像不存在")

        current = profile.preferences or {}
        merged = {**DEFAULT_PREFERENCES, **current, **new_prefs}

        validated = UserPreferences(**merged)
        profile.preferences = validated.model_dump()
        profile.updated_at = datetime.now(timezone.utc)
        await self.db.flush()
        return profile

    # ========================================
    #  Onboarding — 统一入口
    # ========================================

    async def process_onboarding(
        self, user_id: uuid.UUID, answers: OnboardingRequest
    ) -> UserProfile:
        """
        处理 Onboarding 问卷 — 同时兼容新旧格式。
        新格式字段优先于旧格式字段。
        """
        profile = await self.get_profile(user_id)
        if not profile:
            raise ValueError("用户画像不存在")

        prefs = dict(DEFAULT_PREFERENCES)
        prefs.update(profile.preferences or {})
        tags = list(profile.tags or [])

        # ── Stage 1: 作息 & 偏好 ──
        prefs, tags = self._apply_stage1(answers, prefs, tags)

        # ── Stage 2: 精灵强度 ──
        intensity_values = self._extract_intensity_values(answers)

        # ── Stage 3: 冲突策略 ──
        if answers.conflict_strategy:
            prefs["conflict_strategy"] = answers.conflict_strategy

        # ── 旧格式兼容 ──
        prefs = self._apply_legacy_fields(answers, prefs)

        # ── 写入 ──
        validated = UserPreferences(**prefs)
        profile.preferences = validated.model_dump()
        profile.tags = list(set(tags))
        profile.onboarding_completed = True
        profile.updated_at = datetime.now(timezone.utc)

        # 设置精灵强度
        if intensity_values:
            for si in profile.spirit_intensities:
                if si.spirit_code in intensity_values:
                    si.base_intensity = intensity_values[si.spirit_code]
                    si.updated_at = datetime.now(timezone.utc)

        await self.db.flush()
        logger.info(
            "onboarding_processed",
            user_id=str(user_id),
            intensity_values=intensity_values,
            tags=profile.tags,
        )
        return profile

    # ========================================
    #  Stage 1 处理
    # ========================================

    def _apply_stage1(
        self, answers: OnboardingRequest, prefs: dict, tags: list
    ) -> tuple[dict, list]:
        """应用 Stage 1 的 4 个问题到偏好"""

        # Q1: 作息类型
        if answers.chronotype:
            mapping = CHRONOTYPE_MAP.get(answers.chronotype, {})
            prefs.update(mapping)

        # Q2: 任务衔接
        if answers.task_transition:
            mapping = TASK_TRANSITION_MAP.get(answers.task_transition, {})
            prefs.update(mapping)

        # Q3: 拆分风格
        if answers.chunk_style:
            mapping = CHUNK_STYLE_MAP.get(answers.chunk_style, {})
            prefs.update(mapping)

        # Q4: 年度关键词
        if answers.annual_keyword:
            mapping = ANNUAL_KEYWORD_MAP.get(answers.annual_keyword, {})
            new_tags = mapping.pop("_tags", [])
            prefs.update(mapping)
            tags.extend(new_tags)

        return prefs, tags

    # ========================================
    #  Stage 2 处理 — 精灵强度提取
    # ========================================

    def _extract_intensity_values(self, answers: OnboardingRequest) -> dict[str, int]:
        """从 Stage 2 的 ABC 选项提取精灵强度数值"""
        result = {}

        spirit_fields = {
            "light": answers.light_intensity,
            "water": answers.water_intensity,
            "soil": answers.soil_intensity,
            "air": answers.air_intensity,
            "nutrition": answers.nutrition_intensity,
        }

        for spirit_code, level in spirit_fields.items():
            if level:
                result[spirit_code] = INTENSITY_LEVEL_MAP.get(level, 50)

        return result

    # ========================================
    #  旧格式兼容
    # ========================================

    def _apply_legacy_fields(self, answers: OnboardingRequest, prefs: dict) -> dict:
        """处理旧格式字段（如果新字段已处理，旧字段不会覆盖）"""

        # 只在新字段未提供时才用旧字段
        if not answers.chronotype and answers.work_schedule:
            mapping = LEGACY_WORK_SCHEDULE_MAP.get(answers.work_schedule, {})
            for k, v in mapping.items():
                prefs.setdefault(k, v)

        if not answers.chronotype and answers.energy_pattern:
            mapping = LEGACY_ENERGY_MAP.get(answers.energy_pattern, {})
            for k, v in mapping.items():
                prefs.setdefault(k, v)

        if answers.exercise_habit:
            mapping = LEGACY_EXERCISE_MAP.get(answers.exercise_habit, {})
            prefs.update(mapping)

        if answers.social_frequency:
            mapping = LEGACY_SOCIAL_MAP.get(answers.social_frequency, {})
            prefs.update(mapping)

        return prefs

    # ========================================
    #  Onboarding — 分阶段接口
    # ========================================

    async def process_onboarding_stage1(
        self, user_id: uuid.UUID, stage1: OnboardingStage1
    ) -> UserProfile:
        """处理 Stage 1 — 注册即问"""
        full = OnboardingRequest(
            chronotype=stage1.chronotype,
            task_transition=stage1.task_transition,
            chunk_style=stage1.chunk_style,
            annual_keyword=stage1.annual_keyword,
        )
        return await self.process_onboarding(user_id, full)

    async def process_onboarding_stage2(
        self, user_id: uuid.UUID, stage2: OnboardingStage2
    ) -> UserProfile:
        """处理 Stage 2 — 精灵强度设置"""
        full = OnboardingRequest(
            light_intensity=stage2.light_intensity,
            water_intensity=stage2.water_intensity,
            soil_intensity=stage2.soil_intensity,
            air_intensity=stage2.air_intensity,
            nutrition_intensity=stage2.nutrition_intensity,
        )
        return await self.process_onboarding(user_id, full)

    async def process_onboarding_stage3(
        self, user_id: uuid.UUID, stage3: OnboardingStage3
    ) -> UserProfile:
        """处理 Stage 3 — 冲突处理偏好"""
        full = OnboardingRequest(conflict_strategy=stage3.conflict_strategy)
        return await self.process_onboarding(user_id, full)

    # ========================================
    #  为其他服务提供参数
    # ========================================

    async def get_scheduling_params(self, user_id: uuid.UUID) -> dict:
        """为日程调度器提供个性化参数"""
        profile = await self.get_profile(user_id)
        if not profile:
            return {}

        prefs = UserPreferences(**(profile.preferences or {}))
        tags = profile.tags or []

        return {
            "wake_time": prefs.wake_time,
            "sleep_time": prefs.sleep_time,
            "peak_hours": prefs.peak_hours,
            "meal_times": prefs.meal_times,
            "max_continuous_work_minutes": prefs.max_continuous_work_minutes,
            "preferred_break_duration": prefs.preferred_break_duration,
            "buffer_minutes_between_tasks": prefs.buffer_minutes_between_tasks,
            "chunk_style": prefs.chunk_style,
            "daily_exercise_target_minutes": prefs.daily_exercise_target_minutes,
            "conflict_strategy": prefs.conflict_strategy,
            "buffer_multiplier": 1.3 if "procrastinator" in tags else 1.0,
            "max_daily_work_hours": 8 if "overcommitter" in tags else 10,
            "task_time_preferences": (profile.stats or {}).get("preferred_task_times", {}),
        }

    async def get_spirit_params(self, user_id: uuid.UUID, spirit_code: str) -> dict:
        """
        为精灵 Agent 提供个性化参数。
        *** 新增：返回当前精灵的有效强度，供精灵按强度切换 Prompt ***
        """
        profile = await self.get_profile(user_id)
        if not profile:
            return {"intensity": 50}

        # 查找对应精灵的强度
        intensity = 50
        for si in (profile.spirit_intensities or []):
            if si.spirit_code == spirit_code:
                intensity = si.effective_intensity
                break

        stats = profile.stats or {}
        prefs = profile.preferences or {}

        return {
            "intensity": intensity,
            "chunk_style": prefs.get("chunk_style", "balanced"),
            "conflict_strategy": prefs.get("conflict_strategy", "ask"),
            "annual_keyword": prefs.get("annual_keyword", ""),
            "chronotype": prefs.get("chronotype", "standard"),
            "completion_rate": stats.get("task_completion_rate", {}).get(spirit_code, 0.5),
            "avg_delay": stats.get("average_delay_minutes", {}).get(spirit_code, 0),
            "preferred_hours": stats.get("preferred_task_times", {}).get(spirit_code, {}),
            "user_tags": profile.tags or [],
        }

    async def get_negotiation_params(self, user_id: uuid.UUID) -> dict:
        """为协商引擎提供个性化参数"""
        profile = await self.get_profile(user_id)
        if not profile:
            return {}

        prefs = UserPreferences(**(profile.preferences or {}))
        tags = profile.tags or []
        return {
            "spirit_priority": prefs.spirit_priority,
            "health_strict": "health_conscious" in tags,
            "social_importance": prefs.social_importance,
            "conflict_strategy": prefs.conflict_strategy,
        }
