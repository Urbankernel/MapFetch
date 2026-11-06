"""SourceAdapter 抽象基类（docs/data-sources.md §5 插件接口）。

职责边界：
- 坐标：AOI 内部统一 WGS84；适配器负责 WGS84 <-> 源坐标系 的转换。
- URL：按配置模板拼装瓦片地址；子域轮询属官方负载均衡机制（允许项）。
- 新增数据源：继承本类并在 sources/__init__.py 注册，不动调度与处理层。
"""

from __future__ import annotations

import itertools
from abc import ABC, abstractmethod

from ..core.aoi import AOI
from ..core.geo import crs, tile as tilemod
from ..core.geo.tile import Tile


class SourceAdapter(ABC):
    """数据源适配器抽象基类。"""

    def __init__(self, cfg):
        self.cfg = cfg                      # config.loader.SourceConfig
        self._subdomain_cycle = itertools.cycle(cfg.subdomains or [""])

    # ---------------- 基本信息 ----------------
    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    def crs(self) -> str:
        return self.cfg.crs

    def styles(self) -> list[str]:
        return list(self.cfg.styles.keys())

    def style_name(self, style: str) -> str:
        return self.cfg.styles[style].name

    def level_range(self, style: str) -> tuple[int, int]:
        s = self.cfg.styles[style]
        return s.min_level, s.max_level

    def attribution_text(self) -> str:
        return self.cfg.attribution

    # ---------------- 坐标 ----------------
    def to_source_crs(self, lon: float, lat: float) -> tuple[float, float]:
        """WGS84 -> 源坐标系（经纬度系；BD09MC 由子类另行处理）。"""
        return crs.transform(lon, lat, "WGS84", self.crs)

    def from_source_crs(self, x: float, y: float) -> tuple[float, float]:
        """源坐标系 -> WGS84。"""
        return crs.transform(x, y, self.crs, "WGS84")

    # ---------------- 瓦片 ----------------
    @property
    def y_axis_down(self) -> bool:
        """瓦片 y 轴方向（拼接层据此确定北上/南下与行序）。

        True  = XYZ 标准（高德/天地图）：原点左上角，y 向南递增；
        False = 百度式：原点为墨卡托原点，y 向北递增（x/y 可为负）。
        """
        return True

    @abstractmethod
    def tile_range(self, aoi: AOI, z: int) -> tuple[int, int, int, int]:
        """AOI(WGS84) -> 该源的瓦片范围 (x0,y0,x1,y1)。"""
        ...

    @abstractmethod
    def tile_bounds_wgs84(self, t: Tile) -> tuple[float, float, float, float]:
        """瓦片 -> WGS84 四至 (min_lon, min_lat, max_lon, max_lat)，用于配准。"""
        ...

    def tile_url(self, style: str, t: Tile) -> str:
        """拼装瓦片 URL（子域轮询 + Key 注入）。"""
        s = self.cfg.styles[style]
        sub = next(self._subdomain_cycle) if self.cfg.subdomains else ""
        url = s.url.replace("{s}", str(sub))
        url = url.replace("{x}", str(t.x)).replace("{y}", str(t.y)).replace("{z}", str(t.z))
        if "{tk}" in url or "{key}" in url:
            url = url.replace("{tk}", self.cfg.get_key()).replace("{key}", self.cfg.get_key())
        if "{udt}" in url:
            url = url.replace("{udt}", self._udt())
        return url

    def _udt(self) -> str:
        """百度 udt 参数（数据版本戳）。当前返回固定值，实测后更新。"""
        return "20240701"

    def iter_tiles(self, aoi: AOI, z: int):
        x0, y0, x1, y1 = self.tile_range(aoi, z)
        yield from tilemod.iter_tiles(x0, y0, x1, y1, z)

    def estimate_tiles(self, aoi: AOI, levels: list[int]) -> dict[int, int]:
        out: dict[int, int] = {}
        for z in levels:
            out[z] = tilemod.count_tiles(*self.tile_range(aoi, z))
        return out

    # ---------------- 引擎 B（可选） ----------------
    def render_templates(self) -> list[str]:
        return self.cfg.render_templates
