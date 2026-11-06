"""数据源适配层（插件式，docs/data-sources.md §5）。"""

from .base import SourceAdapter
from .amap import AmapAdapter
from .baidu import BaiduAdapter
from .tianditu import TiandituAdapter

_ADAPTERS = {
    "tianditu": TiandituAdapter,
    "amap": AmapAdapter,
    "baidu": BaiduAdapter,
}


def create_adapter(name: str, cfg) -> SourceAdapter:
    """按名称实例化适配器（cfg 为 config.loader.SourceConfig）。"""
    if name not in _ADAPTERS:
        raise ValueError(f"未知数据源: {name!r}，可用: {list(_ADAPTERS)}")
    return _ADAPTERS[name](cfg)


__all__ = ["SourceAdapter", "TiandituAdapter", "AmapAdapter", "BaiduAdapter", "create_adapter"]
