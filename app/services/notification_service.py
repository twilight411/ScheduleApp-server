"""
通知服务 — 设备注册 + 通知设置 + 站内通知 CRUD + 推送通道

推送架构:
  create_notification() → 写入 notifications 表(站内信)
                        → 检查用户设置 + 静默时段
                        → 调用 _push_to_devices() (FCM/APNs，MVP 阶段预留)

静默时段:
  quiet_hours_start ~ quiet_hours_end 期间不推送到设备，
  但站内通知仍然创建（用户打开 App 后可看到）。
"""
import uuid
from datetime import datetime, time, timezone
from typing import Optional

from sqlalchemy import select, update, desc, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import UserDevice, NotificationSetting, Notification

import structlog

logger = structlog.get_logger()

# 通知类型常量
NOTIFICATION_TYPES = {
    "task_reminder", "daily_schedule", "weekly_report",
    "monthly_fruit", "spirit_tip", "sedentary", "system",
}

# 通知类型 → NotificationSetting 字段映射
TYPE_TO_SETTING = {
    "daily_schedule": "daily_schedule_push",
    "task_reminder": "task_reminder_push",
    "weekly_report": "weekly_report_push",
    "monthly_fruit": "monthly_fruit_push",
    "spirit_tip": "spirit_tip_push",
    "sedentary": "sedentary_reminder",
}


class NotificationService:

    def __init__(self, db: AsyncSession):
        self.db = db

    # ========================================
    #  设备注册
    # ========================================

    async def register_device(
        self,
        user_id: uuid.UUID,
        device_token: str,
        platform: str,
    ) -> UserDevice:
        """
        注册或更新推送设备。
        同一 device_token 不重复注册，更新时间戳即可。
        """
        if platform not in ("ios", "android", "web"):
            raise ValueError(f"不支持的平台: {platform}")

        # 查找已有记录
        result = await self.db.execute(
            select(UserDevice).where(
                UserDevice.user_id == user_id,
                UserDevice.device_token == device_token,
            )
        )
        existing = result.scalar_one_or_none()

        if existing:
            existing.platform = platform
            existing.is_active = True
            existing.updated_at = datetime.now(timezone.utc)
            await self.db.flush()
            return existing

        device = UserDevice(
            user_id=user_id,
            device_token=device_token,
            platform=platform,
            is_active=True,
        )
        self.db.add(device)
        await self.db.flush()
        return device

    async def deactivate_device(
        self, user_id: uuid.UUID, device_token: str
    ):
        """注销设备"""
        await self.db.execute(
            update(UserDevice)
            .where(
                UserDevice.user_id == user_id,
                UserDevice.device_token == device_token,
            )
            .values(is_active=False, updated_at=datetime.now(timezone.utc))
        )
        await self.db.flush()

    # ========================================
    #  通知设置
    # ========================================

    async def get_settings(
        self, user_id: uuid.UUID
    ) -> NotificationSetting:
        """获取通知设置（不存在则创建默认）"""
        result = await self.db.execute(
            select(NotificationSetting).where(
                NotificationSetting.user_id == user_id
            )
        )
        settings = result.scalar_one_or_none()
        if settings:
            return settings

        # 创建默认设置
        settings = NotificationSetting(user_id=user_id)
        self.db.add(settings)
        await self.db.flush()
        return settings

    async def update_settings(
        self,
        user_id: uuid.UUID,
        updates: dict,
    ) -> NotificationSetting:
        """更新通知设置"""
        settings = await self.get_settings(user_id)

        allowed_fields = {
            "daily_schedule_push", "task_reminder_push",
            "weekly_report_push", "monthly_fruit_push",
            "spirit_tip_push", "sedentary_reminder",
            "quiet_hours_start", "quiet_hours_end",
        }

        for key, value in updates.items():
            if key in allowed_fields:
                setattr(settings, key, value)

        settings.updated_at = datetime.now(timezone.utc)
        await self.db.flush()
        return settings

    # ========================================
    #  通知创建与发送
    # ========================================

    async def create_notification(
        self,
        user_id: uuid.UUID,
        type: str,
        title: str,
        body: str = None,
        data: dict = None,
        push: bool = True,
    ) -> Notification:
        """
        创建站内通知。
        push=True 时同时尝试推送到设备（受设置和静默时段控制）。
        """
        notification = Notification(
            user_id=user_id,
            type=type,
            title=title,
            body=body,
            data=data,
            is_read=False,
            is_pushed=False,
        )
        self.db.add(notification)
        await self.db.flush()

        if push:
            pushed = await self._try_push(user_id, type, title, body, data)
            if pushed:
                notification.is_pushed = True
                await self.db.flush()

        return notification

    async def batch_create(
        self,
        user_id: uuid.UUID,
        notifications: list[dict],
    ) -> list[Notification]:
        """批量创建通知（不推送，用于系统批量生成）"""
        records = []
        for n in notifications:
            record = Notification(
                user_id=user_id,
                type=n.get("type", "system"),
                title=n["title"],
                body=n.get("body"),
                data=n.get("data"),
            )
            self.db.add(record)
            records.append(record)

        await self.db.flush()
        return records

    # ========================================
    #  通知查询
    # ========================================

    async def get_history(
        self,
        user_id: uuid.UUID,
        page: int = 1,
        page_size: int = 20,
        type_filter: str = None,
    ) -> dict:
        """获取通知历史（分页）"""
        base_query = select(Notification).where(
            Notification.user_id == user_id
        )
        if type_filter and type_filter in NOTIFICATION_TYPES:
            base_query = base_query.where(Notification.type == type_filter)

        # 总数
        count_result = await self.db.execute(
            select(func.count()).select_from(
                base_query.subquery()
            )
        )
        total = count_result.scalar() or 0

        # 分页查询
        offset = (page - 1) * page_size
        result = await self.db.execute(
            base_query
            .order_by(desc(Notification.created_at))
            .offset(offset)
            .limit(page_size)
        )
        items = list(result.scalars().all())

        # 未读数
        unread_result = await self.db.execute(
            select(func.count()).where(
                Notification.user_id == user_id,
                Notification.is_read == False,
            )
        )
        unread_count = unread_result.scalar() or 0

        return {
            "items": [
                {
                    "id": str(n.id),
                    "type": n.type,
                    "title": n.title,
                    "body": n.body,
                    "data": n.data,
                    "is_read": n.is_read,
                    "created_at": n.created_at.isoformat() if n.created_at else None,
                }
                for n in items
            ],
            "pagination": {
                "page": page,
                "page_size": page_size,
                "total": total,
                "total_pages": (total + page_size - 1) // page_size,
            },
            "unread_count": unread_count,
        }

    async def mark_read(
        self,
        user_id: uuid.UUID,
        notification_ids: list[uuid.UUID],
    ) -> int:
        """标记指定通知为已读，返回更新条数"""
        result = await self.db.execute(
            update(Notification)
            .where(
                Notification.user_id == user_id,
                Notification.id.in_(notification_ids),
                Notification.is_read == False,
            )
            .values(is_read=True)
        )
        await self.db.flush()
        return result.rowcount

    async def mark_all_read(self, user_id: uuid.UUID) -> int:
        """标记所有通知为已读"""
        result = await self.db.execute(
            update(Notification)
            .where(
                Notification.user_id == user_id,
                Notification.is_read == False,
            )
            .values(is_read=True)
        )
        await self.db.flush()
        return result.rowcount

    async def get_unread_count(self, user_id: uuid.UUID) -> int:
        """获取未读数"""
        result = await self.db.execute(
            select(func.count()).where(
                Notification.user_id == user_id,
                Notification.is_read == False,
            )
        )
        return result.scalar() or 0

    # ========================================
    #  推送控制
    # ========================================

    async def _try_push(
        self,
        user_id: uuid.UUID,
        type: str,
        title: str,
        body: str = None,
        data: dict = None,
    ) -> bool:
        """
        尝试推送到设备。
        检查：用户设置开关 → 静默时段 → 活跃设备 → 发送
        """
        # 1. 检查用户是否开启了该类型通知
        settings = await self.get_settings(user_id)
        setting_field = TYPE_TO_SETTING.get(type)
        if setting_field and not getattr(settings, setting_field, True):
            return False

        # 2. 检查静默时段
        if self._is_quiet_hours(settings):
            logger.debug("push_skipped_quiet_hours", user_id=str(user_id), type=type)
            return False

        # 3. 获取活跃设备
        result = await self.db.execute(
            select(UserDevice).where(
                UserDevice.user_id == user_id,
                UserDevice.is_active == True,
            )
        )
        devices = list(result.scalars().all())
        if not devices:
            return False

        # 4. 推送（MVP: 仅记录日志，预留 FCM/APNs 接口）
        for device in devices:
            await self._send_to_device(device, title, body, data)

        return True

    @staticmethod
    def _is_quiet_hours(settings: NotificationSetting) -> bool:
        """检查当前是否在静默时段"""
        now = datetime.now(timezone.utc).time()
        try:
            start_parts = settings.quiet_hours_start.split(":")
            end_parts = settings.quiet_hours_end.split(":")
            quiet_start = time(int(start_parts[0]), int(start_parts[1]))
            quiet_end = time(int(end_parts[0]), int(end_parts[1]))
        except (ValueError, IndexError, AttributeError):
            return False

        if quiet_start <= quiet_end:
            # 简单情况：22:00 - 23:59
            return quiet_start <= now <= quiet_end
        else:
            # 跨午夜：22:00 - 08:00
            return now >= quiet_start or now <= quiet_end

    async def _send_to_device(
        self,
        device: UserDevice,
        title: str,
        body: str = None,
        data: dict = None,
    ):
        """
        发送推送到单个设备。
        MVP 阶段仅记录日志。生产环境对接 FCM/APNs。
        """
        logger.info(
            "push_notification_sent",
            platform=device.platform,
            device_id=str(device.id),
            title=title,
        )
        # TODO: 生产环境实现
        # if device.platform == "ios":
        #     await apns_client.send(device.device_token, title, body, data)
        # elif device.platform == "android":
        #     await fcm_client.send(device.device_token, title, body, data)
        # elif device.platform == "web":
        #     await web_push_client.send(device.device_token, title, body, data)