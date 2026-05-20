"""
生成测试数据 — 创建测试用户和完整画像
运行: python -m scripts.seed_test_data
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import async_session_factory, init_db
from app.models.user import User
from app.models.profile import UserProfile, SpiritIntensity
from app.models.notification import NotificationSetting
from app.utils.jwt import hash_password
from sqlalchemy import select

SPIRIT_CODES = ["light", "water", "soil", "air", "nutrition"]

TEST_USERS = [
    {
        "email": "test@example.com",
        "name": "测试用户",
        "password": "testpass123",
        "preferences": {
            "wake_time": "07:00",
            "sleep_time": "23:00",
            "energy_pattern": "morning",
            "peak_hours": ["09:00-11:00", "14:00-16:00"],
            "spirit_priority": ["light", "soil", "air", "nutrition", "water"],
        },
        "intensities": {"light": 70, "water": 40, "soil": 60, "air": 50, "nutrition": 40},
    },
    {
        "email": "student@example.com",
        "name": "考试冲刺同学",
        "password": "testpass123",
        "preferences": {
            "wake_time": "06:30",
            "sleep_time": "23:30",
            "energy_pattern": "morning",
            "peak_hours": ["07:00-09:00", "09:00-12:00"],
            "spirit_priority": ["light", "soil", "nutrition", "water", "air"],
        },
        "intensities": {"light": 90, "water": 20, "soil": 50, "air": 20, "nutrition": 30},
    },
]


async def main():
    await init_db()

    async with async_session_factory() as session:
        for user_data in TEST_USERS:
            # 检查是否已存在
            result = await session.execute(
                select(User).where(User.email == user_data["email"])
            )
            if result.scalar_one_or_none():
                print(f"  跳过（已存在）: {user_data['email']}")
                continue

            # 创建用户
            user = User(
                email=user_data["email"],
                name=user_data["name"],
                hashed_password=hash_password(user_data["password"]),
            )
            session.add(user)
            await session.flush()

            # 创建画像
            profile = UserProfile(
                user_id=user.id,
                preferences=user_data["preferences"],
                stats={},
                tags=[],
                onboarding_completed=True,
            )
            session.add(profile)
            await session.flush()

            # 创建精灵强度
            for code in SPIRIT_CODES:
                si = SpiritIntensity(
                    profile_id=profile.id,
                    spirit_code=code,
                    base_intensity=user_data["intensities"].get(code, 50),
                )
                session.add(si)

            # 创建通知设置
            ns = NotificationSetting(user_id=user.id)
            session.add(ns)

            print(f"  ✅ 创建: {user_data['name']} ({user_data['email']})")

        await session.commit()
        print("\n测试数据初始化完成！")


if __name__ == "__main__":
    asyncio.run(main())
