"""
Sprint 1 数据库迁移脚本

执行内容:
  1. 新建 weekly_focus 表 (含 unique index)
  2. subtasks 表新增 3 列: completion_percent / self_reported_at / quality_note
  3. spirit_weekly_scores 表新增 4 列: raw_score / focus_weight / display_score / focus_at_scoring
  4. 回填:
     - subtasks 已 status='completed' 的设 completion_percent=100
     - 其他设 completion_percent=0
     - spirit_weekly_scores 历史记录 focus_weight=1.0 (无基调)

设计原则:
  - 幂等: 反复运行不会报错; 列已存在 / 表已存在都跳过
  - 双兼容: SQLite + PostgreSQL 自动适配
  - 不破坏现有数据

使用方式:
  开发环境 (SQLite 或 PG):
    python -m scripts.migrate_sprint1

  生产环境前请先备份数据库再跑。

依赖项目内部模块: app.database (拿到 engine + settings)
"""
import asyncio
import sys

from sqlalchemy import text, inspect
from sqlalchemy.ext.asyncio import AsyncEngine

# 项目内部
from app.database import engine, Base
# 触发所有模型 import (含 WeeklyFocus)
import app.models  # noqa: F401


def _is_sqlite(engine: AsyncEngine) -> bool:
    return engine.dialect.name == "sqlite"


def _is_postgres(engine: AsyncEngine) -> bool:
    return engine.dialect.name in ("postgresql", "postgres")


async def _column_exists(conn, table: str, column: str) -> bool:
    """跨 DB 检查某列是否存在"""
    def _check(sync_conn):
        insp = inspect(sync_conn)
        if table not in insp.get_table_names():
            return False
        cols = {c["name"] for c in insp.get_columns(table)}
        return column in cols
    return await conn.run_sync(_check)


async def _table_exists(conn, table: str) -> bool:
    def _check(sync_conn):
        insp = inspect(sync_conn)
        return table in insp.get_table_names()
    return await conn.run_sync(_check)


# ====================================================================
#  Step 1: 新建 weekly_focus 表
# ====================================================================

async def step1_create_weekly_focus(conn) -> str:
    """利用 Base.metadata 只创建缺失的表 (create_all 是幂等的)"""
    if await _table_exists(conn, "weekly_focus"):
        return "  ✓ weekly_focus 表已存在,跳过"

    # 仅创建 WeeklyFocus 这一张
    from app.models.weekly_focus import WeeklyFocus
    await conn.run_sync(
        lambda sc: WeeklyFocus.__table__.create(sc, checkfirst=True)
    )
    return "  ✓ weekly_focus 表已创建"


# ====================================================================
#  Step 2: subtasks 表加列
# ====================================================================

# (列名, 类型 - SQLite, 类型 - PG, 默认值 SQL 片段)
SUBTASK_NEW_COLUMNS = [
    ("completion_percent", "INTEGER", "INTEGER", "DEFAULT 0 NOT NULL"),
    ("self_reported_at",   "DATETIME", "TIMESTAMP", ""),
    ("quality_note",       "TEXT", "TEXT", ""),
]


async def step2_add_subtask_columns(conn) -> list[str]:
    msgs = []
    is_sqlite = _is_sqlite(engine)

    for col_name, sqlite_type, pg_type, default in SUBTASK_NEW_COLUMNS:
        if await _column_exists(conn, "subtasks", col_name):
            msgs.append(f"  ✓ subtasks.{col_name} 已存在,跳过")
            continue

        col_type = sqlite_type if is_sqlite else pg_type
        sql = f"ALTER TABLE subtasks ADD COLUMN {col_name} {col_type} {default}".strip()
        await conn.execute(text(sql))
        msgs.append(f"  + subtasks.{col_name} ({col_type}) 已添加")

    return msgs


# ====================================================================
#  Step 3: spirit_weekly_scores 表加列
# ====================================================================

SCORE_NEW_COLUMNS = [
    ("raw_score",        "REAL", "DOUBLE PRECISION", ""),
    ("focus_weight",     "REAL", "DOUBLE PRECISION", "DEFAULT 1.0 NOT NULL"),
    ("display_score",    "REAL", "DOUBLE PRECISION", ""),
    ("focus_at_scoring", "VARCHAR(30)", "VARCHAR(30)", ""),
]


async def step3_add_score_columns(conn) -> list[str]:
    msgs = []
    is_sqlite = _is_sqlite(engine)

    for col_name, sqlite_type, pg_type, default in SCORE_NEW_COLUMNS:
        if await _column_exists(conn, "spirit_weekly_scores", col_name):
            msgs.append(f"  ✓ spirit_weekly_scores.{col_name} 已存在,跳过")
            continue

        col_type = sqlite_type if is_sqlite else pg_type
        sql = (
            f"ALTER TABLE spirit_weekly_scores "
            f"ADD COLUMN {col_name} {col_type} {default}"
        ).strip()
        await conn.execute(text(sql))
        msgs.append(f"  + spirit_weekly_scores.{col_name} ({col_type}) 已添加")

    return msgs


# ====================================================================
#  Step 4: 数据回填
# ====================================================================

async def step4_backfill(conn) -> list[str]:
    """
    回填:
      - subtasks.status='completed' 且 completion_percent IS NULL/0 → 100
      - 其他 completion_percent 留 0 (列默认值已是 0)
      - 已存在的 SpiritWeeklyScore.focus_weight 默认 1.0 (列默认已设)
    """
    msgs = []

    # SQLite + PG 都支持的 SQL
    result = await conn.execute(text(
        "UPDATE subtasks SET completion_percent = 100 "
        "WHERE status = 'completed' AND (completion_percent IS NULL OR completion_percent = 0)"
    ))
    rc = result.rowcount or 0
    msgs.append(f"  → 回填 subtasks.completion_percent=100: 影响 {rc} 行")

    # 防御性: 把残留的 NULL 都补成 0 (列定义是 NOT NULL DEFAULT 0, 但 ALTER 老数据可能仍为 NULL)
    result = await conn.execute(text(
        "UPDATE subtasks SET completion_percent = 0 "
        "WHERE completion_percent IS NULL"
    ))
    rc = result.rowcount or 0
    if rc:
        msgs.append(f"  → 补 NULL → 0: 影响 {rc} 行")

    # spirit_weekly_scores.focus_weight 同理
    result = await conn.execute(text(
        "UPDATE spirit_weekly_scores SET focus_weight = 1.0 "
        "WHERE focus_weight IS NULL"
    ))
    rc = result.rowcount or 0
    if rc:
        msgs.append(f"  → 回填 spirit_weekly_scores.focus_weight=1.0: 影响 {rc} 行")

    return msgs


# ====================================================================
#  主入口
# ====================================================================

async def main():
    print("=" * 60)
    print("Sprint 1 数据库迁移")
    print(f"数据库类型: {engine.dialect.name}")
    print("=" * 60)

    async with engine.begin() as conn:
        print("\n[1/4] 创建 weekly_focus 表")
        msg = await step1_create_weekly_focus(conn)
        print(msg)

        print("\n[2/4] 给 subtasks 表加列")
        for m in await step2_add_subtask_columns(conn):
            print(m)

        print("\n[3/4] 给 spirit_weekly_scores 表加列")
        for m in await step3_add_score_columns(conn):
            print(m)

        print("\n[4/4] 数据回填")
        for m in await step4_backfill(conn):
            print(m)

    print("\n" + "=" * 60)
    print("✅ Sprint 1 迁移完成")
    print("=" * 60)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print(f"\n❌ 迁移失败: {e}", file=sys.stderr)
        raise
