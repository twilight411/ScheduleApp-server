"""
精灵日程管理系统 — FastAPI 主入口
"""
import structlog
from contextlib import asynccontextmanager
from fastapi import FastAPI

from app.config import get_settings
from app.database import init_db, close_db
from app.middleware.cors import setup_cors
from app.middleware.error_handler import register_error_handlers
from app.middleware.request_id import RequestIdMiddleware
from app.middleware.rate_limit import RateLimitMiddleware
# from app.utils.redis_client import init_redis, close_redis, get_redis

# ===== 配置结构化日志 =====
structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer() if get_settings().is_development
        else structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(20),  # INFO
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
)

logger = structlog.get_logger()


# ===== 生命周期管理 =====
@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用启动/关闭时的初始化和清理"""
    settings = get_settings()
    logger.info("app_starting", env=settings.app_env, version=settings.app_version)

    # 启动：初始化数据库
    if settings.is_development:
        await init_db()
        logger.info("database_tables_created")

    # 将 settings 挂载到 app.state
    app.state.settings = settings

    yield

    # 关闭：释放资源
    await close_db()
    logger.info("app_stopped")


# ===== 创建 FastAPI 实例 =====
settings = get_settings()

app = FastAPI(
    title="精灵日程管理系统",
    description="基于 AI 的智能日程管理系统，通过5个拟人化精灵帮助用户管理时间",
    version=settings.app_version,
    lifespan=lifespan,
    docs_url="/docs" if settings.is_development else None,
    redoc_url="/redoc" if settings.is_development else None,
)


# ===== 注册中间件（注意顺序：后添加的先执行）=====
setup_cors(app)
app.add_middleware(RequestIdMiddleware)
# RateLimitMiddleware 从 request.app.state.redis 延迟获取 Redis，无时序问题
app.add_middleware(RateLimitMiddleware)


# ===== 注册全局错误处理 =====
register_error_handlers(app)


# ===== 注册路由 =====
from app.routers import (
    system,
    auth,
    users,
    profile,
    tasks,
    schedule,
    ai,
    reports,
    tree,
    fruits,
    notifications,
    weekly_focus,  # Sprint 1
)

API_V1 = "/api/v1"

# 系统接口（无需鉴权）
app.include_router(system.router, prefix=API_V1)

# 鉴权接口
app.include_router(auth.router, prefix=API_V1)

# 需要鉴权的业务接口
app.include_router(users.router, prefix=API_V1)
app.include_router(profile.router, prefix=API_V1)
app.include_router(tasks.router, prefix=API_V1)
app.include_router(schedule.router, prefix=API_V1)
app.include_router(ai.router, prefix=API_V1)
app.include_router(reports.router, prefix=API_V1)
app.include_router(tree.router, prefix=API_V1)
app.include_router(fruits.router, prefix=API_V1)
app.include_router(notifications.router, prefix=API_V1)
app.include_router(weekly_focus.router, prefix=API_V1)  # Sprint 1

# ===== 挂载 Flutter Web 静态文件 =====
from fastapi.staticfiles import StaticFiles
import os
_web_build = os.path.join(os.path.dirname(__file__), "..", "web_build")
if os.path.isdir(_web_build):
    app.mount("/", StaticFiles(directory=_web_build, html=True), name="web")
