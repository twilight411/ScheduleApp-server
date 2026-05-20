"""
文件服务 — 头像上传（本地存储 + OSS 预留）
MVP 阶段使用本地存储，生产切换到对象存储
"""
import os
import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.file_upload import FileUpload

settings = get_settings()

# 本地上传目录
UPLOAD_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "uploads")
AVATAR_DIR = os.path.join(UPLOAD_DIR, "avatars")
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
MAX_AVATAR_SIZE = 5 * 1024 * 1024  # 5MB


class FileService:

    def __init__(self, db: AsyncSession):
        self.db = db

    async def save_avatar(
        self,
        user_id: uuid.UUID,
        file_content: bytes,
        content_type: str,
        filename: str,
    ) -> str:
        """
        保存头像文件，返回可访问的 URL。
        """
        # 校验
        if content_type not in ALLOWED_IMAGE_TYPES:
            raise ValueError(f"不支持的图片格式: {content_type}，支持 JPEG/PNG/WebP/GIF")

        if len(file_content) > MAX_AVATAR_SIZE:
            raise ValueError(f"文件大小超限: 最大 {MAX_AVATAR_SIZE // 1024 // 1024}MB")

        # 生成文件名
        ext = self._get_extension(filename, content_type)
        new_filename = f"{user_id}_{uuid.uuid4().hex[:8]}{ext}"

        if settings.oss_endpoint:
            # 生产模式：上传到对象存储
            file_url = await self._upload_to_oss(new_filename, file_content, content_type)
        else:
            # 开发模式：本地存储
            file_url = await self._save_local(new_filename, file_content)

        # 记录上传日志
        upload_record = FileUpload(
            user_id=user_id,
            file_type="avatar",
            file_url=file_url,
            file_size=len(file_content),
        )
        self.db.add(upload_record)
        await self.db.flush()

        return file_url

    async def _save_local(self, filename: str, content: bytes) -> str:
        """本地存储"""
        os.makedirs(AVATAR_DIR, exist_ok=True)
        filepath = os.path.join(AVATAR_DIR, filename)
        with open(filepath, "wb") as f:
            f.write(content)
        return f"/uploads/avatars/{filename}"

    async def _upload_to_oss(self, filename: str, content: bytes, content_type: str) -> str:
        """上传到对象存储（预留）"""
        # TODO: 对接 OSS/S3
        raise NotImplementedError("OSS 上传未实现，请设置 OSS_ENDPOINT=空 以使用本地存储")

    @staticmethod
    def _get_extension(filename: str, content_type: str) -> str:
        """获取文件扩展名"""
        ext_map = {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/webp": ".webp",
            "image/gif": ".gif",
        }
        if "." in filename:
            return "." + filename.rsplit(".", 1)[1].lower()
        return ext_map.get(content_type, ".jpg")
