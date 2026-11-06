"""pytest 配置：把 src/ 加入导入路径（无需安装即可跑测试）。

hermetic_cfg 夹具：复制仓库配置到临时目录，不创建 keys.txt——保证测试
不受本机真实 Key 污染（可重复、可 CI）。
"""

import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mapfetch.config.loader import DEFAULT_CONFIG_DIR  # noqa: E402


@pytest.fixture()
def hermetic_cfg(tmp_path):
    """消毒后的配置目录：无 keys.txt（无真实 Key）。"""
    for f in ("sources.yaml", "limits.yaml"):
        shutil.copy(DEFAULT_CONFIG_DIR / f, tmp_path / f)
    # 不创建 keys.txt：load_config 视为未配置，回退 sources.yaml 的 key_env 环境变量
    return tmp_path
