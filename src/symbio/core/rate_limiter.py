"""并发流控器 - 基于令牌桶算法的异步流控"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict

from symbio.utils.logger import get_logger

logger = get_logger("rate_limiter")


class TokenBucket:
    """令牌桶"""

    def __init__(self, rate: float, capacity: int):
        """
        Args:
            rate: 令牌生成速率（个/秒）
            capacity: 桶容量
        """
        self.rate = rate
        self.capacity = capacity
        self._tokens = capacity
        self._last_time = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: int = 1) -> None:
        """获取令牌，必要时等待"""
        async with self._lock:
            while True:
                self._refill()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                # 计算需要等待的时间
                wait_time = (tokens - self._tokens) / self.rate
                logger.debug(f"令牌不足，等待 {wait_time:.2f}s")
                await asyncio.sleep(wait_time)

    def _refill(self) -> None:
        """补充令牌"""
        now = time.monotonic()
        elapsed = now - self._last_time
        new_tokens = elapsed * self.rate
        self._tokens = min(self.capacity, self._tokens + new_tokens)
        self._last_time = now

    def release(self, tokens: int = 1) -> None:
        """释放令牌"""
        self._tokens = min(self.capacity, self._tokens + tokens)


class RateLimitExceededError(Exception):
    """速率限制超限错误"""

    pass


class RateLimiter:
    """并发流控器

    为不同模型维护独立的令牌桶，实现速率限制。
    """

    # 默认速率配置（请求/秒）
    DEFAULT_RATES = {
        "claude-3-5-haiku-20241022": (50, 100),  # (rate, capacity)
        "claude-sonnet-4-20250514": (20, 50),
        "claude-opus-4-20250514": (10, 30),
        "default": (10, 30),
    }

    # 重试配置
    MAX_RETRIES = 3
    BASE_DELAY = 1.0
    MAX_DELAY = 60.0

    def __init__(self):
        self._buckets: dict[str, TokenBucket] = {}
        self._retry_counts: dict[str, int] = defaultdict(int)

    def _get_bucket(self, model: str) -> TokenBucket:
        """获取或创建令牌桶"""
        if model not in self._buckets:
            rate, capacity = self.DEFAULT_RATES.get(model, self.DEFAULT_RATES["default"])
            self._buckets[model] = TokenBucket(rate, capacity)
            logger.debug(f"创建令牌桶: {model}, rate={rate}, capacity={capacity}")
        return self._buckets[model]

    async def acquire(self, model: str, tokens: int = 1) -> None:
        """获取执行许可

        Args:
            model: 模型名称
            tokens: 需要的令牌数
        """
        bucket = self._get_bucket(model)
        await bucket.acquire(tokens)
        logger.debug(f"获取令牌成功: {model}, tokens={tokens}")

    async def handle_rate_limit(self, model: str, error: Exception) -> None:
        """处理 429 错误，指数退避重试

        Args:
            model: 模型名称
            error: 原始错误

        Raises:
            RateLimitExhaustedError: 重试次数耗尽
        """
        # 修复：使用 model 作为 key，而不是 id(error)
        # id(error) 每次都是不同的值，导致 retry_count 永远从 1 开始
        # 这样就无法正确计数重试次数，导致内存泄漏
        retry_key = model
        self._retry_counts[retry_key] += 1
        retry_count = self._retry_counts[retry_key]

        if retry_count > self.MAX_RETRIES:
            del self._retry_counts[retry_key]
            raise RateLimitExceededError(f"模型 {model} 速率限制重试次数耗尽")

        # 指数退避
        delay = min(self.BASE_DELAY * (2 ** (retry_count - 1)), self.MAX_DELAY)
        logger.warning(f"速率限制: {model}, 第 {retry_count} 次重试, 等待 {delay:.1f}s")
        await asyncio.sleep(delay)

    def clear_retry(self, model: str) -> None:
        """清除重试计数"""
        keys_to_remove = [k for k in self._retry_counts if k.startswith(model)]
        for key in keys_to_remove:
            del self._retry_counts[key]

    def get_status(self, model: str) -> dict:
        """获取速率限制状态"""
        bucket = self._buckets.get(model)
        if not bucket:
            return {"model": model, "status": "not_initialized"}

        return {
            "model": model,
            "tokens": bucket._tokens,
            "capacity": bucket.capacity,
            "rate": bucket.rate,
        }
