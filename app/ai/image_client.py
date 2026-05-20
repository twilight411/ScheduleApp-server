"""
图像生成客户端 — 支持 OpenAI DALL-E / MidJourney / 即梦API / 其他生图API
带重试、兜底、计费
"""
import json
import time
from typing import Optional

import httpx
import structlog

from app.config import get_settings

logger = structlog.get_logger()
settings = get_settings()

# Provider → Base URL 映射
PROVIDER_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "midjourney": "https://api.midjourney.com/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "dashscope": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "jiyun": "https://visual.volcengineapi.com",
    "local": "http://localhost:8080/v1",
}

MAX_RETRIES = 2
TIMEOUT_SECONDS = 120
JIYUN_POLL_INTERVAL = 3  # 轮询间隔（秒）
JIYUN_MAX_POLLS = 20     # 最大轮询次数


class ImageClient:
    """
    图像生成客户端 — 支持多种API。
    支持生成图像，带重试和 fallback。
    """

    def __init__(self):
        self.provider = settings.image_provider
        self.api_key = settings.image_api_key if settings.image_api_key else settings.llm_api_key
        self.model = settings.image_model
        self.size = settings.image_size
        self.quality = settings.image_quality
        # 即梦API凭证（火山引擎）
        self.jiyun_access_key_id = settings.jiyun_access_key_id
        self.jiyun_secret_access_key = settings.jiyun_secret_access_key
        # 优先用显式配置的 base_url
        if settings.image_base_url:
            self.base_url = settings.image_base_url
        else:
            self.base_url = PROVIDER_BASE_URLS.get(self.provider, "https://api.openai.com/v1")
        
        # 即梦SDK客户端（延迟初始化）
        self._jiyun_client = None

    async def generate(
        self,
        prompt: str,
        size: Optional[str] = None,
        quality: Optional[str] = None,
        user_id: Optional[str] = None,
        purpose: Optional[str] = None,
    ) -> str:
        """调用图像生成API生成图像"""
        start_time = time.time()

        # 如果没有 API Key 就走 fallback
        if self.provider == "jiyun":
            if not self.jiyun_access_key_id or not self.jiyun_secret_access_key:
                logger.warning("jiyun_no_credentials", purpose=purpose)
                return await self._fallback(prompt, purpose)
        elif not self.api_key:
            logger.warning("image_no_api_key", purpose=purpose)
            return await self._fallback(prompt, purpose)

        if self.provider == "jiyun":
            # 即梦API特殊处理（异步任务模式）
            return await self._generate_jiyun(prompt, size, purpose, start_time)
        else:
            # OpenAI兼容API
            return await self._generate_openai_compatible(prompt, size, quality, purpose, start_time)

    async def _generate_openai_compatible(
        self,
        prompt: str,
        size: Optional[str],
        quality: Optional[str],
        purpose: Optional[str],
        start_time: float,
    ) -> str:
        """调用OpenAI兼容的图像生成API"""
        effective_size = size or self.size
        effective_quality = quality or self.quality

        body = {
            "model": self.model,
            "prompt": prompt,
            "n": 1,
            "size": effective_size,
            "quality": effective_quality,
            "response_format": "url",
        }

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        last_error = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
                    # 根据provider选择不同的API路径
                    if self.provider == "deepseek":
                        # DeepSeek图像生成API路径
                        url = f"{self.base_url}/text2image"
                        body = {
                            "prompt": prompt,
                            "width": 1024,
                            "height": 1024,
                            "model": "deepseek-chat",
                        }
                    else:
                        url = f"{self.base_url}/images/generations"
                    
                    resp = await client.post(url, json=body, headers=headers)
                    resp.raise_for_status()
                    data = resp.json()

                # 解析响应（不同API返回格式可能不同）
                if self.provider == "deepseek":
                    image_url = data.get("result", {}).get("url", "")
                else:
                    image_url = data["data"][0]["url"]
                duration_ms = int((time.time() - start_time) * 1000)

                logger.info(
                    "image_generation_success",
                    model=self.model,
                    purpose=purpose,
                    duration_ms=duration_ms,
                    size=effective_size,
                )
                return image_url

            except httpx.HTTPStatusError as e:
                last_error = e
                if e.response.status_code == 429:
                    logger.warning("image_rate_limited", attempt=attempt)
                    await _async_sleep(2 ** attempt)
                elif e.response.status_code >= 500:
                    logger.warning("image_server_error", attempt=attempt, status=e.response.status_code)
                    await _async_sleep(1)
                else:
                    logger.error("image_client_error", status=e.response.status_code, body=e.response.text[:500])
                    break
            except (httpx.TimeoutException, httpx.ConnectError) as e:
                last_error = e
                logger.warning("image_timeout", attempt=attempt)
                await _async_sleep(1)

        logger.error("image_all_retries_failed", error=str(last_error), purpose=purpose)
        return await self._fallback(prompt, purpose)

    async def _generate_jiyun(
        self,
        prompt: str,
        size: Optional[str],
        purpose: Optional[str],
        start_time: float,
    ) -> str:
        """调用即梦API生成图像（使用火山引擎官方SDK）"""
        try:
            # 初始化SDK客户端
            if self._jiyun_client is None:
                from volcengine.visual.VisualService import VisualService
                self._jiyun_client = VisualService()
                self._jiyun_client.set_ak(self.jiyun_access_key_id)
                self._jiyun_client.set_sk(self.jiyun_secret_access_key)
            
            # 第一步：提交任务
            width, height = self._get_jiyun_dimensions(size)
            
            submit_body = {
                "req_key": "jimeng_t2i_v30",
                "prompt": prompt[:800],
                "seed": -1,
                "width": width,
                "height": height,
                "use_pre_llm": True,
            }

            logger.debug("jiyun_submit", body=submit_body)
            
            # 使用SDK提交任务
            submit_data = self._jiyun_client.cv_sync2async_submit_task(submit_body)
            logger.debug("jiyun_submit_response", response=submit_data)
            
            if submit_data.get("code") != 10000:
                logger.error("jiyun_submit_failed", response=submit_data)
                return await self._fallback(prompt, purpose)

            task_id = submit_data["data"].get("task_id")
            if not task_id:
                logger.error("jiyun_no_task_id", response=submit_data)
                return await self._fallback(prompt, purpose)

            logger.info("jiyun_task_submitted", task_id=task_id)

            # 第二步：轮询查询结果
            for attempt in range(JIYUN_MAX_POLLS):
                await _async_sleep(JIYUN_POLL_INTERVAL)
                
                query_body = {
                    "req_key": "jimeng_t2i_v30",
                    "task_id": task_id,
                    "req_json": '{"return_url":true}',
                }

                # 使用SDK查询结果
                query_data = self._jiyun_client.cv_sync2async_get_result(query_body)
                logger.debug("jiyun_query_response", response=query_data)

                if query_data.get("code") != 10000:
                    logger.error("jiyun_query_failed", response=query_data)
                    continue

                status = query_data["data"].get("status")
                if status == "done":
                    image_urls = query_data["data"].get("image_urls", [])
                    if image_urls:
                        duration_ms = int((time.time() - start_time) * 1000)
                        logger.info(
                            "jiyun_image_generation_success",
                            task_id=task_id,
                            duration_ms=duration_ms,
                        )
                        return image_urls[0]
                    else:
                        logger.error("jiyun_no_image_url", response=query_data)
                        return await self._fallback(prompt, purpose)
                elif status in ["in_queue", "generating"]:
                    logger.debug("jiyun_task_pending", task_id=task_id, status=status)
                    continue
                elif status in ["not_found", "expired"]:
                    logger.error("jiyun_task_failed", task_id=task_id, status=status)
                    return await self._fallback(prompt, purpose)

            logger.error("jiyun_poll_timeout", task_id=task_id)
            return await self._fallback(prompt, purpose)

        except Exception as e:
            logger.error("jiyun_api_error", error=str(e))
            return await self._fallback(prompt, purpose)

    @staticmethod
    def _get_jiyun_dimensions(size: Optional[str]) -> tuple[int, int]:
        """将尺寸字符串转换为即梦API的宽高"""
        size_map = {
            "1024x1024": (1024, 1024),
            "1024x1792": (1024, 1792),
            "1792x1024": (1792, 1024),
            "512x512": (512, 512),
            "1328x1328": (1328, 1328),
        }
        return size_map.get(size or "1328x1328", (1328, 1328))

    async def _fallback(self, prompt: str, purpose: str = None) -> str:
        """图像生成不可用时的兜底方案"""
        logger.info("image_using_fallback", purpose=purpose)
        return "https://neeko-copilot.bytedance.net/api/text_to_image?prompt=minimalist%20healing%20illustration%20cute%20dreamy%20style&image_size=square"


async def _async_sleep(seconds: float):
    import asyncio
    await asyncio.sleep(seconds)


# 全局实例
image_client = ImageClient()