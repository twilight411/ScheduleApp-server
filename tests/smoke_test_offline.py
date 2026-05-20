"""
Sprint 1 离线 schema 校验
- 不依赖数据库, 只校验 Pydantic 模型
- 覆盖: WeeklyFocusUpsertRequest / SubTaskCompletionUpdateRequest / THEME_PRESETS
"""
import sys
import os

HERE = os.path.dirname(__file__)
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from pydantic import ValidationError

from app.schemas.weekly_focus import WeeklyFocusUpsertRequest
from app.schemas.task import SubTaskCompletionUpdateRequest
from app.services.weekly_focus_service import THEME_PRESETS, SPIRIT_CODES


def expect(cond, msg):
    global pass_count, fail_count
    if cond:
        pass_count += 1
        print(f"  ✓ {msg}")
    else:
        fail_count += 1
        print(f"  ✗ {msg}")


pass_count = 0
fail_count = 0

print("=" * 70)
print("Sprint 1 离线 Schema 校验")
print("=" * 70)

print("\n--- 1. WeeklyFocusUpsertRequest 校验 ---")

expect(
    all(set(preset["spirit_weights"].keys()) == set(SPIRIT_CODES) 
        for preset in THEME_PRESETS.values()),
    "所有主题预设包含全部 5 个精灵"
)

expect(
    all(0.5 <= w <= 2.0 for preset in THEME_PRESETS.values() 
        for w in preset["spirit_weights"].values()),
    "所有权重在 [0.5, 2.0] 范围内"
)

expect(
    THEME_PRESETS["balanced"]["key_spirits"] == [],
    "balanced 主题无重点精灵"
)

expect(
    THEME_PRESETS["exam_prep"]["spirit_weights"]["light"] == 1.8,
    "exam_prep light 权重 = 1.8"
)

print("\n--- 2. SubTaskCompletionUpdateRequest 校验 ---")

valid_percents = [0, 25, 50, 75, 100]
for pct in valid_percents:
    try:
        req = SubTaskCompletionUpdateRequest(completion_percent=pct)
        expect(req.completion_percent == pct, f"有效完成度 {pct}%")
    except ValidationError:
        expect(False, f"有效完成度 {pct}% 被拒绝")

invalid_percents = [10, 33, 66, -1, 101]
for pct in invalid_percents:
    try:
        req = SubTaskCompletionUpdateRequest(completion_percent=pct)
        expect(False, f"无效完成度 {pct}% 被接受")
    except ValidationError:
        expect(True, f"无效完成度 {pct}% 被正确拒绝")

print("\n--- 3. 用户反馈校验 ---")

valid_feedbacks = ["easy", "just_right", "hard"]
for fb in valid_feedbacks:
    try:
        req = SubTaskCompletionUpdateRequest(completion_percent=100, user_feedback=fb)
        expect(req.user_feedback == fb, f"有效反馈 '{fb}'")
    except ValidationError:
        expect(False, f"有效反馈 '{fb}' 被拒绝")

try:
    req = SubTaskCompletionUpdateRequest(completion_percent=100, user_feedback="amazing")
    expect(False, "无效反馈 'amazing' 被接受")
except ValidationError:
    expect(True, "无效反馈 'amazing' 被正确拒绝")

print("\n--- 4. 自定义主题需要 custom_label ---")

try:
    req = WeeklyFocusUpsertRequest(
        week_start="2026-05-19",
        theme="custom",
        spirit_weights={"light": 1.5},
        key_spirits=["light"],
        custom_label=None
    )
    expect(False, "custom 主题无 label 被接受")
except ValidationError:
    expect(True, "custom 主题无 label 被正确拒绝")

print("\n" + "=" * 70)
print(f"校验完成: 通过 {pass_count} / 失败 {fail_count}")
print("=" * 70)

if fail_count > 0:
    sys.exit(1)
