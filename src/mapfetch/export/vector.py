"""矢量导出：GeoPackage（默认）/ Shapefile / GeoJSON。

- GeoJSON：纯 json 实现，无重依赖
- GPKG/SHP：优先 geopandas/pyogrio；未安装时明确报错（不静默降级）
- PRJ/内嵌 CRS 必须与数值一致（标签一致性原则）
"""

from __future__ import annotations

import json
from pathlib import Path


def write_geojson(feats: list[dict], out_path: str | Path, *,
                  layer_name: str = "features",
                  crs: str = "CGCS2000") -> Path:
    """写 GeoJSON（RFC 7946；crs 字段写入 properties 供追溯， 元数据要求）。"""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fc = {
        "type": "FeatureCollection",
        "name": layer_name,
        "features": [
            {
                "type": "Feature",
                "properties": {k: v for k, v in f.items() if k != "geometry"} | {"crs": crs},
                "geometry": f["geometry"],
            }
            for f in feats
        ],
    }
    out_path.write_text(json.dumps(fc, ensure_ascii=False), encoding="utf-8")
    return out_path


def write_vector(feats: list[dict], out_path: str | Path, *,
                 layer_name: str = "features",
                 crs_epsg: int = 4490,
                 driver: str | None = None) -> Path:
    """写 GeoPackage / Shapefile（geopandas 延迟导入）。

    driver 缺省按扩展名推断：.gpkg -> GPKG，.shp -> ESRI Shapefile。
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    suffix = out_path.suffix.lower()
    if suffix == ".geojson" or driver == "GeoJSON":
        return write_geojson(feats, out_path, layer_name=layer_name,
                             crs="CGCS2000" if crs_epsg == 4490 else f"EPSG:{crs_epsg}")

    try:
        import geopandas as gpd
        from shapely.geometry import shape as shp_shape
    except ImportError as exc:
        raise RuntimeError(
            "导出 GPKG/SHP 需要 geopandas（pip install geopandas pyogrio）；"
            "或改用 .geojson 输出（无重依赖）"
        ) from exc

    if driver is not None:
        drv = driver
    elif suffix == ".gpkg":
        drv = "GPKG"
    elif suffix == ".shp":
        drv = "ESRI Shapefile"
    else:
        raise ValueError(f"无法按扩展名推断驱动: {suffix}（支持 .gpkg/.shp/.geojson）")

    gdf = gpd.GeoDataFrame(
        [{k: v for k, v in f.items() if k != "geometry"} for f in feats],
        geometry=[shp_shape(f["geometry"]) for f in feats],
        crs=f"EPSG:{crs_epsg}",
    )
    if drv == "GPKG":
        gdf.to_file(out_path, driver=drv, layer=layer_name)
    else:
        gdf.to_file(out_path, driver=drv)
    return out_path
