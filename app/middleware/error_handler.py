"""
全局异常处理 — 统一错误响应格式
"""
import traceback

import structlog
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

logger = structlog.get_logger()


def register_error_handlers(app: FastAPI):
    """注册全局异常处理器"""

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        """Pydantic 校验失败"""
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "success": False,
                "data": None,
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "输入校验失败",
                    "details": exc.errors(),
                },
            },
        )

    @app.exception_handler(404)
    async def not_found_handler(request: Request, exc):
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={
                "success": False,
                "data": None,
                "error": {
                    "code": "RESOURCE_NOT_FOUND",
                    "message": "请求的资源不存在",
                    "details": None,
                },
            },
        )

    @app.exception_handler(Exception)
    async def general_error_handler(request: Request, exc: Exception):
        """兜底：未处理的异常"""
        request_id = getattr(request.state, "request_id", "unknown")
        logger.error(
            "unhandled_exception",
            request_id=request_id,
            path=request.url.path,
            method=request.method,
            error=str(exc),
            traceback=traceback.format_exc(),
        )
        
        # 安全获取 settings，避免未初始化导致的 AttributeError
        app_debug = False
        try:
            app_debug = getattr(request.app.state, "settings", None)
            app_debug = app_debug.app_debug if app_debug else False
        except AttributeError:
            pass
        
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "success": False,
                "data": None,
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": "服务器内部错误",
                    "details": str(exc) if app_debug else None,
                },
            },
        )
