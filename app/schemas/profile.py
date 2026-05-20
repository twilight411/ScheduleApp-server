"""
用户画像 Schemas — Onboarding 问卷 + 偏好设置 + 精灵强度

OnboardingRequest 的问题设计来自「弹出问题.docx」，
分为两个阶段：
  Stage 1 — 注册即问（基础画像 4 题）
  Stage 2 — 进入主界面后引导设置（五精灵强度 5 题）
  Stage 3 — 首次冲突时弹出（冲突处理偏好 1 题）
"""
from typing import Optional, Literal
from pydantic import BaseModel, Field


# ====================================================================
#  用户偏好（完整数据结构）
# ====================================================================

class UserPreferences(BaseModel):
    """
    用户画像偏好 — 存储在 user_profiles.preferences (JSONB)
    所有字段均有默认值，支持增量更新。
    """
    # ── 基础作息 ──
    wake_time: str = "07:30"
    sleep_time: str = "23:30"
    peak_hours: list[str] = Field(default_factory=lambda: ["09:00-11:00", "14:00-16:00"])
    meal_times: list[str] = Field(default_factory=lambda: ["08:00", "12:00", "18:30"])
    energy_pattern: str = "balanced"
    # morning / night / balanced

    # ── 工作偏好 ──
    max_continuous_work_minutes: int = 120
    preferred_break_duration: int = 15
    buffer_minutes_between_tasks: int = 15
    # 对应弹出问题.docx Q2: 严丝合缝=5 / 游刃有余=15 / 随性而为=30

    # ── 任务拆分偏好 ──
    chunk_style: str = "balanced"
    # ant(蚂蚁搬家：30-60min碎片) / balanced(稳扎稳打：2-3h) / sprint(暴力通关：半天+)

    # ── 年度关键词 ──
    annual_keyword: str = ""
    # breakthrough(突破) / repair(修复) / explore(探索) / stable(稳定)

    # ── 健康 ──
    daily_exercise_target_minutes: int = 30

    # ── 社交 ──
    social_importance: str = "medium"
    # high / medium / low

    # ── 精灵优先级（协商时用）──
    spirit_priority: list[str] = Field(
        default_factory=lambda: ["light", "soil", "water", "air", "nutrition"]
    )

    # ── 冲突处理偏好 ──
    conflict_strategy: str = "ask"
    # auto_defer(全部自动顺延) / ask(全部征求意见) / auto_trim(自动取舍保睡眠)

    # ── 作息类型标签（来自 Q1）──
    chronotype: str = "standard"
    # early_bird(规律晨型人) / standard(朝九晚五) / night_owl(灵感夜猫子)


# ====================================================================
#  Onboarding 请求 — Stage 1: 注册即问（4 题）
# ====================================================================

class OnboardingStage1(BaseModel):
    """
    注册即问 — 对应弹出问题.docx 的前 4 个问题。
    前端在注册成功后立即展示。
    """
    # Q1: 标准作息类型
    chronotype: Literal["early_bird", "standard", "night_owl"]
    # A=early_bird, B=standard, C=night_owl

    # Q2: 任务衔接偏好
    task_transition: Literal["tight", "comfortable", "loose"]
    # A=tight(严丝合缝), B=comfortable(游刃有余), C=loose(随性而为)

    # Q3: 大项目处理风格
    chunk_style: Literal["ant", "balanced", "sprint"]
    # A=ant(蚂蚁搬家), B=balanced(稳扎稳打), C=sprint(暴力通关)

    # Q4: 年度关键词（可多选，但建议选 1 个）
    annual_keyword: Literal["breakthrough", "repair", "explore", "stable"]
    # 突破 / 修复 / 探索 / 稳定


# ====================================================================
#  Onboarding 请求 — Stage 2: 精灵强度设置（5 题）
# ====================================================================

class OnboardingStage2(BaseModel):
    """
    五精灵强度引导 — 对应弹出问题.docx 的 5 个强度问题。
    前端在进入主界面后弹出引导设置。

    每个精灵三选一: high(A) / mid(B) / low(C)
    """
    # Q1: 学业工作 — 光精灵
    light_intensity: Literal["high", "mid", "low"]
    # A=拼命三郎(high), B=职场主力(mid), C=暂时躺平(low)

    # Q2: 休闲娱乐 — 水精灵
    water_intensity: Literal["high", "mid", "low"]
    # A=资深玩咖(high), B=质量达人(mid), C=闭目养神(low)

    # Q3: 身心健康 — 土壤精灵
    soil_intensity: Literal["high", "mid", "low"]
    # A=修炼狂人(high), B=养生博主(mid), C=忙碌打工人(low)

    # Q4: 社交互动 — 空气精灵
    air_intensity: Literal["high", "mid", "low"]
    # A=社交达人(high), B=定时开放(mid), C=深度社恐(low)

    # Q5: 兴趣成长 — 营养精灵
    nutrition_intensity: Literal["high", "mid", "low"]
    # A=头号玩家(high), B=业余爱好者(mid), C=纯粹凑热闹(low)


# ====================================================================
#  Onboarding 请求 — Stage 3: 冲突处理偏好（1 题）
# ====================================================================

class OnboardingStage3(BaseModel):
    """
    冲突处理偏好 — 在用户首次产生日程冲突或首次任务未完成时弹出。
    """
    # Q: 如果计划临时有变，你希望我怎么做？
    conflict_strategy: Literal["auto_defer", "ask", "auto_trim"]
    # A=auto_defer(全部自动顺延)
    # B=ask(全部征求意见)
    # C=auto_trim(自动取舍保睡眠)


# ====================================================================
#  兼容旧接口的统一 OnboardingRequest
# ====================================================================

class OnboardingRequest(BaseModel):
    """
    统一的 Onboarding 请求体。
    支持一次性提交所有阶段，也支持分阶段提交。

    向后兼容：旧字段 work_schedule / energy_pattern 仍可用。
    """
    # ── Stage 1 字段 ──
    chronotype: Optional[Literal["early_bird", "standard", "night_owl"]] = None
    task_transition: Optional[Literal["tight", "comfortable", "loose"]] = None
    chunk_style: Optional[Literal["ant", "balanced", "sprint"]] = None
    annual_keyword: Optional[Literal["breakthrough", "repair", "explore", "stable"]] = None

    # ── Stage 2 字段 ──
    light_intensity: Optional[Literal["high", "mid", "low"]] = None
    water_intensity: Optional[Literal["high", "mid", "low"]] = None
    soil_intensity: Optional[Literal["high", "mid", "low"]] = None
    air_intensity: Optional[Literal["high", "mid", "low"]] = None
    nutrition_intensity: Optional[Literal["high", "mid", "low"]] = None

    # ── Stage 3 字段 ──
    conflict_strategy: Optional[Literal["auto_defer", "ask", "auto_trim"]] = None

    # ── 旧接口兼容 ──
    work_schedule: Optional[str] = None
    energy_pattern: Optional[str] = None
    exercise_habit: Optional[str] = None
    social_frequency: Optional[str] = None


# ====================================================================
#  精灵强度 Schema
# ====================================================================

class SpiritIntensityOut(BaseModel):
    spirit_code: str
    spirit_name: str = ""
    base_intensity: int
    learned_delta: float = 0.0
    effective_intensity: int = 50
    is_locked: bool = False


class IntensityUpdateRequest(BaseModel):
    spirit_code: str
    base_intensity: int = Field(ge=0, le=100)
    is_locked: Optional[bool] = None


class BatchIntensityUpdateRequest(BaseModel):
    intensities: dict[str, int]
    # {"light": 80, "soil": 60, ...}


class IntensityTemplateOut(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    icon: Optional[str] = None
    intensities: dict[str, int]


# ====================================================================
#  画像响应
# ====================================================================

class ProfileOut(BaseModel):
    preferences: dict = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    onboarding_completed: bool = False
    spirit_intensities: list[SpiritIntensityOut] = Field(default_factory=list)


class PreferencesUpdateRequest(BaseModel):
    """增量更新偏好 — 只传需要改的字段"""
    wake_time: Optional[str] = None
    sleep_time: Optional[str] = None
    peak_hours: Optional[list[str]] = None
    meal_times: Optional[list[str]] = None
    energy_pattern: Optional[str] = None
    max_continuous_work_minutes: Optional[int] = None
    preferred_break_duration: Optional[int] = None
    buffer_minutes_between_tasks: Optional[int] = None
    chunk_style: Optional[str] = None
    annual_keyword: Optional[str] = None
    daily_exercise_target_minutes: Optional[int] = None
    social_importance: Optional[str] = None
    spirit_priority: Optional[list[str]] = None
    conflict_strategy: Optional[str] = None
    chronotype: Optional[str] = None
