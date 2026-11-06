"""栅格 → 矢量。

- 面要素（建筑/绿地/水体）：rasterio.features.shapes（GDAL Polygonize 等价路径）
- 道路中心线：skimage skeletonize 细化 → 8 邻域图追踪为 LineString
- 输出坐标：由调用方提供仿射变换（度/像素），矢量直接落到 WGS84/CGCS2000

重依赖（numpy/rasterio/shapely/skimage）延迟导入。
"""

from __future__ import annotations

import math


def polygonize_mask(mask, transform, *, min_area_m2: float = 0.0,
                    lat_deg: float = 30.0) -> list[dict]:
    """二值掩膜 → Polygon 要素列表（V2 + V2b 小面剔除）。

    - mask：numpy 布尔/0-1 数组
    - transform：rasterio Affine（像素 -> 目标 CRS 度数）
    - min_area_m2：小面剔除阈值；面积换算用等距近似（仅作筛选，非成果面积）
    返回 [{"geometry": geojson_dict, "area_m2": float}, ...]
    """
    import numpy as np
    from rasterio.features import shapes
    from shapely.geometry import shape as shp_shape

    m = np.asarray(mask, dtype="uint8")
    kx = 111320.0 * math.cos(math.radians(lat_deg))
    deg2_to_m2 = kx * 111320.0

    feats: list[dict] = []
    for geom, value in shapes(m, mask=m > 0, transform=transform):
        if value == 0:
            continue
        poly = shp_shape(geom)
        area_m2 = poly.area * deg2_to_m2
        if area_m2 < min_area_m2:
            continue
        feats.append({"geometry": geom, "area_m2": area_m2})
    return feats


def polygonize_values(arr, transform, *, min_area_m2: float = 0.0,
                      lat_deg: float = 30.0) -> list[dict]:
    """按像素值分区域 Polygonize，保留每个区域的栅格值（如层数档位）。

    - arr：单通道数组（floors 层数灰度图：0=背景，非 0=层数档位）
    - 返回 [{"geometry": geojson, "area_m2": float, "value": int}, ...]
    与 polygonize_mask 的区别：不把非 0 值坍缩成 1，而是按原值切分区域，
    供下游把「层数档位」映射到属性字段（如 height_min_m / height_max_m）。
    """
    import numpy as np
    from rasterio.features import shapes
    from shapely.geometry import shape as shp_shape

    m = np.asarray(arr)
    if m.ndim == 3:
        m = m[:, :, 0]
    kx = 111320.0 * math.cos(math.radians(lat_deg))
    deg2_to_m2 = kx * 111320.0

    feats: list[dict] = []
    for geom, value in shapes(m, transform=transform):
        v = int(value)
        if v == 0:
            continue
        poly = shp_shape(geom)
        area_m2 = poly.area * deg2_to_m2
        if area_m2 < min_area_m2:
            continue
        feats.append({"geometry": geom, "area_m2": area_m2, "value": v})
    return feats


def skeletonize_mask(mask):
    """骨架提取（V1：道路中心线第一步）。"""
    from skimage.morphology import skeletonize
    return skeletonize(mask > 0)


def _simplify_path(points, simplify_px=0.0, collinear_deg=170.0):
    """像素坐标点列 → 抽稀 + 合并共线点（单位均为像素，scale 无关）。

    - simplify_px>0：先做 Douglas-Peucker（shapely），把斜向道路的
      “台阶”顶点压成直线段（台阶相对真实线的偏差 ≤ 0.5px，故
      simplify_px≈0.5~1.0 即可拉直）。
    - 再合并三点夹角 > collinear_deg 的近共线中间点（几何无损）。
    - simplify_px<=0：原样返回（完全禁用，向后兼容）。
    points 形如 [(x, y), ...]（像素中心坐标），返回同格式点列。
    """
    if len(points) < 3 or simplify_px <= 0:
        return list(points)
    from shapely.geometry import LineString
    pts = list(LineString(points).simplify(simplify_px,
                                           preserve_topology=False).coords)
    if len(pts) < 3:
        return pts
    out = [pts[0]]
    for i in range(1, len(pts) - 1):
        ax, ay = pts[i - 1]
        bx, by = pts[i]
        cx, cy = pts[i + 1]
        v1x, v1y = ax - bx, ay - by
        v2x, v2y = cx - bx, cy - by
        m = math.hypot(v1x, v1y) * math.hypot(v2x, v2y)
        if m == 0.0:
            continue  # 重复点，丢弃中间点
        dot = v1x * v2x + v1y * v2y
        ang = math.degrees(math.acos(max(-1.0, min(1.0, dot / m))))
        if ang < collinear_deg:  # 真实折角 → 保留
            out.append(pts[i])
    out.append(pts[-1])
    return out


def skeleton_to_lines(skel, transform, *, min_length_px: int = 4,
                      simplify_px: float = 0.0,
                      collinear_deg: float = 170.0) -> list[dict]:
    """骨架像素 → LineString（V1b 细化追踪 + 像素空间抽稀）。

    算法：8 邻域图。度≠2 的像素为节点（端点/分叉），从节点出发
    逐链追踪；剩余未访问像素按闭合环处理。拓扑清理（打断/合并/
    去毛刺）由 min_length_px 与后处理共同完成（V1c）。

    每条链先经 _simplify_path 在像素空间抽稀并合并共线点，
    simplify_px 以像素为单位（0=禁用），用于消除斜向道路的台阶锯齿。
    """
    import numpy as np
    from shapely.geometry import LineString, mapping

    s = np.asarray(skel, dtype=bool)
    rows, cols = s.shape
    visited: set[tuple[int, int]] = set()

    def neighbors(rc):
        r, c = rc
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                if dr == 0 and dc == 0:
                    continue
                rr, cc = r + dr, c + dc
                if 0 <= rr < rows and 0 <= cc < cols and s[rr, cc]:
                    yield (rr, cc)

    def is_node(rc) -> bool:
        return sum(1 for _ in neighbors(rc)) != 2  # 端点(度1)或分叉(度≥3)

    def path_to_world(path):
        """像素链 → 抽稀后的世界坐标点列（不闭合）。"""
        pts = _simplify_path([(c + 0.5, r + 0.5) for (r, c) in path],
                             simplify_px, collinear_deg)
        return [(transform.c + x * transform.a,
                 transform.f + y * transform.e) for (x, y) in pts]

    def trace(start, first):
        path = [start, first]
        visited.add(first)
        prev, cur = start, first
        while True:
            nxt = [n for n in neighbors(cur) if n != prev and n not in visited]
            if not nxt:
                break
            cur_next = nxt[0]
            path.append(cur_next)
            visited.add(cur_next)
            if is_node(cur_next):
                break
            prev, cur = cur, cur_next
        return path

    lines: list[dict] = []

    # 1) 从节点出发的链
    for r in range(rows):
        for c in range(cols):
            rc = (r, c)
            if not s[rc] or not is_node(rc):
                continue
            for nb in neighbors(rc):
                if nb in visited:
                    continue
                path = trace(rc, nb)
                if len(path) >= min_length_px:
                    lines.append({"geometry": mapping(LineString(path_to_world(path)))})

    # 2) 闭合环（无节点残留，如环岛）
    for r in range(rows):
        for c in range(cols):
            rc = (r, c)
            if not s[rc] or rc in visited:
                continue
            ring = [rc]
            visited.add(rc)
            prev, cur = None, rc
            while True:
                nxt = [n for n in neighbors(cur) if n != prev and n not in visited]
                if not nxt:
                    break
                prev, cur = cur, nxt[0]
                ring.append(cur)
                visited.add(cur)
            if len(ring) >= min_length_px:
                coords = path_to_world(ring)
                coords.append(coords[0])  # 闭合
                lines.append({"geometry": mapping(LineString(coords))})
    return lines
