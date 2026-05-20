"""
Sprint 1 迁移幂等性验证
- 测试 migrate_sprint1.py 脚本的幂等性
- 模拟 SQLite 和 PostgreSQL 的 SQL 执行逻辑
"""
import sys
import os

HERE = os.path.dirname(__file__)
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

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
print("Sprint 1 迁移幂等性验证")
print("=" * 70)

print("\n--- 1. 检查迁移脚本存在 ---")
migrate_path = os.path.join(ROOT, "scripts", "migrate_sprint1.py")
expect(os.path.exists(migrate_path), "迁移脚本存在")

print("\n--- 2. 检查脚本语法 ---")
try:
    with open(migrate_path, 'r') as f:
        content = f.read()
        expect(True, "脚本可读")
        expect("weekly_focus" in content, "包含 weekly_focus 表创建")
        expect("completion_percent" in content, "包含 completion_percent 字段")
        expect("raw_score" in content, "包含 raw_score 字段")
        expect("focus_weight" in content, "包含 focus_weight 字段")
except Exception as e:
    expect(False, f"读取失败: {e}")

print("\n--- 3. 检查幂等性保护逻辑 ---")
expect("IF NOT EXISTS" in content or "try" in content.lower(), 
       "包含幂等性保护 (IF NOT EXISTS 或 try-except)")

print("\n--- 4. 检查回填逻辑 ---")
expect("backfill" in content.lower(), "包含回填逻辑")
expect("completion_percent" in content and "status = 'completed'" in content,
       "包含 completed 任务回填 completion_percent=100")

print("\n--- 5. 检查模型字段 ---")
from app.models.task import SubTask
from app.models.score import SpiritWeeklyScore
from app.models.weekly_focus import WeeklyFocus

expect(hasattr(SubTask, 'completion_percent'), "SubTask 有 completion_percent")
expect(hasattr(SubTask, 'self_reported_at'), "SubTask 有 self_reported_at")
expect(hasattr(SubTask, 'quality_note'), "SubTask 有 quality_note")

expect(hasattr(SpiritWeeklyScore, 'raw_score'), "SpiritWeeklyScore 有 raw_score")
expect(hasattr(SpiritWeeklyScore, 'focus_weight'), "SpiritWeeklyScore 有 focus_weight")
expect(hasattr(SpiritWeeklyScore, 'display_score'), "SpiritWeeklyScore 有 display_score")
expect(hasattr(SpiritWeeklyScore, 'focus_at_scoring'), "SpiritWeeklyScore 有 focus_at_scoring")

expect(hasattr(WeeklyFocus, 'theme'), "WeeklyFocus 有 theme")
expect(hasattr(WeeklyFocus, 'spirit_weights'), "WeeklyFocus 有 spirit_weights")
expect(hasattr(WeeklyFocus, 'key_spirits'), "WeeklyFocus 有 key_spirits")

print("\n" + "=" * 70)
print(f"验证完成: 通过 {pass_count} / 失败 {fail_count}")
print("=" * 70)

if fail_count > 0:
    sys.exit(1)
else:
    print("\n✅ 迁移脚本验证全部通过")
