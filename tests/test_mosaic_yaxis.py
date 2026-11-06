"""百度源拼接 y 轴方向回归测试。

百度瓦片（BD-09MC）y 向北递增，与 XYZ（高德/天地图，y 向南递增）相反。
此前 plan_mosaic / compose_geotiff 统一按 XYZ 处理，导致百度源：
- 拼接内容南北颠倒（截图表现为中潭路在上、虹口足球场在下）；
- 地理配准错误（north/south 取自错误的瓦片行，res_y 为负或为 0）。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from mapfetch.core.geo.tile import TILE_SIZE, Tile
from mapfetch.process.mosaic import compose_geotiff, plan_mosaic


class _FakeXYZ:
    """高德/天地图口径：tile(x,y) 四至 = (x, -(y+1), x+1, -y)，y 向南递增。"""

    y_axis_down = True

    def tile_bounds_wgs84(self, t: Tile):
        return float(t.x), float(-(t.y + 1)), float(t.x + 1), float(-t.y)


class _FakeBaidu:
    """百度口径：tile(x,y) 四至 = (x, y, x+1, y+1)，y 向北递增。"""

    y_axis_down = False

    def tile_bounds_wgs84(self, t: Tile):
        return float(t.x), float(t.y), float(t.x + 1), float(t.y + 1)


def _tiles(z: int, x0: int, x1: int, y0: int, y1: int) -> list[Tile]:
    return [Tile(z, x, y)
            for x in range(x0, x1 + 1)
            for y in range(y0, y1 + 1)]


class TestPlanMosaicYAxis:
    def test_baidu_north_south_correct(self):
        """百度：y0 是最南行，north 必须取自 (x0, y1)。"""
        tiles = _tiles(17, 214800, 214802, 107500, 107503)
        plan = plan_mosaic(_FakeBaidu(), tiles)
        assert (plan.west, plan.south) == (214800.0, 107500.0)
        assert (plan.east, plan.north) == (214803.0, 107504.0)
        assert plan.north > plan.south          # 修复前 north=11 < south=12
        assert plan.res_y > 0                   # 修复前为负/为 0
        assert plan.y_down is False

    def test_xyz_unchanged(self):
        """高德/天地图（XYZ）行为不变：y0 是最北行。"""
        tiles = _tiles(15, 26450, 26452, 12400, 12403)
        plan = plan_mosaic(_FakeXYZ(), tiles)
        assert (plan.west, plan.south) == (26450.0, -12404.0)
        assert (plan.east, plan.north) == (26453.0, -12400.0)
        assert plan.north > plan.south
        assert plan.y_down is True


class TestComposeRows:
    @pytest.fixture
    def tile_pngs(self, tmp_path):
        """每行瓦片填充唯一灰度（10*y），便于校验摆放行序。"""
        cache = tmp_path / "cache"
        cache.mkdir()
        paths = {}
        for y in (10, 11, 12):
            p = cache / f"tile_{y}.png"
            Image.new("RGB", (TILE_SIZE, TILE_SIZE), (10 * y % 255,) * 3).save(p)
            paths[y] = p
        return paths

    def test_baidu_rows_north_up(self, tmp_path, tile_pngs):
        """百度 3 行瓦片：最北行（y=12）必须在图像第 0 行（顶部）。"""
        tiles = _tiles(17, 100, 100, 10, 12)  # 单列 3 行
        plan = plan_mosaic(_FakeBaidu(), tiles)
        out = tmp_path / "out.tif"

        def path_fn(t: Tile):
            return tile_pngs[t.y]

        compose_geotiff(plan, path_fn, out, crs_epsg=4326)

        import rasterio
        with rasterio.open(out) as src:
            assert src.transform.e < 0            # 北上栅格：行号向下纬度递减
            assert src.transform.f == plan.north  # 顶行纬度 = 最北瓦片上缘
            arr = src.read(1)
        assert arr[4, 4] == 10 * 12 % 255    # 顶部 = 最北行 y1
        assert arr[TILE_SIZE + 4, 4] == 10 * 11 % 255
        assert arr[2 * TILE_SIZE + 4, 4] == 10 * 10 % 255  # 底部 = 最南行 y0

    def test_xyz_rows_unchanged(self, tmp_path, tile_pngs):
        """XYZ 3 行：最北行（y0）仍在图像顶部（原有行为不回归）。"""
        tiles = _tiles(17, 100, 100, 10, 12)
        plan = plan_mosaic(_FakeXYZ(), tiles)
        out = tmp_path / "out.tif"

        def path_fn(t: Tile):
            return tile_pngs[t.y]

        compose_geotiff(plan, path_fn, out, crs_epsg=4326)

        import rasterio
        with rasterio.open(out) as src:
            arr = src.read(1)
        assert arr[4, 4] == 10 * 10 % 255    # 顶部 = 最北行 y0
        assert arr[2 * TILE_SIZE + 4, 4] == 10 * 12 % 255
