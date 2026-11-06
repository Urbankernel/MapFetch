"""高德适配器（docs/data-sources.md §3）。

- 坐标系 GCJ-02：瓦片范围为 WGS84 四至 -> GCJ-02 -> 标准 XYZ；
  瓦片四至再转回 WGS84 供配准（拼接前纠偏 + 重投影）。
- 行政区边界查询已改用 DataV GeoAtlas 详细边界（见 datav.py）；
  本模块的 query_district 保留为备用能力（官方 District REST API）。
- 要素分离渲染（引擎 B）模板集。
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request

from ..core.aoi import AOI
from ..core.geo import crs, tile as tilemod
from ..core.geo.tile import Tile
from .base import SourceAdapter


class AmapAdapter(SourceAdapter):
    @property
    def name(self) -> str:
        return "amap"

    def tile_range(self, aoi: AOI, z: int) -> tuple[int, int, int, int]:
        min_lon, min_lat, max_lon, max_lat = aoi.bbox
        # WGS84 四至 -> GCJ-02（高德坐标输入为 GCJ-02）
        sw = crs.wgs84_to_gcj02(min_lon, min_lat)
        ne = crs.wgs84_to_gcj02(max_lon, max_lat)
        return tilemod.tile_range_for_bbox(sw[0], sw[1], ne[0], ne[1], z)

    def tile_bounds_wgs84(self, t: Tile) -> tuple[float, float, float, float]:
        min_lon, min_lat, max_lon, max_lat = tilemod.tile_to_lonlat_bounds(t.x, t.y, t.z)
        sw = crs.gcj02_to_wgs84(min_lon, min_lat)
        ne = crs.gcj02_to_wgs84(max_lon, max_lat)
        return sw[0], sw[1], ne[0], ne[1]

    # ------------------------------------------------------------------
    # 行政区边界（官方公开 REST API）
    # ------------------------------------------------------------------
    def query_district(self, keywords: str, *, level: str = "district",
                       rest_key: str | None = None) -> AOI:
        """查询行政区边界并返回 WGS84 AOI。

        - extensions=all 才返回边界 polyline
        - 官方说明：不返回乡镇/街道级边界
        - 返回的 polyline 为 GCJ-02，本方法负责转 WGS84（关键规则）
        - 使用 Web 服务 Key（rest_key_env / keys.txt amap_rest），
          未配置时回退主 Key（keys.txt 注释：两种类型不可混用）
        """
        base = self.cfg.rest["district"]
        key = rest_key or self.cfg.get_rest_key()
        params = {
            "key": key,
            "keywords": keywords,
            "subdistrict": "0",
            "extensions": "all",
            "level": level,
        }
        url = base + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": "mapfetch/0.1"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        if data.get("status") != "1" or not data.get("districts"):
            raise RuntimeError(f"高德行政区查询失败: {data.get('info')} ({data.get('infocode')})")
        d = data["districts"][0]
        polyline = d.get("polyline", "")
        if not polyline:
            raise RuntimeError(
                f"行政区 {keywords} 未返回边界（可能为乡镇/街道级，官方不支持）"
            )
        aoi_gcj = AOI.from_amap_district_polyline(
            polyline, adcode=d.get("adcode", ""), name=d.get("name", "")
        )
        # GCJ-02 -> WGS84（AOI 统一 WGS84 存储）
        rings_wgs = [[crs.gcj02_to_wgs84(x, y) for x, y in ring] for ring in aoi_gcj.rings]
        return AOI(rings=rings_wgs, source=aoi_gcj.source, meta=aoi_gcj.meta)
