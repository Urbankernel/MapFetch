"""启动自检与已知地标用例。

设计：
- LANDMARKS：已知地标的 WGS84 坐标（用于反算瓦片索引并与实测瓦片比对）
- 纯计算部分（expected_tile）可离线单测
- 任何层级映射/URL 模板变更必须先过本自检（CONTRIBUTING 硬性约定 2）
"""

from __future__ import annotations

from dataclasses import dataclass

from ..core.geo import crs, tile as tilemod
from ..core.geo.tile import Tile


@dataclass(frozen=True)
class Landmark:
    name: str
    lon: float  # WGS84
    lat: float


# 已知控制点（3–5 个地标；坐标为公开地标位置的 WGS84 参考值）
LANDMARKS = [
    Landmark("天安门", 116.39750, 39.90870),
    Landmark("东方明珠", 121.49970, 31.23970),
    Landmark("广州塔", 113.32450, 23.10550),
    Landmark("青岛五四广场", 120.39600, 36.06400),
]


def expected_tile_standard(lon: float, lat: float, z: int,
                           source_crs: str) -> Tile:
    """标准 XYZ 源的期望瓦片（WGS84 → 源坐标系 → 瓦片索引）。

    用于天地图（CGCS2000≈WGS84）与高德（GCJ-02）的离线自检。
    """
    s_lon, s_lat = crs.transform(lon, lat, "WGS84", source_crs)
    return tilemod.lonlat_to_tile(s_lon, s_lat, z)


def expected_tile_baidu(lon: float, lat: float, z: int) -> Tile:
    """百度源的期望瓦片（WGS84 → BD-09LL → BD-09MC → 瓦片索引）。"""
    ll = crs.wgs84_to_bd09ll(lon, lat)
    mc = crs.bd09ll_to_bd09mc(*ll)
    return tilemod.bd09mc_to_tile(mc[0], mc[1], z)


def tile_contains_point(adapter, t: Tile, lon: float, lat: float) -> bool:
    """校验瓦片的 WGS84 四至是否包含控制点（离线自检的核心判定）。"""
    min_lon, min_lat, max_lon, max_lat = adapter.tile_bounds_wgs84(t)
    return (min_lon - 1e-9 <= lon <= max_lon + 1e-9
            and min_lat - 1e-9 <= lat <= max_lat + 1e-9)


def run_offline_selfcheck(adapters: dict, z: int = 17) -> list[dict]:
    """离线自检：对每个适配器、每个地标，验证瓦片索引与四至一致。

    - adapters: {name: SourceAdapter}
    - 返回 [{source, landmark, tile, contains, ok}, ...]，ok=False 即失败
    """
    results: list[dict] = []
    for name, adapter in adapters.items():
        for lm in LANDMARKS:
            if name == "baidu":
                t = expected_tile_baidu(lm.lon, lm.lat, z)
            else:
                t = expected_tile_standard(lm.lon, lm.lat, z, adapter.crs)
            contains = tile_contains_point(adapter, t, lm.lon, lm.lat)
            results.append({
                "source": name, "landmark": lm.name,
                "tile": (t.z, t.x, t.y), "contains": contains, "ok": contains,
            })
    return results


def baidu_level_consistency_check(z: int = 17) -> dict:
    """百度层级映射一致性检查（警示：公开资料存在分歧）。

    检查内容（离线可做）：相邻层级的瓦片覆盖率关系——
    z+1 级同一地标应恰好落在 z 级瓦片的某个象限内（2×2 细分）。
    若 BAIDU_LEVEL_OFFSET 被改动，此关系必须仍成立。
    """
    out = {"ok": True, "details": []}
    for lm in LANDMARKS:
        t_z = expected_tile_baidu(lm.lon, lm.lat, z)
        t_z1 = expected_tile_baidu(lm.lon, lm.lat, z + 1)
        quad = (t_z1.x - 2 * t_z.x, t_z1.y - 2 * t_z.y)
        ok = (0 <= quad[0] <= 1) and (0 <= quad[1] <= 1)
        out["details"].append({"landmark": lm.name, "quadrant": quad, "ok": ok})
        out["ok"] = out["ok"] and ok
    return out
