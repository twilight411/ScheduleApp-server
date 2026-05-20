"""
时间工具函数
"""
from datetime import datetime, date, timedelta


def get_week_start(d: date = None) -> date:
    """获取某日所在周的周一"""
    if d is None:
        d = date.today()
    return d - timedelta(days=d.weekday())


def get_week_end(d: date = None) -> date:
    """获取某日所在周的周日"""
    return get_week_start(d) + timedelta(days=6)


def get_month_start(d: date = None) -> date:
    """获取某日所在月的第一天"""
    if d is None:
        d = date.today()
    return d.replace(day=1)


def parse_time(time_str: str) -> tuple[int, int]:
    """解析 HH:MM 格式时间字符串，返回 (hour, minute)"""
    parts = time_str.split(":")
    return int(parts[0]), int(parts[1])


def time_str_to_minutes(time_str: str) -> int:
    """将 HH:MM 转为当天的分钟数"""
    h, m = parse_time(time_str)
    return h * 60 + m


def minutes_to_time_str(minutes: int) -> str:
    """将分钟数转为 HH:MM"""
    h = minutes // 60
    m = minutes % 60
    return f"{h:02d}:{m:02d}"
