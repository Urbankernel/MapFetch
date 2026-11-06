"""基础设施测试：限速 / 配额 / 缓存 / 断点。

均为纯本地逻辑，不发起网络请求。
"""

import asyncio

from mapfetch.core.task import CheckpointStore, TileState
from mapfetch.fetch.cache import TileCache
from mapfetch.fetch.ratelimit import (AIMDController, CircuitBreaker,
                                      DailyBudget, TokenBucket)
from mapfetch.core.geo.tile import Tile


class TestTokenBucket:
    def test_burst_then_wait(self):
        bucket = TokenBucket(rate=10, capacity=2)

        async def run():
            t0 = asyncio.get_event_loop().time()
            await bucket.acquire()
            await bucket.acquire()  # 突发容量 2：立即获得
            await bucket.acquire()  # 第三个需等待 ~0.1s
            return asyncio.get_event_loop().time() - t0

        elapsed = asyncio.run(run())
        assert elapsed >= 0.05  # 有等待发生

    def test_aimd(self):
        bucket = TokenBucket(rate=2.0, capacity=5)
        aimd = AIMDController(bucket, max_rate=4.0, success_window=3, increase=0.5)
        for _ in range(3):
            aimd.on_success()
        assert bucket.rate == 2.5  # 加性增备
        aimd.on_throttle()
        assert bucket.rate == 1.25  # 乘性减小 ×0.5
        assert bucket.rate >= 0.1  # 下限保护


class TestBudgetAndBreaker:
    def test_budget_states(self, tmp_path):
        with CheckpointStore(tmp_path / "t.db") as store:
            budget = DailyBudget(store, "tianditu", 10, warn_at=0.8)
            assert budget.check() == "ok"
            budget.consume(8)
            assert budget.check() == "warn"
            budget.consume(2)
            assert budget.check() == "stop"

    def test_breaker(self):
        b = CircuitBreaker(threshold=3, cooldown_sec=999)
        b.on_429()
        b.on_429()
        assert not b.is_open
        b.on_429()
        assert b.is_open


class TestCache:
    def test_roundtrip(self, tmp_path):
        cache = TileCache(tmp_path, expire_days=30)
        t = Tile(17, 109752, 53172)
        assert cache.get("amap", "satellite", t) is None
        cache.put("amap", "satellite", t, b"\x89PNG\r\n\x1a\n" + b"0" * 200)
        assert cache.get("amap", "satellite", t) is not None
        assert cache.exists_fresh("amap", "satellite", t)

    def test_expired_not_deleted(self, tmp_path):
        cache = TileCache(tmp_path, expire_days=0)  # 立即过期
        t = Tile(17, 1, 1)
        cache.put("amap", "street", t, b"x" * 200)
        p = cache.path("amap", "street", t)
        assert p.exists()  # 过期不自动删除


class TestCheckpoint:
    def test_register_and_resume(self, tmp_path):
        db = tmp_path / "cp.db"
        tiles = [Tile(15, 100, 200), Tile(15, 101, 200)]
        with CheckpointStore(db) as store:
            store.register_many("amap", "street", tiles)
            store.set_state("amap", "street", 15, 100, 200, TileState.DONE)
        # 重启续跑：重新打开数据库
        with CheckpointStore(db) as store:
            pending = store.pending("amap", "street")
            assert pending == [(15, 101, 200)]
            stats = store.stats("amap", "street")
            assert stats.get("done") == 1 and stats.get("pending") == 1
