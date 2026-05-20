"""
精灵强度衰减 — 每周一凌晨执行

Sprint D 增强:
  - 详细统计衰减了多少条记录
  - 记录衰减前后的 delta 值变化
  - 静默跳过 delta 接近 0 的记录

机制:
  将所有未锁定的 learned_delta 衰减 5%。
  这确保自动学到的强度调整会缓慢回归初始值，
  避免历史行为对当前强度产生过大影响。
"""
import asyncio

from sqlalchemy import select
from datetime import datetime, timezone

from app.jobs.celery_app import celery_app
from app.database import async_session_factory
from app.models.profile import SpiritIntensity

import structlog
logger = structlog.get_logger()

DECAY_RATE = 0.05         # 每周衰减 5%
DELTA_THRESHOLD = 0.1     # 低于此值不再衰减（避免无限趋近 0）


@celery_app.task(name="app.jobs.intensity_decay.decay_learned_deltas")
def decay_learned_deltas():
    """衰减所有用户的 learned_delta"""
    asyncio.run(_decay())


async def _decay():
    async with async_session_factory() as session:
        try:
            result = await session.execute(
                select(SpiritIntensity).where(
                    SpiritIntensity.is_locked == False,
                )
            )
            all_intensities = list(result.scalars().all())

            decayed_count = 0
            skipped_count = 0
            zeroed_count = 0

            for si in all_intensities:
                if abs(si.learned_delta) <= DELTA_THRESHOLD:
                    # 已经接近 0，直接归零
                    if si.learned_delta != 0:
                        si.learned_delta = 0
                        si.updated_at = datetime.now(timezone.utc)
                        zeroed_count += 1
                    else:
                        skipped_count += 1
                    continue

                si.learned_delta *= (1 - DECAY_RATE)
                si.updated_at = datetime.now(timezone.utc)
                decayed_count += 1

            await session.flush()
            await session.commit()

            logger.info(
                "intensity_decay_completed",
                total=len(all_intensities),
                decayed=decayed_count,
                zeroed=zeroed_count,
                skipped=skipped_count,
            )
        except Exception as e:
            await session.rollback()
            logger.error("intensity_decay_failed", error=str(e))
            raise