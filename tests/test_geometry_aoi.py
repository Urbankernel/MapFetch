"""几何工具与 AOI 模型测试。"""

import json

import pytest

from mapfetch.core.aoi import AOI, AOISource
from mapfetch.core.geo import geometry as gm


class TestGeometry:
    def test_bbox_area(self):
        # 1°×1° @ 赤道 ≈ 111.32 × 111.32 ≈ 12392 km²
        a = gm.bbox_area_km2((0, 0, 1, 1))
        assert abs(a - 12392) < 100

    def test_ring_simple(self):
        square = [(0, 0), (1, 0), (1, 1), (0, 1)]
        bowtie = [(0, 0), (1, 1), (0, 1), (1, 0)]
        assert gm.ring_is_simple(square) is True
        assert gm.ring_is_simple(bowtie) is False

    def test_antimeridian(self):
        assert gm.crosses_antimeridian((170, 30, -170, 40)) is True
        assert gm.crosses_antimeridian((120, 30, 121, 40)) is False


class TestAOI:
    def test_rect(self):
        aoi = AOI.from_rect(120.30, 36.05, 120.42, 36.12)
        assert aoi.source == AOISource.RECT
        assert aoi.bbox == (120.30, 36.05, 120.42, 36.12)
        est = aoi.estimate_tiles([15])
        assert est[15] > 0

    def test_rect_invalid(self):
        with pytest.raises(ValueError):
            AOI.from_rect(121.0, 36.0, 120.0, 37.0)

    def test_polygon_self_intersect_rejected(self):
        with pytest.raises(ValueError):
            AOI.from_polygon([(0, 0), (1, 1), (0, 1), (1, 0)])

    def test_polygon_ok(self):
        aoi = AOI.from_polygon([(120.3, 36.0), (120.5, 36.0), (120.4, 36.2)])
        assert aoi.area_km2 > 0

    def test_amap_polyline_parse(self):
        polyline = "120.0,36.0;120.1,36.0;120.1,36.1;120.0,36.1|120.5,36.5;120.6,36.5;120.55,36.6"
        aoi = AOI.from_amap_district_polyline(polyline, adcode="370200", name="测试")
        assert len(aoi.rings) == 2
        assert aoi.meta["adcode"] == "370200"
        gj = aoi.to_geojson()
        assert gj["geometry"]["type"] == "MultiPolygon"

    def test_geojson_roundtrip(self, tmp_path):
        aoi = AOI.from_rect(120.0, 36.0, 120.1, 36.1)
        p = tmp_path / "aoi.geojson"
        p.write_text(json.dumps(aoi.to_geojson(), ensure_ascii=False), encoding="utf-8")
        aoi2 = AOI.from_geojson(p)
        assert aoi2.bbox == aoi.bbox

    def test_center_radius(self):
        aoi = AOI.from_center_radius(120.38, 36.07, 1000.0)
        min_lon, min_lat, max_lon, max_lat = aoi.bbox
        assert abs((max_lat - min_lat) * 111320 - 2000) < 50

    def test_from_file_gpkg(self, tmp_path):
        import geopandas as gpd
        from shapely.geometry import Polygon

        gdf = gpd.GeoDataFrame(
            {"name": ["a"]},
            geometry=[Polygon([(120.0, 36.0), (120.1, 36.0), (120.1, 36.1), (120.0, 36.1)])],
            crs="EPSG:4326",
        )
        p = tmp_path / "aoi.gpkg"
        gdf.to_file(p, driver="GPKG")
        aoi = AOI.from_file(p)
        assert aoi.source == AOISource.FILE
        min_lon, min_lat, max_lon, max_lat = aoi.bbox
        assert (min_lon, min_lat) == pytest.approx((120.0, 36.0))
        assert (max_lon, max_lat) == pytest.approx((120.1, 36.1))

    def test_from_file_shp(self, tmp_path):
        import geopandas as gpd
        from shapely.geometry import Polygon

        gdf = gpd.GeoDataFrame(
            geometry=[Polygon([(120.0, 36.0), (120.1, 36.0), (120.1, 36.1), (120.0, 36.1)])],
            crs="EPSG:4326",
        )
        p = tmp_path / "aoi.shp"
        gdf.to_file(p, driver="ESRI Shapefile")
        aoi = AOI.from_file(p)
        assert aoi.bbox == (120.0, 36.0, 120.1, 36.1)

    def test_from_file_crs_transform(self, tmp_path):
        import geopandas as gpd
        from pyproj import Transformer
        from shapely.geometry import Polygon

        tr = Transformer.from_crs(4326, 3857, always_xy=True)
        c00 = tr.transform(120.0, 36.0)
        c10 = tr.transform(120.1, 36.0)
        c11 = tr.transform(120.1, 36.1)
        c01 = tr.transform(120.0, 36.1)
        gdf = gpd.GeoDataFrame(
            geometry=[Polygon([c00, c10, c11, c01])],
            crs="EPSG:3857",
        )
        p = tmp_path / "aoi_merc.gpkg"
        gdf.to_file(p, driver="GPKG")
        aoi = AOI.from_file(p)
        min_lon, min_lat, max_lon, max_lat = aoi.bbox
        assert (min_lon, min_lat) == pytest.approx((120.0, 36.0), abs=1e-6)
        assert (max_lon, max_lat) == pytest.approx((120.1, 36.1), abs=1e-6)

    def test_from_file_no_crs(self, tmp_path):
        import geopandas as gpd
        from shapely.geometry import Polygon

        gdf = gpd.GeoDataFrame(
            geometry=[Polygon([(120.0, 36.0), (120.1, 36.0), (120.1, 36.1), (120.0, 36.1)])],
            crs=None,
        )
        p = tmp_path / "aoi_nocrs.shp"
        gdf.to_file(p, driver="ESRI Shapefile")
        with pytest.raises(ValueError):
            AOI.from_file(p)

    def test_from_file_unsupported(self, tmp_path):
        p = tmp_path / "aoi.txt"
        p.write_text("not a spatial file", encoding="utf-8")
        with pytest.raises(ValueError):
            AOI.from_file(p)
