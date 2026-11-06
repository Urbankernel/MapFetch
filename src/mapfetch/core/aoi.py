"""AOI（工作区域）模型。

三种选择方式的统一抽象：
    - 行政区（ADMIN）：DataV GeoAtlas 详细边界（默认）/ 天地图行政区划 API（抽稀，备用）
    - 矩形框选（RECT）：BBox
    - 多边形绘制（POLYGON）：顶点环，闭合时做自相交检测
    - 文件导入（FILE）：GeoJSON / GeoPackage / Shapefile 的 Polygon/MultiPolygon
    - 坐标输入（COORD）：四至或中心点+半径

关键规则：AOI 统一以 WGS84 存储与展示；请求各数据源前才转换
（由 sources 适配器负责）；瓦片范围为 AOI 外包矩形与层级计算。
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from .geo import tile as tilemod
from .geo.geometry import BBox, Ring, bbox_area_km2, bbox_of_ring, ring_area_km2, ring_is_simple

MAX_POLYGON_VERTICES = 500  #  顶点数上限


class AOISource(str, Enum):
    ADMIN = "admin"
    RECT = "rect"
    POLYGON = "polygon"
    FILE = "file"
    COORD = "coord"


@dataclass
class AOI:
    """统一 AOI 对象（WGS84）。"""

    rings: list[Ring]  # 一个或多个外环（MultiPolygon 拆为多环）
    source: AOISource
    meta: dict = field(default_factory=dict)

    # ------------------------------------------------------------------
    # 构造
    # ------------------------------------------------------------------
    @classmethod
    def from_rect(cls, min_lon: float, min_lat: float, max_lon: float, max_lat: float) -> "AOI":
        """矩形框选。"""
        if not (min_lon < max_lon and min_lat < max_lat):
            raise ValueError(f"矩形四至非法: ({min_lon}, {min_lat}, {max_lon}, {max_lat})")
        ring = [(min_lon, min_lat), (max_lon, min_lat), (max_lon, max_lat), (min_lon, max_lat)]
        return cls(rings=[ring], source=AOISource.RECT)

    @classmethod
    def from_polygon(cls, ring: Ring) -> "AOI":
        """多边形绘制（顶点上限 500，自相交检测）。"""
        if len(ring) > MAX_POLYGON_VERTICES:
            raise ValueError(f"多边形顶点数 {len(ring)} 超过上限 {MAX_POLYGON_VERTICES}")
        if len(ring) < 3:
            raise ValueError("多边形至少需要 3 个顶点")
        if not ring_is_simple(ring):
            raise ValueError("多边形自相交，请调整顶点（有效性校验）")
        return cls(rings=[list(ring)], source=AOISource.POLYGON)

    @classmethod
    def from_center_radius(cls, lon: float, lat: float, radius_m: float,
                           *, vertices: int = 72) -> "AOI":
        """中心点 + 半径：按参数方程生成**圆形多边形**。

        - 圆形边界用于 AOI 落盘（aoi.gpkg）与面积计算（≈πr²）；
        - 瓦片抓取范围仍按圆**外接矩形** bbox 计算（瓦片为方形、
          GeoTIFF 为矩形，圆形范围需在 GIS 中按边界自行裁剪）。
        - vertices 默认 72（半径 2 km 时弦长 ≈ 174 m，视觉平滑）。
        """
        if radius_m <= 0:
            raise ValueError(f"半径必须为正数: {radius_m}")
        dlat = radius_m / 111320.0
        dlon = radius_m / (111320.0 * math.cos(math.radians(lat)))
        ring: Ring = [
            (lon + dlon * math.cos(2.0 * math.pi * i / vertices),
             lat + dlat * math.sin(2.0 * math.pi * i / vertices))
            for i in range(vertices)
        ]
        return cls(
            rings=[ring],
            source=AOISource.POLYGON,
            meta={"shape": "circle", "radius_m": radius_m, "vertices": vertices},
        )

    @classmethod
    def from_amap_district_polyline(cls, polyline: str, *, adcode: str = "", name: str = "") -> "AOI":
        """解析高德 District API 的 polyline 字段。

        格式：多部件以 ``|`` 分隔；点间以 ``;`` 分隔；坐标为 ``经度,纬度``。
        返回值为 GCJ-02，调用方需用 crs.transform 转 WGS84（适配器负责）。
        """
        rings: list[Ring] = []
        for part in polyline.split("|"):
            ring: Ring = []
            for pt in part.split(";"):
                if not pt:
                    continue
                lon_s, lat_s = pt.split(",")
                ring.append((float(lon_s), float(lat_s)))
            if len(ring) >= 3:
                rings.append(ring)
        if not rings:
            raise ValueError("polyline 中未解析出有效边界环")
        return cls(
            rings=rings,
            source=AOISource.ADMIN,
            meta={"adcode": adcode, "name": name, "source_crs": "GCJ02"},
        )

    @classmethod
    def from_tianditu_boundary(cls, boundary: str, *, adcode: str = "", name: str = "") -> "AOI":
        """解析天地图行政区划 V2.0 的 boundary 字段（标准 WKT MULTIPOLYGON）。

        格式：``MULTIPOLYGON(((lon lat,lon lat,...),(...)),((...)))``
        - 坐标对以空格分隔（经度 纬度），点间以逗号分隔；
        - 每个 ``((...))`` 是一个多边形（含飞地/岛屿，即 MultiPolygon 的成员），
          一个多边形内部再分多个环时以内层括号区分。
        - 天地图坐标为 CGCS2000，与 WGS84 工程等价，无需纠偏。

        本方法用正则提取所有最内层坐标环（``([^]+)`` 天然匹配最内层），
        与 :meth:`from_amap_district_polyline` 一致地平铺为 rings。
        """
        rings: list[Ring] = []
        for m in re.finditer(r"\(([^]+)\)", boundary):
            ring: Ring = []
            for pt in m.group(1).split(","):
                parts = pt.split()
                if len(parts) >= 2:
                    ring.append((float(parts[0]), float(parts[1])))
            if len(ring) >= 3:
                rings.append(ring)
        if not rings:
            raise ValueError("boundary 中未解析出有效边界环")
        return cls(
            rings=rings,
            source=AOISource.ADMIN,
            meta={"adcode": adcode, "name": name, "source_crs": "CGCS2000"},
        )

    @classmethod
    def from_geojson(cls, path: str | Path) -> "AOI":
        """导入 GeoJSON：支持 Feature/FeatureCollection/Polygon/MultiPolygon。"""
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        geoms: list[dict] = []
        t = data.get("type")
        if t == "FeatureCollection":
            geoms = [f["geometry"] for f in data.get("features", []) if f.get("geometry")]
        elif t == "Feature":
            geoms = [data["geometry"]]
        elif t in ("Polygon", "MultiPolygon"):
            geoms = [data]
        else:
            raise ValueError(f"不支持的 GeoJSON 类型: {t}")

        rings: list[Ring] = []
        for g in geoms:
            if g["type"] == "Polygon":
                rings.append([(float(x), float(y)) for x, y in g["coordinates"][0]])
            elif g["type"] == "MultiPolygon":
                for poly in g["coordinates"]:
                    rings.append([(float(x), float(y)) for x, y in poly[0]])
        if not rings:
            raise ValueError("GeoJSON 中未找到 Polygon/MultiPolygon 几何")
        return cls(rings=rings, source=AOISource.FILE)

    @classmethod
    def from_file(cls, path: str | Path) -> "AOI":
        """统一文件导入入口，按扩展名分派。

        - ``.geojson`` / ``.json`` → :meth:`from_geojson`（纯 JSON，无重依赖）
        - ``.gpkg`` / ``.shp``   → :meth:`_from_vector_file`（geopandas 读取）
        - 其他扩展名 → 明确报错
        """
        p = Path(path)
        suffix = p.suffix.lower()
        if suffix in (".geojson", ".json"):
            return cls.from_geojson(p)
        if suffix in (".gpkg", ".shp"):
            return cls._from_vector_file(p)
        raise ValueError(
            f"不支持的文件格式: {suffix or '(无扩展名)'}（支持 .geojson / .gpkg / .shp）"
        )

    @classmethod
    def _from_vector_file(cls, path: Path) -> "AOI":
        """从 GeoPackage / Shapefile 导入面要素。

        规则：
        - 仅取 Polygon / MultiPolygon 外环，忽略点/线与内环（与 GeoJSON 口径一致）；
        - 非 WGS84 自动 ``to_crs(4326)``；文件无 CRS 则报错，不瞎猜坐标系。
        """
        try:
            import geopandas as gpd
        except ImportError as exc:
            raise RuntimeError(
                "导入 GPKG/SHP 需要 geopandas（pip install geopandas pyogrio）；"
                "或改用 .geojson 输入（无重依赖）"
            ) from exc

        gdf = gpd.read_file(path)
        if gdf.empty:
            raise ValueError(f"文件中未读到任何要素: {path}")

        gdf = gdf[gdf.geometry.geom_type.isin(("Polygon", "MultiPolygon"))]
        if gdf.empty:
            raise ValueError(f"文件中未找到 Polygon/MultiPolygon 面要素: {path}")

        src_crs = gdf.crs
        if src_crs is None:
            raise ValueError(
                f"文件缺少坐标系定义（无 CRS）: {path}；"
                "请补充 .prj（Shapefile）或在软件中指定坐标系后再导入"
            )
        crs_label = str(src_crs)
        if not (src_crs.is_geographic and src_crs.to_epsg() == 4326):
            gdf = gdf.to_crs(4326)

        rings: list[Ring] = []
        for geom in gdf.geometry:
            polys = [geom] if geom.geom_type == "Polygon" else list(geom.geoms)
            for poly in polys:
                rings.append([(float(x), float(y)) for x, y in poly.exterior.coords])

        if not rings:
            raise ValueError(f"文件中未解析出有效边界环: {path}")
        return cls(
            rings=rings,
            source=AOISource.FILE,
            meta={"source_file": str(path), "source_crs": crs_label},
        )

    # ------------------------------------------------------------------
    # 派生量
    # ------------------------------------------------------------------
    @property
    def bbox(self) -> BBox:
        boxes = [bbox_of_ring(r) for r in self.rings]
        return (
            min(b[0] for b in boxes),
            min(b[1] for b in boxes),
            max(b[2] for b in boxes),
            max(b[3] for b in boxes),
        )

    @property
    def area_km2(self) -> float:
        """AOI 面积（展示项）。多边形取环面积，矩形取 BBox 面积。"""
        if self.source in (AOISource.RECT, AOISource.COORD):
            return bbox_area_km2(self.bbox)
        return sum(ring_area_km2(r) for r in self.rings)

    def tile_range(self, z: int) -> tuple[int, int, int, int]:
        """瓦片范围（标准 XYZ）；AOI 为 WGS84 时可直接用于高德/天地图
        （适配器会先把四至转为源坐标系再调用，见 sources/base.py）。"""
        return tilemod.tile_range_for_bbox(*self.bbox, z)

    def estimate_tiles(self, levels: list[int]) -> dict[int, int]:
        """各层瓦片数预估（展示项）。"""
        out: dict[int, int] = {}
        for z in levels:
            x0, y0, x1, y1 = self.tile_range(z)
            out[z] = tilemod.count_tiles(x0, y0, x1, y1)
        return out

    def to_geojson(self) -> dict:
        """导出 GeoJSON（存为项目内 aoi.geojson，WGS84）。"""
        if len(self.rings) == 1:
            geom = {"type": "Polygon", "coordinates": [[list(p) for p in self.rings[0]]]}
        else:
            geom = {
                "type": "MultiPolygon",
                "coordinates": [[[list(p) for p in ring]] for ring in self.rings],
            }
        return {
            "type": "Feature",
            "properties": {"source": self.source.value, **self.meta},
            "geometry": geom,
        }
