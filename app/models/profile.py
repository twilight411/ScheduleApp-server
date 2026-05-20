"""
用户画像 + 精灵强度 + 强度模板
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import String, Integer, Float, Boolean, DateTime, ForeignKey, Text, ARRAY
from sqlalchemy import JSON
from app.models._types import GUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class UserProfile(Base):
    __tablename__ = "user_profiles"

    id: Mapped[uuid.UUID] = mapped_column(
        GUID, primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="CASCADE"),
        unique=True, nullable=False, index=True,
    )

    # 显式偏好（用户设置）
    preferences: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    # 默认值结构见 schemas/profile.py 的 UserPreferences

    # 学习统计（系统自动更新）
    stats: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    # 推断标签
    tags: Mapped[list] = mapped_column(JSON, default=list)

    # 是否完成 onboarding
    onboarding_completed: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )

    user: Mapped["User"] = relationship(back_populates="profile")
    spirit_intensities: Mapped[list["SpiritIntensity"]] = relationship(
        back_populates="profile", cascade="all, delete-orphan"
    )


class SpiritIntensity(Base):
    """精灵强度设置 — 每个用户每个精灵一条记录"""
    __tablename__ = "spirit_intensities"

    id: Mapped[uuid.UUID] = mapped_column(
        GUID, primary_key=True, default=uuid.uuid4
    )
    profile_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("user_profiles.id", ondelete="CASCADE"),
        nullable=False,
    )
    spirit_code: Mapped[str] = mapped_column(String(20), nullable=False)
    # light / water / soil / air / nutrition

    base_intensity: Mapped[int] = mapped_column(Integer, default=50)
    # 用户手动设定的基础强度 0-100

    learned_delta: Mapped[float] = mapped_column(Float, default=0.0)
    # 系统学习到的强度调整值

    is_locked: Mapped[bool] = mapped_column(Boolean, default=False)
    # 用户锁定后系统不再自动调整

    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )

    profile: Mapped["UserProfile"] = relationship(back_populates="spirit_intensities")

    @property
    def effective_intensity(self) -> int:
        """最终有效强度 = 基础 + 学习调整，限制在 0-100"""
        return max(0, min(100, int(self.base_intensity + self.learned_delta)))


class IntensityTemplate(Base):
    """预设的精灵强度模板（系统只读数据）"""
    __tablename__ = "intensity_templates"

    id: Mapped[uuid.UUID] = mapped_column(
        GUID, primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    # e.g. "考试冲刺", "健康优先", "均衡发展"

    description: Mapped[str] = mapped_column(Text, nullable=True)
    icon: Mapped[str] = mapped_column(String(10), nullable=True)
    # emoji icon

    intensities: Mapped[dict] = mapped_column(JSON, nullable=False)
    # {"light": 90, "water": 20, "soil": 40, "air": 30, "nutrition": 20}

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
