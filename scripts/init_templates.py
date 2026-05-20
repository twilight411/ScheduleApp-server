"""
导入强度模板初始数据

模板来自弹出问题.docx 中的典型场景组合。
使用方式: python -m scripts.init_templates
"""
import asyncio
import uuid

from sqlalchemy import select

from app.database import async_session_factory
from app.models.profile import IntensityTemplate


TEMPLATES = [
    {
        "name": "考试冲刺",
        "description": "学业优先，压缩娱乐和社交，保留基础健康",
        "icon": "📚",
        "intensities": {"light": 90, "water": 20, "soil": 40, "air": 20, "nutrition": 15},
        "sort_order": 1,
    },
    {
        "name": "均衡发展",
        "description": "五个维度齐头并进，适合日常节奏",
        "icon": "⚖️",
        "intensities": {"light": 50, "water": 50, "soil": 50, "air": 50, "nutrition": 50},
        "sort_order": 2,
    },
    {
        "name": "健康优先",
        "description": "身心修复为主，降低工作强度，增加休闲",
        "icon": "🌿",
        "intensities": {"light": 30, "water": 60, "soil": 85, "air": 40, "nutrition": 40},
        "sort_order": 3,
    },
    {
        "name": "社交达人",
        "description": "人际关系为核心，兼顾工作和兴趣",
        "icon": "💬",
        "intensities": {"light": 50, "water": 40, "soil": 40, "air": 85, "nutrition": 50},
        "sort_order": 4,
    },
    {
        "name": "兴趣驱动",
        "description": "优先保护兴趣爱好时间，工作维持日常",
        "icon": "✨",
        "intensities": {"light": 40, "water": 50, "soil": 50, "air": 40, "nutrition": 85},
        "sort_order": 5,
    },
    {
        "name": "佛系躺平",
        "description": "一切从简，低压力模式",
        "icon": "☁️",
        "intensities": {"light": 25, "water": 70, "soil": 30, "air": 25, "nutrition": 25},
        "sort_order": 6,
    },
    {
        "name": "暴力冲刺",
        "description": "全维度高强度，适合短期爆发（不建议长期使用）",
        "icon": "🔥",
        "intensities": {"light": 85, "water": 30, "soil": 60, "air": 50, "nutrition": 70},
        "sort_order": 7,
    },
]


async def seed_templates():
    async with async_session_factory() as session:
        for tmpl_data in TEMPLATES:
            # 检查是否已存在
            result = await session.execute(
                select(IntensityTemplate).where(
                    IntensityTemplate.name == tmpl_data["name"]
                )
            )
            existing = result.scalar_one_or_none()

            if existing:
                # 更新
                existing.description = tmpl_data["description"]
                existing.icon = tmpl_data["icon"]
                existing.intensities = tmpl_data["intensities"]
                existing.sort_order = tmpl_data["sort_order"]
                existing.is_active = True
                print(f"  ✅ 更新模板: {tmpl_data['name']}")
            else:
                # 新增
                template = IntensityTemplate(
                    id=uuid.uuid4(),
                    name=tmpl_data["name"],
                    description=tmpl_data["description"],
                    icon=tmpl_data["icon"],
                    intensities=tmpl_data["intensities"],
                    sort_order=tmpl_data["sort_order"],
                    is_active=True,
                )
                session.add(template)
                print(f"  ✅ 新增模板: {tmpl_data['name']}")

        await session.commit()
        print(f"\n完成！共 {len(TEMPLATES)} 个模板。")


if __name__ == "__main__":
    print("正在导入强度模板...")
    asyncio.run(seed_templates())
