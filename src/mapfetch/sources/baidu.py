"""百度适配器（docs/data-sources.md §4）。

- 瓦片编号基于 BD-09MC（墨卡托米制），x/y 可为负。
- y 轴方向与 XYZ 相反：y 向北递增（y_axis_down=False），
  拼接层（process/mosaic.py）据此确定西北角瓦片与行摆放顺序。
- 层级映射采用 core/geo/tile.py 的假设口径（2^(18-z)），
  以已知地标自检用例校准（见 tests/test_tile.py）。
"""

from __future__ import annotations

from ..core.aoi import AOI
from ..core.geo import crs, tile as tilemod
from ..core.geo.tile import Tile
from .base import SourceAdapter


class BaiduAdapter(SourceAdapter):
    @property
    def name(self) -> str:
        return "baidu"

    @property
    def y_axis_down(self) -> bool:
        return False  # 百度瓦片 y 向北递增（BD-09MC 原点在墨卡托原点）

    def tile_range(self, aoi: AOI, z: int) -> tuple[int, int, int, int]:
        min_lon, min_lat, max_lon, max_lat = aoi.bbox
        # WGS84 -> BD-09LL -> BD-09MC（百度瓦片坐标输入为 BD-09MC）
        sw_ll = crs.wgs84_to_bd09ll(min_lon, min_lat)
        ne_ll = crs.wgs84_to_bd09ll(max_lon, max_lat)
        sw_mc = crs.bd09ll_to_bd09mc(*sw_ll)
        ne_mc = crs.bd09ll_to_bd09mc(*ne_ll)
        return tilemod.baidu_tile_range_for_mc_bbox(sw_mc[0], sw_mc[1], ne_mc[0], ne_mc[1], z)

    def tile_bounds_wgs84(self, t: Tile) -> tuple[float, float, float, float]:
        min_x, min_y, max_x, max_y = tilemod.baidu_tile_to_mc_bounds(t.x, t.y, t.z)
        sw_ll = crs.bd09mc_to_bd09ll(min_x, min_y)
        ne_ll = crs.bd09mc_to_bd09ll(max_x, max_y)
        sw = crs.bd09ll_to_wgs84(*sw_ll)
        ne = crs.bd09ll_to_wgs84(*ne_ll)
        return sw[0], sw[1], ne[0], ne[1]
