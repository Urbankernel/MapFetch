"""配置加载与 CLI 入口测试。"""

import pytest

from mapfetch.config import load_config


class TestConfig:
    def test_load_default(self, hermetic_cfg):
        cfg = load_config(hermetic_cfg)
        assert set(cfg.sources) == {"tianditu", "amap", "baidu"}
        assert cfg.default_source() == "tianditu"  # 

    def test_rate_limits_baseline(self, hermetic_cfg):
        cfg = load_config(hermetic_cfg)
        rl = cfg.limits.rate_limits
        # 保守基线
        assert rl["tianditu"].qps == 1 and rl["tianditu"].daily_budget == 8000
        assert rl["amap_tile"].qps == 2 and rl["amap_tile"].max_concurrent == 4

    def test_thresholds(self, hermetic_cfg):
        cfg = load_config(hermetic_cfg)
        assert cfg.limits.task_thresholds.max_tiles == 50000
        assert cfg.limits.cache.expire_days == 30

    def test_masked_key(self, hermetic_cfg, monkeypatch):
        cfg = load_config(hermetic_cfg)
        monkeypatch.setenv("MAPFETCH_TK_TIANDITU", "abcdef123456")
        assert cfg.masked_key("tianditu") == "abcd****"
        monkeypatch.delenv("MAPFETCH_TK_TIANDITU")
        assert cfg.masked_key("tianditu") == "<未配置>"


class TestCli:
    def test_version(self, capsys):
        from mapfetch.ui.cli import main
        with pytest.raises(SystemExit) as e:
            main(["--version"])
        assert e.value.code == 0
        assert "mapfetch" in capsys.readouterr().out

    def test_parse_levels(self):
        from mapfetch.ui.cli import parse_levels
        assert parse_levels("15-17") == [15, 16, 17]
        assert parse_levels("14,16,18") == [14, 16, 18]

    def test_parse_aoi_rect(self):
        from argparse import Namespace

        from mapfetch.ui.cli import parse_aoi
        aoi = parse_aoi(Namespace(aoi="rect:120.30,36.05,120.42,36.12", config=None))
        assert aoi.bbox == (120.30, 36.05, 120.42, 36.12)

    def test_bad_aoi_spec(self):
        from argparse import Namespace

        from mapfetch.ui.cli import parse_aoi
        with pytest.raises(ValueError):
            parse_aoi(Namespace(aoi="foo:bar", config=None))

    def test_doctor_runs(self, capsys):
        from mapfetch.ui.cli import main
        code = main(["doctor"])
        out = capsys.readouterr().out
        assert "Python" in out
        assert code in (0, 1)  # 取决于本机环境，但必须能跑完
