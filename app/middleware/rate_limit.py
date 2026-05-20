"""
API 限流中间件 — 基于 Redis 的滑动窗口限流
"""
import time

import structlog
from fastapi import Request, Response, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

logger = structlog.get_logger()

# 默认限流配置：每分钟100次
DEFAULT_RATE_LIMIT = 100
DEFAULT_WINDOW_SECONDS = 60


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    全局 API 限流。
    基于用户 IP 或 user_id（如果已鉴权）。
    """

    def __init__(self, app, redis_client=None):
        super().__init__(app)
        self.redis = redis_client

    async def dispatch(self, request: Request, call_next) -> Response:
        # 跳过健康检查和鉴权接口
        if request.url.path in ("/api/v1/health", "/api/v1/health/ready"):
            return await call_next(request)

        if not self.redis:
            return await call_next(request)

        # 使用 IP 作为限流 key（鉴权后可改为 user_id）
        client_ip = request.client.host if request.client else "unknown"
        key = f"rate_limit:{client_ip}"

        try:
            current = await self.redis.get(key)
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

            pipe = self.redis.pipeline()
            pipe.incr(key)
            pipe.expire(key, DEFAULT_WINDOW_SECONDS)
            await pipe.execute()
        except Exception as e:
            # Redis 故障时放行，不影响正常请求
            logger.warning("rate_limit_redis_error", error=str(e))

        return await call_next(request)
