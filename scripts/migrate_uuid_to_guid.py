#!/usr/bin/env python3
"""
迁移脚本：把 app/models/ 下所有文件的
  from sqlalchemy.dialects.postgresql import UUID
  UUID(as_uuid=True)
替换为：
  from app.models._types import GUID
  GUID

用法：
  cd 项目根目录
  python scripts/migrate_uuid_to_guid.py        # 干跑，预览改动
  python scripts/migrate_uuid_to_guid.py --apply # 实际写入

涉及文件（基于代码 check）：
  app/models/conversation.py
  app/models/file_upload.py
  app/models/notification.py
  app/models/profile.py
  app/models/report.py
  app/models/schedule.py
  app/models/score.py
  app/models/task.py
  app/models/user.py
"""
import re
import sys
from pathlib import Path

MODELS_DIR = Path("app/models")
TARGET_FILES = [
    "conversation.py",
    "file_upload.py",
    "notification.py",
    "profile.py",
    "report.py",
    "schedule.py",
    "score.py",
    "task.py",
    "user.py",
]


def transform(content: str) -> tuple[str, int]:
    """返回 (new_content, change_count)"""
    original = content
    changes = 0

    # 1. 替换 import：删掉 PG UUID 的 import，加上 GUID
    if "from sqlalchemy.dialects.postgresql import UUID" in content:
        # 单独 import 一行
        content = re.sub(
            r"^from sqlalchemy\.dialects\.postgresql import UUID\s*\n",
            "from app.models._types import GUID\n",
            content,
            flags=re.MULTILINE,
        )
        # 也可能 UUID 跟其它一起 import：from sqlalchemy.dialects.postgresql import UUID, JSONB
        content = re.sub(
            r"from sqlalchemy\.dialects\.postgresql import\s+([^\n]*?)\bUUID\b\s*,?\s*([^\n]*)",
            lambda m: _rebuild_pg_import(m.group(1), m.group(2)),
            content,
        )
        changes += 1

    # 2. 替换列定义：UUID(as_uuid=True) → GUID
    new_content, n = re.subn(
        r"UUID\s*\(\s*as_uuid\s*=\s*True\s*\)",
        "GUID",
        content,
    )
    if n > 0:
        content = new_content
        changes += n

    # 3. 防御：裸 UUID 用作类型也替换（少见，但保险）
    # 仅在 mapped_column(UUID, ...) 这种位置
    new_content, n = re.subn(
        r"mapped_column\(\s*UUID\s*,",
        "mapped_column(GUID,",
        content,
    )
    if n > 0:
        content = new_content
        changes += n

    return content, changes


def _rebuild_pg_import(before: str, after: str) -> str:
    """处理 'from ... import A, UUID, B' 这种情况"""
    parts_before = [p.strip() for p in before.split(",") if p.strip()]
    parts_after = [p.strip() for p in after.split(",") if p.strip()]
    remaining = parts_before + parts_after
    line = ""
    if remaining:
        line += f"from sqlalchemy.dialects.postgresql import {', '.join(remaining)}\n"
    line += "from app.models._types import GUID"
    return line


def main():
    apply = "--apply" in sys.argv
    if not MODELS_DIR.exists():
        print(f"❌ 找不到 {MODELS_DIR}，请在项目根目录运行")
        sys.exit(1)

    total_changes = 0
    files_changed = 0

    for filename in TARGET_FILES:
        path = MODELS_DIR / filename
        if not path.exists():
            print(f"⚠ 跳过（不存在）: {path}")
            continue

        content = path.read_text(encoding="utf-8")
        new_content, n = transform(content)

        if n == 0:
            print(f"✓ 无需修改: {path}")
            continue

        files_changed += 1
        total_changes += n
        print(f"✏ {path}: {n} 处改动")

        if apply:
            path.write_text(new_content, encoding="utf-8")
            print(f"  ✓ 已写入")

    print(f"\n汇总: {files_changed} 个文件 / {total_changes} 处改动")
    if not apply:
        print("\n这是预览，加 --apply 实际写入：")
        print("  python scripts/migrate_uuid_to_guid.py --apply")


if __name__ == "__main__":
    main()
