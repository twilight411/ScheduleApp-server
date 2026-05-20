"""
API 限流中间件 — 基于 Redis 的滑动窗口限流

修复: 从 request.app.state.redis 延迟获取 Redis 客户端，
解决中间件注册时 Redis 尚未初始化的时序问题。
"""
import structlog
from fastapi import Request, Response, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

logger = structlog.get_logger()

# 默认限流配置：每分钟100次
DEFAULT_RATE_LIMIT = 100
DEFAULT_WINDOW_SECONDS = 60

# 无需限流的路径
EXEMPT_PATHS = {
    "/api/v1/health",
    "/api/v1/health/ready",
    "/docs",
    "/redoc",
    "/openapi.json",
}


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    全局 API 限流。
    基于客户端 IP 的滑动窗口计数。
    Redis 从 app.state 延迟获取，无 Redis 时放行。
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        # 跳过健康检查等豁免路径
        if request.url.path in EXEMPT_PATHS:
            return await call_next(request)

        # 从 app.state 延迟获取 redis（解决初始化时序问题）
        redis = getattr(request.app.state, "redis", None)
        if not redis:
            return await call_next(request)

        # 使用 IP 作为限流 key
        client_ip = request.client.host if request.client else "unknown"
        key = f"rate_limit:{client_ip}"

        try:
            current = await redis.get(key)
            if current and int(current) >= DEFAULT_RATE_LIMIT:
                logger.warning("rate_limit_exceeded", ip=client_ip, path=request.url.path)
                return JSONResponse(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    content={
                        "success": False,
                        "data": None,
                        "error": {
                            "code": "RATE_LIMIT_EXCEEDED",
                            "message": "请求频率超限，请稍后再试",
                            "details": None,
                        },
                    },
                )

            pipe = redis.pipeline()
            pipe.incr(key)
            pipe.expire(key, DEFAULT_WINDOW_SECONDS)
            await pipe.execute()
        except Exception as e:
            # Redis 故障时放行，不影响正常请求
            logger.warning("rate_limit_redis_error", error=str(e))

        return await call_next(request)
