"""几何工具（core/geo/geometry.py）：BBox/Polygon 工具与面积估算。

仅依赖标准库；几何以 WGS84 经纬度坐标表达（内部基准）。
面积采用等距圆柱近似，用于瓦片数/磁盘/耗时预估，
不作为法定面积成果。
"""

from __future__ import annotations

import math

EARTH_RADIUS_M = 6371008.8  # 平均地球半径

BBox = tuple[float, float, float, float]  # (min_lon, min_lat, max_lon, max_lat)
Ring = list[tuple[float, float]]          # 闭合或不闭合的顶点序列


def bbox_area_km2(bbox: BBox) -> float:
    """BBox 面积估算（km²，等距圆柱近似）。"""
    min_lon, min_lat, max_lon, max_lat = bbox
    mid_lat = math.radians((min_lat + max_lat) / 2)
    w = math.radians(max_lon - min_lon) * EARTH_RADIUS_M * math.cos(mid_lat)
    h = math.radians(max_lat - min_lat) * EARTH_RADIUS_M
    return abs(w * h) / 1e6


def ring_area_km2(ring: Ring) -> float:
    """多边形面积估算（km²， shoelace + 纬度修正）。"""
    if len(ring) < 3:
        return 0.0
    pts = ring if ring[0] == ring[-1] else ring + [ring[0]]
    mid_lat = math.radians(sum(p[1] for p in pts) / len(pts))
    kx = math.radians(1) * EARTH_RADIUS_M * math.cos(mid_lat)
    ky = math.radians(1) * EARTH_RADIUS_M
    area = 0.0
    for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
        area += (x1 * kx) * (y2 * ky) - (x2 * kx) * (y1 * ky)
    return abs(area) / 2 / 1e6


def bbox_of_ring(ring: Ring) -> BBox:
    lons = [p[0] for p in ring]
    lats = [p[1] for p in ring]
    return min(lons), min(lats), max(lons), max(lats)


def ring_is_simple(ring: Ring) -> bool:
    """自相交检测（多边形绘制闭合时必须校验）。

    O(n²) 线段相交检查；顶点数上限 500 时可接受。
    """
    pts = ring if ring[0] == ring[-1] else ring + [ring[0]]
    segs = list(zip(pts, pts[1:]))
    n = len(segs)
    for i in range(n):
        for j in range(i + 2, n):
            # 相邻线段（共享顶点）跳过
            if i == 0 and j == n - 1:
                continue
            if _segments_intersect(segs[i][0], segs[i][1], segs[j][0], segs[j][1]):
                return False
    return True


def _segments_intersect(p1, p2, p3, p4) -> bool:
    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    d1 = cross(p3, p4, p1)
    d2 = cross(p3, p4, p2)
    d3 = cross(p1, p2, p3)
    d4 = cross(p1, p2, p4)
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0))


def crosses_antimeridian(bbox: BBox) -> bool:
    """是否跨越 ±180°（禁止生成跨 180° 的单一 BBox）。"""
    return bbox[0] > bbox[2]
