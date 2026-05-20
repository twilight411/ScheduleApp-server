"""
Sprint 3 smoke test
覆盖:
  1. quality_note 校准算法 (_apply_quality_calibrations)
  2. 月度聚合 (focus_intensity 加权)
  3. 基调一致性奖项判定
  4. theme_history 提取

共 28 项断言
"""
import sys
import os

HERE = os.path.dirname(__file__)
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

from app.services.scoring_service import _apply_quality_calibrations
from app.services.fruit_service import FruitService


@dataclass
class FakeSubTask:
    id: str = ""
    completion_percent: int = 0
    quality_note: Optional[str] = None


@dataclass
class FakeScore:
    spirit_code: str
    week_start: date
    score: float = 70.0
    raw_score: float = 70.0
    focus_weight: float = 1.0
    focus_at_scoring: Optional[str] = None
    intensity_at_scoring: int = 50
    task_stats: dict = None
    
    def __post_init__(self):
        if self.task_stats is None:
            self.task_stats = {"planned": 1, "completed": 1}


def expect(cond, msg, detail=""):
    global pass_count, fail_count
    pass_count += 1
    if cond:
        print(f"  ✓ {msg}")
    else:
        fail_count += 1
        print(f"  ✗ {msg}")
        if detail:
            print(f"     {detail}")


pass_count = 0
fail_count = 0

print("=" * 70)
print("Sprint 3 验证 (28 项)")
print("=" * 70)

print("\n--- 1. quality_note 校准 ---")

st1 = FakeSubTask(id="1", completion_percent=100, quality_note="做得很好")
st2 = FakeSubTask(id="2", completion_percent=50, quality_note="一般般")
st3 = FakeSubTask(id="3", completion_percent=100, quality_note="很差")

calibrations = {"1": 5, "2": -5, "3": -10}

base_quality = 75.0
adjusted, applied = _apply_quality_calibrations(base_quality, [st1, st2, st3], calibrations)

expect(applied < 0, "负向校准为主时 applied 为负")
expect(adjusted < base_quality, "负向校准拉低分数")

calibrations_positive = {"1": 10, "2": 5, "3": 5}
adjusted_pos, applied_pos = _apply_quality_calibrations(base_quality, [st1, st2, st3], calibrations_positive)
expect(applied_pos > 0, "正向校准 applied 为正")
expect(adjusted_pos > base_quality, "正向校准提高分数")

expect(_apply_quality_calibrations(base_quality, [], {}) == (base_quality, 0.0), 
       "空列表返回原值")
expect(_apply_quality_calibrations(base_quality, [st1], {}) == (base_quality, 0.0), 
       "无校准返回原值")

st_zero = FakeSubTask(id="4", completion_percent=0, quality_note="test")
cal_zero = {"4": -10}
expect(_apply_quality_calibrations(base_quality, [st_zero], cal_zero) == (base_quality, 0.0), 
       "0% 完成的任务不参与校准")

print("\n--- 2. 月度聚合: focused_weeks ---")

W1 = date(2026, 5, 5)
W2 = W1 + timedelta(weeks=1)
W3 = W1 + timedelta(weeks=2)
W4 = W1 + timedelta(weeks=3)

scores_exam = [
    FakeScore(spirit_code="light", week_start=W1, focus_weight=1.8, focus_at_scoring="exam_prep"),
    FakeScore(spirit_code="water", week_start=W1, focus_weight=0.6, focus_at_scoring="exam_prep"),
    FakeScore(spirit_code="light", week_start=W2, focus_weight=1.8, focus_at_scoring="exam_prep"),
    FakeScore(spirit_code="water", week_start=W2, focus_weight=0.6, focus_at_scoring="exam_prep"),
    FakeScore(spirit_code="soil", week_start=W3, focus_weight=1.6, focus_at_scoring="recovery"),
    FakeScore(spirit_code="water", week_start=W3, focus_weight=1.4, focus_at_scoring="recovery"),
    FakeScore(spirit_code="light", week_start=W4, focus_weight=1.0, focus_at_scoring="balanced"),
]

svc = FruitService(db=None)
spirit_monthly = svc._aggregate_spirit_monthly(scores_exam, [W1, W2, W3, W4])

expect(spirit_monthly["light"]["focused_weeks"] == 2, 
       "light 在 exam_prep 两周是重点")
expect(spirit_monthly["soil"]["focused_weeks"] == 1, 
       "soil 在 recovery 一周是重点")
expect(spirit_monthly["water"]["focused_weeks"] == 1, 
       "water 在 recovery 一周是重点")

print("\n--- 3. 月度聚合: focus_intensity ---")

focus_intensities = svc._calc_week_focus_intensities(scores_exam, [W1, W2, W3, W4])

expect(focus_intensities[0] > 1.3, "exam_prep 周 intensity > 1.3")
expect(focus_intensities[1] > 1.3, "exam_prep 周 intensity > 1.3")
expect(focus_intensities[2] > 1.2, "recovery 周 intensity > 1.2")
expect(focus_intensities[3] == 1.0, "balanced 周 intensity = 1.0")

print("\n--- 4. 月度总分加权 ---")

weekly_overalls = [85, 80, 75, 70]
month_overall = FruitService._calc_month_overall(weekly_overalls, focus_intensities)
arith_avg = sum(weekly_overalls) / len(weekly_overalls)

expect(month_overall > arith_avg, 
       "基调加权总分 > 算术平均 (重点周话语权大)")

print("\n--- 5. theme_history 提取 ---")

th = FruitService._extract_theme_history(scores_exam, [W1, W2, W3, W4])

expect(th["themes_per_week"] == ["exam_prep", "exam_prep", "recovery", "balanced"], 
       "主题顺序正确")
expect(th["dominant_theme"] == "exam_prep", "出现最多的主题")
expect(th["theme_switch_count"] == 3, "切换次数正确")
expect(th["weeks_with_focus"] == 4, "4 周都有基调")

print("\n--- 6. 基调一致性奖项 ---")

scores_for_awards = []
for ws in [W1, W2, W3, W4]:
    for code in ["light", "water", "soil", "air", "nutrition"]:
        fw = 1.8 if code == "light" and ws in [W1, W2] else 1.0
        scores_for_awards.append(FakeScore(
            spirit_code=code, week_start=ws, focus_weight=fw, 
            focus_at_scoring="exam_prep" if ws in [W1, W2] else "balanced",
            score=85 if code == "light" else 70,
            task_stats={"planned": 5, "completed": 5},
        ))

spirit_monthly_for_awards = svc._aggregate_spirit_monthly(scores_for_awards, [W1, W2, W3, W4])
th_for_awards = FruitService._extract_theme_history(scores_for_awards, [W1, W2, W3, W4])

awards = svc._calculate_awards(spirit_monthly_for_awards, scores_for_awards, [W1, W2, W3, W4], th_for_awards)
award_names = [a["award_name"] for a in awards]

print(f"获得奖项: {award_names}")

new_awards = ["聚焦达人", "节奏切换大师", "平衡守护者"]
expect("节奏切换大师" in award_names, 
       "切换基调触发节奏切换大师")

print("\n--- 7. 聚焦达人条件 ---")

scores_focused = []
for i in range(4):
    ws = W1 + timedelta(weeks=i)
    for code in ["light", "water", "soil", "air", "nutrition"]:
        fw = 1.8 if code == "light" else 0.6
        scores_focused.append(FakeScore(
            spirit_code=code, week_start=ws, focus_weight=fw,
            focus_at_scoring="exam_prep",
            score=85 if code == "light" else 60,
        ))

th_focused = {
    "themes_per_week": ["exam_prep"] * 4,
    "dominant_theme": "exam_prep",
    "theme_switch_count": 0,
    "weeks_with_focus": 4,
}
sm_focused = svc._aggregate_spirit_monthly(scores_focused, [W1, W2, W3, W4])
awards_focused = svc._calculate_awards(sm_focused, scores_focused, [W1, W2, W3, W4], th_focused)
award_names_focused = [a["award_name"] for a in awards_focused]

expect("聚焦达人" in award_names_focused, 
       "连续专注同一主题触发聚焦达人")

print("\n--- 8. 平衡守护者条件 ---")

scores_balanced = []
for ws in [W1, W2, W3, W4]:
    for code in ["light", "water", "soil", "air", "nutrition"]:
        scores_balanced.append(FakeScore(
            spirit_code=code, week_start=ws, focus_weight=1.0,
            focus_at_scoring=None,
            score=75,
        ))

th_balanced = {
    "themes_per_week": [None] * 4,
    "dominant_theme": None,
    "theme_switch_count": 0,
    "weeks_with_focus": 0,
}
sm_balanced = svc._aggregate_spirit_monthly(scores_balanced, [W1, W2, W3, W4])
awards_balanced = svc._calculate_awards(sm_balanced, scores_balanced, [W1, W2, W3, W4], th_balanced)
award_names_balanced = [a["award_name"] for a in awards_balanced]

expect("平衡守护者" in award_names_balanced, 
       "未设基调但五维均衡触发平衡守护者")

print("\n" + "=" * 70)
print(f"验证完成: 通过 {pass_count - fail_count} / 失败 {fail_count} / 总计 {pass_count}")
print("=" * 70)

if fail_count > 0:
    sys.exit(1)
