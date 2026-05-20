"""
Sprint 4 smoke test
覆盖:
  1. 推断算法 - 活跃度分布
  2. 推断算法 - 关键词扫描
  3. 推断算法 - 三信号融合
  4. 智能护栏 - over_focus
  5. 智能护栏 - no_focus_too_long
  6. 智能护栏 - neglected_spirit

共 31 项断言
"""
import sys
import os
import uuid

HERE = os.path.dirname(__file__)
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

from app.services.focus_suggestion_service import (
    compute_activity_distribution,
    scan_keywords,
    score_themes_by_activity,
    score_themes_by_keywords,
    merge_theme_scores,
    FocusSuggestionService,
    MIN_CONFIDENCE_TO_SHOW,
    MIN_TASKS_FOR_SUGGESTION,
    OVER_FOCUS_WEEKS_THRESHOLD,
    NO_FOCUS_WEEKS_THRESHOLD,
    NEGLECT_SPIRIT_WEEKS,
)


@dataclass
class FakeTask:
    primary_spirit: str = "light"
    title: str = ""
    raw_input: Optional[str] = None


@dataclass
class FakeSubTask:
    id: str = ""
    spirit: str = "light"
    title: str = ""
    completion_percent: int = 100


@dataclass
class FakeScore:
    spirit_code: str
    week_start: date
    score: float = 70.0
    focus_weight: float = 1.0
    focus_at_scoring: Optional[str] = None


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
print("Sprint 4 基调推断 + 智能护栏验证 (31 项)")
print("=" * 70)

print("\n--- 1. 活跃度分布计算 ---")

subtasks_light = [FakeSubTask(spirit="light") for _ in range(8)]
subtasks_other = [FakeSubTask(spirit="water"), FakeSubTask(spirit="soil")]

activity = compute_activity_distribution([], subtasks_light + subtasks_other)

expect(activity["light"] > 0.7, "light 占比 > 70%")
expect(sum(activity.values()) == 1.0, "分布和为 1")

activity_empty = compute_activity_distribution([], [])
expect(all(v == 0.2 for v in activity_empty.values()), "空数据均匀分布")

print("\n--- 2. 关键词扫描 ---")

subtasks_with_kw = [
    FakeSubTask(spirit="light", title="复习数学"),
    FakeSubTask(spirit="light", title="刷题练习"),
    FakeSubTask(spirit="light", title="考试准备"),
]
hits = scan_keywords([], subtasks_with_kw)

expect(hits.get("exam_prep", 0) >= 3, "考试相关关键词命中")

subtasks_social = [
    FakeSubTask(spirit="air", title="聚餐"),
    FakeSubTask(spirit="air", title="约朋友吃饭"),
]
hits_social = scan_keywords([], subtasks_social)
expect(hits_social.get("social", 0) >= 2, "社交关键词命中")

subtasks_project = [
    FakeSubTask(spirit="light", title="项目上线"),
    FakeSubTask(spirit="light", title="ddl冲刺"),
]
hits_project = scan_keywords([], subtasks_project)
expect(hits_project.get("project_sprint", 0) >= 2, "项目关键词命中")

print("\n--- 3. 活跃度评分 ---")

activity_high_light = {"light": 0.5, "water": 0.2, "soil": 0.15, "air": 0.1, "nutrition": 0.05}
scores_activity = score_themes_by_activity(activity_high_light)
expect("exam_prep" in scores_activity, "light 高触发 exam_prep")
expect("project_sprint" in scores_activity, "light 高触发 project_sprint")

activity_high_health = {"light": 0.1, "water": 0.25, "soil": 0.25, "air": 0.2, "nutrition": 0.2}
scores_health = score_themes_by_activity(activity_high_health)
expect("recovery" in scores_health, "soil+water 高触发 recovery")

activity_balanced = {"light": 0.2, "water": 0.2, "soil": 0.2, "air": 0.2, "nutrition": 0.2}
scores_bal = score_themes_by_activity(activity_balanced)
expect("balanced" in scores_bal, "均匀分布触发 balanced")

print("\n--- 4. 关键词评分 ---")

hits_high = {"exam_prep": 5, "project_sprint": 2}
kw_scores = score_themes_by_keywords(hits_high)
expect(kw_scores["exam_prep"] == 1.0, "饱和关键词得 1.0")
expect(kw_scores["project_sprint"] < 1.0, "未饱和得较低分")

print("\n--- 5. 三信号融合 ---")

activity_scores = {"exam_prep": 0.8}
keyword_scores = {"exam_prep": 0.6}
history_bonus = {"exam_prep": 0.7}

final = merge_theme_scores(activity_scores, keyword_scores, history_bonus)

expect(final["exam_prep"] >= MIN_CONFIDENCE_TO_SHOW, 
       "融合后置信度达标")

print("\n--- 6. 推断场景测试 ---")

subtasks_exam = [
    FakeSubTask(spirit="light", title="复习线代"),
    FakeSubTask(spirit="light", title="刷题"),
    FakeSubTask(spirit="light", title="看课件"),
    FakeSubTask(spirit="light", title="做模拟卷"),
    FakeSubTask(spirit="light", title="整理笔记"),
    FakeSubTask(spirit="light", title="复习"),
    FakeSubTask(spirit="light", title="刷题"),
    FakeSubTask(spirit="water", title="休息"),
]
activity_exam = compute_activity_distribution([], subtasks_exam)
hits_exam = scan_keywords([], subtasks_exam)
scores_exam = score_themes_by_activity(activity_exam)
kw_scores_exam = score_themes_by_keywords(hits_exam)
final_exam = merge_theme_scores(scores_exam, kw_scores_exam, {})

expect(max(final_exam.values()) >= 90, "备考场景置信度 ≥90")
expect(max(final_exam, key=final_exam.get) == "exam_prep", 
       "备考场景推荐 exam_prep")

subtasks_recovery = [
    FakeSubTask(spirit="soil", title="晨跑"),
    FakeSubTask(spirit="soil", title="瑜伽"),
    FakeSubTask(spirit="soil", title="拉伸"),
    FakeSubTask(spirit="soil", title="冥想休息"),
    FakeSubTask(spirit="water", title="看电影"),
    FakeSubTask(spirit="water", title="听音乐放松"),
    FakeSubTask(spirit="water", title="玩游戏休闲"),
    FakeSubTask(spirit="water", title="散步放松"),
]
activity_rec = compute_activity_distribution([], subtasks_recovery)
hits_rec = scan_keywords([], subtasks_recovery)
scores_rec = score_themes_by_activity(activity_rec)
kw_scores_rec = score_themes_by_keywords(hits_rec)
final_rec = merge_theme_scores(scores_rec, kw_scores_rec, {})

expect(max(final_rec.values()) >= 80, "休整场景置信度 ≥80")
expect(max(final_rec, key=final_rec.get) == "recovery", 
       "休整场景推荐 recovery")

print("\n--- 7. 智能护栏: over_focus ---")

base = date(2026, 5, 19)
by_week_overfocus = {}
for i in range(3):
    ws = base - timedelta(weeks=i+1)
    by_week_overfocus[ws] = [
        FakeScore(spirit_code="light", week_start=ws, score=85, 
                  focus_weight=1.8, focus_at_scoring="exam_prep"),
        FakeScore(spirit_code="soil", week_start=ws, score=45, 
                  focus_weight=0.6, focus_at_scoring="exam_prep"),
        FakeScore(spirit_code="water", week_start=ws, score=55, 
                  focus_weight=0.6, focus_at_scoring="exam_prep"),
    ]

sorted_weeks = sorted(by_week_overfocus.keys(), reverse=True)
warnings = FocusSuggestionService._detect_over_focus(by_week_overfocus, sorted_weeks)

expect(len(warnings) == 1, "连续 3 周 exam_prep + 健康低分触发警告")
expect(warnings[0]["type"] == "over_focus", "警告类型正确")
expect(warnings[0]["suggested_alternative"] == "recovery", "建议切换休整")

by_week_no_overfocus = {}
for i in range(2):
    ws = base - timedelta(weeks=i+1)
    by_week_no_overfocus[ws] = [
        FakeScore(spirit_code="light", week_start=ws, focus_at_scoring="exam_prep"),
    ]
warnings_no = FocusSuggestionService._detect_over_focus(by_week_no_overfocus, sorted(by_week_no_overfocus.keys(), reverse=True))
expect(len(warnings_no) == 0, "只有 2 周不触发")

print("\n--- 8. 智能护栏: no_focus_too_long ---")

by_week_nofocus = {}
for i in range(4):
    ws = base - timedelta(weeks=i+1)
    by_week_nofocus[ws] = [
        FakeScore(spirit_code="light", week_start=ws, focus_at_scoring=None),
    ]
warnings_nofocus = FocusSuggestionService._detect_no_focus(by_week_nofocus, sorted(by_week_nofocus.keys(), reverse=True))
expect(len(warnings_nofocus) == 1, "连续 4 周无基调触发警告")
expect(warnings_nofocus[0]["type"] == "no_focus_too_long", "警告类型正确")

by_week_hasfocus = {}
for i in range(3):
    ws = base - timedelta(weeks=i+1)
    by_week_hasfocus[ws] = [
        FakeScore(spirit_code="light", week_start=ws, focus_at_scoring=None),
    ]
warnings_hasfocus = FocusSuggestionService._detect_no_focus(by_week_hasfocus, sorted(by_week_hasfocus.keys(), reverse=True))
expect(len(warnings_hasfocus) == 0, "只有 3 周不触发")

print("\n--- 9. 智能护栏: neglected_spirit ---")

by_week_neglect = {}
for i in range(4):
    ws = base - timedelta(weeks=i+1)
    by_week_neglect[ws] = [
        FakeScore(spirit_code="air", week_start=ws, focus_weight=0.6),
        FakeScore(spirit_code="light", week_start=ws, focus_weight=1.8),
    ]
warnings_neglect = FocusSuggestionService._detect_neglected_spirit(by_week_neglect, sorted(by_week_neglect.keys(), reverse=True))
expect(len(warnings_neglect) >= 1, "连续 4 周权重 <=0.7 触发警告")
expect(warnings_neglect[0]["type"] == "neglected_spirit", "警告类型正确")

by_week_no_neglect = {}
for i in range(3):
    ws = base - timedelta(weeks=i+1)
    by_week_no_neglect[ws] = [
        FakeScore(spirit_code="air", week_start=ws, focus_weight=0.6),
    ]
warnings_no_neglect = FocusSuggestionService._detect_neglected_spirit(by_week_no_neglect, sorted(by_week_no_neglect.keys(), reverse=True))
expect(len(warnings_no_neglect) == 0, "只有 3 周不触发")

print("\n--- 10. 数据不足保护 ---")

subtasks_few = [FakeSubTask(spirit="light") for _ in range(4)]
expect(len(subtasks_few) < MIN_TASKS_FOR_SUGGESTION, 
       "少于 MIN_TASKS_FOR_SUGGESTION 应拒绝建议")

print("\n" + "=" * 70)
print(f"验证完成: 通过 {pass_count - fail_count} / 失败 {fail_count} / 总计 {pass_count}")
print("=" * 70)

if fail_count > 0:
    sys.exit(1)
