"""矢量后处理（V2c/V2d）。

算法来源：`_v250529.py`（可读源码），
此处用 Shapely 重写以获得 GEOS 性能与鲁棒性（注）。

运行时默认参数（与原工具 param.txt 一致）：
    nearby_edge_limit=3, concave_edge_limit=1.5, nearby_edge_percent=0.4,
    cut_edge_limit=5, nearby_edge_multi=1, degree_limit=15
（长度单位为米；输入几何需为投影/米制或调用方先做等距缩放）
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RebuildParams:
    """凸折点重构参数（对应原工具 param_1/param_2）。"""

    nearby_edge_limit: float = 3.0    # 邻近边长下限（米）
    concave_edge_limit: float = 1.5   # 凹点邻边下限（米）


@dataclass
class RectanglifyParams:
    """切角正交化参数（对应原工具 param_3~param_6）。"""

    nearby_edge_percent: float = 0.4
    cut_edge_limit: float = 5.0
    nearby_edge_multi: float = 1.0
    degree_limit: float = 15.0


def simplify_polygon(geom, *, tolerance_m: float = 0.5):
    """几何简化：Douglas-Peucker，容差单位与输入坐标一致。"""
    return geom.simplify(tolerance_m, preserve_topology=True)


def is_convex_vertex_ring(coords: list[tuple[float, float]],
                          params: RebuildParams | None = None) -> list[tuple[float, float]]:
    """凸折点提取（is_convex_vertex， V2c）。

    叉积判定凸/凹/共线（ArcGIS 多边形外环为顺时针，判定条件相反——
    与原工具注释一致）；仅保留两侧邻边均足够长的凸折点。
    输入为不闭合坐标环（首尾不重复）。
    """
    import math

    p = params or RebuildParams()
    n = len(coords)
    if n < 3:
        return []

    def line_len(a, b):
        return math.hypot(a[0] - b[0], a[1] - b[1])

    # 第一遍：剔除三点共线
    clear = []
    for i in range(n):
        prev, cur, nxt = coords[i - 1], coords[i], coords[(i + 1) % n]
        ab = (cur[0] - prev[0], cur[1] - prev[1])
        bc = (nxt[0] - cur[0], nxt[1] - cur[1])
        if ab[0] * bc[1] - ab[1] * bc[0] != 0:
            clear.append(cur)

    # 第二遍：保留凸折点（顺时针外环：叉积 < 0 为凸，与原工具一致）
    keep = []
    n = len(clear)
    for i in range(n):
        prev, cur, nxt = clear[i - 1], clear[i], clear[(i + 1) % n]
        ab = (cur[0] - prev[0], cur[1] - prev[1])
        bc = (nxt[0] - cur[0], nxt[1] - cur[1])
        cross = ab[0] * bc[1] - ab[1] * bc[0]
        if cross < 0:  # 凸顶点
            prev_edge = line_len(prev, cur)
            next_edge = line_len(cur, nxt)
            if prev_edge >= p.nearby_edge_limit and next_edge >= p.nearby_edge_limit:
                keep.append(cur)
        else:  # 凹点：邻边足够长时也保留（维持轮廓）
            if line_len(cur, nxt) >= p.concave_edge_limit:
                keep.append(cur)
    return keep


def rebuild_polygon(geom, params: RebuildParams | None = None):
    """建筑轮廓重构（V2c）：凸折点提取后重连为多边形。"""
    from shapely.geometry import Polygon

    coords = list(geom.exterior.coords)[:-1]  # 去闭合点
    keep = is_convex_vertex_ring(coords, params)
    if len(keep) < 3:
        return geom  # 退化为原几何
    return Polygon(keep)


def rectanglify_polygon(geom, params: RectanglifyParams | None = None):
    """切角正交化（V2d，rectanglify 的角度修整思路）。

    实现：对近似直角的转角（90°±degree_limit）做正交吸附——
    将短边方向量化为水平/垂直，使轮廓呈正交形态；
    不满足条件的顶点保持原样。
    """
    import math

    from shapely.geometry import Polygon

    p = params or RectanglifyParams()
    coords = list(geom.exterior.coords)[:-1]
    n = len(coords)
    if n < 4:
        return geom

    def angle_deg(a, b, c):
        v1 = (a[0] - b[0], a[1] - b[1])
        v2 = (c[0] - b[0], c[1] - b[1])
        dot = v1[0] * v2[0] + v1[1] * v2[1]
        m = math.hypot(*v1) * math.hypot(*v2)
        if m == 0:
            return 0.0
        return math.degrees(math.acos(max(-1.0, min(1.0, dot / m))))

    new = []
    for i in range(n):
        prev, cur, nxt = coords[i - 1], coords[i], coords[(i + 1) % n]
        ang = angle_deg(prev, cur, nxt)
        if abs(ang - 90.0) <= p.degree_limit:
            # 近似直角：将顶点吸附为使两邻边正交的位置
            ex = (prev[0] + nxt[0]) / 2
            ey = (prev[1] + nxt[1]) / 2
            if abs(prev[0] - nxt[0]) > abs(prev[1] - nxt[1]):
                new.append((cur[0], ey))
            else:
                new.append((ex, cur[1]))
        else:
            new.append(cur)
    poly = Polygon(new)
    return poly if poly.is_valid else geom


def postprocess_buildings(feats: list[dict], *, min_area_m2: float = 10.0,
                          rebuild_params: RebuildParams | None = None,
                          rect_params: RectanglifyParams | None = None) -> list[dict]:
    """建筑图底后处理管线（V2b 小面剔除 → V2c 重构 → V2d 正交化）。

    输入/输出：[{"geometry": geojson_dict, "area_m2": float}, ...]
    ⚠️ 假设标注：参数长度单位为"度等效米"——调用方在 WGS84 下运行时，
    容差按纬度换算（见 vectorize.polygonize_mask 的换算系数说明）。
    """
    from shapely.geometry import mapping, shape as shp_shape

    out: list[dict] = []
    for f in feats:
        if f.get("area_m2", 0.0) < min_area_m2:
            continue
        geom = shp_shape(f["geometry"])
        geom = rebuild_polygon(geom, rebuild_params)
        geom = rectanglify_polygon(geom, rect_params)
        if geom.is_empty or not geom.is_valid:
            geom = shp_shape(f["geometry"])  # 失败回退原几何
        out.append({**f, "geometry": mapping(geom)})
    return out
