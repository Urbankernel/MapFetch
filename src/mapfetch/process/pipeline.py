"""矢量化生产管线。

输入：GeoTIFF（rasterio）或 PNG+PGW（PIL + 世界文件解析）
流程：
    道路 → 二值 → 骨架提取 → 中心线 LineString → 拓扑清理 → 导出
    建筑 → 二值 → Polygonize → 小面剔除 → 轮廓重构 → 正交化 → 导出
    背景 → 监督分类（规则基线）→ 按类别 Polygonize → 导出
输出：GeoPackage（默认）/ Shapefile / GeoJSON，CRS 标签与数值一致
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class RasterSource:
    """栅格输入的统一视图（数组 + 仿射变换 + 基准纬度）。"""

    array: object        # numpy 数组（H,W,3/4 或 H,W）
    transform: object    # rasterio Affine
    lat_deg: float       # 中心纬度（面积换算用）
    crs_epsg: int = 4490


def parse_pgw(pgw_path: str | Path) -> tuple[float, float, float, float, float, float]:
    """解析 6 行世界文件（纯函数）。

    返回 (res_x, rot_x, rot_y, res_y, center_x, center_y)。
    注意第 5/6 行是**像素中心**坐标（ESRI 规范），换算左上角需减半个像素。
    """
    vals = [float(v) for v in
            Path(pgw_path).read_text(encoding="ascii").strip().split("\n")]
    if len(vals) != 6:
        raise ValueError(f"PGW 必须是 6 行: {pgw_path}")
    return tuple(vals)  # type: ignore[return-value]


def transform_from_pgw(pgw_path: str | Path):
    """PGW -> Affine（像素中心修正为边缘）。

    优先用 affine.Affine（rasterio 传递依赖）；未安装时退化为
    只含 a/c/e/f 属性的轻量对象（下游 vectorize 仅用这四个属性）。
    """
    res_x, rot_x, rot_y, res_y, cx, cy = parse_pgw(pgw_path)
    # 世界文件存的是 (0,0) 像素中心；Affine 需要左上角边缘
    west = cx - res_x / 2
    north = cy - res_y / 2
    try:
        from affine import Affine
        return Affine(res_x, rot_x, west, rot_y, res_y, north)
    except ImportError:
        from dataclasses import dataclass

        @dataclass(frozen=True)
        class SimpleAffine:  # 与 affine.Affine 的属性接口一致（下游仅用 a/c/e/f）
            a: float
            b: float
            c: float
            d: float
            e: float
            f: float

        return SimpleAffine(res_x, rot_x, west, rot_y, res_y, north)


def load_raster(path: str | Path) -> RasterSource:
    """加载 GeoTIFF 或 PNG+PGW（重依赖延迟导入，缺失时给安装提示）。"""
    path = Path(path)
    try:
        import numpy as np
    except ImportError as exc:
        raise RuntimeError(
            "矢量化需要 numpy/Pillow/rasterio：pip install -e . （见 docs/installation.md）"
        ) from exc

    if not path.exists():
        raise FileNotFoundError(f"输入栅格不存在: {path}")
    if path.suffix.lower() in (".tif", ".tiff"):
        import rasterio
        with rasterio.open(path) as src:
            arr = src.read()
            arr = np.moveaxis(arr, 0, -1)  # (bands,H,W) -> (H,W,bands)
            if arr.shape[2] == 1:
                arr = arr[:, :, 0]
            bounds = src.bounds
            epsg = src.crs.to_epsg() if src.crs else 4490
            return RasterSource(array=arr, transform=src.transform,
                                lat_deg=(bounds.bottom + bounds.top) / 2,
                                crs_epsg=epsg or 4490)
    # PNG + 同名 .pgw
    from PIL import Image

    pgw = path.with_suffix(".pgw")
    if not pgw.exists():
        raise FileNotFoundError(f"PNG 输入需要同名 .pgw 世界文件: {pgw}")
    arr = np.asarray(Image.open(path))
    t = transform_from_pgw(pgw)
    # 从 PGW 估中心纬度（y 为度数时）
    lat = t.f + (arr.shape[0] / 2) * t.e
    return RasterSource(array=arr, transform=t, lat_deg=lat)


def _luminance_mask(src: RasterSource, *, threshold: int = 100):
    """亮度阈值二值化（道路/建筑，对应 classify 的规则）。"""
    import numpy as np

    arr = src.array
    if arr.ndim == 2:
        lum = arr.astype(int)
        return lum > threshold
    rgb = arr[:, :, :3].astype(int)
    lum = rgb.sum(axis=2)
    return lum > threshold * 3


def _alpha_mask(src: RasterSource):
    """RGBA 透明通道二值化（引擎 B 重分类输出直接可用）。"""
    arr = src.array
    if arr.ndim == 3 and arr.shape[2] == 4:
        return arr[:, :, 3] > 0
    return None


def _close_mask(mask, *, kernel_size: int = 3):
    """形态学闭运算（道路掩膜预平滑）：填平小孔、平滑边缘毛刺，
    减少 skeletonize 产生的小分支与中心线抖动。kernel_size<=1 原样返回。
    """
    if kernel_size <= 1:
        return mask
    from skimage.morphology import closing, footprint_rectangle
    return closing(mask > 0, footprint_rectangle((kernel_size, kernel_size)))


def vectorize_raster(input_path: str | Path, kind: str, out_path: str | Path,
                     *, min_area_m2: float = 10.0, threshold: int = 100,
                     simplify_tolerance: float = 0.7,
                     floor_step: int = 3) -> dict:
    """栅格 -> 矢量主入口。

    - kind: road / building / water / green / floors
    - floor_step：kind=floors 时的高度换算步长（须与 floors 命令的
      --floor-step 一致， f 命令；默认 3）
    - simplify_tolerance：道路中心线抽稀容差（像素，0=禁用；默认 0.7），
      用于消除骨架逐像素连线带来的台阶锯齿
    - 返回 {layer: 输出路径} 及要素统计
    """
    from ..export.vector import write_vector  # noqa: F401  （_export 使用）
    from .postprocess import postprocess_buildings
    from .vectorize import (polygonize_mask, skeleton_to_lines,
                            skeletonize_mask)

    src = load_raster(input_path)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    results: dict[str, dict] = {}

    if kind == "road":
        mask = _alpha_mask(src)
        if mask is None:
            mask = _luminance_mask(src, threshold=threshold)
        mask = _close_mask(mask, kernel_size=3)
        skel = skeletonize_mask(mask)
        feats = skeleton_to_lines(skel, src.transform,
                                  simplify_px=simplify_tolerance)
        results["roads"] = _export(feats, out_path, src.crs_epsg)

    elif kind == "building":
        mask = _alpha_mask(src)
        if mask is None:
            mask = _luminance_mask(src, threshold=threshold)
        feats = polygonize_mask(mask, src.transform,
                                min_area_m2=min_area_m2, lat_deg=src.lat_deg)
        feats = postprocess_buildings(feats, min_area_m2=min_area_m2)
        results["buildings"] = _export(feats, out_path, src.crs_epsg)

    elif kind in ("water", "green"):
        # 渲染端已按色域分离成二值图（α 掩膜即水系/绿地），直接面矢量化，
        # 不做建筑正交化等后处理。
        mask = _alpha_mask(src)
        if mask is None:
            mask = _luminance_mask(src, threshold=threshold)
        feats = polygonize_mask(mask, src.transform,
                                min_area_m2=min_area_m2, lat_deg=src.lat_deg)
        results[kind] = _export(feats, out_path, src.crs_epsg)

    elif kind == "floors":
        # 层数扫描单通道：像素值 = 层数档位（0=背景/无建筑，非 0=该处建筑
        # 高度档位，如扫描 0/3/6…/36 时值 6 表示第 6 档）。按像素值分区域
        # polygonize，读回每栋楼的实际档位并换算参考高度区间。
        from ..fetch.render import floor_to_height_range
        from .vectorize import polygonize_values

        arr = src.array if src.array.ndim == 2 else src.array[:, :, 0]
        feats = polygonize_values(arr, src.transform,
                                  min_area_m2=min_area_m2, lat_deg=src.lat_deg)
        for f in feats:
            v = f.pop("value")
            lo, hi = floor_to_height_range(v, step=floor_step)
            f["floor"] = v
            f["height_min_m"], f["height_max_m"] = lo, hi
        results["buildings_floors"] = _export(feats, out_path, src.crs_epsg)
    else:
        raise ValueError(f"未知矢量化类型: {kind!r}（road/building/water/green/floors）")

    return results


def _export(feats: list[dict], out_path: Path, crs_epsg: int) -> dict:
    from ..export.vector import write_vector

    if not feats:
        return {"path": None, "count": 0, "note": "无要素"}
    write_vector(feats, out_path, layer_name=out_path.stem, crs_epsg=crs_epsg)
    return {"path": str(out_path), "count": len(feats)}
