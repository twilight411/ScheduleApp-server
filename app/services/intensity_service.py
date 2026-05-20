"""
精灵强度服务 — CRUD + 模板系统 + 强度衰减
"""
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.profile import UserProfile, SpiritIntensity, IntensityTemplate

SPIRIT_CODES = ["light", "water", "soil", "air", "nutrition"]
SPIRIT_NAMES = {
    "light": "光精灵", "water": "水精灵", "soil": "土壤精灵",
    "air": "空气精灵", "nutrition": "营养精灵",
}


class IntensityService:

    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_intensities(self, user_id: uuid.UUID) -> list[SpiritIntensity]:
        """获取用户的所有精灵强度"""
        profile = await self._get_profile(user_id)
        if not profile:
            return []

        result = await self.db.execute(
            select(SpiritIntensity)
            .where(SpiritIntensity.profile_id == profile.id)
            .order_by(SpiritIntensity.spirit_code)
        )
        return list(result.scalars().all())

    async def update_intensity(
        self,
        user_id: uuid.UUID,
        spirit_code: str,
        base_intensity: int,
        is_locked: Optional[bool] = None,
    ) -> SpiritIntensity:
        """更新单个精灵强度"""
        if spirit_code not in SPIRIT_CODES:
            raise ValueError(f"无效的精灵代码: {spirit_code}")
        if not (0 <= base_intensity <= 100):
            raise ValueError("强度必须在 0-100 之间")

        profile = await self._get_profile(user_id)
        if not profile:
            raise ValueError("用户画像不存在")

        result = await self.db.execute(
            select(SpiritIntensity).where(
                SpiritIntensity.profile_id == profile.id,
                SpiritIntensity.spirit_code == spirit_code,
            )
        )
        intensity = result.scalar_one_or_none()
        if not intensity:
            raise ValueError(f"精灵强度记录不存在: {spirit_code}")

        intensity.base_intensity = base_intensity
        if is_locked is not None:
            intensity.is_locked = is_locked
        intensity.updated_at = datetime.now(timezone.utc)

        await self.db.flush()
        return intensity

    async def batch_update_intensities(
        self,
        user_id: uuid.UUID,
        intensities: dict[str, int],
    ) -> list[SpiritIntensity]:
        """批量更新精灵强度 — {"light": 80, "soil": 60, ...}"""
        profile = await self._get_profile(user_id)
        if not profile:
            raise ValueError("用户画像不存在")

        result = await self.db.execute(
            select(SpiritIntensity).where(SpiritIntensity.profile_id == profile.id)
        )
        existing = {si.spirit_code: si for si in result.scalars().all()}

        updated = []
        for code, value in intensities.items():
            if code not in SPIRIT_CODES:
                continue
            value = max(0, min(100, value))
            si = existing.get(code)
            if si:
                si.base_intensity = value
                si.updated_at = datetime.now(timezone.utc)
                updated.append(si)

        await self.db.flush()
        return updated

    async def apply_template(
        self, user_id: uuid.UUID, template_id: uuid.UUID
    ) -> list[SpiritIntensity]:
        """应用强度模板"""
        # 获取模板
        result = await self.db.execute(
            select(IntensityTemplate).where(
                IntensityTemplate.id == template_id,
                IntensityTemplate.is_active == True,
            )
        )
        template = result.scalar_one_or_none()
        if not template:
            raise ValueError("模板不存在或已停用")

        return await self.batch_update_intensities(user_id, template.intensities)

    async def get_templates(self) -> list[IntensityTemplate]:
        """获取所有活跃的强度模板"""
        result = await self.db.execute(
            select(IntensityTemplate)
            .where(IntensityTemplate.is_active == True)
            .order_by(IntensityTemplate.sort_order)
        )
        return list(result.scalars().all())

    async def get_effective_intensity(
        self, user_id: uuid.UUID, spirit_code: str
    ) -> int:
        """获取精灵的有效强度值（base + learned_delta）"""
        profile = await self._get_profile(user_id)
        if not profile:
            return 50

        result = await self.db.execute(
            select(SpiritIntensity).where(
                SpiritIntensity.profile_id == profile.id,
                SpiritIntensity.spirit_code == spirit_code,
            )
        )
        si = result.scalar_one_or_none()
        if not si:
            return 50

        return si.effective_intensity

    async def decay_learned_deltas(self, decay_rate: float = 0.05):
        """
        全局强度衰减 — 每周对所有未锁定的 learned_delta 衰减 5%
        由定时任务调用
        """
        result = await self.db.execute(
            select(SpiritIntensity).where(SpiritIntensity.is_locked == False)
        )
        for si in result.scalars().all():
            if abs(si.learned_delta) > 0.1:
                si.learned_delta *= (1 - decay_rate)
                si.updated_at = datetime.now(timezone.utc)

        await self.db.flush()

    async def _get_profile(self, user_id: uuid.UUID) -> Optional[UserProfile]:
        result = await self.db.execute(
            select(UserProfile).where(UserProfile.user_id == user_id)
        )
        return result.scalar_one_or_none()
