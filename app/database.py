"""
异步数据库连接池 — 基于 SQLAlchemy 2.0 async

[P1 修复]
  - SQLite 改用 NullPool（强制每次请求新连接），消除"假并发持有写锁"
  - PostgreSQL 走正常连接池
  - SQLite WAL + busy_timeout 仍保留作为防御
  - 自动检测 DB 类型决定参数

NOTE：生产强烈建议切到 PostgreSQL；SQLite 仅供本地开发。
"""
import asyncio
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool
from sqlalchemy import event

from app.config import get_settings

settings = get_settings()


def _is_sqlite() -> bool:
    return "sqlite" in settings.database_url.lower()


def _is_postgres() -> bool:
    url = settings.database_url.lower()
    return "postgresql" in url or "postgres" in url


def _create_engine():
    """根据数据库类型创建对应的引擎配置"""
    if _is_sqlite():
        # SQLite：用 NullPool 强制每次新连接
        # 配合 PRAGMA WAL + busy_timeout 把写锁概率降到最低
        # 但 SQLite 仍是单写者数据库，高并发请直接切 PG
        return create_async_engine(
            settings.database_url,
            echo=settings.database_echo,
            poolclass=NullPool,
            connect_args={
                "check_same_thread": False,
                "timeout": 30,
            },
        )

    # PostgreSQL（推荐生产环境）
    return create_async_engine(
        settings.database_url,
        echo=settings.database_echo,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
        pool_recycle=1800,        # 30 分钟后回收，防止数据库主动断连
        pool_timeout=30,
    )


engine = _create_engine()

async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


class Base(DeclarativeBase):
    """所有 ORM 模型的基类"""
    pass


async def get_db() -> AsyncSession:
    """FastAPI 依赖注入：获取数据库 session"""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


# SQLite 特定的 PRAGMA — 仅 SQLite 时启用
if _is_sqlite():
    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA synchronous=NORMAL;")
        cursor.execute("PRAGMA busy_timeout=30000;")
        cursor.execute("PRAGMA foreign_keys=ON;")
        cursor.close()


async def init_db():
    """开发用：应用启动时创建所有表（生产用 Alembic）"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def close_db():
    """应用关闭时释放连接池"""
    await engine.dispose()


async def health_check_db() -> bool:
    """数据库连通性检查（用于 /health/ready）"""
    try:
        async with async_session_factory() as session:
            from sqlalchemy import text
            await session.execute(text("SELECT 1"))
        return True
    except Exception:
        return False