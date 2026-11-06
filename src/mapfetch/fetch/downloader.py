"""HTTP 下载器（重试与退避、 反爬虫红线）。

- httpx 异步客户端；重依赖延迟导入（保证无 httpx 环境下 core 仍可运行）。
- User-Agent 标明工具名（允许项）；不做 UA 伪装、不做代理池。
- 错误语义：429 降速、403 停源、404 记空瓦片不重试、5xx/超时指数退避。
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from enum import Enum


class FetchOutcome(str, Enum):
    OK = "ok"
    EMPTY = "empty"        # 404：瓦片不存在
    THROTTLED = "throttled"  # 429
    FORBIDDEN = "forbidden"  # 403：停源
    FAILED = "failed"      # 重试耗尽


@dataclass
class FetchResult:
    outcome: FetchOutcome
    data: bytes | None = None
    status: int | None = None
    error: str | None = None
    retry_after: float | None = None


USER_AGENT = "mapfetch/0.1 (research tool; contact: project maintainer)"  #  允许项


def validate_tile_bytes(data: bytes, *, min_bytes: int = 100) -> bool:
    """瓦片内容校验（响应体异常检测的第一道：尺寸与签名）。

    纯色/占位图检测在 process 层做（需要图像解码），这里只做廉价检查：
    PNG/JPEG 文件签名 + 最小字节数。
    """
    if data is None or len(data) < min_bytes:
        return False
    png = data[:8] == b"\x89PNG\r\n\x1a\n"
    jpg = data[:3] == b"\xff\xd8\xff"
    return png or jpg


def server_message(resp) -> str:
    """提取服务端 JSON 错误提示（天地图返回 code/msg/resolve， 可诊断性）。"""
    if "json" not in resp.headers.get("content-type", ""):
        return ""
    try:
        data = resp.json()
    except Exception:
        return ""
    if isinstance(data, dict):
        return " / ".join(str(data[k]) for k in ("code", "msg", "resolve")
                          if data.get(k))
    return str(data)[:200]


class TileDownloader:
    """异步瓦片下载器：信号量并发 + 指数退避 + 均匀抖动。"""

    def __init__(self, *, max_concurrent: int, retry_cfg, timeout: float = 20.0,
                 extra_headers: dict[str, str] | None = None):
        try:
            import httpx  # 延迟导入（依赖，仅引擎 A 运行时需要）
        except ImportError as exc:
            raise RuntimeError(
                "引擎 A 需要 httpx：pip install httpx（≥0.27）"
            ) from exc

        self._sem = asyncio.Semaphore(max_concurrent)
        self._retry = retry_cfg
        headers = {"User-Agent": USER_AGENT}
        if extra_headers:  # 如浏览器端 Key 需要的 Referer 白名单
            headers.update(extra_headers)
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers=headers,
            follow_redirects=True,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "TileDownloader":
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.close()

    def _backoff(self, attempt: int) -> float:
        """t = min(base * 2^n, 60s) + 均匀抖动 ±30%。"""
        base = min(self._retry.backoff_base_sec * (2 ** attempt), self._retry.backoff_max_sec)
        jitter = base * self._retry.jitter * random.uniform(-1, 1)
        return max(0.1, base + jitter)

    async def fetch(self, url: str) -> FetchResult:
        async with self._sem:
            last_error: str | None = None
            for attempt in range(self._retry.max_attempts):
                try:
                    resp = await self._client.get(url)
                except Exception as exc:  # 连接超时/网络错误：退避重试
                    last_error = f"{type(exc).__name__}: {exc}"
                    await asyncio.sleep(self._backoff(attempt))
                    continue

                status = resp.status_code
                if status == 200:
                    if not validate_tile_bytes(resp.content):
                        return FetchResult(FetchOutcome.FAILED, status=status,
                                           error="响应体非有效瓦片（尺寸/签名异常）")
                    return FetchResult(FetchOutcome.OK, data=resp.content, status=status)
                if status == 404:
                    # 瓦片不存在：不重试，记空瓦片
                    return FetchResult(FetchOutcome.EMPTY, status=status)
                if status == 429:
                    retry_after = float(resp.headers.get("Retry-After", "0") or 0)
                    return FetchResult(FetchOutcome.THROTTLED, status=status,
                                       retry_after=retry_after)
                if status == 403:
                    # 停源，不重试：提示检查 Key/配额/Referer 白名单
                    detail = server_message(resp)
                    err = "403 Forbidden：请检查 Key/配额/Referer 白名单"
                    if detail:
                        err += f"；服务端提示：{detail}"
                    return FetchResult(FetchOutcome.FORBIDDEN, status=status, error=err)
                # 5xx 等：退避重试
                last_error = f"HTTP {status}"
                await asyncio.sleep(self._backoff(attempt))

            return FetchResult(FetchOutcome.FAILED, error=last_error or "重试耗尽")
