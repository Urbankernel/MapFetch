"""天地图适配器（默认源，合规优先；docs/data-sources.md §2）。

- 坐标系 CGCS2000，无加密偏移，与 WGS84 工程等价；
  因此瓦片范围计算直接用 WGS84 四至走标准 XYZ，无需纠偏。
- 成果目标为 WGS84/CGCS2000 时跳过重投影（天地图特权）。
- 限速全源最保守（1 QPS / 并发 2 / 日预算 8000），
  官方明示检测爬取行为将拒绝服务。
- 行政区边界走官方「行政区划 V2.0」Web 服务 API（默认源，替代高德 District）。
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request

from ..core.aoi import AOI
from ..core.geo import tile as tilemod
from ..core.geo.tile import Tile
from .base import SourceAdapter


class TiandituAdapter(SourceAdapter):
    @property
    def name(self) -> str:
        return "tianditu"

    def tile_range(self, aoi: AOI, z: int) -> tuple[int, int, int, int]:
        # CGCS2000 ≈ WGS84，直接按标准 XYZ 计算
        return tilemod.tile_range_for_bbox(*aoi.bbox, z)

    def tile_bounds_wgs84(self, t: Tile) -> tuple[float, float, float, float]:
        return tilemod.tile_to_lonlat_bounds(t.x, t.y, t.z)

    def needs_reprojection(self, output_crs: str) -> bool:
        """天地图特权：目标为 WGS84/CGCS2000 时跳过重投影。"""
        from ..core.geo.crs import normalize_crs
        return normalize_crs(output_crs) not in ("WGS84", "CGCS2000")

    # ------------------------------------------------------------------
    # 行政区边界（行政区划 V2.0 Web 服务 API）
    # ------------------------------------------------------------------
    def query_district(self, keywords: str, *, rest_key: str | None = None) -> AOI:
        """查询行政区边界并返回 WGS84 AOI。

        - 接口：``api.tianditu.gov.cn/v2/administrative``（Web 服务 API）
        - 参数：keyword（名称或编码）、childLevel=0、extensions=true、tk
        - boundary 为标准 WKT MULTIPOLYGON，坐标 CGCS2000 ≈ WGS84（无需纠偏）
        - 编码适配：6 位国标码自动补国家前缀 156（天地图 gb 码 = 156 + 国标码），
          使 ``admin:310109`` 这类高德习惯的写法仍可直接使用
        - Key：优先 REST 专用 Key（未配置则回退主 Key）；浏览器端 Key 直连
          服务端接口必须带 Referer（与瓦片 code 301012 同因）
        """
        base = self.cfg.rest.get("district")
        if not base:
            raise RuntimeError("天地图未配置行政区划服务地址（sources.yaml 的 rest.district）")
        key = rest_key or self.cfg.get_rest_key()

        kw = keywords.strip()
        if kw.isdigit() and len(kw) == 6:
            kw = "156" + kw  # 国标 6 位码 → 天地图 gb 码

        params = {"keyword": kw, "childLevel": "0", "extensions": "true", "tk": key}
        url = base + "?" + urllib.parse.urlencode(params)
        headers = {"User-Agent": "mapfetch/0.1"}
        if self.cfg.referer:
            headers["Referer"] = self.cfg.referer
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        if data.get("status") != 200:
            raise RuntimeError(
                f"天地图行政区查询失败: {data.get('message')} (status={data.get('status')})"
            )
        dists = (data.get("data") or {}).get("district") or []
        if not dists:
            raise RuntimeError(
                f"未查到行政区 {keywords!r}（天地图行政区划 V2.0：可用名称或完整 gb 编码）"
            )
        d = dists[0]
        boundary = d.get("boundary", "")
        if not boundary:
            raise RuntimeError(f"行政区 {d.get('name')} 未返回边界轮廓（extensions 需为 true）")
        return AOI.from_tianditu_boundary(
            boundary, adcode=d.get("gb", ""), name=d.get("name", "")
        )
