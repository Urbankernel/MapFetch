"""瓦片拼接。

设计要点：
- **不一次性载入内存**（避免一次性载入大图导致峰值内存过高）：
  按瓦片范围计算整体仿射变换后，逐瓦片流式写入 GeoTIFF 分块；
  内存峰值 ≈ 单瓦片 + 一个块行。
- 天地图链路在目标 CRS 为 WGS84/CGCS2000 时跳过重投影（特权）；
  高德/百度链路瓦片四至已纠偏为 WGS84（由各适配器 tile_bounds_wgs84 提供），
  重投影由 export/geotiff.py 的 warp 选项处理。

rasterio/PIL 延迟导入。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from ..core.geo.tile import TILE_SIZE, Tile


@dataclass
class MosaicPlan:
    """拼接计划：输出尺寸与仿射变换（度/像素）。"""

    z: int
    x0: int
    y0: int
    x1: int
    y1: int
    west: float   # WGS84 四至（已纠偏）
    south: float
    east: float
    north: float
    y_down: bool = True  # True=XYZ（y 向南递增）；False=百度式（y 向北递增）

    @property
    def cols(self) -> int:
        return self.x1 - self.x0 + 1

    @property
    def rows(self) -> int:
        return self.y1 - self.y0 + 1

    @property
    def width(self) -> int:
        return self.cols * TILE_SIZE

    @property
    def height(self) -> int:
        return self.rows * TILE_SIZE

    @property
    def res_x(self) -> float:
        return (self.east - self.west) / self.width

    @property
    def res_y(self) -> float:
        return (self.north - self.south) / self.height


def plan_mosaic(adapter, tiles: list[Tile]) -> MosaicPlan:
    """由瓦片集合与适配器（提供 WGS84 四至）计算拼接计划。

    ⚠️ y 轴方向因源而异：XYZ（高德/天地图）y 向南递增，西北角=(x0,y0)；
    百度（BD-09MC）y 向北递增，西北角=(x0,y1)。此前统一按 XYZ 处理，
    导致百度源拼接南北颠倒、地理配准错误（north/south 互换、res_y 为负）。
    """
    xs = [t.x for t in tiles]
    ys = [t.y for t in tiles]
    z = tiles[0].z
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    y_down = getattr(adapter, "y_axis_down", True)
    if y_down:
        nw_t, se_t = Tile(z, x0, y0), Tile(z, x1, y1)
    else:  # 百度式：最小 y 在最南，西北角 = (x0, y1)
        nw_t, se_t = Tile(z, x0, y1), Tile(z, x1, y0)
    nw = adapter.tile_bounds_wgs84(nw_t)
    se = adapter.tile_bounds_wgs84(se_t)
    return MosaicPlan(z=z, x0=x0, y0=y0, x1=x1, y1=y1,
                      west=nw[0], south=se[1], east=se[2], north=nw[3],
                      y_down=y_down)


def compose_geotiff(plan: MosaicPlan, tile_path_fn, out_path: str | Path,
                    *, crs_epsg: int = 4490, block: int = 4096) -> Path:
    """逐瓦片流式写出 GeoTIFF（全程内存受控）。

    - tile_path_fn(Tile) -> Path | None：取瓦片 PNG（None 记为空瓦片留黑）
    - crs_epsg：默认 4490（CGCS2000）；WGS84 传 4326
    - block：分块大小（每块 4096×4096）
    """
    import numpy as np
    import rasterio
    from PIL import Image
    from rasterio.transform import from_origin

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    transform = from_origin(plan.west, plan.north, plan.res_x, plan.res_y)

    with rasterio.open(
        out_path, "w", driver="GTiff",
        width=plan.width, height=plan.height, count=3, dtype="uint8",
        crs=f"EPSG:{crs_epsg}", transform=transform,
        tiled=True, blockxsize=256, blockysize=256, compress="lzw",
    ) as dst:
        for gx in range(plan.cols):
            for gy in range(plan.rows):
                # 图像行序固定北上（row 0 = 最北行）；百度 y 向北递增，
                # 最北行是 y1，故行 gy 对应瓦片 y = y1 - gy（XYZ 则为 y0 + gy）。
                ty = (plan.y0 + gy) if plan.y_down else (plan.y1 - gy)
                t = Tile(plan.z, plan.x0 + gx, ty)
                p = tile_path_fn(t)
                if p is None or not Path(p).exists():
                    arr = np.zeros((TILE_SIZE, TILE_SIZE, 3), dtype="uint8")  # 空瓦片
                else:
                    with Image.open(p) as im:
                        arr = np.asarray(im.convert("RGB"), dtype="uint8")
                # 瓦片像素直接对位写入（同层级同尺寸无需重采样， 天地图链路）
                window = rasterio.windows.Window(
                    col_off=gx * TILE_SIZE, row_off=gy * TILE_SIZE,
                    width=TILE_SIZE, height=TILE_SIZE)
                dst.write(arr[:, :, 0], 1, window=window)
                dst.write(arr[:, :, 1], 2, window=window)
                dst.write(arr[:, :, 2], 3, window=window)

        # 内部金字塔
        overviews = _overview_levels(plan.width, plan.height)
        if overviews:
            dst.build_overviews(overviews, rasterio.enums.Resampling.average)
            dst.update_tags(ns="rio_overview", resampling="average")
    return out_path


def _overview_levels(width: int, height: int) -> list[int]:
    levels: list[int] = []
    f = 2
    while max(width, height) // f >= 256 and f <= 32:
        levels.append(f)
        f *= 2
    return levels


def split_blocks(plan: MosaicPlan, merge_size: int = 4) -> list[tuple[int, int, int, int]]:
    """按 merge_size×merge_size 瓦片分块（merge_size 分块策略），
    返回 [(x0,y0,x1,y1), ...] 瓦片坐标块，避免单张输出过大。
    """
    blocks = []
    for bx0 in range(plan.x0, plan.x1 + 1, merge_size):
        for by0 in range(plan.y0, plan.y1 + 1, merge_size):
            blocks.append((bx0, by0, min(bx0 + merge_size - 1, plan.x1),
                           min(by0 + merge_size - 1, plan.y1)))
    return blocks
