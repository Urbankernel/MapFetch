"""MBTiles 导出：供 QGIS / Leaflet 直接加载。

MBTiles 即 SQLite：metadata 表 + tiles 表（z/x/y 翻转 TMS y）。
⚠️ MBTiles 规范要求瓦片为 Web Mercator XYZ；天地图 _w 与高德瓦片
满足（高德数值带 GCJ-02 偏移，属源数据特性，元数据中声明）；
百度瓦片非标准 XYZ，不写入 MBTiles（调用方负责源判断）。
纯 sqlite3 实现，无重依赖。
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from ..core.geo.tile import Tile

_SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (name TEXT, value TEXT);
CREATE TABLE IF NOT EXISTS tiles (
    zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB,
    PRIMARY KEY (zoom_level, tile_column, tile_row));
"""


def write_mbtiles(out_path: str | Path, *, name: str, description: str,
                  tile_rows: list[tuple[Tile, bytes]],
                  bounds: tuple[float, float, float, float],
                  center: tuple[float, float, int],
                  attribution: str, crs_note: str) -> Path:
    """写入 MBTiles。

    - tile_rows：[(Tile, png_bytes), ...]，Tile 为标准 XYZ 编号
    - bounds/center：WGS84 四至与中心（展示用元数据）
    - crs_note：坐标系说明（元数据要求）
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()

    conn = sqlite3.connect(str(out_path))
    try:
        conn.executescript(_SCHEMA)
        meta = {
            "name": name,
            "format": "png",
            "version": "1.0.0",
            "description": description,
            "attribution": attribution,
            "type": "overlay",
            "bounds": ",".join(f"{v:.6f}" for v in bounds),
            "center": f"{center[0]:.6f},{center[1]:.6f},{center[2]}",
            "crs_note": crs_note,
            "created": str(int(time.time())),
        }
        conn.executemany("INSERT INTO metadata(name,value) VALUES(?,?)", meta.items())

        def tms_y(t: Tile) -> int:
            return (2 ** t.z - 1) - t.y  # XYZ -> TMS 翻转

        conn.executemany(
            "INSERT OR REPLACE INTO tiles(zoom_level,tile_column,tile_row,tile_data) VALUES(?,?,?,?)",
            [(t.z, t.x, tms_y(t), data) for t, data in tile_rows],
        )
        conn.commit()
    finally:
        conn.close()
    return out_path
