"""
通用 Schema — 统一响应格式、分页、错误码
"""
from typing import Any, Generic, Optional, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: Any = None


class ApiResponse(BaseModel, Generic[T]):
    """统一响应格式"""
    success: bool = True
    data: Optional[T] = None
    error: Optional[ErrorDetail] = None
    message: Optional[str] = None


class PaginatedData(BaseModel, Generic[T]):
    """分页数据"""
    items: list[T]
    total: int
    page: int
    page_size: int
    total_pages: int


class PaginationParams(BaseModel):
    """分页参数"""
    page: int = Field(1, ge=1)
    page_size: int = Field(20, ge=1, le=100)

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


def success_response(data: Any = None, message: str = None) -> dict:
    """快速构建成功响应"""
    return {"success": True, "data": data, "message": message}


def error_response(code: str, message: str, details: Any = None) -> dict:
    """快速构建错误响应"""
    return {
        "success": False,
        "data": None,
        "error": {"code": code, "message": message, "details": details},
    }
