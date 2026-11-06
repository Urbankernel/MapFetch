"""瓦片本地缓存。

- 路径：cache/tiles/{source}/{style}/{z}/{x}/{y}.png
- 命中缓存不消耗配额计数
- 默认过期 30 天；过期瓦片标记但**不自动删除**，由用户决策
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

from ..core.geo.tile import Tile


class TileCache:
    def __init__(self, root: str | Path, *, expire_days: int = 30):
        self.root = Path(root)
        self.expire_days = expire_days

    @staticmethod
    def cache_key(source: str, style: str, t: Tile) -> str:
        """缓存键（sha1(source+style+z+x+y)，用于审计与去重）。"""
        raw = f"{source}:{style}:{t.z}:{t.x}:{t.y}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def path(self, source: str, style: str, t: Tile) -> Path:
        return self.root / source / style / str(t.z) / str(t.x) / f"{t.y}.png"

    def get(self, source: str, style: str, t: Tile) -> bytes | None:
        """读取缓存；过期返回 None（但不删除文件）。"""
        p = self.path(source, style, t)
        if not p.exists():
            return None
        if self.expire_days > 0:
            age_days = (time.time() - p.stat().st_mtime) / 86400
            if age_days > self.expire_days:
                return None  # 标记过期，重抓覆盖；不自动删除
        return p.read_bytes()

    def put(self, source: str, style: str, t: Tile, data: bytes) -> Path:
        p = self.path(source, style, t)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p

    def exists_fresh(self, source: str, style: str, t: Tile) -> bool:
        return self.get(source, style, t) is not None
