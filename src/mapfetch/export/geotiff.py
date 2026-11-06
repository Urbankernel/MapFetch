"""GeoTIFF 导出（内嵌坐标系 + 内部金字塔 + LZW 压缩）。

拼接主逻辑在 process/mosaic.py；本模块提供 CRS 处理与重投影入口：
- 天地图链路：目标 WGS84/CGCS2000 时跳过重投影（特权，直接写 CRS 标签）
- 高德/百度链路：瓦片四至已纠偏为 WGS84；目标 CGCS2000 与 WGS84 工程等价，
  仅需写正确标签；目标其他投影（如投影坐标系）时走 rasterio.warp
"""

from __future__ import annotations

from pathlib import Path

from ..core.geo.crs import normalize_crs

CRS_TO_EPSG = {"WGS84": 4326, "CGCS2000": 4490}  # 经纬度系成果


def epsg_for_crs(crs_name: str) -> int:
    n = normalize_crs(crs_name)
    if n not in CRS_TO_EPSG:
        raise ValueError(
            f"成果坐标系 {crs_name} 暂不支持直接写 GeoTIFF（支持 WGS84/CGCS2000 经纬度系；"
            "GCJ-02/BD-09 不允许作为成果坐标系， 标签一致性原则）"
        )
    return CRS_TO_EPSG[n]


def export_geotiff(plan, tile_path_fn, out_path: str | Path,
                   *, output_crs: str = "CGCS2000") -> Path:
    """拼接并导出 GeoTIFF（ArcGIS/QGIS 加载位置正确，内存受控）。

    ⚠️ 标签一致性（纠正错标签做法）：写入的 CRS 标签与瓦片数值
    严格一致——调用方必须保证瓦片四至已是 output_crs 对应数值
    （本工具链中：天地图=CGCS2000 直出；高德/百度四至已由适配器纠偏
    为 WGS84，CGCS2000 与 WGS84 工程等价，写 EPSG:4490 合法）。
    """
    from ..process.mosaic import compose_geotiff

    epsg = epsg_for_crs(output_crs)
    return compose_geotiff(plan, tile_path_fn, out_path, crs_epsg=epsg)


def clip_geotiff_to_aoi(src_tif: str | Path, aoi, dst_tif: str | Path,
                        *, nodata: int = 0) -> Path:
    """按 AOI 边界裁剪 GeoTIFF，AOI 外像素填充 nodata（GIS 中透明显示）。

    - ``src_tif``：已拼接的完整 GeoTIFF（EPSG:4490 CGCS2000）。
    - ``aoi``：AOI 对象（WGS84）。CGCS2000 与 WGS84 工程等价，
      坐标值直接复用为裁剪几何，与 aoi.gpkg 落盘口径一致。
    - 输出范围收缩到 AOI 外接矩形，AOI 外像素 = ``nodata``（默认 0）。

    实现说明：用 ``rasterio.features.rasterize`` 生成掩膜 + 手动窗口裁剪。
    不用 ``rasterio.mask.mask`` / ``rasterio.windows.from_bounds``——二者内部走
    ``numpy.linalg.solve``，在 numpy 2.5.x + rasterio 1.5.x 下会触发
    ``STATUS_PROCEDURE_NOT_FOUND`` 崩溃；本实现用仿射参数直接算窗口绕开。
    """
    import math

    import numpy as np
    import rasterio
    from affine import Affine
    from rasterio.features import rasterize
    from shapely.geometry import MultiPolygon, Polygon

    polys = [Polygon(r) for r in aoi.rings if len(r) >= 3]
    if not polys:
        raise ValueError("AOI 无可裁剪的有效环")
    geom = polys[0] if len(polys) == 1 else MultiPolygon(polys)

    dst_tif = Path(dst_tif)
    dst_tif.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(src_tif) as src:
        # 1) 在完整栅格坐标系下生成 AOI 掩膜（1=内，0=外）
        mask_arr = rasterize([(geom.__geo_interface__, 1)],
                             out_shape=(src.height, src.width),
                             transform=src.transform, fill=0, dtype="uint8")

        # 2) AOI 外接矩形 → 像素窗口（north-up 无旋转，直接用仿射参数反算，
        #    绕开 from_bounds 的 numpy.linalg.solve）
        a, c = src.transform.a, src.transform.c
        e, f = src.transform.e, src.transform.f
        min_lon, min_lat, max_lon, max_lat = aoi.bbox
        xs = sorted([(min_lon - c) / a, (max_lon - c) / a])
        ys = sorted([(max_lat - f) / e, (min_lat - f) / e])  # e<0：max_lat 在上
        col0 = max(0, int(math.floor(xs[0])))
        col1 = min(src.width, int(math.ceil(xs[1])))
        row0 = max(0, int(math.floor(ys[0])))
        row1 = min(src.height, int(math.ceil(ys[1])))
        width = max(1, col1 - col0)
        height = max(1, row1 - row0)

        # 3) 读取窗口数据 + 掩膜，AOI 外置 nodata
        data = src.read(window=((row0, row1), (col0, col1)))  # (bands, H, W)
        mask_win = mask_arr[row0:row1, col0:col1]
        data[:, mask_win == 0] = nodata

        # 4) 写出（transform 随窗口原点平移）
        out_transform = src.transform * Affine.translation(col0, row0)
        out_meta = src.meta.copy()
        out_meta.update({
            "driver": "GTiff",
            "height": height,
            "width": width,
            "transform": out_transform,
            "nodata": nodata,
        })
        with rasterio.open(dst_tif, "w", **out_meta) as dst:
            dst.write(data)
    return dst_tif
