"""
报告系统 Schema — 周报、生命树、月度果实
"""
from datetime import date
from typing import Optional

from pydantic import BaseModel


class TreeBranchOut(BaseModel):
    spirit_code: str
    spirit_name: str
    spirit_emoji: str
    position: str
    score: float
    level: str
    color: str
    intensity: int
    comment: Optional[str] = None


class TreeDataOut(BaseModel):
    week_start: date
    overall_score: float
    overall_level: str
    branches: list[TreeBranchOut]
    tree_health: str
    season_label: str
    weekly_summary_line: Optional[str] = None
    weakest_spirit: Optional[str] = None
    weakest_suggestion: Optional[str] = None


class WeeklyReportOut(BaseModel):
    week_start: date
    week_end: date
    headline: str
    overall_score: float
    vs_last_week: Optional[float]
    stats: dict
    tree: TreeDataOut
    analysis: dict
    next_week_suggestions: Optional[list] = None


class MonthlyFruitOut(BaseModel):
    month: str
    fruit_type: str
    fruit_name: str
    fruit_emoji: str = ""
    fruit_rarity: str
    fruit_description: str = ""
    overall_score: float
    weekly_scores: list[float]
    spirit_monthly: dict
    best_spirit: Optional[str]
    weakest_spirit: Optional[str]
    awards: Optional[list] = None
    monthly_narrative: Optional[str] = None
