"""
本周基调 Schemas — Sprint 1

校验规则严格对齐设计文档:
  - week_start 必须是周一
  - theme 必须在 VALID_THEMES 中
  - spirit_weights 五个 key 必填, 范围 [0.5, 2.0]
  - key_spirits 最多 2 个, 元素必须是合法精灵代码
  - theme=custom 时必须提供 custom_label
"""
from datetime import date
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from app.models.weekly_focus import (
    VALID_THEMES,
    VALID_SOURCES,
    MIN_WEIGHT,
    MAX_WEIGHT,
    DEFAULT_WEIGHT,
    MAX_KEY_SPIRITS,
)

SPIRIT_CODES = {"light", "water", "soil", "air", "nutrition"}


# ====================================================================
#  输入
# ====================================================================

class WeeklyFocusUpsertRequest(BaseModel):
    """
    设置/更新本周基调 (upsert)。
    同一 (user_id, week_start) 已有记录则覆盖。
    """
    week_start: date = Field(..., description="周一日期 YYYY-MM-DD")
    theme: str = Field("balanced", description="预设主题或 custom")
    custom_label: Optional[str] = Field(
        None, max_length=100, description="theme=custom 时必填"
    )
    spirit_weights: dict[str, float] = Field(
        ..., description="5精灵权重, 范围 [0.5, 2.0], 缺省按 1.0 处理"
    )
    key_spirits: list[str] = Field(
        default_factory=list,
        description=f"本周重点精灵, 最多 {MAX_KEY_SPIRITS} 个",
    )
    reason: Optional[str] = Field(None, max_length=500, description="设置原因")
    source: str = Field("manual")

    @field_validator("week_start")
    @classmethod
    def _week_start_must_be_monday(cls, v: date) -> date:
        if v.weekday() != 0:
            raise ValueError("week_start 必须是周一 (weekday=0)")
        return v

    @field_validator("theme")
    @classmethod
    def _theme_must_be_valid(cls, v: str) -> str:
        if v not in VALID_THEMES:
            raise ValueError(
                f"theme 必须是 {sorted(VALID_THEMES)} 之一, 收到 {v!r}"
            )
        return v

    @field_validator("source")
    @classmethod
    def _source_must_be_valid(cls, v: str) -> str:
        if v not in VALID_SOURCES:
            raise ValueError(
                f"source 必须是 {sorted(VALID_SOURCES)} 之一, 收到 {v!r}"
            )
        return v

    @field_validator("key_spirits")
    @classmethod
    def _key_spirits_valid(cls, v: list[str]) -> list[str]:
        if len(v) > MAX_KEY_SPIRITS:
            raise ValueError(
                f"key_spirits 最多 {MAX_KEY_SPIRITS} 个, 收到 {len(v)} 个"
            )
        for s in v:
            if s not in SPIRIT_CODES:
                raise ValueError(
                    f"key_spirits 元素 {s!r} 不在合法精灵代码 {sorted(SPIRIT_CODES)} 中"
                )
        return list(dict.fromkeys(v))

    @field_validator("spirit_weights")
    @classmethod
    def _spirit_weights_valid(cls, v: dict[str, float]) -> dict[str, float]:
        """
        校验 + 规范化:
          - 拒绝未知 key
          - 缺失的精灵自动补 1.0
          - 每个 weight 必须在 [0.5, 2.0] 内
        """
        unknown = set(v.keys()) - SPIRIT_CODES
        if unknown:
            raise ValueError(f"spirit_weights 出现未知精灵代码: {sorted(unknown)}")

        cleaned: dict[str, float] = {}
        for code in SPIRIT_CODES:
            w = v.get(code, DEFAULT_WEIGHT)
            try:
                w = float(w)
            except (TypeError, ValueError):
                raise ValueError(f"{code} 的权重必须是数字, 收到 {w!r}")
            if not (MIN_WEIGHT <= w <= MAX_WEIGHT):
                raise ValueError(
                    f"{code} 的权重 {w} 超出范围 [{MIN_WEIGHT}, {MAX_WEIGHT}]"
                )
            cleaned[code] = round(w, 2)
        return cleaned

    @model_validator(mode="after")
    def _custom_requires_label(self):
        if self.theme == "custom" and not (self.custom_label or "").strip():
            raise ValueError("theme=custom 时必须提供 custom_label")
        return self


# ====================================================================
#  输出
# ====================================================================

class WeeklyFocusOut(BaseModel):
    id: str
    week_start: date
    theme: str
    custom_label: Optional[str] = None
    spirit_weights: dict[str, float]
    key_spirits: list[str]
    reason: Optional[str] = None
    source: str
    display_label: str = ""


class WeeklyFocusTemplateOut(BaseModel):
    """预设基调模板,给前端选择 UI"""
    theme: str
    label: str
    description: str
    icon: str
    spirit_weights: dict[str, float]
    key_spirits: list[str]
