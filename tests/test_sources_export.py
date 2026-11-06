"""数据源适配器与导出模块测试（本地逻辑，不发网络请求）。

URL 拼装、瓦片范围换算、PGW/MBTiles/GeoJSON 导出。
"""

import json
import sqlite3

import pytest

from mapfetch.core.aoi import AOI
from mapfetch.core.geo.tile import Tile
from mapfetch.sources import create_adapter


@pytest.fixture()
def cfg(hermetic_cfg):
    from mapfetch.config import load_config
    return load_config(hermetic_cfg)


class TestAdapters:
    def test_tianditu_tile_range_matches_wgs84(self, cfg):
        aoi = AOI.from_rect(120.30, 36.05, 120.32, 36.07)
        ad = create_adapter("tianditu", cfg.sources["tianditu"])
        x0, y0, x1, y1 = ad.tile_range(aoi, 15)
        from mapfetch.core.geo import tile as tilemod
        assert (x0, y0, x1, y1) == tilemod.tile_range_for_bbox(*aoi.bbox, 15)

    def test_tianditu_no_reprojection(self, cfg):
        ad = create_adapter("tianditu", cfg.sources["tianditu"])
        assert ad.needs_reprojection("CGCS2000") is False
        assert ad.needs_reprojection("EPSG:4326") is False

    def test_amap_tile_range_offset(self, cfg):
        # 高德瓦片范围应相对 WGS84 结果有偏移（GCJ-02）
        aoi = AOI.from_rect(120.30, 36.05, 120.32, 36.07)
        ad = create_adapter("amap", cfg.sources["amap"])
        r_amap = ad.tile_range(aoi, 15)
        r_wgs = AOI.from_rect(120.30, 36.05, 120.32, 36.07).tile_range(15)
        # 范围尺寸相近（同区域），但坐标编号可能不同
        assert abs((r_amap[2] - r_amap[0]) - (r_wgs[2] - r_wgs[0])) <= 1

    def test_baidu_tile_negative_possible(self, cfg):
        ad = create_adapter("baidu", cfg.sources["baidu"])
        t = ad.tile_range(AOI.from_rect(-0.01, -0.01, 0.01, 0.01), 15)
        assert t[0] <= t[2] and t[1] <= t[3]

    def test_url_building(self, cfg, monkeypatch):
        monkeypatch.setenv("MAPFETCH_TK_TIANDITU", "testkey123")
        ad = create_adapter("tianditu", cfg.sources["tianditu"])
        url = ad.tile_url("img_w", Tile(15, 27000, 13000))
        assert "T=img_w" in url and "x=27000" in url and "y=13000" and "l=15" in url
        assert "tk=testkey123" in url

    def test_missing_key_error(self, cfg, monkeypatch):
        monkeypatch.delenv("MAPFETCH_TK_TIANDITU", raising=False)
        ad = create_adapter("tianditu", cfg.sources["tianditu"])
        with pytest.raises(RuntimeError, match="MAPFETCH_TK_TIANDITU"):
            ad.tile_url("img_w", Tile(15, 1, 1))


class TestExport:
    def test_worldfile(self, tmp_path):
        from mapfetch.export.worldfile import write_worldfile
        png = tmp_path / "a.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n")
        pgw, aux = write_worldfile(png, west=120.0, north=36.0,
                                   res_x=1e-5, res_y=1e-5, crs="CGCS2000")
        lines = pgw.read_text().strip().split("\n")
        assert len(lines) == 6
        assert float(lines[0]) == pytest.approx(1e-5)
        assert float(lines[3]) == pytest.approx(-1e-5)
        # 像素中心 = 边缘 + 半像素
        assert float(lines[4]) == pytest.approx(120.0 + 5e-6)
        assert "China Geodetic Coordinate System 2000" in aux.read_text(encoding="utf-8")

    def test_worldfile_gcj02_rejected(self, tmp_path):
        from mapfetch.export.worldfile import write_worldfile
        with pytest.raises(ValueError):
            write_worldfile(tmp_path / "a.png", west=0, north=0,
                            res_x=1, res_y=1, crs="GCJ02")

    def test_mbtiles(self, tmp_path):
        from mapfetch.export.mbtiles import write_mbtiles
        out = write_mbtiles(
            tmp_path / "t.mbtiles", name="test", description="d",
            tile_rows=[(Tile(15, 100, 200), b"\x89PNG" + b"0" * 100)],
            bounds=(120.0, 36.0, 120.1, 36.1), center=(120.05, 36.05, 15),
            attribution="天地图", crs_note="CGCS2000")
        conn = sqlite3.connect(str(out))
        row = conn.execute(
            "SELECT zoom_level, tile_column, tile_row FROM tiles").fetchone()
        assert row == (15, 100, 2**15 - 1 - 200)  # TMS y 翻转
        meta = dict(conn.execute("SELECT name, value FROM metadata").fetchall())
        assert meta["crs_note"] == "CGCS2000"
        conn.close()

    def test_geojson_export(self, tmp_path):
        from mapfetch.export.vector import write_geojson
        feats = [{"geometry": {"type": "Point", "coordinates": [120.0, 36.0]},
                  "area_m2": 12.5}]
        out = write_geojson(feats, tmp_path / "f.geojson", layer_name="test")
        data = json.loads(out.read_text(encoding="utf-8"))
        assert data["features"][0]["properties"]["crs"] == "CGCS2000"
        assert data["features"][0]["properties"]["area_m2"] == 12.5
