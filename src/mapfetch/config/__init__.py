"""配置加载（docs/configuration.md）：YAML + Pydantic 校验。"""

from .loader import AppConfig, load_config

__all__ = ["AppConfig", "load_config"]
