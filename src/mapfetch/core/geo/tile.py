"""瓦片索引计算。

- 高德/天地图：标准 Web Mercator XYZ（与 Google 一致），256px。
- 百度：BD-09MC 米制，原点为墨卡托原点，y 轴向上为正，x/y 可为负。

⚠️ 假设标注（文档缺口）：百度瓦片分辨率采用公开资料主流口径
``resolution(z) = 2 ** (18 - z)``（m/px），即 z18≈1m/px、z19≈0.5m/px。
 指出层级映射存在分歧，最终值以 瓦片接口记录校准；
``BAIDU_LEVEL_OFFSET`` 为此预留的修正位（实测后若非 0 则调整并通过自检用例）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterator

from .crs import MERCATOR_LAT_LIMIT

TILE_SIZE = 256
EARTH_RADIUS = 6378137.0
ORIGIN_SHIFT = math.pi * EARTH_RADIUS  # 20037508.342789244

# 百度层级偏移修正位（校准；当前假设为 0）
BAIDU_LEVEL_OFFSET = 0


@dataclass(frozen=True)
class Tile:
    """瓦片坐标。百度源的 x/y 可为负值。"""

    z: int
    x: int
    y: int


def resolution_web_mercator(lat_deg: float, z: int, tile_size: int = TILE_SIZE) -> float:
    """标准 Web Mercator 地面分辨率（m/px， 公式）。"""
    return 156543.03392 * math.cos(math.radians(lat_deg)) / (2 ** z)


def lonlat_to_tile(lon: float, lat: float, z: int) -> Tile:
    """经纬度 -> 标准 XYZ 瓦片索引（输入为该源坐标系的经纬度）。"""
    lat = max(min(lat, MERCATOR_LAT_LIMIT), -MERCATOR_LAT_LIMIT)
    n = 2 ** z
    x = int(math.floor((lon + 180.0) / 360.0 * n))
    lat_rad = math.radians(lat)
    y = int(math.floor((1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * n))
    return Tile(z, min(max(x, 0), n - 1), min(max(y, 0), n - 1))


def tile_to_lonlat_bounds(x: int, y: int, z: int) -> tuple[float, float, float, float]:
    """瓦片 -> (min_lon, min_lat, max_lon, max_lat)，标准 XYZ。"""
    n = 2 ** z
    min_lon = x / n * 360.0 - 180.0
    max_lon = (x + 1) / n * 360.0 - 180.0

    def _lat(ty: int) -> float:
        v = math.pi * (1 - 2 * ty / n)
        return math.degrees(math.atan(math.sinh(v)))

    return min_lon, _lat(y + 1), max_lon, _lat(y)


def tile_range_for_bbox(
    min_lon: float, min_lat: float, max_lon: float, max_lat: float, z: int
) -> tuple[int, int, int, int]:
    """经纬度 BBox -> (x_min, y_min, x_max, y_max)（含两端，标准 XYZ）。

    禁止生成跨越 ±180° 的单一 BBox（边界表）。
    """
    if min_lon > max_lon:
        raise ValueError("BBox 跨越 ±180° 经线，请先拆分 AOI")
    t_nw = lonlat_to_tile(min_lon, max_lat, z)
    t_se = lonlat_to_tile(max_lon, min_lat, z)
    # XYZ 的 y 轴向南递增：西北角 y 更小
    return t_nw.x, t_nw.y, t_se.x, t_se.y


def iter_tiles(x_min: int, y_min: int, x_max: int, y_max: int, z: int) -> Iterator[Tile]:
    """按行列顺序遍历瓦片范围（含两端）。"""
    for x in range(x_min, x_max + 1):
        for y in range(y_min, y_max + 1):
            yield Tile(z, x, y)


def count_tiles(x_min: int, y_min: int, x_max: int, y_max: int) -> int:
    return (x_max - x_min + 1) * (y_max - y_min + 1)


# ---------------------------------------------------------------------------
# 百度（BD-09MC 基准，x/y 可为负）
# ---------------------------------------------------------------------------

def baidu_resolution(z: int) -> float:
    """百度地面分辨率 m/px（假设口径 2^(18-z)，见模块 docstring 假设标注）。"""
    return 2.0 ** (18 - (z + BAIDU_LEVEL_OFFSET))


def bd09mc_to_tile(mc_x: float, mc_y: float, z: int) -> Tile:
    """BD-09MC 米制坐标 -> 百度瓦片索引（原点为墨卡托原点，y 向上为正）。"""
    size = baidu_resolution(z) * TILE_SIZE
    return Tile(z, int(math.floor(mc_x / size)), int(math.floor(mc_y / size)))


def baidu_tile_to_mc_bounds(x: int, y: int, z: int) -> tuple[float, float, float, float]:
    """百度瓦片 -> (min_x, min_y, max_x, max_y) 米制四至。"""
    size = baidu_resolution(z) * TILE_SIZE
    return x * size, y * size, (x + 1) * size, (y + 1) * size


def baidu_tile_range_for_mc_bbox(
    min_x: float, min_y: float, max_x: float, max_y: float, z: int
) -> tuple[int, int, int, int]:
    """BD-09MC BBox -> (x_min, y_min, x_max, y_max)（含两端，百度规则）。"""
    t_min = bd09mc_to_tile(min_x, min_y, z)
    t_max = bd09mc_to_tile(max_x, max_y, z)
    return t_min.x, t_min.y, t_max.x, t_max.y
