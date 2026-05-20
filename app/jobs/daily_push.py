"""
定时任务 — 每天早上 7:00 推送今日日程摘要

Sprint D 增强:
  - AI 生成个性化精灵晨间寄语
  - 根据今日日程重点精灵选择寄语精灵
  - 包含生命树健康度提示（如果近期下降）

注册名: app.jobs.daily_push.push_daily_schedule
触发: celery_app.conf.beat_schedule["daily_morning_push"]

流程:
  1. 遍历所有活跃用户
  2. 加载今日日程
  3. 生成精灵寄语 + 日程摘要
  4. 通过 NotificationService 创建通知 + 推送
"""
import asyncio
from datetime import datetime, date, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.jobs.celery_app import celery_app
from app.database import async_session_factory
from app.models.user import User
from app.services.schedule_service import ScheduleService
from app.services.notification_service import NotificationService
from app.ai.llm_client import llm_client

import structlog

logger = structlog.get_logger()

# 用户默认时区（后续可从 user.timezone 读取）
DEFAULT_TZ = ZoneInfo("Asia/Shanghai")

SPIRIT_EMOJIS = {
    "light": "💡", "water": "💧", "soil": "🌱",
    "air": "💨", "nutrition": "✨",
}

SPIRIT_NAMES = {
    "light": "光精灵", "water": "水精灵", "soil": "土壤精灵",
    "air": "空气精灵", "nutrition": "营养精灵",
}


@celery_app.task(name="app.jobs.daily_push.push_daily_schedule")
def push_daily_schedule():
    """入口"""
    asyncio.run(_push_all())


async def _push_all():
    """遍历所有活跃用户，推送今日日程"""
    async with async_session_factory() as session:
        today = datetime.now(timezone.utc).date()

        result = await session.execute(
            select(User.id, User.name).where(
                User.is_active == True,
                User.is_deleted == False,
            )
        )
        users = result.all()

        logger.info("daily_push_start", user_count=len(users), date=str(today))

        success = 0
        skipped = 0
        errors = 0

        for user_id, user_name in users:
            try:
                pushed = await _push_for_user(session, user_id, user_name, today)
                if pushed:
                    success += 1
                else:
                    skipped += 1
                await session.commit()
            except Exception as e:
                await session.rollback()
                errors += 1
                logger.error(
                    "daily_push_user_error",
                    user_id=str(user_id),
                    error=str(e),
                )

        logger.info(
            "daily_push_done",
            success=success,
            skipped=skipped,
            errors=errors,
            date=str(today),
        )


async def _push_for_user(
    session,
    user_id,
    user_name: str,
    today: date,
) -> bool:
    """为单个用户生成并推送今日日程摘要"""
    schedule_svc = ScheduleService(session)
    notif_svc = NotificationService(session)

    # 获取今日日程
    day_data = await schedule_svc.get_day_schedule(user_id, today)

    if not day_data:
        # 今天没有日程，发一条轻提醒
        await notif_svc.create_notification(
            user_id=user_id,
            type="daily_schedule",
            title="☀️ 早安！今天暂无安排",
            body="享受自由的一天吧，或者给精灵们安排些任务？",
            data={"date": str(today), "item_count": 0},
            push=True,
        )
        return True

    # 解析日程项
    items = day_data.items if hasattr(day_data, "items") else day_data.get("items", [])
    if isinstance(items, str):
        import json
        items = json.loads(items)

    if not items:
        await notif_svc.create_notification(
            user_id=user_id,
            type="daily_schedule",
            title="☀️ 早安！今天暂无安排",
            body="享受自由的一天吧！",
            data={"date": str(today), "item_count": 0},
            push=True,
        )
        return True

    # Sprint D: 生成精灵寄语 + 摘要
    title, body = await _build_smart_summary(user_name, items, today)

    await notif_svc.create_notification(
        user_id=user_id,
        type="daily_schedule",
        title=title,
        body=body,
        data={
            "date": str(today),
            "item_count": len(items),
        },
        push=True,
    )
    return True


async def _build_smart_summary(
    user_name: str, items: list, today: date
) -> tuple[str, str]:
    """
    Sprint D: 智能日程摘要 — 含精灵寄语。

    策略:
      1. 找到今日主导精灵（任务数最多的精灵）
      2. 用该精灵的语气生成一句寄语
      3. 拼接日程摘要
    """
    count = len(items)
    greeting = _get_greeting(user_name)

    # 按精灵分组统计
    by_spirit: dict[str, int] = {}
    first_item_time = None
    first_title = ""
    for item in items:
        spirit = item.get("spirit", "light") if isinstance(item, dict) else "light"
        by_spirit[spirit] = by_spirit.get(spirit, 0) + 1
        if first_item_time is None:
            t = item.get("time_start", "") if isinstance(item, dict) else ""
            if t:
                first_item_time = t
                first_title = item.get("title", "") if isinstance(item, dict) else ""

    # 主导精灵
    dominant_spirit = max(by_spirit, key=by_spirit.get) if by_spirit else "light"

    # 标题: 精灵寄语风格
    title = f"{greeting}今天有 {count} 项安排"

    # 精灵分布
    spirit_parts = []
    for code, cnt in sorted(by_spirit.items(), key=lambda x: -x[1]):
        emoji = SPIRIT_EMOJIS.get(code, "📌")
        spirit_parts.append(f"{emoji}×{cnt}")
    spirit_line = " ".join(spirit_parts)

    # 尝试 AI 生成寄语（轻量级，失败不阻塞）
    spirit_greeting = await _generate_spirit_greeting(
        dominant_spirit, count, first_title, today
    )

    body = f"📋 {spirit_line}"
    if spirit_greeting:
        body = f"{spirit_greeting}\n{body}"
    if first_item_time and first_title:
        body += f"\n⏰ 第一项: {first_item_time} {first_title}"

    return title, body


async def _generate_spirit_greeting(
    spirit_code: str,
    task_count: int,
    first_task: str,
    today: date,
) -> str:
    """
    用主导精灵的语气生成一句晨间寄语（≤30字）。
    失败返回 fallback 寄语。
    """
    name = SPIRIT_NAMES.get(spirit_code, "精灵")
    emoji = SPIRIT_EMOJIS.get(spirit_code, "")

    # 尝试 AI 生成
    try:
        system = f"""你是{emoji}{name}。用你的性格特点，写一句晨间寄语（不超过30字）。
要求：温暖、有精灵特色、给人今天的动力。不要用引号，直接输出一句话。"""

        weekday_names = ["一", "二", "三", "四", "五", "六", "日"]
        weekday = weekday_names[today.weekday()]

        user = f"今天周{weekday}，有{task_count}个任务，第一项是「{first_task}」"

        result = await llm_client.complete(
            system=system,
            user=user,
            max_tokens=60,
            purpose="daily_spirit_greeting",
        )

        if result and not result.startswith("[FALLBACK]"):
            return f"{emoji} {result.strip().strip('\"')}"
    except Exception:
        pass

    # Fallback 寄语
    fallback = {
        "light": f"{emoji} 聚焦目标，效率拉满！",
        "water": f"{emoji} 记得在忙碌中给自己留一点柔软时间~",
        "soil": f"{emoji} 身体是一切的基石，动起来！",
        "air": f"{emoji} 今天有社交安排，带上好心情出发吧！",
        "nutrition": f"{emoji} 用心感受生活，灵感在路上~",
    }
    return fallback.get(spirit_code, f"{emoji} 新的一天，加油！")


def _get_greeting(name: str) -> str:
    """根据时间生成问候语（使用正确的时区转换）"""
    hour = datetime.now(DEFAULT_TZ).hour

    display_name = name[:6] if name else ""

    if hour < 9:
        return f"☀️ 早安{display_name}！"
    elif hour < 12:
        return f"🌤 上午好{display_name}！"
    else:
        return f"👋 {display_name}，"