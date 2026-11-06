"""瓦片下载调度器（调度流程）。

流程：瓦片索引集 → 令牌桶 → 并发信号量 → 缓存命中? → 日配额? → HTTP
      → 200 校验入缓存 / 429 降速重排 / 5xx 退避 / 403 停源 / 404 记空瓦片

缓存命中不消耗配额；断点续跑经 CheckpointStore。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from ..core.geo.tile import Tile
from ..core.task import CheckpointStore, TileState
from ..sources.base import SourceAdapter
from .cache import TileCache
from .downloader import FetchOutcome, TileDownloader
from .ratelimit import AIMDController, CircuitBreaker, DailyBudget, TokenBucket

logger = logging.getLogger("mapfetch.fetch")


class ScheduleAbort(Exception):
    """熔断或停源导致的中止（L3 /  403）。"""


@dataclass
class ScheduleReport:
    total: int = 0
    done: int = 0
    cached: int = 0
    empty: int = 0
    failed: int = 0
    throttled_events: int = 0
    aborted: bool = False
    abort_reason: str | None = None
    failed_tiles: list[Tile] = field(default_factory=list)


class TileScheduler:
    """按既定流程调度一个 (source, style, z) 图层下的瓦片集合。"""

    def __init__(
        self,
        adapter: SourceAdapter,
        style: str,
        *,
        cache: TileCache,
        store: CheckpointStore,
        rate_cfg,          # config.loader.RateLimitConfig
        retry_cfg,
        breaker_cfg,
        progress_cb=None,  # fn(done, total, msg)
        extra_headers: dict[str, str] | None = None,
    ):
        self.adapter = adapter
        self.style = style
        self.extra_headers = extra_headers or None
        self.cache = cache
        self.store = store
        self.bucket = TokenBucket(rate_cfg.qps, rate_cfg.burst)
        self.aimd = AIMDController(self.bucket, max_rate=rate_cfg.qps * 2)
        self.budget = DailyBudget(store, adapter.name, rate_cfg.daily_budget)
        self.breaker = CircuitBreaker(breaker_cfg.consecutive_429_threshold)
        self.retry_cfg = retry_cfg
        self.max_concurrent = rate_cfg.max_concurrent
        self.progress_cb = progress_cb
        self.source = adapter.name

    async def run(self, tiles: list[Tile]) -> ScheduleReport:
        report = ScheduleReport(total=len(tiles))
        async with TileDownloader(max_concurrent=self.max_concurrent,
                                  retry_cfg=self.retry_cfg,
                                  extra_headers=self.extra_headers) as dl:
            for t in tiles:
                if self.breaker.is_open:
                    report.aborted = True
                    report.abort_reason = (
                        f"熔断器开启（连续 429），冷却 {self.breaker.remaining_cooldown():.0f}s（L3）"
                    )
                    break

                budget_state = self.budget.check()
                if budget_state == "stop":
                    report.aborted = True
                    report.abort_reason = (
                        f"日配额预算耗尽（{self.budget.used()} 次），任务熔断（L3）"
                    )
                    break
                if budget_state == "warn":
                    logger.warning("数据源 %s 日配额已达 80%%（%d 次）",
                                   self.source, self.budget.used())

                try:
                    outcome = await self._fetch_one(dl, t, report)
                except ScheduleAbort as exc:
                    report.aborted = True
                    report.abort_reason = str(exc)
                    break
                if outcome is False:
                    continue  # 已计入 report

                self._progress(report)
        return report

    async def _fetch_one(self, dl: TileDownloader, t: Tile, report: ScheduleReport) -> bool:
        # 缓存命中：不计配额
        if self.cache.exists_fresh(self.source, self.style, t):
            self.store.set_state(self.source, self.style, t.z, t.x, t.y, TileState.DONE)
            report.done += 1
            report.cached += 1
            return True

        await self.bucket.acquire()          # L1 令牌桶
        url = self.adapter.tile_url(self.style, t)
        self.budget.consume()                # L3 配额计数（仅真实请求）
        result = await dl.fetch(url)

        if result.outcome is FetchOutcome.OK:
            self.cache.put(self.source, self.style, t, result.data)
            self.store.set_state(self.source, self.style, t.z, t.x, t.y, TileState.DONE)
            self.aimd.on_success()
            self.breaker.on_success()
            report.done += 1
        elif result.outcome is FetchOutcome.EMPTY:
            self.store.set_state(self.source, self.style, t.z, t.x, t.y, TileState.EMPTY)
            report.empty += 1
        elif result.outcome is FetchOutcome.THROTTLED:
            self.aimd.on_throttle()          # 速率 ×0.5
            self.breaker.on_429()
            report.throttled_events += 1
            wait = result.retry_after or 5.0
            logger.warning("429 限流，降速并等待 %.0fs（%s %d/%d/%d）",
                           wait, self.source, t.z, t.x, t.y)
            await asyncio.sleep(wait)
            await self._fetch_one(dl, t, report)  # 重排本瓦片
            return True
        elif result.outcome is FetchOutcome.FORBIDDEN:
            raise ScheduleAbort(
                f"403 Forbidden，已停止数据源 {self.source}：{result.error}"
            )
        else:  # FAILED
            self.store.set_state(self.source, self.style, t.z, t.x, t.y, TileState.FAILED,
                                 attempts=self.retry_cfg.max_attempts)
            report.failed += 1
            report.failed_tiles.append(t)
        return True

    def _progress(self, report: ScheduleReport) -> None:
        if self.progress_cb:
            self.progress_cb(report.done + report.empty, report.total,
                             f"{self.source}/{self.style}")
