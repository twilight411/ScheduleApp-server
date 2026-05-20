#!/usr/bin/env python3
"""
展示完整的周报内容
"""
import asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session_factory
from app.models.user import User
from app.models.report import WeeklyReport


async def main():
    print("📊 查看完整周报内容...\n")
    
    async with async_session_factory() as db:
        # 查询用户
        result = await db.execute(select(User))
        users = result.scalars().all()
        
        if not users:
            print("❌ 没有找到用户数据")
            return
        
        user = users[-1]
        print(f"👤 用户: {user.name} ({user.email})\n")
        
        # 查询周报
        result = await db.execute(
            select(WeeklyReport)
            .where(WeeklyReport.user_id == user.id)
            .order_by(WeeklyReport.week_start)
        )
        reports = result.scalars().all()
        
        for i, report in enumerate(reports, 1):
            print(f"{'='*80}")
            print(f"📅 周报 {i}: {report.week_start} ~ {report.week_end}")
            print(f"{'='*80}")
            print(f"📝 标题: {report.headline}")
            print(f"\n📊 统计:")
            print(f"  - 计划任务: {report.stats.get('total_tasks_planned', 0)}")
            print(f"  - 完成任务: {report.stats.get('total_tasks_completed', 0)}")
            print(f"  - 完成率: {report.stats.get('completion_rate', 0):.0%}")
            print(f"  - 最高效日: {report.stats.get('most_productive_day', 'N/A')}")
            
            print(f"\n📖 完整叙事:")
            print(report.analysis.get('narrative', '无'))
            
            print(f"\n✨ 亮点:")
            for h in report.analysis.get('highlights', []):
                print(f"  - {h}")
            
            print(f"\n💡 改进建议:")
            for i in report.analysis.get('improvements', []):
                print(f"  - {i}")
            
            print(f"\n🔍 行为模式:")
            for p in report.analysis.get('patterns', []):
                print(f"  - {p}")
            
            print(f"\n📋 下周建议:")
            for s in report.analysis.get('suggestions', []):
                print(f"  - {s}")
            
            print(f"\n🌳 树描述:")
            print(report.tree_data.get('tree_narrative', '无')[:200] + '...')
            
            print(f"\n")
        
        print(f"{'='*80}")
        print("✅ 完成！")


if __name__ == "__main__":
    asyncio.run(main())
