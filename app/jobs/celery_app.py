"""
Celery 配置 — 定时任务调度
"""
from celery import Celery
from celery.schedules import crontab

from app.config import get_settings

settings = get_settings()

celery_app = Celery(
    "spirit-scheduler",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Asia/Shanghai",
    enable_utc=False,
)

# ===== 定时任务注册 =====
celery_app.conf.beat_schedule = {
    # 每天早上 7:00 推送今日日程
    "daily_morning_push": {
        "task": "app.jobs.daily_push.push_daily_schedule",
        "schedule": crontab(hour=7, minute=0),
    },
    # 每天 22:00 检查未完成任务
    "overdue_task_check": {
        "task": "app.jobs.overdue_check.check_overdue_tasks",
        "schedule": crontab(hour=22, minute=0),
    },
    # 每天 3:00 清理过期的对话任务建议
    "suggestion_expire": {
        "task": "app.jobs.overdue_check.expire_suggestions",
        "schedule": crontab(hour=3, minute=0),
    },
    # 每周日 20:30 计算精灵周得分
    "weekly_scoring": {
        "task": "app.jobs.weekly_scoring.calculate_all_scores",
        "schedule": crontab(hour=20, minute=30, day_of_week=0),
    },
    # 每周日 21:00 生成周报
    "weekly_report_gen": {
        "task": "app.jobs.weekly_report.generate_all_reports",
        "schedule": crontab(hour=21, minute=0, day_of_week=0),
    },
    # 每周日 21:30 生成行为摘要
    "weekly_summary_gen": {
        "task": "app.jobs.weekly_summary.generate_all_summaries",
        "schedule": crontab(hour=21, minute=30, day_of_week=0),
    },
    # 每月1号 10:00 生成月度果实
    "monthly_fruit_gen": {
        "task": "app.jobs.monthly_fruit.generate_all_fruits",
        "schedule": crontab(hour=10, minute=0, day_of_month=1),
    },
    # [P1-3 修复] 每月1号 10:30 生成月度摘要
    "monthly_digest_gen": {
        "task": "app.jobs.monthly_digest.generate_all_digests",
        "schedule": crontab(hour=10, minute=30, day_of_month=1),
    },
    # 每周一 3:00 强度衰减
    "intensity_decay": {
        "task": "app.jobs.intensity_decay.decay_learned_deltas",
        "schedule": crontab(hour=3, minute=0, day_of_week=1),
    },
    # 每月1号 4:00 清理90天前的事件
    "data_cleanup": {
        "task": "app.jobs.data_cleanup.cleanup_old_events",
        "schedule": crontab(hour=4, minute=0, day_of_month=1),
    },
    # 每天 4:00 清理过期 token
    "token_cleanup": {
        "task": "app.jobs.token_cleanup.cleanup_expired_tokens",
        "schedule": crontab(hour=4, minute=0),
    },
}

# ===== 自动发现任务 =====
celery_app.autodiscover_tasks([
    "app.jobs.daily_push",
    "app.jobs.overdue_check",
    "app.jobs.weekly_scoring",
    "app.jobs.weekly_report",
    "app.jobs.weekly_summary",
    "app.jobs.monthly_fruit",
    "app.jobs.monthly_digest",
    "app.jobs.intensity_decay",
    "app.jobs.data_cleanup",
    "app.jobs.token_cleanup",
])