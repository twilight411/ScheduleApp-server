"""
定时任务 — 每月1号生成月度行为摘要（用于 Context Engineering）

与 monthly_fruit 不同，这里生成的是精简的文本摘要，
供后续精灵对话和协商时作为 context 使用。

依赖 MonthlyDigest ORM (app/models/report.py)
"""
import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.jobs.celery_app import celery_app
from app.database import async_session_factory
from app.models.user import User
from app.models.report import MonthlyFruit, MonthlyDigest
from app.services.scoring_service import SPIRIT_CODES

import structlog

logger = structlog.get_logger()

SPIRIT_NAMES = {
    "light": "光精灵", "water": "水精灵", "soil": "土壤精灵",
    "air": "空气精灵", "nutrition": "营养精灵",
}


@celery_app.task(name="app.jobs.monthly_digest.generate_all_digests")
def generate_all_digests():
    """入口"""
    asyncio.run(_generate_all())


async def _generate_all():
    """遍历活跃用户，生成上月行为摘要"""
    async with async_session_factory() as session:
        today = datetime.now(timezone.utc).date()
        first_of_month = today.replace(day=1)
        last_month_end = first_of_month - timedelta(days=1)
        month_str = last_month_end.strftime("%Y-%m")

        result = await session.execute(
            select(User.id).where(
                User.is_active == True,
                User.is_deleted == False,
            )
        )
        user_ids = [row[0] for row in result.all()]

        logger.info("monthly_digest_start", user_count=len(user_ids), month=month_str)

        success = 0
        errors = 0
        for uid in user_ids:
            try:
                await _generate_for_user(session, uid, month_str)
                await session.commit()
                success += 1
            except Exception as e:
                await session.rollback()
                errors += 1
                logger.error("monthly_digest_error", user_id=str(uid), error=str(e))

        logger.info("monthly_digest_done", success=success, errors=errors, month=month_str)


async def _generate_for_user(session, user_id, month_str: str):
    """为单个用户生成月度摘要"""
    # 幂等检查
    existing = await session.execute(
        select(MonthlyDigest).where(
            MonthlyDigest.user_id == user_id,
            MonthlyDigest.month == month_str,
        )
    )
    if existing.scalar_one_or_none():
        return

    # 读取已生成的月度果实数据
    fruit_result = await session.execute(
        select(MonthlyFruit).where(
            MonthlyFruit.user_id == user_id,
            MonthlyFruit.month == month_str,
        )
    )
    fruit = fruit_result.scalar_one_or_none()

    if fruit:
        narrative = _build_narrative_from_fruit(fruit, month_str)
        key_metrics = {
            "overall_score": fruit.overall_score,
            "fruit_type": fruit.fruit_type,
            "best_spirit": fruit.best_spirit,
            "weakest_spirit": fruit.weakest_spirit,
            "weekly_scores": fruit.weekly_scores,
        }
    else:
        narrative = f"{month_str} 暂无数据记录。"
        key_metrics = {}

    digest = MonthlyDigest(
        user_id=user_id,
        month=month_str,
        narrative=narrative,
        key_metrics=key_metrics,
    )
    session.add(digest)
    await session.flush()


def _build_narrative_from_fruit(fruit: MonthlyFruit, month_str: str) -> str:
    """从果实数据构建精简摘要（供 Context 使用，不调 LLM）"""
    parts = [f"{month_str}月度总分{fruit.overall_score}，果实：{fruit.fruit_name}。"]

    spirit_data = fruit.spirit_monthly or {}
    best = fruit.best_spirit
    weakest = fruit.weakest_spirit

    if best and best in spirit_data:
        best_name = SPIRIT_NAMES.get(best, best)
        best_avg = spirit_data[best].get("avg_score", 0)
        parts.append(f"最佳：{best_name}(均分{best_avg})")

    if weakest and weakest in spirit_data:
        worst_name = SPIRIT_NAMES.get(weakest, weakest)
        worst_avg = spirit_data[weakest].get("avg_score", 0)
        parts.append(f"最弱：{worst_name}(均分{worst_avg})")

    # 趋势
    scores = fruit.weekly_scores or []
    if len(scores) >= 2:
        if scores[-1] > scores[0] + 5:
            parts.append("月内呈上升趋势。")
        elif scores[-1] < scores[0] - 5:
            parts.append("月内呈下降趋势。")
        else:
            parts.append("月内表现稳定。")

    return " ".join(parts)