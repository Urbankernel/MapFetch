"""任务定义与断点恢复（core/task.py、 缓存与断点续跑）。

- TaskSpec：一次抓取任务的完整描述（可 JSON 序列化）。
- CheckpointStore：SQLite 持久化任务状态（待办/完成/失败/空瓦片），
  进程重启后可续跑。缓存命中不消耗配额计数。
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path


class TileState(str, Enum):
    PENDING = "pending"
    DONE = "done"
    EMPTY = "empty"    # 404 等确认不存在的瓦片（不重试）
    FAILED = "failed"  # 重试耗尽后标记，可手动补抓


@dataclass
class TaskSpec:
    """任务定义（core/task.py）。"""

    project: str                 # 项目目录名
    source: str                  # tianditu / amap / baidu
    style: str                   # 图层样式 key（如 img_w / satellite / street）
    levels: list[int]            # 缩放层级列表
    aoi_geojson: dict            # AOI（WGS84 GeoJSON Feature）
    output_crs: str = "EPSG:4490"  # 默认 CGCS2000
    vectorize: bool = False      # 是否进行栅格反演矢量化
    created_at: float = field(default_factory=time.time)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, text: str) -> "TaskSpec":
        return cls(**json.loads(text))


_SCHEMA = """
CREATE TABLE IF NOT EXISTS tiles (
    source TEXT NOT NULL,
    style  TEXT NOT NULL,
    z INTEGER NOT NULL,
    x INTEGER NOT NULL,
    y INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL,
    PRIMARY KEY (source, style, z, x, y)
);
CREATE TABLE IF NOT EXISTS quota (
    source TEXT NOT NULL,
    day    TEXT NOT NULL,      -- YYYY-MM-DD（本地日期）
    used   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (source, day)
);
"""


class CheckpointStore:
    """瓦片状态 + 日配额计数的 SQLite 持久化（L3 预算层）。"""

    def __init__(self, db_path: str | Path):
        self._conn = sqlite3.connect(str(db_path))
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "CheckpointStore":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # ---------------- 瓦片状态 ----------------
    def register(self, source: str, style: str, z: int, x: int, y: int) -> None:
        """登记待办瓦片（已存在则忽略，保证幂等续跑）。"""
        self._conn.execute(
            "INSERT OR IGNORE INTO tiles(source,style,z,x,y,state,updated_at) VALUES(?,?,?,?,?,?,?)",
            (source, style, z, x, y, TileState.PENDING.value, time.time()),
        )

    def register_many(self, source: str, style: str, tiles) -> None:
        self._conn.executemany(
            "INSERT OR IGNORE INTO tiles(source,style,z,x,y,state,updated_at) VALUES(?,?,?,?,?,?,?)",
            [(source, style, t.z, t.x, t.y, TileState.PENDING.value, time.time()) for t in tiles],
        )
        self._conn.commit()

    def set_state(self, source: str, style: str, z: int, x: int, y: int,
                  state: TileState, *, attempts: int | None = None) -> None:
        if attempts is None:
            self._conn.execute(
                "UPDATE tiles SET state=?, updated_at=? WHERE source=? AND style=? AND z=? AND x=? AND y=?",
                (state.value, time.time(), source, style, z, x, y),
            )
        else:
            self._conn.execute(
                "UPDATE tiles SET state=?, attempts=?, updated_at=? WHERE source=? AND style=? AND z=? AND x=? AND y=?",
                (state.value, attempts, time.time(), source, style, z, x, y),
            )
        self._conn.commit()

    def pending(self, source: str, style: str) -> list[tuple[int, int, int]]:
        """待抓瓦片 [(z,x,y), ...]（含失败待补抓的不含 EMPTY/DONE）。"""
        cur = self._conn.execute(
            "SELECT z,x,y FROM tiles WHERE source=? AND style=? AND state IN (?,?) ORDER BY z,x,y",
            (source, style, TileState.PENDING.value, TileState.FAILED.value),
        )
        return cur.fetchall()

    def stats(self, source: str, style: str) -> dict[str, int]:
        cur = self._conn.execute(
            "SELECT state, COUNT(*) FROM tiles WHERE source=? AND style=? GROUP BY state",
            (source, style),
        )
        return {row[0]: row[1] for row in cur.fetchall()}

    # ---------------- 日配额 ----------------
    def quota_used(self, source: str, day: str) -> int:
        cur = self._conn.execute(
            "SELECT used FROM quota WHERE source=? AND day=?", (source, day)
        )
        row = cur.fetchone()
        return row[0] if row else 0

    def quota_incr(self, source: str, day: str, n: int = 1) -> int:
        """消耗配额并返回新值。缓存命中不调用本方法。"""
        self._conn.execute(
            "INSERT INTO quota(source,day,used) VALUES(?,?,?) "
            "ON CONFLICT(source,day) DO UPDATE SET used=used+?",
            (source, day, n, n),
        )
        self._conn.commit()
        return self.quota_used(source, day)
