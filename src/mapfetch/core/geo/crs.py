"""坐标系转换内核。

四系关系：
    WGS84 --(eviltransform 加密偏移)--> GCJ-02（高德/腾讯）
    GCJ-02 --(极坐标二次偏移)--> BD-09LL（百度经纬度）
    BD-09LL --(分段10项多项式)--> BD-09MC（百度墨卡托米制，瓦片编号基准）
    CGCS2000（天地图）与 WGS84 工程等价，直接传递（注）。

所有转换集中于本模块，其余模块不得自行实现（docs/coordinate-systems.md）。
边界条件（表）：
    - 境外不加 GCJ-02 偏移；
    - 纬度超出 ±85.05° 拒绝；
    - BD-09MC 输入非法抛 ValueError，不静默返回 0。
"""

from __future__ import annotations

import math

# Krasovsky 1940 椭球参数
_A = 6378245.0
_EE = 0.00669342162296594323
_X_PI = math.pi * 3000.0 / 180.0

MERCATOR_LAT_LIMIT = 85.05  # Web Mercator 纬度界限（边界表）

# ---------------------------------------------------------------------------
# BD-09LL <-> BD-09MC 分段多项式系数（百度官方 JS 同类查表）
# ---------------------------------------------------------------------------
_LLBAND = [75, 60, 45, 30, 15, 0]
_LL2MC = [
    [-0.0015702102444, 111320.7020616939, 1704480524535203.0, -10338987376042340.0,
     26112667856603880.0, -35149669176653700.0, 26595700718403920.0, -10725012454188240.0,
     1800819912950474.0, 82.5],
    [0.0008277824516172526, 111320.7020463578, 647795574.6671607, -4082003173.641316,
     10774905663.51142, -15171875531.51559, 12053065338.62167, -5124939663.577472,
     913311935.9512032, 67.5],
    [0.00337398766765, 111320.7020202162, 4481351.045890365, -23393751.19931662,
     79682215.47186455, -115964993.2797253, 97236711.15602145, -43661946.33752821,
     8477230.501135234, 52.5],
    [0.00220636496208, 111320.7020209128, 51751.86112841131, 3796837.749470245,
     992013.7397791013, -1221952.21711287, 1340652.697009075, -620943.6990984312,
     144416.9293806241, 37.5],
    [-0.0003441963504368392, 111320.7020576856, 278.2353980772752, 2485758.690035394,
     6070.750963243378, 54821.18345352118, 9540.606633304236, -2710.55326746645,
     1405.483844121726, 22.5],
    [-0.0003218135878613132, 111320.7020701615, 0.00369383431289, 823725.6402795718,
     0.46104986909093, 2351.343141331292, 1.58060784298199, 8.77738589078284,
     0.37238884252424, 7.45],
]
_MCBAND = [12890594.86, 8362377.87, 5591021.0, 3481989.83, 1678043.12, 0.0]
_MC2LL = [
    [1.410526172116255e-08, 8.98305509648872e-06, -1.9939833816331, 200.9824383106796,
     -187.2403703815547, 91.6087516669843, -23.38765649603339, 2.57121317296198,
     -0.03801003308653, 17337981.2],
    [-7.435856389565537e-09, 8.983055097726239e-06, -0.78625201886289, 96.32687599759846,
     -1.85204757529826, -59.36935905485877, 47.40033549296737, -16.50741931063887,
     2.28786674699375, 10260144.86],
    [-3.030883460898826e-08, 8.98305509983578e-06, 0.30071316287616, 59.74293618442277,
     7.357984074871, -25.38371002664745, 13.45380521110908, -3.29883767235584,
     0.32710905363475, 6856817.37],
    [-1.981981304930552e-08, 8.983055099779535e-06, 0.03278182852591, 40.31678527705744,
     0.65659298677277, -4.44255534477492, 0.85341911805263, 0.12923347998204,
     -0.04625736007561, 4482777.06],
    [3.09191371068437e-09, 8.983055096812155e-06, 0.00006995724062, 23.10934304144901,
     -0.00023663490511, -0.6321817810242, -0.00663494467273, 0.03430082397953,
     -0.00466043876332, 2555164.4],
    [2.890871144776878e-09, 8.983055095805407e-06, -3.068298e-08, 7.47137025468032,
     -3.53937994e-06, -0.02145144861037, -0.00001234426596, 0.00010322952773,
     -0.00000323890364, 826088.5],
]


def _check_lat(lat: float) -> None:
    if not -MERCATOR_LAT_LIMIT <= lat <= MERCATOR_LAT_LIMIT:
        raise ValueError(
            f"纬度 {lat} 超出 Web Mercator 定义域 ±{MERCATOR_LAT_LIMIT}°（边界表）"
        )


def out_of_china(lon: float, lat: float) -> bool:
    """是否在中国大陆范围外（境外不加 GCJ-02 偏移）。"""
    return not (73.66 < lon < 135.05 and 3.86 < lat < 53.55)


def _transform_lat(lon: float, lat: float) -> float:
    ret = -100.0 + 2.0 * lon + 3.0 * lat + 0.2 * lat * lat + 0.1 * lon * lat + 0.2 * math.sqrt(abs(lon))
    ret += (20.0 * math.sin(6.0 * lon * math.pi) + 20.0 * math.sin(2.0 * lon * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(lat * math.pi) + 40.0 * math.sin(lat / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (160.0 * math.sin(lat / 12.0 * math.pi) + 320 * math.sin(lat * math.pi / 30.0)) * 2.0 / 3.0
    return ret


def _transform_lon(lon: float, lat: float) -> float:
    ret = 300.0 + lon + 2.0 * lat + 0.1 * lon * lon + 0.1 * lon * lat + 0.1 * math.sqrt(abs(lon))
    ret += (20.0 * math.sin(6.0 * lon * math.pi) + 20.0 * math.sin(2.0 * lon * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(lon * math.pi) + 40.0 * math.sin(lon / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (150.0 * math.sin(lon / 12.0 * math.pi) + 300.0 * math.sin(lon / 30.0 * math.pi)) * 2.0 / 3.0
    return ret


def wgs84_to_gcj02(lon: float, lat: float) -> tuple[float, float]:
    """WGS84 -> GCJ-02（境外直接返回原值）。"""
    _check_lat(lat)
    if out_of_china(lon, lat):
        return lon, lat
    dlat = _transform_lat(lon - 105.0, lat - 35.0)
    dlon = _transform_lon(lon - 105.0, lat - 35.0)
    radlat = lat / 180.0 * math.pi
    magic = math.sin(radlat)
    magic = 1 - _EE * magic * magic
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((_A * (1 - _EE)) / (magic * sqrtmagic) * math.pi)
    dlon = (dlon * 180.0) / (_A / sqrtmagic * math.cos(radlat) * math.pi)
    return lon + dlon, lat + dlat


def gcj02_to_wgs84(lon: float, lat: float, *, iterations: int = 3) -> tuple[float, float]:
    """GCJ-02 -> WGS84，迭代反解（默认 3 次， 精度 < 1 m）。"""
    _check_lat(lat)
    if out_of_china(lon, lat):
        return lon, lat
    wgs_lon, wgs_lat = lon, lat
    for _ in range(iterations):
        g_lon, g_lat = wgs84_to_gcj02(wgs_lon, wgs_lat)
        wgs_lon -= g_lon - lon
        wgs_lat -= g_lat - lat
    return wgs_lon, wgs_lat


def gcj02_to_bd09ll(lon: float, lat: float) -> tuple[float, float]:
    """GCJ-02 -> BD-09LL（极坐标偏移法）。"""
    z = math.sqrt(lon * lon + lat * lat) + 0.00002 * math.sin(lat * _X_PI)
    theta = math.atan2(lat, lon) + 0.000003 * math.cos(lon * _X_PI)
    return z * math.cos(theta) + 0.0065, z * math.sin(theta) + 0.006


def bd09ll_to_gcj02(lon: float, lat: float) -> tuple[float, float]:
    """BD-09LL -> GCJ-02（减修正量后反解）。"""
    x, y = lon - 0.0065, lat - 0.006
    z = math.sqrt(x * x + y * y) - 0.00002 * math.sin(y * _X_PI)
    theta = math.atan2(y, x) - 0.000003 * math.cos(x * _X_PI)
    return z * math.cos(theta), z * math.sin(theta)


def wgs84_to_bd09ll(lon: float, lat: float) -> tuple[float, float]:
    return gcj02_to_bd09ll(*wgs84_to_gcj02(lon, lat))


def bd09ll_to_wgs84(lon: float, lat: float) -> tuple[float, float]:
    return gcj02_to_wgs84(*bd09ll_to_gcj02(lon, lat))


def _convert_ll2mc(lon: float, lat: float) -> tuple[float, float]:
    lat = max(min(lat, 74.0), -74.0)  # 表内有效范围
    for i, band in enumerate(_LLBAND):
        if abs(lat) >= band:
            c = _LL2MC[i]
            break
    else:
        c = _LL2MC[-1]
    if lat < 0:  # 南半球取系数后取反
        lon_c, lat_c = _poly(lon, abs(lat), c)
        return lon_c, -lat_c
    return _poly(lon, lat, c)


def _convert_mc2ll(x: float, y: float) -> tuple[float, float]:
    for i, band in enumerate(_MCBAND):
        if abs(y) >= band:
            c = _MC2LL[i]
            break
    else:
        c = _MC2LL[-1]
    if y < 0:
        lon_c, lat_c = _poly(x, abs(y), c)
        return lon_c, -lat_c
    return _poly(x, y, c)


def _poly(x: float, y: float, c: list[float]) -> tuple[float, float]:
    lon = c[0] + c[1] * abs(x)
    t = y / c[9]
    lat = (c[2] + c[3] * t + c[4] * t * t + c[5] * t ** 3 + c[6] * t ** 4
           + c[7] * t ** 5 + c[8] * t ** 6)
    return lon * (-1 if x < 0 else 1), lat * (-1 if y < 0 else 1)


def bd09ll_to_bd09mc(lon: float, lat: float) -> tuple[float, float]:
    """BD-09LL -> BD-09MC（分段 10 项多项式；输入非法抛 ValueError）。"""
    if lon is None or lat is None or not (-180.0 <= lon <= 180.0) or not (-90.0 <= lat <= 90.0):
        raise ValueError(f"BD-09LL 坐标非法: ({lon}, {lat})（不静默返回 0）")
    return _convert_ll2mc(lon, lat)


def bd09mc_to_bd09ll(x: float, y: float) -> tuple[float, float]:
    """BD-09MC -> BD-09LL；输入非法抛 ValueError。"""
    if x is None or y is None:
        raise ValueError("BD-09MC 坐标非法: None")
    lon, lat = _convert_mc2ll(x, y)
    if not (-180.0 <= lon <= 180.0) or not (-90.0 <= lat <= 90.0):
        raise ValueError(f"BD-09MC 坐标 ({x}, {y}) 反解结果越界: ({lon}, {lat})")
    return lon, lat


# CGCS2000（天地图）与 WGS84 工程等价，直接传递（注）
def cgcs2000_to_wgs84(lon: float, lat: float) -> tuple[float, float]:
    _check_lat(lat)
    return lon, lat


def wgs84_to_cgcs2000(lon: float, lat: float) -> tuple[float, float]:
    _check_lat(lat)
    return lon, lat


# ---------------------------------------------------------------------------
# 统一入口：按坐标系名称互转（供 sources 适配器与 process 层调用）
# ---------------------------------------------------------------------------
CRS_ALIASES = {
    "WGS84": "WGS84", "EPSG:4326": "WGS84",
    "GCJ02": "GCJ02", "GCJ-02": "GCJ02",
    "BD09LL": "BD09LL", "BD-09LL": "BD09LL", "BD09": "BD09LL", "BD-09": "BD09LL",
    "CGCS2000": "CGCS2000", "EPSG:4490": "CGCS2000",
}

_PATHS = {  # 有向转换图（BD09MC 只与 BD09LL 直连）
    ("WGS84", "GCJ02"): wgs84_to_gcj02,
    ("GCJ02", "WGS84"): gcj02_to_wgs84,
    ("GCJ02", "BD09LL"): gcj02_to_bd09ll,
    ("BD09LL", "GCJ02"): bd09ll_to_gcj02,
    ("WGS84", "CGCS2000"): wgs84_to_cgcs2000,
    ("CGCS2000", "WGS84"): cgcs2000_to_wgs84,
}
_ADJ = {
    "WGS84": ["GCJ02", "CGCS2000"],
    "GCJ02": ["WGS84", "BD09LL"],
    "BD09LL": ["GCJ02", "BD09MC"],
    "BD09MC": ["BD09LL"],
    "CGCS2000": ["WGS84"],
}


def normalize_crs(name: str) -> str:
    key = name.strip().upper()
    if key not in CRS_ALIASES:
        raise ValueError(f"未知坐标系: {name!r}，支持: {sorted(set(CRS_ALIASES.values()))}")
    return CRS_ALIASES[key]


def _find_path(src: str, dst: str) -> list[str]:
    """BFS 求转换链（如 WGS84 -> GCJ02 -> BD09LL）。"""
    from collections import deque
    queue = deque([(src, [src])])
    visited = {src}
    while queue:
        node, path = queue.popleft()
        if node == dst:
            return path
        for nxt in _ADJ.get(node, []):
            if nxt not in visited:
                visited.add(nxt)
                queue.append((nxt, path + [nxt]))
    raise ValueError(f"无转换路径: {src} -> {dst}")


def _direct_fn(src: str, dst: str):
    if (src, dst) in _PATHS:
        return _PATHS[(src, dst)]
    if src == "BD09LL" and dst == "BD09MC":
        return bd09ll_to_bd09mc
    if src == "BD09MC" and dst == "BD09LL":
        return bd09mc_to_bd09ll
    raise ValueError(f"无直接转换: {src} -> {dst}")


def transform(lon: float, lat: float, src: str, dst: str) -> tuple[float, float]:
    """统一坐标转换入口。src/dst 支持别名（如 'EPSG:4326'、'GCJ-02'）。"""
    s, d = normalize_crs(src), normalize_crs(dst)
    if s == d:
        return lon, lat
    path = _find_path(s, d)
    x, y = lon, lat
    for a, b in zip(path, path[1:]):
        x, y = _direct_fn(a, b)(x, y)
    return x, y
