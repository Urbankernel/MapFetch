"""PGW + AUX.XML 导出（兼容既有工作流）。

⚠️ 关键区别：SRS 标签与数值**严格一致**。
2.0 把 GCJ-02 数值贴 WGS84 标签是刻意的兼容性 hack，本工具不继承；
调用方需保证传入的四至与声明 CRS 匹配。
"""

from __future__ import annotations

from pathlib import Path

_SRS_WKT = {
    "WGS84": 'GEOGCS["GCS_WGS_1984",DATUM["WGS_1984",SPHEROID["WGS_1984",6378137.0,298.257223563]],PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]]',
    "CGCS2000": 'GEOGCS["China Geodetic Coordinate System 2000",DATUM["China_2000",SPHEROID["CGCS2000",6378137.0,298.257222101]],PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]]',
}


def write_worldfile(out_png: str | Path, *, west: float, north: float,
                    res_x: float, res_y: float,
                    crs: str = "CGCS2000") -> tuple[Path, Path]:
    """写出 .pgw（6 行世界文件）与 .png.aux.xml（SRS 声明）。

    - res_x/res_y：度/像素（res_y 取正值，文件内写负值，与 ESRI 约定一致）
    - 世界文件第 5/6 行为**像素中心**坐标（左上角 + 半像素，ESRI 规范）
    """
    out_png = Path(out_png)
    pgw = out_png.with_suffix(".pgw")
    # 像素中心 = 左上角边缘 + 半个像素
    cx = west + res_x / 2
    cy = north - res_y / 2
    pgw.write_text(
        f"{res_x!r}\n0\n-0\n{-res_y!r}\n{cx!r}\n{cy!r}\n", encoding="ascii"
    )

    aux = out_png.parent / (out_png.name + ".aux.xml")
    srs = _SRS_WKT.get(crs.upper().replace("-", ""))
    if srs is None:
        raise ValueError(f"不支持的声明 CRS: {crs}（仅 WGS84/CGCS2000）")
    aux.write_text(
        f'<PAMDataset><SRS>{srs.replace(chr(34), "&quot;")}</SRS>'
        '<Metadata domain="IMAGE_STRUCTURE"><MDI key="INTERLEAVE">PIXEL</MDI>'
        "</Metadata></PAMDataset>",
        encoding="utf-8",
    )
    return pgw, aux


def clip_png_to_aoi(src_png: str | Path, aoi, dst_png: str | Path,
                    *, crs: str = "CGCS2000") -> Path:
    """按 AOI 边界裁剪 PNG（+同名 .pgw），AOI 外像素填充 0（透明/黑）。

    - ``src_png``：已拼接的完整 PNG（RGBA 或灰度 L）+ 同名 .pgw 世界文件。
    - ``aoi``：AOI 对象（WGS84）。CGCS2000 与 WGS84 工程等价，
      坐标值直接复用为裁剪几何，与 aoi.gpkg 落盘口径一致。
    - 输出范围收缩到 AOI 外接矩形，AOI 外像素 = 0（RGBA 时含 alpha 置 0 → 透明）。

    实现说明：与 ``clip_geotiff_to_aoi`` 同策略——用
    ``rasterio.features.rasterize`` 生成掩膜 + 仿射参数反算像素窗口，绕开
    ``numpy.linalg.solve`` 在 numpy 2.5.x + rasterio 1.5.x 下的 LAPACK 崩溃。
    """
    import math

    import numpy as np
    from affine import Affine
    from PIL import Image
    from rasterio.features import rasterize
    from shapely.geometry import MultiPolygon, Polygon

    src_png = Path(src_png)
    pgw = src_png.with_suffix(".pgw")
    if not pgw.exists():
        raise FileNotFoundError(f"PNG 裁剪需要同名 .pgw 世界文件: {pgw}")
    vals = [float(v) for v in pgw.read_text(encoding="ascii").strip().split("\n")]
    if len(vals) != 6:
        raise ValueError(f"PGW 必须是 6 行: {pgw}")
    res_x, rot_x, rot_y, res_y, cx, cy = vals
    # 世界文件第 5/6 行是像素中心坐标 → 换算左上角边缘
    west = cx - res_x / 2
    north = cy - res_y / 2
    t = Affine(res_x, rot_x, west, rot_y, res_y, north)

    polys = [Polygon(r) for r in aoi.rings if len(r) >= 3]
    if not polys:
        raise ValueError("AOI 无可裁剪的有效环")
    geom = polys[0] if len(polys) == 1 else MultiPolygon(polys)

    img = Image.open(src_png)
    arr = np.asarray(img)
    h, w = arr.shape[:2]

    # 1) 在完整栅格坐标系下生成 AOI 掩膜（1=内，0=外）
    mask = rasterize([(geom.__geo_interface__, 1)], out_shape=(h, w),
                     transform=t, fill=0, dtype="uint8")

    # 2) AOI 外接矩形 → 像素窗口（north-up 无旋转，直接用仿射参数反算）
    a, c = t.a, t.c
    e, f = t.e, t.f
    min_lon, min_lat, max_lon, max_lat = aoi.bbox
    xs = sorted([(min_lon - c) / a, (max_lon - c) / a])
    ys = sorted([(max_lat - f) / e, (min_lat - f) / e])  # e<0：max_lat 在上
    # bbox/res 的浮点误差可能使上限略大于整数（ceil 多 1 像素），用 eps 拉回
    eps = 1e-9
    col0 = max(0, int(math.floor(xs[0] + eps)))
    col1 = min(w, int(math.ceil(xs[1] - eps)))
    row0 = max(0, int(math.floor(ys[0] + eps)))
    row1 = min(h, int(math.ceil(ys[1] - eps)))
    # 防御：AOI 落在栅格外时收缩到至少 1 像素，避免空窗口
    col0, col1 = min(col0, w - 1), max(col0 + 1, col1)
    row0, row1 = min(row0, h - 1), max(row0 + 1, row1)

    # 3) 裁剪窗口 + 掩膜，AOI 外置 0
    win = arr[row0:row1, col0:col1].copy()
    win[mask[row0:row1, col0:col1] == 0] = 0

    # 4) 写出 PNG + 新世界文件（窗口原点平移，res 不变）
    dst_png = Path(dst_png)
    dst_png.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(win).save(dst_png)
    write_worldfile(dst_png, west=c + col0 * a, north=f + row0 * e,
                    res_x=a, res_y=-e, crs=crs)
    return dst_png
