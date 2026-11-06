"""命令行入口（总体数据流）。

子命令：
    mapfetch fetch   抓取（引擎 A 瓦片直下；--engine render 走引擎 B）
    mapfetch aoi     行政区边界查询（DataV GeoAtlas 详细边界）
    mapfetch doctor  环境自检（Python/依赖/Firefox/geckodriver/Key）
    mapfetch gui     启动 PySide6 GUI

数据流：AOI → 瓦片索引 → 阈值确认 → 调度下载（引擎 A）
    → 坐标处理 → 拼接 → GeoTIFF/其他 → manifest + ATTRIBUTION → 报告
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

from .. import DEFAULT_OUTPUT_CRS, __version__

logger = logging.getLogger("mapfetch")


# ---------------------------------------------------------------------------
# AOI 解析（三种方式 + 文件导入 + 坐标输入）
# ---------------------------------------------------------------------------

def _split_aoi_crs(body: str) -> tuple[str, str]:
    """从 rect/circle 规格体末尾拆出坐标系后缀（@ 分隔），返回 (坐标主体, 源坐标系)。

    源坐标系取值（不区分大小写）：
        wgs84(默认) / gcj02(高德·腾讯拾取) / bd09(百度拾取) / cgcs2000(天地图)
    百度/高德「坐标拾取」网页给出的是 GCJ02 / BD09 坐标，直接当 WGS84 用会偏移
    数百米，须经本后缀标注后再由工具统一纠偏为 WGS84。
    """
    body = body.strip()
    if "@" in body:
        body, crs = body.rsplit("@", 1)
        crs = crs.strip() or "WGS84"
    else:
        crs = "WGS84"
    return body.strip(), crs


def _to_wgs84(lon: float, lat: float, crs: str) -> tuple[float, float]:
    """把任意源坐标系坐标统一转为 WGS84（CGCS2000 与 WGS84 工程等价、直接传递）。"""
    from ..core.geo.crs import transform

    return transform(lon, lat, crs, "WGS84")


def parse_aoi(args) -> "object":
    from ..core.aoi import AOI

    # nargs='+' 时 args.aoi 为列表（不加引号、逗号后带空格会被 shell 拆成多段），
    # 用空格拼回；普通字符串（加了引号）则原样使用。
    spec = " ".join(args.aoi) if isinstance(args.aoi, (list, tuple)) else args.aoi
    if spec.startswith("rect:"):
        # rect:min_lon,min_lat,max_lon,max_lat[@坐标系]（默认 WGS84）
        body, crs = _split_aoi_crs(spec[5:])
        vals = [float(v.strip()) for v in body.split(",")]
        if len(vals) != 4:
            raise ValueError(
                "rect 需要 4 个数值: min_lon,min_lat,max_lon,max_lat（可选 @坐标系 后缀）"
            )
        min_lon, min_lat, max_lon, max_lat = vals
        min_lon, min_lat = _to_wgs84(min_lon, min_lat, crs)
        max_lon, max_lat = _to_wgs84(max_lon, max_lat, crs)
        # 纠偏后若四至顺序翻转则纠正（极小概率，仅防御）
        if min_lon > max_lon:
            min_lon, max_lon = max_lon, min_lon
        if min_lat > max_lat:
            min_lat, max_lat = max_lat, min_lat
        return AOI.from_rect(min_lon, min_lat, max_lon, max_lat)
    if spec.startswith("circle:"):
        # circle:lon,lat,radius_m[@坐标系]（默认 WGS84）
        body, crs = _split_aoi_crs(spec[7:])
        vals = [float(v.strip()) for v in body.split(",")]
        if len(vals) != 3:
            raise ValueError("circle 需要 3 个数值: lon,lat,radius_m（可选 @坐标系 后缀）")
        lon, lat, radius_m = vals
        lon, lat = _to_wgs84(lon, lat, crs)
        return AOI.from_center_radius(lon, lat, radius_m)
    if spec.startswith("admin:"):
        # admin:名称或编码（走 DataV GeoAtlas 详细边界，GCJ02 已自动纠偏 WGS84；
        # 天地图行政区划 API 边界为抽稀版，保留于 TiandituAdapter.query_district 备用）
        from ..sources import datav

        return datav.query_district(spec[6:])
    if spec.startswith("file:"):
        return AOI.from_file(spec[5:])
    raise ValueError(f"无法识别的 AOI 规格: {spec!r}（支持 rect:/circle:/admin:/file:）")


def parse_levels(spec: str) -> list[int]:
    """'15-17' 或 '15,16,17'。"""
    if "-" in spec:
        a, b = spec.split("-", 1)
        return list(range(int(a), int(b) + 1))
    return [int(v) for v in spec.split(",")]


# ---------------------------------------------------------------------------
# 任务配置文件（-c/--config）解析：fetch / render / floors / vectorize 四命令统一
# 优先级：命令行显式值 > 命令节 > common 节 > 内置默认（由各 resolve 函数兜底）
# ---------------------------------------------------------------------------

def _load_run_yaml(args) -> dict:
    """读取任务 YAML（顶层须为 dict）。

    优先 -c/--config 显式指定；未指定时默认读 configs/config.yaml
    （由 example.yaml 复制而来），不存在则回退命令行参数。
    """
    import yaml

    from ..config.loader import default_config_dir

    run_config = getattr(args, "run_config", None)
    if run_config:
        p = Path(run_config)
    else:
        base = Path(args.config_dir) if args.config_dir else default_config_dir()
        p = base / "config.yaml"
        if not p.exists():
            return {}
    if not p.exists():
        raise FileNotFoundError(f"任务配置文件不存在: {p}")
    yml = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(yml, dict):
        raise ValueError(f"任务配置文件 {p} 顶层必须是键值映射（dict）")
    return yml


def _apply_run_config(args, section: str, yml: dict, *, scalars=(),
                      bools=(), no_common=()) -> None:
    """把任务 YAML 合并进 args（就地修改）。

    - scalars：命令节可出现的标量参数名（命令行 nargs='+' 的 list 拼回字符串）
    - bools：store_true 开关（命令行已真则跳过，否则取配置 bool）
    - no_common：不从 common 节继承的字段（语义不同，如 vectorize 的 input/out）
    优先级：命令行显式值 > 命令节 > common 节 > 内置默认（默认由调用方兜底）。
    """
    sec = yml.get(section, {}) or {}
    common = yml.get("common", {}) or {}

    for f in scalars:
        cli_val = getattr(args, f, None)
        if cli_val is not None:
            if isinstance(cli_val, (list, tuple)):
                setattr(args, f, " ".join(str(x) for x in cli_val))
            continue
        v = sec.get(f)
        if v is None and f not in no_common:
            v = common.get(f)
        if v is not None:
            setattr(args, f, v)

    for f in bools:
        if getattr(args, f, False):
            continue
        v = sec.get(f)
        if v is None and f not in no_common:
            v = common.get(f)
        if v is not None:
            setattr(args, f, bool(v))


def _require(args, field: str, label: str) -> None:
    """必填项校验（命令行或配置文件至少一处提供）。"""
    if not getattr(args, field):
        raise ValueError(
            f"缺少必填参数 {label}：请在任务配置文件（-c）或命令行中指定"
            "（示例见 configs/example.yaml）")


def _resolve_fetch_args(args) -> None:
    """fetch：合并任务 YAML 与命令行参数。"""
    yml = _load_run_yaml(args)
    _apply_run_config(args, "fetch", yml,
                      scalars=("aoi", "source", "style", "levels", "out",
                               "project", "output_crs"),
                      bools=("no_mosaic", "yes"))
    if isinstance(args.levels, list):  # YAML 可写 [15,16,17] 或 "15-17"
        args.levels = ",".join(str(int(x)) for x in args.levels)
    args.project = args.project or "mapfetch_project"
    args.output_crs = args.output_crs or DEFAULT_OUTPUT_CRS
    for f, label in (("aoi", "aoi"), ("style", "style"), ("out", "out")):
        _require(args, f, label)


_RENDER_TEMPLATES = ("rd", "bld", "water", "green")


def _resolve_render_args(args) -> None:
    """render：合并任务 YAML 与命令行参数。"""
    yml = _load_run_yaml(args)
    _apply_run_config(args, "render", yml,
                      scalars=("aoi", "template", "zoom", "window", "wait",
                               "threshold", "out", "project"),
                      bools=("reclassify", "no_merge", "headless", "yes"))
    args.zoom = args.zoom if args.zoom is not None else 17
    args.window = args.window if args.window is not None else 2000
    args.wait = args.wait if args.wait is not None else 1.0
    args.project = args.project or "mapfetch_render"
    for f, label in (("aoi", "aoi"), ("template", "template"), ("out", "out")):
        _require(args, f, label)
    if args.template not in _RENDER_TEMPLATES:
        raise ValueError(
            f"未知渲染模板: {args.template!r}，可用: {list(_RENDER_TEMPLATES)}")


def _resolve_floors_args(args) -> None:
    """floors：合并任务 YAML 与命令行参数。"""
    yml = _load_run_yaml(args)
    _apply_run_config(args, "floors", yml,
                      scalars=("aoi", "floor_min", "floor_max", "floor_step",
                               "wait", "out", "project"),
                      bools=("no_merge", "headless", "yes"))
    args.floor_min = args.floor_min if args.floor_min is not None else 0
    args.floor_max = args.floor_max if args.floor_max is not None else 36
    args.floor_step = args.floor_step if args.floor_step is not None else 3
    args.wait = args.wait if args.wait is not None else 1.0
    args.project = args.project or "mapfetch_floors"
    for f, label in (("aoi", "aoi"), ("out", "out")):
        _require(args, f, label)


_VECTORIZE_KINDS = ("road", "building", "water", "green", "floors")

# kind -> render/floors 产物相对路径（input 未指定时自动定位）
_VECTORIZE_INPUT_PATTERNS = {
    "floors":   "raster/floors_z17/{project}_floors.png",
    "road":     "raster/render_rd_z{zoom}/{project}_rd_bin.png",
    "building": "raster/render_bld_z{zoom}/{project}_bld_bin.png",
    "water":    "raster/render_water_z{zoom}/{project}_water_bin.png",
    "green":    "raster/render_green_z{zoom}/{project}_green_bin.png",
}
# road/building 二值图（_bin）不存在时回退到原始渲染图
_VECTORIZE_RAW_FALLBACK = {"road": "rd", "building": "bld"}


def _default_vectorize_input(kind: str, out_root: str, project: str,
                             zoom: int) -> str:
    """input 未指定时，按 kind 自动定位 render/floors 产物路径。"""
    cand = Path(out_root) / _VECTORIZE_INPUT_PATTERNS[kind].format(
        project=project, zoom=zoom)
    if kind in _VECTORIZE_RAW_FALLBACK and not cand.exists():
        alt = cand.with_name(cand.name.replace("_bin.png", ".png"))
        if alt.exists():
            cand = alt
    return str(cand)


def _resolve_vectorize_args(args) -> None:
    """vectorize：合并任务 YAML 与命令行参数；input 缺省自动定位上游产物。"""
    yml = _load_run_yaml(args)
    # vectorize 的 out 是导出文件（非目录），input/out 等不继承 common 节；
    # 定位 input 用的项目输出目录/项目名/层级从 vectorize 节或 common 节读取。
    _apply_run_config(args, "vectorize", yml,
                      scalars=("input", "kind", "min_area", "threshold",
                               "simplify", "floor_step", "out"),
                      no_common=("input", "kind", "min_area", "threshold",
                                 "simplify", "floor_step", "out"))
    args.min_area = args.min_area if args.min_area is not None else 10.0
    args.threshold = args.threshold if args.threshold is not None else 100
    if getattr(args, "simplify", None) is None:
        args.simplify = 0.7
    args.floor_step = 3 if getattr(args, "floor_step", None) is None \
        else args.floor_step
    _require(args, "kind", "kind")
    if args.kind not in _VECTORIZE_KINDS:
        raise ValueError(
            f"未知矢量化类型: {args.kind!r}（road/building/water/green/floors）")
    _require(args, "out", "out")
    if not args.input:
        sec = yml.get("vectorize", {}) or {}
        common = yml.get("common", {}) or {}
        out_root = sec.get("out_dir") or common.get("out")
        if not out_root:
            raise ValueError(
                "缺少 input 且无法自动定位上游产物：请在 vectorize.out_dir 或"
                " common.out 指定（项目输出目录），或用 --input 显式指定")
        project = sec.get("project") or common.get("project") or (
            "mapfetch_floors" if args.kind == "floors" else "mapfetch_render")
        zoom = sec.get("zoom") or 17
        args.input = _default_vectorize_input(args.kind, out_root, project, zoom)


# ---------------------------------------------------------------------------
# fetch 子命令（引擎 A 主链路）
# ---------------------------------------------------------------------------

def cmd_fetch(args) -> int:
    from ..compliance.attribution import write_manifest
    from ..compliance.terms import USAGE_LIMITS
    from ..config import load_config
    from ..core.task import CheckpointStore, TileState
    from ..fetch.cache import TileCache
    from ..fetch.scheduler import TileScheduler
    from ..sources import create_adapter

    _resolve_fetch_args(args)  # 合并任务 YAML 与命令行参数（-c/--config）

    cfg = load_config(args.config_dir)
    source = args.source or cfg.default_source()
    if source not in cfg.sources or not cfg.sources[source].enabled:
        raise SystemExit(f"数据源 {source!r} 不可用（sources.yaml）")
    scfg = cfg.sources[source]

    if args.style not in scfg.styles:
        raise SystemExit(
            f"样式 {args.style!r} 不存在于 {source}，可用: {list(scfg.styles)}")
    adapter = create_adapter(source, scfg)

    # 预检：Key 缺失在下载前即报清晰错误（避免跑到一半才失败）
    if scfg.key_env:
        scfg.get_key()  # 缺失时抛 RuntimeError，由 main 统一格式化

    aoi = parse_aoi(args)
    levels = parse_levels(args.levels)
    lo, hi = adapter.level_range(args.style)
    for z in levels:
        if not lo <= z <= hi:
            raise SystemExit(f"层级 z{z} 超出 {source}/{args.style} 范围 [{lo}, {hi}]")

    #  预估与硬阈值
    estimates = adapter.estimate_tiles(aoi, levels)
    total_tiles = sum(estimates.values())
    print(f"AOI 面积: {aoi.area_km2:.2f} km²; 层级瓦片数: {estimates}; 合计 {total_tiles}")
    th = cfg.limits.task_thresholds
    if total_tiles > th.max_tiles and not args.yes:
        answer = input(f"瓦片数 {total_tiles} 超过阈值 {th.max_tiles}，确认继续？[y/N] ")
        if answer.strip().lower() != "y":
            print("已取消。")
            return 1

    project_dir = Path(args.out)
    project_dir.mkdir(parents=True, exist_ok=True)
    # ：AOI 落盘（GeoPackage，CGCS2000/EPSG:4490，与 GeoTIFF 对齐，GIS 原生格式）
    from ..export.vector import write_vector
    aoi_dir = project_dir / "aoi"
    aoi_dir.mkdir(exist_ok=True)
    _aoi_feat = aoi.to_geojson()
    write_vector(
        [{"geometry": _aoi_feat["geometry"], **_aoi_feat["properties"]}],
        aoi_dir / "aoi.gpkg",
        layer_name="aoi",
        crs_epsg=4490,
    )

    cache = TileCache(cfg.limits.cache.dir, expire_days=cfg.limits.cache.expire_days)
    rate_key = f"{source}_tile" if f"{source}_tile" in cfg.limits.rate_limits else source
    rate_cfg = cfg.limits.rate_limits[rate_key]

    reports = []
    t0 = time.time()
    with CheckpointStore(project_dir / ".mapfetch_checkpoint.db") as store:
        for z in levels:
            tiles = list(adapter.iter_tiles(aoi, z))
            store.register_many(source, args.style, tiles)

            def progress(done, total, tag):
                print(f"\r[{tag}] z{z}: {done}/{total}", end="", flush=True)

            # 浏览器端 Key 需带 Referer 白名单（sources.yaml 的 referer）
            extra_headers = {"Referer": scfg.referer} if scfg.referer else None
            scheduler = TileScheduler(
                adapter, args.style, cache=cache, store=store,
                rate_cfg=rate_cfg, retry_cfg=cfg.limits.retry,
                breaker_cfg=cfg.limits.circuit_breaker,
                progress_cb=progress, extra_headers=extra_headers)
            report = asyncio.run(scheduler.run(tiles))
            print()
            reports.append((z, report))
            if report.aborted:
                logger.error("任务中止: %s", report.abort_reason)
                break

    # 拼接导出（逐层 GeoTIFF）
    exports = []
    if not args.no_mosaic:
        from ..export.geotiff import clip_geotiff_to_aoi, export_geotiff
        from ..process.mosaic import plan_mosaic

        for z, report in reports:
            if report.done == 0:
                continue
            tiles = list(adapter.iter_tiles(aoi, z))
            plan = plan_mosaic(adapter, tiles)

            def tile_path_fn(t, _z=z):
                p = cache.path(source, args.style, t)
                return p if p.exists() else None

            out_tif = (project_dir / "raster" / f"{source}_{args.style}_z{z}"
                       / "export" / f"{args.project}_z{z}.tif")
            try:
                export_geotiff(plan, tile_path_fn, out_tif, output_crs=args.output_crs)
                exports.append(out_tif)
                print(f"已导出: {out_tif}")
            except ImportError as exc:
                logger.warning("跳过 GeoTIFF 导出（缺依赖 %s）；瓦片已在缓存中", exc)
                continue

            # 按 AOI 边界裁剪出第二份 GeoTIFF（AOI 外像素透明）
            try:
                clip_tif = out_tif.with_name(f"{args.project}_z{z}_aoi.tif")
                clip_geotiff_to_aoi(out_tif, aoi, clip_tif)
                exports.append(clip_tif)
                print(f"已导出(AOI 裁剪): {clip_tif}")
            except Exception as exc:  # 裁剪失败不阻断主流程
                logger.warning("跳过 AOI 裁剪（%s）", exc)

    # manifest + ATTRIBUTION + README
    warnings = []
    for z, r in reports:
        if r.empty:
            warnings.append(f"z{z}: 空瓦片 {r.empty} 个")
        if r.failed:
            warnings.append(f"z{z}: 失败 {r.failed} 个（可重跑补抓）")
        if r.throttled_events:
            warnings.append(f"z{z}: 429 降速 {r.throttled_events} 次")
        if r.aborted:
            warnings.append(f"z{z}: 中止 - {r.abort_reason}")

    manifest = {
        "project_name": args.project,
        "tool_version": __version__,
        "aoi": {"source": aoi.source.value, "area_km2": round(aoi.area_km2, 3),
                "bbox_wgs84": aoi.bbox},
        "sources": [{"name": source, "style": args.style, "levels": levels,
                     "tile_count": total_tiles}],
        "crs": {"source_crs": scfg.crs, "output_crs": args.output_crs,
                "transform_method": "adapter纠偏+直接拼接"
                if source == "tianditu" else "adapter纠偏(WGS84)+标签一致"},
        "limits": {"qps": rate_cfg.qps, "max_concurrent": rate_cfg.max_concurrent,
                   "quota_used": None},
        "attribution": scfg.attribution,
        "elapsed_sec": round(time.time() - t0, 1),
        "warnings": warnings,
    }
    write_manifest(project_dir, manifest)

    print(f"\n完成。用时 {time.time() - t0:.1f}s；项目目录: {project_dir}")
    print(USAGE_LIMITS)
    return 0


# ---------------------------------------------------------------------------
# render / floors 子命令（引擎 B）
# ---------------------------------------------------------------------------

def _render_aoi_bounds_gcj02(args):
    """AOI(WGS84) -> GCJ-02 四至（引擎 B 工作范围）。"""
    from ..core.geo import crs as crsmod

    aoi = parse_aoi(args)
    min_lon, min_lat, max_lon, max_lat = aoi.bbox
    sw = crsmod.wgs84_to_gcj02(min_lon, min_lat)
    ne = crsmod.wgs84_to_gcj02(max_lon, max_lat)
    return aoi, (sw[0], sw[1], ne[0], ne[1])


def _render_precheck(args):
    """引擎 B 前置：加载配置、取 Key。

    风险告知已在 docs/compliance.md 与 README「合规声明」中写明，渲染类命令
    （render/floors）不再交互确认——用户运行即视为已知悉并自行承担风险。
    """
    from ..config import load_config

    cfg = load_config(args.config_dir)
    scfg = cfg.sources["amap"]  # 引擎 B 模板集为高德
    key = scfg.get_key()
    return cfg, key


def _clip_pngs_to_aoi(png_paths, aoi) -> None:
    """对每个 PNG 生成 AOI 裁剪版（{stem}_aoi.png）；裁剪失败不阻断主流程。

    对齐 fetch 的裁剪模式：在完整输出基础上再生成 AOI 外透明的第二份。
    """
    from ..export.worldfile import clip_png_to_aoi

    for p in png_paths:
        try:
            clip = p.with_name(p.stem + "_aoi.png")
            clip_png_to_aoi(p, aoi, clip)
            print(f"已导出(AOI 裁剪): {clip}")
        except Exception as exc:  # 裁剪失败不阻断主流程（同 fetch）
            logger.warning("跳过 AOI 裁剪（%s）", exc)


def cmd_render(args) -> int:
    from ..fetch.render import (FLOOR_SCAN_WINDOW, RenderDriver, RenderParams,
                                RenderScanJob, ScanPlanner)

    _resolve_render_args(args)  # 合并任务 YAML 与命令行参数（-c/--config）
    cfg, key = _render_precheck(args)
    aoi, bounds_gcj = _render_aoi_bounds_gcj02(args)

    window = FLOOR_SCAN_WINDOW if args.template == "bld3d" else args.window
    params = RenderParams(zoom=args.zoom, wait_sec=args.wait,
                          window_size=window)
    planner = ScanPlanner(bounds_gcj, args.zoom, window)
    print(f"工作范围(GCJ-02): {bounds_gcj}; 窗口 {window}×{window}; "
          f"网格 {planner.cols}×{planner.rows} = {planner.total} 窗")
    if planner.total > 240 and not args.yes:
        # 基线：240 窗 ≈ 900 km² @ z17，内存/耗时风险提示
        answer = input(f"窗口数 {planner.total} 超过建议上限 240，继续？[y/N] ")
        if answer.strip().lower() != "y":
            print("已取消。")
            return 1

    out_dir = Path(args.out) / "raster" / f"render_{args.template}_z{args.zoom}"
    # 渲染底图样式（keys.txt 的 amap_mapstyle，经 loader 注入到 amap.render_mapstyle）
    # + JS API 2.0 安全密钥（2021-12 后申请的 Key 自定义样式必需）
    ams = cfg.sources["amap"]
    # rd/water/green 用自定义样式（同一 amap_mapstyle，样式 ID 固定不变；
    # 每次根据要获取的要素在高德控制台改样式设置即可）。bld 用官方预置
    # blue——免费版自定义地图无法设置建筑物图层，官方 blue 的建筑为亮色填充、
    # 对比度好。
    mapstyle = ((ams.render_mapstyle or "").strip()
                if args.template in ("rd", "water", "green") else "")
    security = ams.get_security_js_code()
    with RenderDriver(key, template=args.template, params=params,
                      headless=args.headless, mapstyle=mapstyle,
                      security_js_code=security) as driver:
        sw_lon, sw_lat = bounds_gcj[0], bounds_gcj[1]
        driver.start(sw_lon, sw_lat)
        job = RenderScanJob(driver, planner)

        def progress(done, total, tag):
            print(f"\r[{tag}] 窗口 {done}/{total}", end="", flush=True)

        paths = job.run(out_dir, name_prefix=f"{args.project}_{args.template}",
                        progress_cb=progress, merge=not args.no_merge)
        print()

    # 渲染后处理
    # - rd/bld：可选亮度二值化（--reclassify 开启）
    # - water/green：强制颜色分离二值化（模板固有输出：水系/绿地独立图层）
    def _copy_worldfiles(p, bin_p):
        for suf in (".pgw", ".png.aux.xml"):
            src = p.with_suffix(suf) if suf == ".pgw" else p.parent / (p.name + ".aux.xml")
            if src.exists():
                dst = bin_p.with_suffix(".pgw") if suf == ".pgw" else bin_p.parent / (bin_p.name + ".aux.xml")
                dst.write_bytes(src.read_bytes())

    final_pngs = list(paths)  # 最终输出 PNG（原始渲染 + 可能的二值化）

    if args.reclassify and args.template in ("rd", "bld"):
        from ..process.classify import binarize_building, binarize_road
        fn = binarize_road if args.template == "rd" else binarize_building
        # --threshold 未指定时用各函数的默认值（道路 120（下限）/ 建筑 40（上限））
        kw = {} if args.threshold is None else {"threshold": args.threshold}
        for p in paths:
            bin_p = p.with_name(p.stem + "_bin.png")
            fn(p, bin_p, **kw)
            _copy_worldfiles(p, bin_p)
            final_pngs.append(bin_p)
        print(f"已重分类 {len(paths)} 个文件（规则）")

    elif args.template in ("water", "green"):
        from ..process.classify import binarize_background_layer
        for p in paths:
            bin_p = p.with_name(p.stem + "_bin.png")
            binarize_background_layer(p, bin_p, layer=args.template)
            _copy_worldfiles(p, bin_p)
            final_pngs.append(bin_p)
        print(f"已分离 {args.template} 图层（{len(paths)} 个文件， 背景要素分离）")

    # AOI 裁剪（对齐 fetch：完整输出之外再生成 AOI 外透明的第二份）
    if not args.no_merge:
        _clip_pngs_to_aoi(final_pngs, aoi)

    result_desc = "单张拼接图" if not args.no_merge else f"{len(paths)} 个块"
    print(f"完成：{result_desc} -> {out_dir}")
    return 0


def cmd_floors(args) -> int:
    """建筑层数扫描（模式 3）。"""
    from ..fetch.render import (FLOOR_SCAN_WINDOW, FloorScanJob, RenderDriver,
                                RenderParams, ScanPlanner)

    _resolve_floors_args(args)  # 合并任务 YAML 与命令行参数（-c/--config）
    cfg, key = _render_precheck(args)
    aoi, bounds_gcj = _render_aoi_bounds_gcj02(args)

    fmin, fmax, fstep = args.floor_min, args.floor_max, args.floor_step
    params = RenderParams(zoom=17, wait_sec=args.wait,
                          window_size=FLOOR_SCAN_WINDOW,
                          floor_min=fmin, floor_max=fmax, floor_step=fstep)
    params.validate()  #  f 命令约束（整除、范围）

    planner = ScanPlanner(bounds_gcj, 17, FLOOR_SCAN_WINDOW)
    rounds = len(range(fmin, fmax + 1, fstep))
    print(f"层数扫描 {fmin}-{fmax} 间隔 {fstep}（{rounds} 轮）; "
          f"窗口 {planner.total} 个；预计截图 {planner.total * rounds} 张")
    if not args.yes:
        answer = input("层数扫描耗时较长（基准×4×轮数），继续？[y/N] ")
        if answer.strip().lower() != "y":
            print("已取消。")
            return 1

    out_dir = Path(args.out) / "raster" / "floors_z17"
    ams = cfg.sources["amap"]
    with RenderDriver(key, template="bld3d", params=params,
                      headless=args.headless,
                      mapstyle=(ams.render_mapstyle or "").strip(),
                      security_js_code=ams.get_security_js_code()) as driver:
        driver.start(bounds_gcj[0], bounds_gcj[1])
        job = FloorScanJob(driver, planner)

        def progress(done, total, tag):
            print(f"\r[{tag}] 窗口 {done}/{total}", end="", flush=True)

        paths = job.run(out_dir, name_prefix=args.project, params=params,
                        progress_cb=progress, merge=not args.no_merge)
        print()
    # AOI 裁剪（对齐 fetch：完整层数图之外再生成 AOI 外透明的第二份）
    if not args.no_merge:
        _clip_pngs_to_aoi(paths, aoi)
    desc = "单张层数图" if not args.no_merge else f"{len(paths)} 张分窗图"
    print(f"完成：{desc} -> {out_dir}（镶嵌规则：取最大值）")
    return 0


# ---------------------------------------------------------------------------
# vectorize 子命令
# ---------------------------------------------------------------------------

def cmd_vectorize(args) -> int:
    from ..process.pipeline import vectorize_raster

    _resolve_vectorize_args(args)  # 合并任务 YAML 与命令行参数（-c/--config）
    results = vectorize_raster(
        args.input, args.kind, args.out,
        min_area_m2=args.min_area, threshold=args.threshold,
        simplify_tolerance=args.simplify, floor_step=args.floor_step)
    for layer, info in results.items():
        if info["path"]:
            print(f"{layer}: {info['count']} 个要素 -> {info['path']}")
        else:
            print(f"{layer}: {info.get('note', '无输出')}")
    return 0




def cmd_aoi(args) -> int:
    """行政区边界查询并落盘：默认 GeoPackage，支持 .shp/.geojson。"""
    from ..export.vector import write_vector
    from ..sources import datav

    aoi = datav.query_district(args.name)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    _feat = aoi.to_geojson()
    write_vector(
        [{"geometry": _feat["geometry"], **_feat["properties"]}],
        out, layer_name="aoi", crs_epsg=4490)
    print(f"已保存: {out}（{aoi.meta.get('name')}，面积 {aoi.area_km2:.1f} km²，CGCS2000/EPSG:4490）")
    return 0


def cmd_doctor(args) -> int:
    """环境自检（installation.md §5）。"""
    import os
    import shutil

    ok = True

    def check(name, passed, detail=""):
        nonlocal ok
        mark = "OK " if passed else "FAIL"
        print(f"[{mark}] {name}{(' - ' + detail) if detail else ''}")
        ok = ok and passed

    v = sys.version_info
    check("Python 3.11/3.12", (v.major, v.minor) in ((3, 11), (3, 12)),
          f"当前 {v.major}.{v.minor}.{v.micro}")

    for mod in ("yaml", "pydantic"):
        try:
            __import__(mod)
            check(f"依赖 {mod}", True)
        except ImportError:
            check(f"依赖 {mod}", False, "pip install pyyaml pydantic")

    for mod in ("httpx", "numpy", "PIL", "rasterio", "geopandas", "shapely",
                "skimage", "selenium", "PySide6"):
        try:
            __import__(mod)
            check(f"依赖 {mod}（可选，引擎A/B、GUI、矢量化）", True)
        except ImportError:
            print(f"[WARN] 可选依赖 {mod} 未安装（相关功能不可用）")

    def _which_or_env(cmd: str, envvar: str) -> bool:
        """可执行文件在 PATH 中，或由环境变量 envvar 指定且文件存在，均视为可用。"""
        if shutil.which(cmd):
            return True
        p = os.environ.get(envvar, "").strip()
        return bool(p) and os.path.isfile(p)

    check("Firefox", _which_or_env("firefox", "MAPFETCH_FIREFOX_BINARY"),
          "引擎 B 需要（或设 MAPFETCH_FIREFOX_BINARY 指向 firefox 可执行文件）"
          if not _which_or_env("firefox", "MAPFETCH_FIREFOX_BINARY") else "")
    check("geckodriver", _which_or_env("geckodriver", "MAPFETCH_GECKODRIVER"),
          "引擎 B 需要（或设 MAPFETCH_GECKODRIVER 指向 geckodriver 可执行文件）"
          if not _which_or_env("geckodriver", "MAPFETCH_GECKODRIVER") else "")

    try:
        from ..config import load_config
        cfg = load_config(args.config_dir)
        for name, scfg in cfg.sources.items():
            if not scfg.enabled or not scfg.key_env:
                continue
            masked = cfg.masked_key(name)
            print(f"[{'OK ' if masked != '<未配置>' else 'WARN'}] Key {name}: {masked}")
    except Exception as exc:
        check("配置文件", False, str(exc))

    return 0 if ok else 1


def cmd_gui(args) -> int:
    from .app import run_gui
    return run_gui(config_dir=args.config_dir)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mapfetch",
        description="多元地图数据下载工具（天地图/高德/百度 → GeoTIFF/GeoPackage，默认 CGCS2000）")
    p.add_argument("--version", action="version", version=f"mapfetch {__version__}")
    p.add_argument("--config-dir", default=None,
                   help="配置目录（sources/keys/limits.yaml，默认取仓库内置 configs）")
    p.add_argument("-v", "--verbose", action="store_true")

    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", help="抓取瓦片并导出（引擎 A）")
    f.add_argument("-c", "--config", dest="run_config", default=None,
                   help="任务配置文件（YAML；缺省自动读 configs/config.yaml；命令行同名参数优先覆盖）")
    f.add_argument("--aoi", nargs='+', default=None,
                   help="rect:minLon,minLat,maxLon,maxLat[@crs] | circle:lon,lat,r[@crs] | admin:名称/编码 | file:路径(.geojson/.gpkg/.shp)；@crs 可选 wgs84(默认)/gcj02/bd09/cgcs2000（可省略，改从 -c 任务配置读取；逗号后带空格请加引号）")
    f.add_argument("--source", default=None, help="tianditu(默认)/amap/baidu")
    f.add_argument("--style", default=None, help="图层样式 key（如 img_w/satellite/street）")
    f.add_argument("--levels", default=None, help="如 15-17 或 15,16,17")
    f.add_argument("--out", default=None, help="项目输出目录")
    f.add_argument("--project", default=None, help="项目名（默认 mapfetch_project）")
    f.add_argument("--output-crs", default=None,
                   help="成果坐标系（默认 CGCS2000；可选 WGS84）")
    f.add_argument("--no-mosaic", action="store_true", help="只下载瓦片不拼接")
    f.add_argument("-y", "--yes", action="store_true",
                   help="跳过交互确认（瓦片数超阈值等确认）")
    f.set_defaults(func=cmd_fetch)

    a = sub.add_parser("aoi", help="行政区边界查询（DataV GeoAtlas 详细边界 → 矢量文件）")
    a.add_argument("--name", required=True, help="行政区名称或编码（如 青岛市 / 370200）")
    a.add_argument("--out", required=True, help="输出 .gpkg / .shp / .geojson")
    a.set_defaults(func=cmd_aoi)

    r = sub.add_parser("render", help="引擎 B：JS 渲染截图（道路/建筑/背景/影像）")
    r.add_argument("-c", "--config", dest="run_config", default=None,
                   help="任务配置文件（YAML；缺省自动读 configs/config.yaml；命令行同名参数优先覆盖）")
    r.add_argument("--aoi", nargs='+', default=None, help="同 fetch 的 AOI 规格")
    r.add_argument("--template", choices=["rd", "bld", "water", "green"], default=None,
                   help="渲染模板（要素-样式映射）")
    r.add_argument("--zoom", type=int, default=None, help="14–20，建筑需 ≥17")
    r.add_argument("--window", type=int, default=None, help="窗口像素边长")
    r.add_argument("--wait", type=float, default=None, help="窗口移动后等待秒数")
    r.add_argument("--reclassify", action="store_true",
                   help="道路/建筑二值化输出（重分类规则）")
    r.add_argument("--threshold", type=int, default=None,
                   help="二值化亮度阈值（默认：道路 120（高于为前景）/ "
                        "建筑 40（低于为前景，blue 样式建筑比背景暗））")
    r.add_argument("--no-merge", action="store_true",
                   help="关闭拼接，按 4×4 窗分块输出（默认输出单张完整拼接图）")
    r.add_argument("--headless", action="store_true", help="无头模式运行 Firefox")
    r.add_argument("--out", default=None)
    r.add_argument("--project", default=None)
    r.add_argument("-y", "--yes", action="store_true",
                   help="跳过交互确认（窗口数超阈值确认）")
    r.set_defaults(func=cmd_render)

    fl = sub.add_parser("floors", help="建筑层数扫描（3D 遮挡法， 模式 3）")
    fl.add_argument("-c", "--config", dest="run_config", default=None,
                    help="任务配置文件（YAML；缺省自动读 configs/config.yaml；命令行同名参数优先覆盖）")
    fl.add_argument("--aoi", nargs='+', default=None,
                    help="同 fetch 的 AOI 规格（rect/circle 支持 @crs 坐标系后缀）")
    fl.add_argument("--floor-min", type=int, default=None)
    fl.add_argument("--floor-max", type=int, default=None)
    fl.add_argument("--floor-step", type=int, default=None)
    fl.add_argument("--wait", type=float, default=None)
    fl.add_argument("--headless", action="store_true")
    fl.add_argument("--no-merge", action="store_true",
                    help="按窗口分文件输出（默认合并为单张完整层数图）")
    fl.add_argument("--out", default=None)
    fl.add_argument("--project", default=None)
    fl.add_argument("-y", "--yes", action="store_true",
                    help="跳过交互确认（层数扫描耗时确认）")
    fl.set_defaults(func=cmd_floors)

    v = sub.add_parser("vectorize", help="栅格 → 矢量（road/building/water/green/floors）")
    v.add_argument("-c", "--config", dest="run_config", default=None,
                   help="任务配置文件（YAML；缺省自动读 configs/config.yaml；命令行同名参数优先覆盖）")
    v.add_argument("--input", default=None,
                   help="GeoTIFF 或 PNG（需同名 .pgw）；缺省按 kind 自动定位上游产物")
    v.add_argument("--kind", choices=["road", "building", "water", "green", "floors"],
                   default=None)
    v.add_argument("--out", default=None, help="输出 .gpkg / .shp / .geojson")
    v.add_argument("--min-area", type=float, default=None, help="小面剔除阈值 m²")
    v.add_argument("--threshold", type=int, default=None, help="二值化亮度阈值")
    v.add_argument("--simplify", type=float, default=None,
                   help="道路中心线抽稀容差（像素，0=禁用；默认 0.7）")
    v.add_argument("--floor-step", type=int, default=None,
                   help="层数换算步长（kind=floors 时高度区间换算用，"
                        "须与 floors 命令的 --floor-step 一致；默认 3）")
    v.set_defaults(func=cmd_vectorize)

    d = sub.add_parser("doctor", help="环境自检")
    d.set_defaults(func=cmd_doctor)

    g = sub.add_parser("gui", help="启动 GUI（PySide6 + Leaflet 预览）")
    g.set_defaults(func=cmd_gui)

    return p


def _ensure_gdal_data() -> None:
    """conda 环境未激活时 GDAL_DATA 缺失，GDAL 初始化会输出
    "Cannot find gdalvrt.xsd" 警告（无害但每次出现）。
    须在 rasterio/GDAL 首次初始化前调用，按常见布局探测并补设。
    """
    import os
    import sys
    from pathlib import Path

    if os.environ.get("GDAL_DATA"):
        return
    # conda 布局优先（不 import rasterio，避免过早触发 GDAL 初始化）；
    # pip wheel 布局的 rasterio 自带 gdal_data，无需处理。
    for d in (Path(sys.prefix) / "Library" / "share" / "gdal",  # conda (win)
              Path(sys.prefix) / "share" / "gdal"):             # conda (linux/mac)
        if (d / "gdalvrt.xsd").exists():
            os.environ["GDAL_DATA"] = str(d)
            return


def main(argv: list[str] | None = None) -> int:
    _ensure_gdal_data()
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s")
    try:
        return args.func(args)
    except (ValueError, RuntimeError, PermissionError, FileNotFoundError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
