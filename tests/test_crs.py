"""坐标转换自检（往返误差 < 2 m）。

已知控制点：东方明珠 WGS84 (121.49970, 31.23970)。
"""

import math

import pytest

from mapfetch.core.geo import crs

# 控制点（WGS84）
ORIENTAL_PEARL = (121.49970, 31.23970)
TIANANMEN = (116.39750, 39.90870)


def _dist_m(lon1, lat1, lon2, lat2):
    """等距近似距离（米）。"""
    dx = (lon1 - lon2) * 111320 * math.cos(math.radians((lat1 + lat2) / 2))
    dy = (lat1 - lat2) * 111320
    return math.hypot(dx, dy)


class TestGcj02:
    def test_offset_applied_in_china(self):
        g_lon, g_lat = crs.wgs84_to_gcj02(*ORIENTAL_PEARL)
        d = _dist_m(*ORIENTAL_PEARL, g_lon, g_lat)
        assert 100 < d < 800  # 偏移量 50–600m 量级

    def test_roundtrip_under_2m(self):
        for pt in (ORIENTAL_PEARL, TIANANMEN):
            g = crs.wgs84_to_gcj02(*pt)
            w = crs.gcj02_to_wgs84(*g)
            assert _dist_m(*pt, *w) < 2.0

    def test_out_of_china_no_offset(self):
        assert crs.wgs84_to_gcj02(139.75, 35.68) == (139.75, 35.68)  # 东京
        assert crs.out_of_china(139.75, 35.68) is True

    def test_lat_limit_rejected(self):
        with pytest.raises(ValueError):
            crs.wgs84_to_gcj02(120.0, 86.0)


class TestBd09:
    def test_gcj02_bd09_roundtrip(self):
        g = crs.wgs84_to_gcj02(*ORIENTAL_PEARL)
        b = crs.gcj02_to_bd09ll(*g)
        g2 = crs.bd09ll_to_gcj02(*b)
        assert _dist_m(*g, *g2) < 2.0

    def test_bd09mc_roundtrip(self):
        mc = crs.bd09ll_to_bd09mc(116.40, 39.90)
        assert mc[0] > 0 and mc[1] > 0  # 东半球北半球为正
        ll = crs.bd09mc_to_bd09ll(*mc)
        assert abs(ll[0] - 116.40) < 3e-5 and abs(ll[1] - 39.90) < 3e-5

    def test_bd09mc_invalid_raises(self):
        with pytest.raises(ValueError):
            crs.bd09ll_to_bd09mc(999.0, 0.0)


class TestUnifiedTransform:
    def test_alias(self):
        assert crs.normalize_crs("EPSG:4326") == "WGS84"
        assert crs.normalize_crs("gcj-02") == "GCJ02"
        assert crs.normalize_crs("EPSG:4490") == "CGCS2000"

    def test_wgs84_to_bd09_chain(self):
        b = crs.transform(*ORIENTAL_PEARL, "WGS84", "BD09LL")
        b2 = crs.wgs84_to_bd09ll(*ORIENTAL_PEARL)
        assert b == b2

    def test_cgcs2000_passthrough(self):
        assert crs.transform(120.0, 36.0, "WGS84", "CGCS2000") == (120.0, 36.0)

    def test_same_crs_identity(self):
        assert crs.transform(120.0, 36.0, "WGS84", "WGS84") == (120.0, 36.0)
