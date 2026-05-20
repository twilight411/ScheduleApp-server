"""
LLM 调用封装 — 支持 OpenAI 兼容接口 (OpenAI / DeepSeek / 通义千问 / vLLM)
带重试、兜底、计费、JSON 模式、用户级限流
"""
import json
import time
import re
from typing import Optional

import httpx
import structlog

from app.config import get_settings
from app.utils.cost_tracker import cost_tracker, LLMCallRecord

logger = structlog.get_logger()
settings = get_settings()

# Provider → Base URL 映射
PROVIDER_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "dashscope": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "local": "http://localhost:8080/v1",
}

MAX_RETRIES = 2
TIMEOUT_SECONDS = 60


class LLMClient:
    """
    LLM 调用客户端 — OpenAI 兼容接口。
    支持纯文本 / JSON 模式，带重试和 fallback。
    """

    def __init__(self):
        self.provider = settings.llm_provider
        self.api_key = settings.llm_api_key
        self.model = settings.llm_model
        # 优先用显式配置的 base_url
        if settings.llm_base_url:
            self.base_url = settings.llm_base_url
        else:
            self.base_url = PROVIDER_BASE_URLS.get(self.provider, "https://api.openai.com/v1")

    async def _check_user_rate_limit(self, user_id: str) -> None:
        """
        检查用户级 LLM 调用限流。
        使用 Redis INCR + EXPIRE 实现每小时计数。
        超限时抛出 ValueError。
        """
        from app.utils.redis_client import get_redis

        redis = await get_redis()
        if not redis:
            return  # Redis 不可用时放行

        key = f"llm_rate:{user_id}"
        try:
            count = await redis.incr(key)
            if count == 1:
                # 首次计数，设置 1 小时过期
                await redis.expire(key, 3600)
            if count > settings.llm_rate_limit_per_user_per_hour:
                logger.warning(
                    "llm_user_rate_limited",
                    user_id=user_id,
                    count=count,
                    limit=settings.llm_rate_limit_per_user_per_hour,
                )
                raise ValueError("AI 调用次数已达上限，请稍后再试")
        except ValueError:
            raise  # 重新抛出限流错误
        except Exception as e:
            # Redis 故障时放行
            logger.warning("llm_rate_limit_check_error", error=str(e))

    async def complete(
        self,
        system: str,
        user: str,
        max_tokens: int = 2048,
        temperature: float = 0.7,
        json_mode: bool = False,
        user_id: Optional[str] = None,
        purpose: Optional[str] = None,
    ) -> str:
        """调用 LLM 生成文本回复"""
        # ===== 用户级限流检查 =====
        if user_id:
            await self._check_user_rate_limit(user_id)

        start_time = time.time()

        # 如果没有 API Key 就走 fallback
        if not self.api_key:
            logger.warning("llm_no_api_key", purpose=purpose)
            return await self._fallback(system, user, purpose)

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

        body = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        if json_mode:
            body["response_format"] = {"type": "json_object"}

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        last_error = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
                    resp = await client.post(
                        f"{self.base_url}/chat/completions",
                        json=body,
                        headers=headers,
                    )
                    resp.raise_for_status()
                    data = resp.json()

                response_text = data["choices"][0]["message"]["content"]
                usage = data.get("usage", {})
                duration_ms = int((time.time() - start_time) * 1000)

                cost_tracker.record(LLMCallRecord(
                    model=self.model,
                    input_tokens=usage.get("prompt_tokens", 0),
                    output_tokens=usage.get("completion_tokens", 0),
                    duration_ms=duration_ms,
                    user_id=user_id,
                    purpose=purpose,
                ))

                logger.info(
                    "llm_call_success",
                    model=self.model,
                    purpose=purpose,
                    duration_ms=duration_ms,
                    tokens=usage.get("total_tokens", 0),
                )
                return response_text

            except httpx.HTTPStatusError as e:
                last_error = e
                if e.response.status_code == 429:
                    logger.warning("llm_rate_limited", attempt=attempt)
                    await _async_sleep(2 ** attempt)
                elif e.response.status_code >= 500:
                    logger.warning("llm_server_error", attempt=attempt, status=e.response.status_code)
                    await _async_sleep(1)
                else:
                    logger.error("llm_client_error", status=e.response.status_code, body=e.response.text[:500])
                    break
            except (httpx.TimeoutException, httpx.ConnectError) as e:
                last_error = e
                logger.warning("llm_timeout", attempt=attempt)
                await _async_sleep(1)

        # 所有重试失败，走 fallback
        logger.error("llm_all_retries_failed", error=str(last_error), purpose=purpose)
        return await self._fallback(system, user, purpose)

    async def complete_json(
        self,
        system: str,
        user: str,
        max_tokens: int = 2048,
        temperature: float = 0.3,
        user_id: Optional[str] = None,
        purpose: Optional[str] = None,
    ) -> dict:
        """调用 LLM 并解析 JSON 响应，带容错"""
        response = await self.complete(
            system=system,
            user=user,
            max_tokens=max_tokens,
            temperature=temperature,
            json_mode=True,
            user_id=user_id,
            purpose=purpose,
        )
        return _parse_json_response(response)

    async def _fallback(self, system: str, user: str, purpose: str = None) -> str:
        """LLM 不可用时的兜底方案"""
        logger.info("llm_using_fallback", purpose=purpose)
        return "[FALLBACK]"


def _parse_json_response(text: str) -> dict:
    """
    从 LLM 响应中提取 JSON，处理常见的格式问题：
    - Markdown code blocks
    - 前后有多余文本
    - 不完整的 JSON
    """
    # 尝试直接解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 尝试从 markdown code block 中提取
    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    # 尝试找到第一个 { 和最后一个 }
    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        try:
            return json.loads(text[first_brace:last_brace + 1])
        except json.JSONDecodeError:
            pass

    logger.error("json_parse_failed", text=text[:500])
    return {}


async def _async_sleep(seconds: float):
    import asyncio
    await asyncio.sleep(seconds)


# 全局实例
llm_client = LLMClient()