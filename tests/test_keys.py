"""Key 配置解析测试（直填 / env: / 留空回退）。

对应 docs/installation.md §4 的解析顺序：
keys.txt 直填 → env: 环境变量 → sources.yaml key_env → 报错
"""

import shutil
from pathlib import Path

import pytest

from mapfetch.config import load_config
from mapfetch.config.loader import DEFAULT_CONFIG_DIR, _parse_key_value


@pytest.fixture()
def cfg_dir(tmp_path):
    """复制仓库配置到临时目录，避免污染真实配置。

    keys.txt 为用户密钥文件（不入库），测试在临时目录里自行创建，
    保证测试隔离（与 conftest.hermetic_cfg 的处理一致）。
    """
    for f in ("sources.yaml", "limits.yaml"):
        shutil.copy(DEFAULT_CONFIG_DIR / f, tmp_path / f)
    # 默认无 keys.txt（load_config 视为未配置）
    return tmp_path


class TestParseKeyValue:
    def test_empty(self):
        assert _parse_key_value("") == ("", None)
        assert _parse_key_value(None) == ("", None)

    def test_direct(self):
        assert _parse_key_value("abc123") == ("abc123", None)
        assert _parse_key_value("  abc123  ") == ("abc123", None)

    def test_env_prefix(self):
        assert _parse_key_value("env:MY_VAR") == ("", "MY_VAR")


class TestKeyResolution:
    def test_direct_key_wins(self, cfg_dir, monkeypatch):
        (cfg_dir / "keys.txt").write_text("tianditu=direct_key_123\n", encoding="utf-8")
        monkeypatch.setenv("MAPFETCH_TK_TIANDITU", "env_key_456")
        cfg = load_config(cfg_dir)
        assert cfg.sources["tianditu"].get_key() == "direct_key_123"

    def test_env_fallback(self, cfg_dir, monkeypatch):
        monkeypatch.setenv("MAPFETCH_TK_TIANDITU", "env_key_456")
        cfg = load_config(cfg_dir)  # 无 keys.txt
        assert cfg.sources["tianditu"].get_key() == "env_key_456"

    def test_env_prefix_in_txt(self, cfg_dir, monkeypatch):
        (cfg_dir / "keys.txt").write_text("tianditu=env:CUSTOM_TK\n", encoding="utf-8")
        monkeypatch.setenv("CUSTOM_TK", "custom_789")
        monkeypatch.delenv("MAPFETCH_TK_TIANDITU", raising=False)
        cfg = load_config(cfg_dir)
        assert cfg.sources["tianditu"].get_key() == "custom_789"

    def test_missing_key_error_message(self, cfg_dir, monkeypatch):
        monkeypatch.delenv("MAPFETCH_TK_TIANDITU", raising=False)
        cfg = load_config(cfg_dir)
        with pytest.raises(RuntimeError, match="keys.txt"):
            cfg.sources["tianditu"].get_key()

    def test_amap_rest_key_separate(self, cfg_dir):
        (cfg_dir / "keys.txt").write_text(
            "amap_js=js_key\namap_rest=rest_key\n", encoding="utf-8")
        cfg = load_config(cfg_dir)
        assert cfg.sources["amap"].get_key() == "js_key"
        assert cfg.sources["amap"].get_rest_key() == "rest_key"

    def test_amap_rest_fallback_to_main(self, cfg_dir):
        (cfg_dir / "keys.txt").write_text("amap_js=js_key\n", encoding="utf-8")
        cfg = load_config(cfg_dir)
        assert cfg.sources["amap"].get_rest_key() == "js_key"

    def test_masked_direct_key(self, cfg_dir):
        (cfg_dir / "keys.txt").write_text("tianditu=abcdef123456\n", encoding="utf-8")
        cfg = load_config(cfg_dir)
        assert cfg.masked_key("tianditu") == "abcd****"  # 日志脱敏仅前 4 位
