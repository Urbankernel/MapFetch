"""纯逻辑测试（无网络、无重依赖）。

- ScanPlanner：网格规划、蛇形顺序、窗口四至一致性
- PGW 解析：像素中心修正
- 层数换算：floor_to_height_range
- 自检：地标瓦片四至包含性、百度层级一致性
"""

import pytest

from mapfetch.core.aoi import AOI
from mapfetch.fetch.render import (RenderParams, ScanPlanner,
                                   floor_to_height_range)
from mapfetch.process.pipeline import parse_pgw, transform_from_pgw
from mapfetch.sources import create_adapter
from mapfetch.sources.selfcheck import (LANDMARKS,
                                        baidu_level_consistency_check,
                                        run_offline_selfcheck)


@pytest.fixture()
def cfg():
    from mapfetch.config import load_config
    return load_config()


class TestScanPlanner:
    BOUNDS = (120.30, 36.05, 120.42, 36.12)  # 约 10km × 8km @ z17

    def test_grid_dimensions(self):
        p = ScanPlanner(self.BOUNDS, 17, 2000)
        assert p.cols >= 1 and p.rows >= 1
        assert p.total == p.cols * p.rows

    def test_snake_order(self):
        p = ScanPlanner(self.BOUNDS, 17, 2000)
        rows = {}
        for w in p.iter_windows():
            rows.setdefault(w.row, []).append(w.col)
        for row, cols in rows.items():
            if row % 2 == 0:
                assert cols == sorted(cols)          # 偶数行正向
            else:
                assert cols == sorted(cols, reverse=True)  # 奇数行蛇形反向

    def test_window_bounds_cover_aoi(self):
        p = ScanPlanner(self.BOUNDS, 17, 2000)
        w0 = p.window(0, 0)
        b0 = p.window_bounds_source(w0)
        wl = p.window(p.cols - 1, p.rows - 1)
        bl = p.window_bounds_source(wl)
        assert b0[0] <= self.BOUNDS[0] + 1e-6
        assert b0[3] >= self.BOUNDS[3] - 1e-6 or bl[3] >= self.BOUNDS[3] - 1e-3
        assert bl[2] >= self.BOUNDS[2] - 1e-6

    def test_adjacent_windows_seamless(self):
        """相邻窗口像素四至严丝合缝（无重叠无缝隙）。"""
        p = ScanPlanner(self.BOUNDS, 17, 2000)
        w1 = p.window(0, 0)
        w2 = p.window(1, 0)
        assert w1.px1 == w2.px0

    def test_floor_scan_window(self):
        """小窗口的像素总跨度与大窗口一致（双重 ceil 取整下网格数不严格成 4 倍）。"""
        p = ScanPlanner(self.BOUNDS, 17, 500)
        p2 = ScanPlanner(self.BOUNDS, 17, 2000)
        span_small = (p.cols * 500, p.rows * 500)
        span_large = (p2.cols * 2000, p2.rows * 2000)
        # 覆盖同一范围：跨度差应小于一个大窗口
        assert abs(span_small[0] - span_large[0]) < 2000
        assert abs(span_small[1] - span_large[1]) < 2000


class TestRenderParams:
    def test_floor_divisibility(self):
        with pytest.raises(ValueError):
            RenderParams(floor_min=0, floor_max=36, floor_step=5).validate()
        RenderParams(floor_min=0, floor_max=36, floor_step=3).validate()

    def test_zoom_range(self):
        with pytest.raises(ValueError):
            RenderParams(zoom=13).validate()
        with pytest.raises(ValueError):
            RenderParams(zoom=21).validate()

    def test_floor_to_height(self):
        # ：值 0 -> 0-9m；值 3 -> 9-18m；值 6 -> 18-27m
        assert floor_to_height_range(0) == (0.0, 9.0)
        assert floor_to_height_range(3) == (9.0, 18.0)
        assert floor_to_height_range(6) == (18.0, 27.0)


class TestPgwParse:
    def test_parse_and_affine(self, tmp_path):
        pgw = tmp_path / "a.pgw"
        pgw.write_text("1e-05\n0\n-0\n-1e-05\n120.000005\n35.999995\n",
                       encoding="ascii")
        vals = parse_pgw(pgw)
        assert vals[0] == pytest.approx(1e-5)
        t = transform_from_pgw(pgw)
        # 像素中心 120.000005 减半个像素 -> 边缘 120.0
        assert t.c == pytest.approx(120.0, abs=1e-9)
        assert t.f == pytest.approx(36.0, abs=1e-9)

    def test_bad_pgw(self, tmp_path):
        pgw = tmp_path / "bad.pgw"
        pgw.write_text("1\n2\n3\n", encoding="ascii")
        with pytest.raises(ValueError):
            parse_pgw(pgw)


class TestSelfcheck:
    def test_landmarks_valid(self):
        assert len(LANDMARKS) >= 3
        for lm in LANDMARKS:
            assert 73 < lm.lon < 136 and 3 < lm.lat < 54  # 中国大陆范围

    def test_offline_selfcheck_all_pass(self, cfg):
        adapters = {
            name: create_adapter(name, cfg.sources[name])
            for name in ("tianditu", "amap", "baidu")
        }
        results = run_offline_selfcheck(adapters, z=17)
        assert len(results) == 3 * len(LANDMARKS)
        failed = [r for r in results if not r["ok"]]
        assert not failed, f"自检失败: {failed}"

    def test_baidu_level_consistency(self):
        out = baidu_level_consistency_check(17)
        assert out["ok"], f"百度层级一致性失败: {out['details']}"


class TestBackgroundBinarize:
    """绿地/水系通道主导二值化（兼容默认样式与自定义样式配色）。"""

    @staticmethod
    def _make_png(tmp_path, colors):
        from PIL import Image
        import numpy as np
        arr = np.zeros((len(colors), len(colors), 3), dtype="uint8")
        for i, c in enumerate(colors):
            arr[i, :] = c
        p = tmp_path / "bg.png"
        Image.fromarray(arr, "RGB").save(p)
        return p

    def test_custom_style_bright_green(self, tmp_path):
        # 自定义样式绿地 #0efb80 = RGB(14,251,128)，旧固定色域提取不到（bin 全黑）
        from mapfetch.process.classify import binarize_background_layer
        p = self._make_png(tmp_path, [(14, 251, 128), (64, 64, 64)])
        out = binarize_background_layer(p, tmp_path / "bin.png", layer="green")
        from PIL import Image
        import numpy as np
        a = np.asarray(Image.open(out))
        assert a[0, 0, 3] == 255 and a[0, 0, 0] == 255   # 绿地 → 白色不透明
        assert a[1, 0, 3] == 0                            # 灰底 → 透明

    def test_default_style_green_and_water(self, tmp_path):
        from mapfetch.process.classify import binarize_background_layer
        # 默认样式绿(170,211,114) / 默认样式水系(150,200,255) / 灰底
        p = self._make_png(tmp_path, [(170, 211, 114), (150, 200, 255), (64, 64, 64)])
        g = binarize_background_layer(p, tmp_path / "g.png", layer="green")
        w = binarize_background_layer(p, tmp_path / "w.png", layer="water")
        from PIL import Image
        import numpy as np
        ga = np.asarray(Image.open(g))[:, 0, 3]
        wa = np.asarray(Image.open(w))[:, 0, 3]
        assert list(ga) == [255, 0, 0]    # 仅绿地
        assert list(wa) == [0, 255, 0]    # 仅水系

    def test_custom_style_cyan_water(self, tmp_path):
        # 自定义样式水系 #00f5ff = RGB(0,245,255)：青色 b 仅比 g 大 10，
        # 旧判定 b>=g+15 漏提（bin 全黑）；新判定按 g vs b 相对大小划界
        from mapfetch.process.classify import binarize_background_layer
        p = self._make_png(tmp_path, [(0, 245, 255), (45, 45, 45)])
        w = binarize_background_layer(p, tmp_path / "w.png", layer="water")
        from PIL import Image
        import numpy as np
        wa = np.asarray(Image.open(w))
        assert wa[0, 0, 3] == 255 and wa[0, 0, 0] == 255   # 水系 → 白色不透明
        assert wa[1, 0, 3] == 0                             # 灰底 → 透明

    def test_cyan_goes_to_water_bright_green_to_green(self, tmp_path):
        # 划界验证：b==g 的纯青归水系；g>b 的亮绿归绿地；两者互不误判
        from mapfetch.process.classify import binarize_background_layer
        p = self._make_png(tmp_path, [(0, 200, 200), (14, 251, 128)])
        from PIL import Image
        import numpy as np
        w = binarize_background_layer(p, tmp_path / "w.png", layer="water")
        g = binarize_background_layer(p, tmp_path / "g.png", layer="green")
        assert list(np.asarray(Image.open(w))[:, 0, 3]) == [255, 0]
        assert list(np.asarray(Image.open(g))[:, 0, 3]) == [0, 255]


class TestBuildingBinarize:
    """blue 样式建筑二值化（建筑比背景暗，反向阈值 + alpha 过滤）。"""

    def test_building_darker_than_background(self, tmp_path):
        # 虹口 z17 实测四峰配色：建筑(0,47,64)暗 / 背景(3,52,71) /
        # 网格(86,118,131)亮 / 透明像素——只有暗色建筑被选中
        from mapfetch.process.classify import binarize_building
        import numpy as np
        from PIL import Image
        colors = [(0, 47, 64, 255), (3, 52, 71, 255),
                  (86, 118, 131, 255), (0, 0, 0, 0)]
        arr = np.zeros((4, 4, 4), dtype="uint8")
        for i, c in enumerate(colors):
            arr[i, :] = c
        p = tmp_path / "bld.png"
        Image.fromarray(arr, "RGBA").save(p)
        out = binarize_building(p, tmp_path / "bin.png")
        a = np.asarray(Image.open(out))[:, :, 3]
        assert list(a[:, 0]) == [255, 0, 0, 0]  # 仅建筑行


class TestFloorsMosaic:
    """3D 遮挡法层数镶嵌（最后可见层 = 建筑高度档位）。"""

    @staticmethod
    def _shot(arr):
        import io
        from PIL import Image
        buf = io.BytesIO()
        Image.fromarray(arr, "L").save(buf, format="PNG")
        return buf.getvalue()

    def test_last_visible_floor(self, tmp_path):
        # 模拟 3 档（0/3/6）：背景灰 122、建筑浅色 255、探针遮挡 = 纯黑 0
        # 建筑 A（2:4,2:4）：0、3 层可见，6 层被盖 → 高度档 = 3
        # 建筑 B（5:6,5:7）：仅 0 层可见，3 层即被盖 → 高度档 = 0
        import numpy as np
        from mapfetch.fetch.render import FloorScanJob
        base = np.full((8, 8), 122, dtype="uint8")
        base[2:4, 2:4] = 255
        base[5:6, 5:7] = 255
        shots = {}
        for f in (0, 3, 6):
            a = base.copy()
            if f >= 6:
                a[2:4, 2:4] = 0   # 探针盖住建筑 A（6 层起）
            if f >= 3:
                a[5:6, 5:7] = 0   # 探针盖住建筑 B（3 层起）
            shots[f] = self._shot(a)
        h = np.asarray(FloorScanJob._mosaic_floors(shots, 8))
        assert h[2, 2] == 3     # A：最后可见层 3
        assert h[5, 5] == 0     # B：最后可见层 0
        assert h[0, 0] == 0     # 背景：掩膜外恒 0

    def test_mask_excludes_background(self, tmp_path):
        # 背景灰 122 即使始终可见也不会被误标为高层
        import numpy as np
        from mapfetch.fetch.render import FloorScanJob
        shots = {f: self._shot(np.full((4, 4), 122, dtype="uint8"))
                 for f in (0, 3, 6, 9)}
        h = np.asarray(FloorScanJob._mosaic_floors(shots, 4))
        assert h.max() == 0     # 无建筑 → 全 0


class TestTemplateLogoHidden:
    """所有模板须用 CSS 隐藏高德 logo/版权 DOM 覆盖层（防截图污染）。"""

    def test_all_templates_hide_logo(self):
        from mapfetch.fetch.render import _TEMPLATES, render_template
        for name in _TEMPLATES:
            assert ".amap-logo" in _TEMPLATES[name], f"模板 {name} 缺少 logo 隐藏 CSS"
            assert ".amap-copyright" in _TEMPLATES[name], f"模板 {name} 缺少版权隐藏 CSS"

    def test_rendered_html_keeps_rule(self):
        from mapfetch.fetch.render import render_template
        for name in ("rd", "bld", "bld3d", "green"):
            html = render_template(name, key="k", center_lon=121.4,
                                   center_lat=31.2, zoom=17)
            assert "display:none" in html and "amap-logo" in html

class TestPolygonizeValues:
    """floors 矢量化：按像素值分区域，保留每栋楼的层数档位。"""

    def test_values_preserved(self, tmp_path):
        import numpy as np
        from mapfetch.process.vectorize import polygonize_values
        from affine import Affine
        # 背景 0；建筑 A 值 6、建筑 B 值 12
        arr = np.zeros((20, 20), dtype="uint8")
        arr[2:8, 2:8] = 6
        arr[10:16, 12:18] = 12
        t = Affine(0.0001, 0, 121.0, 0, -0.0001, 31.0)
        feats = polygonize_values(arr, t, min_area_m2=0.0, lat_deg=31.0)
        vals = sorted(f["value"] for f in feats)
        assert vals == [6, 12], f"应保留每个面的层数档位，实际 {vals}"

    def test_zero_excluded(self):
        import numpy as np
        from mapfetch.process.vectorize import polygonize_values
        from affine import Affine
        arr = np.zeros((8, 8), dtype="uint8")  # 全 0 → 无要素
        t = Affine(0.0001, 0, 121.0, 0, -0.0001, 31.0)
        assert polygonize_values(arr, t) == []

class TestVectorizeFloorsStep:
    """floors 矢量化高度换算须使用与扫描一致的 floor_step（默认 3）。"""

    @staticmethod
    def _make_floors_png(tmp_path):
        import numpy as np
        from PIL import Image
        from mapfetch.export.worldfile import write_worldfile
        arr = np.zeros((8, 8), dtype="uint8")
        arr[2:4, 2:4] = 1   # 档位 1
        arr[5:6, 5:7] = 2   # 档位 2
        p = tmp_path / "floors.png"
        Image.fromarray(arr, "L").save(p)
        write_worldfile(p, west=121.0, north=31.0, res_x=0.0001, res_y=0.0001)
        return p

    def test_step_1(self, tmp_path):
        from mapfetch.process.pipeline import vectorize_raster
        p = self._make_floors_png(tmp_path)
        out = tmp_path / "v.geojson"
        vectorize_raster(p, "floors", out, min_area_m2=0.0, floor_step=1)
        props = {f["properties"]["floor"]: f["properties"] for f in
                 __import__("json").loads(out.read_text(encoding="utf-8"))["features"]}
        assert props[1]["height_min_m"] == 3
        assert props[1]["height_max_m"] == 6    # (1+1)*3，而非默认 step 的 12
        assert props[2]["height_max_m"] == 9    # (2+1)*3

    def test_default_step_3(self, tmp_path):
        from mapfetch.process.pipeline import vectorize_raster
        p = self._make_floors_png(tmp_path)
        out = tmp_path / "v.geojson"
        vectorize_raster(p, "floors", out, min_area_m2=0.0)  # 缺省 step=3
        props = {f["properties"]["floor"]: f["properties"] for f in
                 __import__("json").loads(out.read_text(encoding="utf-8"))["features"]}
        assert props[1]["height_max_m"] == 12   # (1+3)*3


class TestRoadStraighten:
    """道路中心线抽稀：消除逐像素连线带来的台阶锯齿。"""

    @staticmethod
    def _skel(pixels, rows, cols):
        import numpy as np
        s = np.zeros((rows, cols), dtype=bool)
        for r, c in pixels:
            s[r, c] = True
        return s

    def test_staircase_collapses(self):
        from affine import Affine
        from mapfetch.process.vectorize import skeleton_to_lines
        # 斜率 1/2 的台阶（下→右下→下→右下…）：1 像素宽、8 连通、内部度恒为 2
        pixels = [(0, 0), (1, 0), (2, 1), (3, 1), (4, 2),
                  (5, 2), (6, 3), (7, 3), (8, 4)]
        t = Affine(1.0, 0, 0.0, 0, -1.0, 9.0)
        raw = skeleton_to_lines(self._skel(pixels, 9, 5), t, simplify_px=0.0)
        sim = skeleton_to_lines(self._skel(pixels, 9, 5), t, simplify_px=1.0)
        assert len(raw) == 1 and len(sim) == 1
        n_raw = len(raw[0]["geometry"]["coordinates"])
        n_sim = len(sim[0]["geometry"]["coordinates"])
        assert n_raw >= len(pixels)   # 未抽稀：每像素一顶点
        assert n_sim < n_raw          # 抽稀后顶点显著减少
        assert n_sim <= 5             # 台阶被拉成近似直线

    def test_collinear_horizontal_collapses(self):
        from affine import Affine
        from mapfetch.process.vectorize import skeleton_to_lines
        # 纯水平直线：中间像素全部共线（夹角 180°），抽稀后坍缩为两端点
        pixels = [(5, c) for c in range(10)]
        t = Affine(1.0, 0, 0.0, 0, -1.0, 8.0)
        lines = skeleton_to_lines(self._skel(pixels, 8, 10), t, simplify_px=0.5)
        assert len(lines) == 1
        assert len(lines[0]["geometry"]["coordinates"]) == 2

    def test_road_vectorize_end_to_end(self, tmp_path):
        import json
        import numpy as np
        from PIL import Image
        from mapfetch.export.worldfile import write_worldfile
        from mapfetch.process.pipeline import vectorize_raster
        # 主对角线道路带（RGBA：白=道路、透明=背景），走 alpha 掩膜分支
        arr = np.zeros((32, 32, 4), dtype="uint8")
        for i in range(3, 29):
            for j in range(3, 29):
                if abs(j - i) <= 1:
                    arr[i, j] = (255, 255, 255, 255)
        p = tmp_path / "road.png"
        Image.fromarray(arr, "RGBA").save(p)
        write_worldfile(p, west=121.0, north=31.0, res_x=1e-5, res_y=1e-5)
        out = tmp_path / "road.geojson"
        vectorize_raster(p, "road", out)   # 默认 simplify_tolerance=0.7
        feats = json.loads(out.read_text(encoding="utf-8"))["features"]
        assert feats, "应至少产出一条道路线"
        max_pts = max(len(f["geometry"]["coordinates"]) for f in feats)
        assert max_pts <= 8, f"道路线应被抽稀成近似直线，实际最大顶点数 {max_pts}"
