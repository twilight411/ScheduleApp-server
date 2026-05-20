"""
ORM 模型汇总 — Alembic 和 init_db 需要导入所有模型
"""
from app.models.user import User, RefreshToken
from app.models.profile import UserProfile, SpiritIntensity, IntensityTemplate
from app.models.task import Task, SubTask, TaskEvent
from app.models.schedule import Schedule
from app.models.conversation import Conversation, ChatTaskSuggestion
from app.models.score import SpiritWeeklyScore
from app.models.report import WeeklyReport, WeeklySummary, MonthlyFruit, MonthlyDigest
from app.models.notification import UserDevice, NotificationSetting, Notification
from app.models.file_upload import FileUpload

__all__ = [
    "User", "RefreshToken",
    "UserProfile", "SpiritIntensity", "IntensityTemplate",
    "Task", "SubTask", "TaskEvent",
    "Schedule",
    "Conversation", "ChatTaskSuggestion",
    "SpiritWeeklyScore",
    "WeeklyReport", "WeeklySummary", "MonthlyFruit", "MonthlyDigest",
    "UserDevice", "NotificationSetting", "Notification",
    "FileUpload",
]
