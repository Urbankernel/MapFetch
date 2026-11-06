"""阿里云 DataV GeoAtlas 行政区边界适配器（详细边界，默认边界源）。

为什么需要本模块：
- 天地图行政区划 V2.0 的 boundary 为抽稀版本（区县级仅数十个顶点），
  作为精细 AOI（如道路/建筑级抓取范围）太粗糙；
- DataV GeoAtlas（areas_v3）为高精度边界（区县级数百顶点），
  公开访问、无需 Key，广泛用于 ECharts 等可视化场景。

接口（GET，返回 JSON）：
    {base}/{adcode}.json   按 6 位国标编码取边界（GeoJSON FeatureCollection）
    {base}/all.json        全国区划列表（3238 条：adcode/name/level/parent，无几何）

坐标系：GCJ-02（阿里系数据）。本模块返回前统一纠偏为 WGS84
（关键规则：AOI 统一 WGS84 存储），转换方式与 AmapAdapter.query_district 一致。
"""

from __future__ import annotations

import json
import urllib.request

from ..core.aoi import AOI, AOISource
from ..core.geo import crs
from ..core.geo.geometry import Ring

_BASE = "https://geo.datav.aliyun.com/areas_v3/bound"
_HEADERS = {"User-Agent": "mapfetch/0.1 (research tool)"}


def _fetch_json(url: str, timeout: int = 30):
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def resolve_adcode(keywords: str) -> tuple[str, str, str]:
    """名称/编码 → (adcode, name, level)。

    - 6 位数字编码：直接取边界，从返回属性读 name/level（省一次 all.json 下载）；
    - 名称：下载 all.json 精确匹配；未命中时回退前缀匹配，多候选报错列出。
    """
    kw = keywords.strip()
    if kw.isdigit() and len(kw) == 6:
        gj = _fetch_json(f"{_BASE}/{kw}.json")
        props = gj["features"][0]["properties"]
        return (str(props.get("adcode", kw)), str(props.get("name", kw)),
                str(props.get("level", "")))

    rows = _fetch_json(f"{_BASE}/all.json")
    cands = [r for r in rows if r.get("name") == kw]
    if not cands:
        cands = [r for r in rows if str(r.get("name", "")).startswith(kw)]
        if len(cands) > 1:
            names = " / ".join(f"{r['name']}({r['adcode']})" for r in cands[:8])
            raise ValueError(f"行政区「{kw}」匹配到多个候选：{names}，请写全称或用 6 位编码")
    if not cands:
        raise ValueError(f"未找到行政区「{kw}」，请使用全称（如 虹口区）或 6 位国标编码（如 310109）")
    r = cands[0]
    return str(r["adcode"]), str(r["name"]), str(r.get("level", ""))


def query_district(keywords: str) -> AOI:
    """查询行政区详细边界，返回 WGS84 AOI（GCJ-02 已纠偏）。"""
    adcode, name, level = resolve_adcode(keywords)
    gj = _fetch_json(f"{_BASE}/{adcode}.json")
    feat = gj["features"][0]
    geom = feat["geometry"]
    if geom["type"] == "Polygon":
        polys = [geom["coordinates"]]
    elif geom["type"] == "MultiPolygon":
        polys = geom["coordinates"]
    else:
        raise ValueError(f"DataV 返回不支持的几何类型: {geom['type']}")

    rings: list[Ring] = []
    for poly in polys:
        # poly[0] 为外环（DataV 各面均单环，无内洞）
        ring = [(float(x), float(y)) for x, y in poly[0]]
        ring = [crs.gcj02_to_wgs84(x, y) for x, y in ring]
        if len(ring) >= 3:
            rings.append(ring)
    if not rings:
        raise ValueError("DataV 边界未解析出有效环")
    return AOI(
        rings=rings,
        source=AOISource.ADMIN,
        meta={
            "adcode": adcode,
            "name": name,
            "level": level,
            "boundary_source": "datav_geoatlas",
            "source_crs": "GCJ02→WGS84",
        },
    )
