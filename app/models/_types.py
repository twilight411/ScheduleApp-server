"""
跨数据库类型 — GUID
PostgreSQL → 用原生 UUID 列
SQLite / MySQL → 退化为 CHAR(36) 字符串

使用方式（替换原来的 dialects.postgresql.UUID(as_uuid=True)）:

    from app.models._types import GUID

    class User(Base):
        id: Mapped[uuid.UUID] = mapped_column(
            GUID, primary_key=True, default=uuid.uuid4
        )
"""
import uuid
from typing import Any

from sqlalchemy.types import TypeDecorator, CHAR
from sqlalchemy.dialects.postgresql import UUID as PG_UUID


class GUID(TypeDecorator):
    """
    平台无关的 UUID 类型。

    在 PostgreSQL 上实际为原生 UUID；
    在 SQLite/MySQL 上存为 36 字符 CHAR。

    Python 侧统一以 uuid.UUID 实例工作。
    """
    impl = CHAR
    cache_ok = True

    def __init__(self, *args, **kwargs):
        # 默认 length=36，PG 上会被 load_dialect_impl 覆盖
        super().__init__(36, *args, **kwargs)

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PG_UUID(as_uuid=True))
        return dialect.type_descriptor(CHAR(36))

    def process_bind_param(self, value: Any, dialect) -> Any:
        if value is None:
            return None
        if dialect.name == "postgresql":
            # PG UUID 类型直接接受 uuid.UUID 实例
            return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
        # SQLite / MySQL: 存为 36 字符串
        if isinstance(value, uuid.UUID):
            return str(value)
        return str(uuid.UUID(str(value)))

    def process_result_value(self, value: Any, dialect) -> Any:
        if value is None:
            return None
        if isinstance(value, uuid.UUID):
            return value
        return uuid.UUID(str(value))
