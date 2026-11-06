"""三层限流模型（docs/rate-limiting.md §1）。

L1 令牌桶（速率） + L2 并发信号量 + L3 日配额预算与熔断。
AIMD 调速：连续成功 100 次速率 +0.5（不超上限），
任一降速事件（429/熔断）立即 ×0.5。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass


class TokenBucket:
    """令牌桶（L1）：允许突发但限制平均速率；取不到令牌则等待。"""

    def __init__(self, rate: float, capacity: int):
        self._rate = rate            # 稳态 QPS
        self._capacity = capacity    # 突发容量
        self._tokens = float(capacity)
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    @property
    def rate(self) -> float:
        return self._rate

    def set_rate(self, rate: float) -> None:
        self._rate = max(0.1, rate)

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(self._capacity, self._tokens + (now - self._last) * self._rate)
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                await asyncio.sleep((1.0 - self._tokens) / self._rate)


class AIMDController:
    """加性增备、乘性减小。"""

    def __init__(self, bucket: TokenBucket, *, max_rate: float,
                 success_window: int = 100, increase: float = 0.5, decrease: float = 0.5):
        self._bucket = bucket
        self._max_rate = max_rate
        self._window = success_window
        self._increase = increase
        self._decrease = decrease
        self._streak = 0

    def on_success(self) -> None:
        self._streak += 1
        if self._streak >= self._window:
            self._streak = 0
            self._bucket.set_rate(min(self._max_rate, self._bucket.rate + self._increase))

    def on_throttle(self) -> None:
        """429 或熔断事件：立即乘性减小。"""
        self._streak = 0
        self._bucket.set_rate(self._bucket.rate * self._decrease)


@dataclass
class CircuitBreaker:
    """熔断器（L3）：429 连续超阈值熔断，冷却期后半速恢复。"""

    threshold: int
    cooldown_sec: float = 300.0
    _consecutive_429: int = 0
    _opened_at: float | None = None

    @property
    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        return (time.monotonic() - self._opened_at) < self.cooldown_sec

    def on_429(self) -> None:
        self._consecutive_429 += 1
        if self._consecutive_429 >= self.threshold:
            self._opened_at = time.monotonic()

    def on_success(self) -> None:
        self._consecutive_429 = 0
        if self._opened_at is not None and not self.is_open:
            self._opened_at = None  # 冷却结束，半速恢复由 AIMD 自然爬升完成

    def remaining_cooldown(self) -> float:
        if not self.is_open or self._opened_at is None:
            return 0.0
        return self.cooldown_sec - (time.monotonic() - self._opened_at)


class DailyBudget:
    """日配额预算（L3）：80% 告警、100% 熔断。

    计数持久化在 CheckpointStore（SQLite，跨进程/跨天累计）。
    """

    def __init__(self, store, source: str, budget: int, *, warn_at: float = 0.8):
        self._store = store
        self._source = source
        self._budget = budget
        self._warn_at = warn_at

    @staticmethod
    def _today() -> str:
        return time.strftime("%Y-%m-%d", time.localtime())

    def used(self) -> int:
        return self._store.quota_used(self._source, self._today())

    def check(self) -> str:
        """返回 'ok' / 'warn' / 'stop'（L3 语义）。"""
        if self._budget <= 0:
            return "ok"
        used = self.used()
        if used >= self._budget:
            return "stop"
        if used >= self._budget * self._warn_at:
            return "warn"
        return "ok"

    def consume(self, n: int = 1) -> None:
        self._store.quota_incr(self._source, self._today(), n)
