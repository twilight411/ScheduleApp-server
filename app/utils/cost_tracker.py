"""
LLM 调用成本追踪
"""
import time
from dataclasses import dataclass, field
from typing import Optional

import structlog

logger = structlog.get_logger()


@dataclass
class LLMCallRecord:
    model: str
    input_tokens: int
    output_tokens: int
    duration_ms: int
    user_id: Optional[str] = None
    purpose: Optional[str] = None  # parse / decompose / chat / negotiate / report
    timestamp: float = field(default_factory=time.time)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class CostTracker:
    """
    简单的内存级成本追踪器。
    生产环境应持久化到数据库或时序数据库。
    """

    def __init__(self):
        self._records: list[LLMCallRecord] = []

    def record(self, record: LLMCallRecord):
        self._records.append(record)
        logger.info(
            "llm_call",
            model=record.model,
            tokens=record.total_tokens,
            duration_ms=record.duration_ms,
            purpose=record.purpose,
            user_id=record.user_id,
        )

    def get_user_call_count(self, user_id: str, window_seconds: int = 3600) -> int:
        """获取用户在时间窗口内的调用次数"""
        cutoff = time.time() - window_seconds
        return sum(
            1 for r in self._records
            if r.user_id == user_id and r.timestamp >= cutoff
        )


# 全局实例
cost_tracker = CostTracker()
