"""瓦片索引自检（与已知样本比对一致）。

已知样本：上海 (121.47, 31.23) z17 的标准 XYZ 瓦片索引，
按公式手算基准。
"""

import math

import pytest

from mapfetch.core.geo import tile as tilemod
from mapfetch.core.geo.tile import Tile


class TestStandardXYZ:
    def test_known_tile(self):
        # 手算：lon=121.47 → x=floor((121.47+180)/360*131072)=floor(109752.53)=109752
        t = tilemod.lonlat_to_tile(121.47, 31.23, 17)
        assert t.x == int(math.floor((121.47 + 180) / 360 * 2**17))
        lat_rad = math.radians(31.23)
        expect_y = int(math.floor(
            (1 - math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi) / 2 * 2**17))
        assert t.y == expect_y

    def test_bounds_inverse(self):
        t = tilemod.lonlat_to_tile(121.47, 31.23, 17)
        min_lon, min_lat, max_lon, max_lat = tilemod.tile_to_lonlat_bounds(t.x, t.y, t.z)
        assert min_lon <= 121.47 <= max_lon
        assert min_lat <= 31.23 <= max_lat

    def test_range_and_count(self):
        x0, y0, x1, y1 = tilemod.tile_range_for_bbox(120.30, 36.05, 120.42, 36.12, 15)
        assert x1 >= x0 and y1 >= y0
        assert tilemod.count_tiles(x0, y0, x1, y1) == (x1 - x0 + 1) * (y1 - y0 + 1)

    def test_antimeridian_rejected(self):
        with pytest.raises(ValueError):
            tilemod.tile_range_for_bbox(170.0, 30.0, -170.0, 40.0, 10)

    def test_iter_tiles_complete(self):
        tiles = list(tilemod.iter_tiles(3, 4, 5, 6, 10))
        assert len(tiles) == 3 * 3
        assert tiles[0] == Tile(10, 3, 4)

    def test_resolution_baseline(self):
        # 基线：z17 @ 上海（31.2°N）≈ 1.02 m/px
        r = tilemod.resolution_web_mercator(31.2, 17)
        assert abs(r - 1.02) < 0.02


class TestBaiduTiles:
    def test_mc_tile_negative_ok(self):
        # 百度 x/y 可为负：原点西南侧
        t = tilemod.bd09mc_to_tile(-1000.0, -1000.0, 18)
        assert t.x < 0 and t.y < 0

    def test_tile_mc_bounds_inverse(self):
        mc = (12958161.0, 4852334.0)  # 北京附近的 BD-09MC 量级
        t = tilemod.bd09mc_to_tile(*mc, 18)
        min_x, min_y, max_x, max_y = tilemod.baidu_tile_to_mc_bounds(t.x, t.y, t.z)
        assert min_x <= mc[0] <= max_x and min_y <= mc[1] <= max_y

    def test_resolution_assumption(self):
        # 假设口径 2^(18-z)：z18 = 1 m/px（校准点）
        assert tilemod.baidu_resolution(18) == 1.0
