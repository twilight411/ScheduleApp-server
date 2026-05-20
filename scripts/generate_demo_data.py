#!/usr/bin/env python3
"""
生成演示数据脚本：
1. 创建一个测试用户
2. 创建3周的计划（每周至少30条，涵盖5个维度）
3. 随机生成完成度
4. 生成3份周报和3棵生命树
"""
import asyncio
import uuid
import random
from datetime import date, datetime, timedelta
from typing import List, Tuple

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import async_session_factory, init_db
from app.models.user import User
from app.models.task import Task, SubTask
from app.models.profile import UserProfile
from app.services.auth_service import AuthService
from app.services.task_service import TaskService
from app.services.report_service import ReportService
from app.services.tree_service import TreeService
from app.services.scoring_service import ScoringService

# 五个精灵维度
SPIRITS = ["light", "water", "soil", "air", "nutrition"]
SPIRIT_NAMES = {
    "light": "光精灵（工作学习）",
    "water": "水精灵（娱乐放松）",
    "soil": "土壤精灵（身体健康）",
    "air": "空气精灵（社交互动）",
    "nutrition": "营养精灵（兴趣爱好）",
}

# 每个维度的示例任务
TASK_TEMPLATES = {
    "light": [
        "完成项目文档编写",
        "学习Python高级特性",
        "阅读技术博客3篇",
        "完成在线课程第5章",
        "代码重构任务",
        "准备技术分享PPT",
        "复习数据结构",
        "学习新框架",
        "编写单元测试",
        "性能优化任务",
    ],
    "water": [
        "看一部电影",
        "听音乐放松30分钟",
        "泡澡放松",
        "玩游戏1小时",
        "看综艺节目",
        "冥想15分钟",
        "公园散步",
        "喝咖啡休息",
        "睡个午觉",
        "看漫画",
    ],
    "soil": [
        "晨跑30分钟",
        "做瑜伽",
        "去健身房锻炼",
        "游泳1小时",
        "吃健康早餐",
        "喝够8杯水",
        "散步1万步",
        "做拉伸运动",
        "早睡早起",
        "吃水果",
    ],
    "air": [
        "给朋友打电话",
        "和家人视频聊天",
        "参加聚会",
        "约朋友喝咖啡",
        "参加社群活动",
        "和同事聚餐",
        "认识新朋友",
        "帮助邻居",
        "参加兴趣小组",
        "给家人写信",
    ],
    "nutrition": [
        "练习吉他",
        "画画2小时",
        "学习摄影",
        "写日记",
        "做手工",
        "学习烹饪新菜",
        "阅读小说",
        "种植花草",
        "学习外语",
        "玩乐器",
    ],
}


async def create_test_user(db: AsyncSession) -> Tuple[User, str]:
    """创建测试用户"""
    auth_service = AuthService(db)
    email = f"test_{uuid.uuid4().hex[:8]}@example.com"
    password = "test123456"
    name = "测试用户"
    
    user, tokens = await auth_service.register(email, password, name)
    print(f"✅ 创建用户: {name} ({email})")
    return user, tokens["access_token"]


def generate_task_tasks(week_start: date, num_tasks: int = 30) -> List[dict]:
    """生成任务数据"""
    tasks = []
    for i in range(num_tasks):
        # 均匀分布五个维度
        spirit = SPIRITS[i % len(SPIRITS)]
        template = random.choice(TASK_TEMPLATES[spirit])
        title = f"{template} #{i+1}"
        
        # 随机在这一周的某一天
        day_offset = random.randint(0, 6)
        task_date = week_start + timedelta(days=day_offset)
        
        # 随机时长（30-120分钟）
        duration = random.choice([30, 45, 60, 90, 120])
        
        # 随机优先级
        priority = random.choice(["high", "medium", "low"])
        
        tasks.append({
            "title": title,
            "spirit": spirit,
            "date": task_date,
            "duration": duration,
            "priority": priority,
        })
    return tasks


async def create_tasks_for_week(
    db: AsyncSession,
    user: User,
    week_start: date,
    num_tasks: int = 30,
) -> List[SubTask]:
    """为某一周创建任务和子任务"""
    task_service = TaskService(db)
    task_data_list = generate_task_tasks(week_start, num_tasks)
    
    subtasks_created = []
    
    for task_data in task_data_list:
        # 创建主任务
        task = Task(
            user_id=user.id,
            title=task_data["title"],
            primary_spirit=task_data["spirit"],
            secondary_spirits=[],
            priority=task_data["priority"],
            is_recurring=False,
            status="in_progress",
            source="manual",
            created_at=datetime.now(),
            updated_at=datetime.now(),
        )
        db.add(task)
        await db.flush()
        
        # 创建子任务
        scheduled_start = datetime.combine(task_data["date"], datetime.min.time()) + timedelta(
            hours=random.randint(8, 20)
        )
        scheduled_end = scheduled_start + timedelta(minutes=task_data["duration"])
        
        subtask = SubTask(
            task_id=task.id,
            spirit=task_data["spirit"],
            title=task_data["title"],
            duration_minutes=task_data["duration"],
            suggested_time=random.choice(["morning", "afternoon", "evening", None]),
            scheduled_start=scheduled_start,
            scheduled_end=scheduled_end,
            actual_start=scheduled_start,
            actual_end=scheduled_end,
            priority=task_data["priority"],
            is_fixed=False,
            created_at=datetime.now(),
        )
        db.add(subtask)
        await db.flush()
        
        # 随机生成完成度
        completion_percent = random.choice([0, 25, 50, 75, 100])
        if completion_percent == 100:
            subtask.status = "completed"
        elif completion_percent > 0:
            subtask.status = "in_progress"
        else:
            subtask.status = "pending"
        
        subtask.completion_percent = completion_percent
        
        if 0 < completion_percent < 100:
            subtask.quality_note = f"完成了部分工作，还需要继续努力"
        
        subtask.self_reported_at = scheduled_end
        
        subtasks_created.append(subtask)
    
    await db.commit()
    print(f"✅ 为周 {week_start} 创建了 {len(subtasks_created)} 个任务")
    return subtasks_created


async def generate_weekly_report_and_tree(
    db: AsyncSession,
    user: User,
    week_start: date,
) -> Tuple[dict, dict]:
    """生成周报和树数据"""
    # 先确保有分数
    scoring_service = ScoringService(db)
    await scoring_service.calculate_all_spirits(user.id, week_start)
    await db.commit()  # 提交分数
    
    # 生成周报
    report_service = ReportService(db)
    report = await report_service.generate_weekly_report(user.id, week_start, force=True)
    await db.commit()  # 提交周报
    
    # 生成树数据
    tree_service = TreeService(db)
    tree_data = await tree_service.build_tree_data(user.id, week_start)
    
    print(f"✅ 生成周报和树: {week_start}")
    return report, tree_data


async def main():
    """主函数"""
    print("🚀 开始生成演示数据...")
    
    # 初始化数据库
    await init_db()
    
    async with async_session_factory() as db:
        # 1. 创建测试用户
        user, _ = await create_test_user(db)
        
        # 2. 计算三周的起始日期
        today = date.today()
        # 找到最近的周一
        days_since_monday = today.weekday()
        week1_start = today - timedelta(days=days_since_monday + 14)  # 两周前
        week2_start = week1_start + timedelta(days=7)  # 一周前
        week3_start = week2_start + timedelta(days=7)  # 本周
        
        weeks = [week1_start, week2_start, week3_start]
        
        # 3. 为每周创建任务
        all_subtasks = []
        for week_start in weeks:
            subtasks = await create_tasks_for_week(db, user, week_start, num_tasks=35)
            all_subtasks.extend(subtasks)
        
        # 4. 生成周报和树
        reports = []
        trees = []
        for week_start in weeks:
            report, tree_data = await generate_weekly_report_and_tree(db, user, week_start)
            reports.append(report)
            trees.append(tree_data)
        
        # 5. 打印总结
        print("\n" + "="*60)
        print("📊 演示数据生成完成！")
        print("="*60)
        print(f"👤 用户: {user.name} ({user.email})")
        print(f"📅 共 {len(weeks)} 周数据")
        print(f"📝 共 {len(all_subtasks)} 个任务")
        
        for i, (week_start, report, tree) in enumerate(zip(weeks, reports, trees)):
            print(f"\n--- 第 {i+1} 周 ({week_start}) ---")
            print(f"  总分: {report.overall_score}")
            print(f"  标题: {report.headline}")
            print(f"  树健康度: {tree['tree_health']}")
            print(f"  季节: {tree['season_label']}")
            print(f"  完成率: {report.stats.get('completion_rate', 0):.0%}")
        
        print("\n" + "="*60)
        print("🎉 所有数据已保存到数据库！")
        print("="*60)


if __name__ == "__main__":
    asyncio.run(main())
