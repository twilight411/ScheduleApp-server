#!/usr/bin/env python3
"""
查询并展示生成的演示数据
"""
import asyncio
from datetime import date
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session_factory
from app.models.user import User
from app.models.task import Task, SubTask
from app.models.report import WeeklyReport
from app.models.score import SpiritWeeklyScore


async def main():
    """主函数"""
    print("📊 查询演示数据...\n")

    async with async_session_factory() as db:
        # 1. 查询所有用户
        result = await db.execute(select(User))
        users = result.scalars().all()
        print(f"👤 用户总数: {len(users)}")
        for user in users:
            print(f"   - {user.name} ({user.email}) - ID: {user.id}")

        if not users:
            print("❌ 没有找到用户数据")
            return

        user = users[-1]  # 取最后一个创建的用户
        print(f"\n📋 使用用户: {user.name}\n")

        # 2. 查询任务统计
        result = await db.execute(select(Task).where(Task.user_id == user.id))
        tasks = result.scalars().all()

        result = await db.execute(select(SubTask).join(Task).where(Task.user_id == user.id))
        subtasks = result.scalars().all()

        print(f"📝 主任务数: {len(tasks)}")
        print(f"✅ 子任务数: {len(subtasks)}")

        # 按精灵维度统计
        spirit_stats = {}
        for st in subtasks:
            spirit = st.spirit
            if spirit not in spirit_stats:
                spirit_stats[spirit] = {"total": 0, "completed": 0, "partial": 0}
            spirit_stats[spirit]["total"] += 1
            if st.completion_percent == 100:
                spirit_stats[spirit]["completed"] += 1
            elif st.completion_percent > 0:
                spirit_stats[spirit]["partial"] += 1

        print(f"\n🧚 按精灵维度统计:")
        spirit_names = {
            "light": "💡 光精灵（工作学习）",
            "water": "💧 水精灵（娱乐放松）",
            "soil": "🌱 土壤精灵（身体健康）",
            "air": "💨 空气精灵（社交互动）",
            "nutrition": "✨ 营养精灵（兴趣爱好）",
        }
        for spirit, stats in spirit_stats.items():
            name = spirit_names.get(spirit, spirit)
            print(f"  {name}:")
            print(f"    总计: {stats['total']}")
            print(f"    完成: {stats['completed']}")
            print(f"    部分完成: {stats['partial']}")

        # 3. 查询周报
        result = await db.execute(
            select(WeeklyReport)
            .where(WeeklyReport.user_id == user.id)
            .order_by(WeeklyReport.week_start)
        )
        reports = result.scalars().all()
        print(f"\n📊 周报告数: {len(reports)}")

        for i, report in enumerate(reports, 1):
            print(f"\n--- 周报 {i}: {report.week_start} ~ {report.week_end} ---")
            print(f"  📝 标题: {report.headline}")
            print(f"  ⭐ 总分: {report.overall_score}")
            print(f"  📈 对比上周: {report.vs_last_week if report.vs_last_week else '无'}")
            print(f"  📊 统计:")
            print(f"    - 计划任务: {report.stats.get('total_tasks_planned', 0)}")
            print(f"    - 完成任务: {report.stats.get('total_tasks_completed', 0)}")
            print(f"    - 完成率: {report.stats.get('completion_rate', 0):.0%}")
            print(f"    - 最高效日: {report.stats.get('most_productive_day', 'N/A')}")

            # 查询该周的精灵评分
            result = await db.execute(
                select(SpiritWeeklyScore)
                .where(
                    SpiritWeeklyScore.user_id == user.id,
                    SpiritWeeklyScore.week_start == report.week_start,
                )
            )
            scores = result.scalars().all()
            print(f"  🧚 精灵评分:")
            for score in scores:
                name = spirit_names.get(score.spirit_code, score.spirit_code)
                print(f"    {name}: {score.score} ({score.level})")

            # 树数据
            tree_data = report.tree_data
            print(f"  🌳 生命树:")
            print(f"    - 健康度: {tree_data.get('tree_health', 'N/A')}")
            print(f"    - 季节: {tree_data.get('season_label', 'N/A')}")
            print(f"    - 一句话总结: {tree_data.get('weekly_summary_line', 'N/A')}")
            if tree_data.get('tree_narrative'):
                print(f"    - 树描述: {tree_data['tree_narrative'][:100]}...")

        print("\n✅ 查询完成！")


if __name__ == "__main__":
    asyncio.run(main())
