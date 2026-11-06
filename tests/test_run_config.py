"""任务配置文件（-c/--config）合并逻辑测试：fetch/render/floors/vectorize 四命令统一。

覆盖：common 节继承、命令行优先、内置默认兜底、旧平铺写法废弃、vectorize 自动定位。
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pytest
import yaml

from mapfetch.ui.cli import (
    _resolve_fetch_args,
    _resolve_floors_args,
    _resolve_render_args,
    _resolve_vectorize_args,
)


def _write_run(tmp_path, yml: dict) -> str:
    p = tmp_path / "run.yaml"
    p.write_text(yaml.safe_dump(yml), encoding="utf-8")
    return str(p)


def _ns(run_config, **kw):
    return argparse.Namespace(run_config=run_config, **kw)


class TestFetchResolve:
    def test_reads_common_and_fetch_section(self, tmp_path):
        cfg = _write_run(tmp_path, {
            "common": {"aoi": "admin:310109", "out": "run/demo", "project": "demo", "yes": True},
            "fetch": {"source": "amap", "style": "road", "levels": [15, 16, 17]},
        })
        args = _ns(cfg, aoi=None, source=None, style=None, levels=None, out=None,
                   project=None, output_crs=None, no_mosaic=False, yes=False)
        _resolve_fetch_args(args)
        assert args.aoi == "admin:310109"
        assert args.source == "amap"
        assert args.style == "road"
        assert args.levels == "15,16,17"
        assert args.out == "run/demo"
        assert args.project == "demo"
        assert args.yes is True
        assert args.output_crs == "EPSG:4490"  # 内置默认

    def test_cli_overrides_yaml(self, tmp_path):
        cfg = _write_run(tmp_path, {
            "common": {"aoi": "admin:310109"},
            "fetch": {"source": "tianditu", "style": "img_w", "levels": "15", "out": "run/a"},
        })
        args = _ns(cfg, aoi=["rect:1,2,3,4"], source="baidu", style="street",
                   levels="16", out="run/b", project=None, output_crs=None,
                   no_mosaic=False, yes=False)
        _resolve_fetch_args(args)
        assert args.aoi == "rect:1,2,3,4"       # 命令行 list 拼回字符串
        assert args.source == "baidu"
        assert args.out == "run/b"

    def test_flat_legacy_rejected(self, tmp_path):
        # 旧平铺写法（顶层 aoi/source/...）已废弃，不再被识别
        cfg = _write_run(tmp_path, {"aoi": "admin:1", "source": "tianditu",
                                    "style": "img_w", "levels": "15", "out": "run/d"})
        args = _ns(cfg, aoi=None, source=None, style=None, levels=None, out=None,
                   project=None, output_crs=None, no_mosaic=False, yes=False)
        with pytest.raises(ValueError):
            _resolve_fetch_args(args)


class TestRenderResolve:
    def test_zoom_from_yaml_and_builtin_defaults(self, tmp_path):
        cfg = _write_run(tmp_path, {
            "common": {"aoi": "circle:1,2,500", "out": "run/d", "project": "p"},
            "render": {"template": "rd", "zoom": 18},
        })
        args = _ns(cfg, aoi=None, template=None, zoom=None, window=None, wait=None,
                   threshold=None, out=None, project=None, reclassify=False,
                   no_merge=False, headless=False, yes=False)
        _resolve_render_args(args)
        assert args.template == "rd"
        assert args.zoom == 18          # YAML 值生效（argparse 默认已置 None）
        assert args.window == 2000      # 内置默认
        assert args.wait == 1.0
        assert args.project == "p"      # 继承 common


class TestFloorsResolve:
    def test_defaults_and_section(self, tmp_path):
        cfg = _write_run(tmp_path, {
            "common": {"aoi": "admin:1", "out": "run/d"},
            "floors": {"floor_max": 18},
        })
        args = _ns(cfg, aoi=None, floor_min=None, floor_max=None, floor_step=None,
                   wait=None, out=None, project=None, no_merge=False,
                   headless=False, yes=False)
        _resolve_floors_args(args)
        assert args.floor_min == 0
        assert args.floor_max == 18
        assert args.floor_step == 3


class TestVectorizeResolve:
    def test_auto_locate_input(self, tmp_path):
        cfg = _write_run(tmp_path, {
            "common": {"out": "run/demo", "project": "demo"},
            "vectorize": {"kind": "floors", "out": "run/demo/v.gpkg"},
        })
        args = _ns(cfg, input=None, kind=None, out=None, min_area=None,
                   threshold=None, simplify=None, floor_step=None)
        _resolve_vectorize_args(args)
        assert args.kind == "floors"
        assert Path(args.input) == Path("run/demo") / "raster" / "floors_z17" / "demo_floors.png"
        assert args.min_area == 10.0
        assert args.threshold == 100
        assert args.simplify == 0.7   # 内置默认

    def test_missing_input_and_out_root_raises(self, tmp_path):
        cfg = _write_run(tmp_path, {"vectorize": {"kind": "floors", "out": "x.gpkg"}})
        args = _ns(cfg, input=None, kind=None, out=None, min_area=None,
                   threshold=None, simplify=None, floor_step=None)
        with pytest.raises(ValueError):
            _resolve_vectorize_args(args)

    def test_unknown_kind_raises(self, tmp_path):
        cfg = _write_run(tmp_path, {"vectorize": {"kind": "nope", "out": "x.gpkg"}})
        args = _ns(cfg, input=None, kind=None, out=None, min_area=None,
                   threshold=None, simplify=None, floor_step=None)
        with pytest.raises(ValueError):
            _resolve_vectorize_args(args)
