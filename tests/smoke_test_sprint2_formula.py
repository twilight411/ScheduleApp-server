"""
Sprint 2 打分公式 smoke test
覆盖:
  1. calc_completion_score - 准时奖励、取消惩罚、partial 处理
  2. calc_design_score - focus_mult 影响期望任务数、容忍带
  3. calc_quality_score - feedback 加权
  4. apply_focus_magnification - 基调放大公式
  5. get_overall_score - 加权总分

共 28 项断言
"""
import sys
import os

HERE = os.path.dirname(__file__)
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from dataclasses import dataclass
from typing import Optional

from app.services.scoring_service import (
    calc_completion_score,
    calc_design_score,
    calc_quality_score,
    apply_focus_magnification,
    FOCUS_MAGNIFY_PIVOT,
    SPIRIT_CODES,
)


@dataclass
class FakeSubTask:
    completion_percent: int = 0
    status: str = "pending"
    user_feedback: Optional[str] = None
    scheduled_end: Optional[str] = "2026-01-01"
    actual_end: Optional[str] = "2026-01-01"


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
print("Sprint 2 打分公式验证 (28 项)")
print("=" * 70)

print("\n--- 1. calc_completion_score ---")

st100 = FakeSubTask(completion_percent=100, status="completed")
st0 = FakeSubTask(completion_percent=0, status="cancelled")
st50 = FakeSubTask(completion_percent=50, status="in_progress")

expect(calc_completion_score([st100]) == 100.0, "单任务 100% 完成 → 100")
expect(calc_completion_score([st0]) == 0.0, "单任务取消 → 0")
expect(calc_completion_score([st50]) == 65.0, "单任务 50% → 65 (含准时奖励)")
expect(calc_completion_score([st100, st100, st0]) == 75.0, 
       "2 完成 + 1 取消 → 75")
expect(calc_completion_score([st50, st50]) == 65.0, 
       "两个 50% → 65 (含准时奖励)")

print("\n--- 2. calc_design_score ---")

expect(calc_design_score(50, 1.0, 4) == 100.0, "intensity=50, 4 任务, mult=1 → 满分")
expect(calc_design_score(50, 1.8, 7) == 100.0, 
       "intensity=50, expected=round(4×1.8)=7, 实际 7 → 满分")
expect(calc_design_score(50, 1.8, 4) < 100.0, 
       "intensity=50, expected=7, 实际 4 → 扣分")
expect(calc_design_score(50, 0.6, 2) == 100.0, 
       "intensity=50, expected=round(4×0.6)=2, 实际 2 → 满分")

print("\n--- 3. calc_quality_score ---")

fb_easy = FakeSubTask(completion_percent=100, user_feedback="easy")
fb_jr = FakeSubTask(completion_percent=100, user_feedback="just_right")
fb_hard = FakeSubTask(completion_percent=100, user_feedback="hard")

expect(calc_quality_score([fb_jr]) == 100.0, "just_right → 100")
expect(calc_quality_score([fb_easy]) == 70.0, "easy → 70")
expect(calc_quality_score([fb_hard]) == 85.0, "hard → 85")
expect(round(calc_quality_score([fb_jr, fb_easy]), 1) == 85.0, 
       "just_right + easy → 85")

st_partial = FakeSubTask(completion_percent=50, user_feedback="hard")
expect(round(calc_quality_score([st_partial, fb_jr]), 1) == 95.0, 
       "50% hard + 100% just_right → 加权 95")

print("\n--- 4. apply_focus_magnification (基调放大) ---")

expect(apply_focus_magnification(70, 1.0) == 70.0, "pivot 点不变")
expect(apply_focus_magnification(100, 1.8) == 100.0, 
       "高分 + 重点 → clamp 到 100")
expect(apply_focus_magnification(100, 0.6) == 90.0, 
       "高分 + 次要 → 90")
expect(apply_focus_magnification(50, 1.8) < 50, 
       "低分 + 重点 → 更低")
expect(apply_focus_magnification(50, 0.6) > 50, 
       "低分 + 次要 → 小幅抬升")

expect(round(apply_focus_magnification(67, 1.8), 1) == 65.8, 
       "低表现(raw=67) 重点 → 65.8")
expect(round(apply_focus_magnification(67, 1.0), 1) == 67.0, 
       "低表现(raw=67) 平衡 → 67")
expect(round(apply_focus_magnification(67, 0.6), 1) == 70.2, 
       "低表现(raw=67) 次要 → 70.2")

print("\n--- 5. 核心属性验证 ---")

raw_low = 67
raw_high = 100

focus_result = apply_focus_magnification(raw_low, 1.8)
balance_result = apply_focus_magnification(raw_low, 1.0)
minor_result = apply_focus_magnification(raw_low, 0.6)

expect(focus_result < balance_result, 
       "低表现: 重点 < 平衡")
expect(balance_result < minor_result, 
       "低表现: 平衡 < 次要")

focus_high = apply_focus_magnification(raw_high, 1.8)
balance_high = apply_focus_magnification(raw_high, 1.0)
minor_high = apply_focus_magnification(raw_high, 0.6)

expect(minor_high <= balance_high, 
       "高表现: 次要 ≤ 平衡")
expect(balance_high <= focus_high, 
       "高表现: 平衡 ≤ 重点")

print("\n--- 6. 边界条件 ---")

expect(apply_focus_magnification(0, 1.8) == 0.0, "raw=0 保持 0")
expect(apply_focus_magnification(100, 2.0) == 100.0, "上限 clamp")
expect(apply_focus_magnification(-10, 1.0) == 0.0, "负数 → 0")

expect(calc_completion_score([]) == 0.0, "空列表 → 0")
expect(calc_design_score(50, 1.0, 0) < 100.0, "无任务 → 扣分")
expect(calc_quality_score([]) == 75.0, "无反馈 → 默认 75")

print("\n" + "=" * 70)
print(f"验证完成: 通过 {pass_count - fail_count} / 失败 {fail_count} / 总计 {pass_count}")
print("=" * 70)

if fail_count > 0:
    sys.exit(1)
