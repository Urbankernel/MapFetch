"""配置加载与校验（docs/configuration.md）。

- sources.yaml：数据源注册、URL 模板、图层枚举、署名文本
- limits.yaml：限速与配额（保守基线）
- keys.txt：用户密钥（key=value 文本格式，规则见 docs/configuration.md §3）

启动时 Pydantic 全量校验，参数错误在启动即暴露（技术选型）。
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator

# 仓库内置默认配置目录（随仓库分发；测试用它复制模板文件）
DEFAULT_CONFIG_DIR = Path(__file__).resolve().parents[3] / "configs"


def default_config_dir() -> Path:
    """运行时配置目录：环境变量 MAPFETCH_CONFIG_DIR > 当前工作目录 configs/ > 包内默认。

    pip install . 后 __file__ 指向 site-packages，parents[3] 不再指向仓库；
    故优先取当前工作目录下的 configs/（用户 cd 到仓库/工作目录即可），
    最后回退到随仓库分发的内置 configs（开发/可编辑安装场景）。
    """
    env = os.environ.get("MAPFETCH_CONFIG_DIR")
    if env:
        return Path(env)
    cwd_configs = Path.cwd() / "configs"
    if cwd_configs.is_dir():
        return cwd_configs
    return DEFAULT_CONFIG_DIR


class StyleConfig(BaseModel):
    name: str
    url: str
    max_level: int = 18
    min_level: int = 1


class SourceConfig(BaseModel):
    enabled: bool = True
    default: bool = False
    crs: str                             # GCJ02 / BD09MC / CGCS2000
    key_env: str | None = None           # 环境变量名（备选方式）
    rest_key_env: str | None = None      # REST API 专用 Key 的环境变量名（如高德 Web 服务 Key）
    subdomains: list[str] = Field(default_factory=list)
    styles: dict[str, StyleConfig] = Field(default_factory=dict)
    rest: dict[str, str] = Field(default_factory=dict)
    render_templates: list[str] = Field(default_factory=list)
    # 引擎 B 渲染底图样式（rd/water/green 模板的 mapStyle）——由 load_config 从
    # keys.txt 的 amap_mapstyle 注入（样式 ID 配置于 sources.yaml）。免费版
    # 自定义地图只能发布 1 个样式，样式 ID 保持不变；每次根据要获取的要素在高德
    # 控制台改样式设置即可。留空时用模板内置默认。
    render_mapstyle: str = ""
    attribution: str = ""
    # 浏览器端 Key 的 Referer 白名单域名（天地图 code 301012：服务端直连必须带
    # Referer，且需与控制台配置的白名单一致）。服务端 Key 场景留空。
    referer: str = ""
    # 在线预览底图样式与降级样式（浏览/框选 AOI 用，独立于抓取 styles）：
    # 在线底图走天地图静态地图服务（staticimage，单张整图、加载快），默认用
    # preview_style（矢量），加载失败时按 preview_fallback_style（影像）降级
    # （见 sources.yaml 与 mapview.html 的 showStatic error 降级逻辑）。
    preview_style: str = "vec_w"
    preview_fallback_style: str = "img_w"
    # 由 load_config 从 keys.txt 注入（配置文件直填方式，优先于环境变量）
    key_direct: str = ""
    rest_key_direct: str = ""
    # 高德 JS API 2.0 安全密钥（securityJsCode，2021-12 后申请的 Key 自定义样式必需）
    security_direct: str = ""
    security_env: str | None = None

    def _resolve(self, direct: str, env_name: str | None, purpose: str) -> str:
        """Key 解析顺序（docs/installation.md §4）：
        1. keys.txt 直填值（非空）
        2. 环境变量
        3. 报错（含两种配置方式的提示）
        """
        if direct.strip():
            return direct.strip()
        if env_name:
            key = os.environ.get(env_name, "").strip()
            if key:
                return key
        raise RuntimeError(
            f"缺少{purpose}：请在 configs/keys.txt 中直接填写，"
            f"或设置环境变量 {env_name or '（见 sources.yaml key_env）'}"
            "（申请方式见 docs/installation.md §4）"
        )

    def get_key(self) -> str:
        """主 Key（瓦片/JS API 用）。"""
        if not self.key_env and not self.key_direct:
            return ""
        return self._resolve(self.key_direct, self.key_env, f"数据源密钥（{self.crs} 源）")

    def get_rest_key(self) -> str:
        """REST API Key（如高德 Web 服务 Key，行政区查询用）。

        未配置 REST Key（直填与环境变量均无）时回退主 Key
        （keys.txt 注释：两种 Key 类型不可混用，回退可能被平台拒绝，
        建议单独配置 amap_rest）。
        """
        import os as _os
        has_rest = bool(self.rest_key_direct.strip()) or bool(
            self.rest_key_env and _os.environ.get(self.rest_key_env, "").strip())
        if has_rest:
            return self._resolve(self.rest_key_direct, self.rest_key_env,
                                 "REST API 密钥")
        return self.get_key()

    def get_security_js_code(self) -> str:
        """高德 JS API 2.0 安全密钥（securityJsCode）。

        2021-12 之后申请的 Key 必配：不配置时**自定义样式不生效**
        （官方预置样式不受影响）。解析顺序：keys.txt 直填 → 环境变量。
        """
        import os as _os
        if self.security_direct.strip():
            return self.security_direct.strip()
        if self.security_env:
            v = _os.environ.get(self.security_env, "").strip()
            if v:
                return v
        return _os.environ.get("MAPFETCH_SECURITY_AMAP", "").strip()


class RateLimitConfig(BaseModel):
    qps: float
    burst: int
    max_concurrent: int
    daily_budget: int


class RetryConfig(BaseModel):
    max_attempts: int = 4
    backoff_base_sec: float = 1.0
    backoff_max_sec: float = 60.0
    jitter: float = 0.3


class CircuitBreakerConfig(BaseModel):
    warn_at: float = 0.8
    stop_at: float = 1.0
    consecutive_429_threshold: int = 5


class TaskThresholds(BaseModel):
    max_tiles: int = 50000
    max_duration_hours: float = 4.0
    max_disk_gb: float = 20.0


class CacheConfig(BaseModel):
    dir: str = "cache/tiles"
    expire_days: int = 30


class LimitsConfig(BaseModel):
    rate_limits: dict[str, RateLimitConfig]
    retry: RetryConfig = RetryConfig()
    circuit_breaker: CircuitBreakerConfig = CircuitBreakerConfig()
    task_thresholds: TaskThresholds = TaskThresholds()
    cache: CacheConfig = CacheConfig()
    allow_rate_increase: bool = True  # 上调需二次确认并写日志（强制约束 2）


class AppConfig(BaseModel):
    sources: dict[str, SourceConfig]
    limits: LimitsConfig
    keys: dict[str, str] = Field(default_factory=dict)

    @field_validator("sources")
    @classmethod
    def _exactly_one_default(cls, v: dict[str, SourceConfig]) -> dict[str, SourceConfig]:
        defaults = [k for k, s in v.items() if s.default and s.enabled]
        if len(defaults) > 1:
            raise ValueError(f"默认数据源只能有一个，当前: {defaults}")
        return v

    def default_source(self) -> str:
        for k, s in self.sources.items():
            if s.default and s.enabled:
                return k
        for k, s in self.sources.items():
            if s.enabled:
                return k
        raise RuntimeError("没有可用的数据源")

    def masked_key(self, source: str) -> str:
        """日志脱敏：仅显示前 4 位（docs/configuration.md §3 规则）。"""
        try:
            key = self.sources[source].get_key()
        except RuntimeError:
            return "<未配置>"
        return key[:4] + "****" if len(key) > 4 else "****"


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}")
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _load_txt(path: Path) -> dict:
    """解析 keys.txt（key=value 文本格式，支持 # 注释与空行跳过）。

    不存在时返回空 dict（视为未配置密钥）。
    """
    if not path.exists():
        return {}
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        result[key.strip()] = value.strip()
    return result


def _parse_key_value(raw: str | None) -> tuple[str, str | None]:
    """解析 keys.txt 的值，返回 (直填 Key, 环境变量名或 None)。

    支持三种写法（docs/installation.md §4）：
      ""                -> 未配置（回退 sources.yaml 的 key_env）
      "env:VAR_NAME"    -> 从环境变量 VAR_NAME 读取
      "abc123..."       -> 直接填写的 Key
    """
    if not raw:
        return "", None
    raw = raw.strip()
    if raw.lower().startswith("env:"):
        return "", raw[4:].strip()
    return raw, None


def load_config(config_dir: str | Path | None = None) -> AppConfig:
    """加载 sources.yaml + limits.yaml + keys.txt 并整体校验（启动即暴露参数错误）。

    keys.txt 为用户密钥（key=value 文本格式，由 keys.txt.example 复制而来）；
    不存在时视为未配置（运行时按需报错）。
    """
    d = Path(config_dir) if config_dir else default_config_dir()
    sources_raw = _load_yaml(d / "sources.yaml").get("sources", {})
    limits_raw = _load_yaml(d / "limits.yaml")
    keys_raw = _load_txt(d / "keys.txt")

    sources = {k: SourceConfig(**v) for k, v in sources_raw.items()}

    # 注入直填 Key（映射规则见 docs/installation.md §4 表格）
    _KEY_MAP = {  # source_name -> (主 Key 字段, REST Key 字段)
        "tianditu": ("tianditu", None),
        "amap": ("amap_js", "amap_rest"),
        "baidu": ("baidu", None),
    }
    for name, (main_field, rest_field) in _KEY_MAP.items():
        if name not in sources:
            continue
        direct, env = _parse_key_value(keys_raw.get(main_field))
        if direct:
            sources[name].key_direct = direct
        if env:
            sources[name].key_env = env
        if rest_field:
            rest_direct, rest_env = _parse_key_value(keys_raw.get(rest_field))
            if rest_direct:
                sources[name].rest_key_direct = rest_direct
            if rest_env:
                sources[name].rest_key_env = rest_env

    # 高德 JS API 2.0 安全密钥（securityJsCode；自定义样式必需）
    if "amap" in sources:
        sec_direct, sec_env = _parse_key_value(keys_raw.get("amap_security"))
        if sec_direct:
            sources["amap"].security_direct = sec_direct
        if sec_env:
            sources["amap"].security_env = sec_env

    # 高德自定义地图样式 ID（引擎 B 渲染模板 rd/water/green 的 mapStyle）。
    # 配置于 keys.txt。免费版只能发布 1 个样式，样式 ID
    # 保持不变；每次根据要获取的要素在高德控制台改样式设置（图层显隐/颜色）即可。
    # 留空 = 回退各模板内置默认。
    if "amap" in sources:
        style_direct, style_env = _parse_key_value(keys_raw.get("amap_mapstyle"))
        if style_direct:
            sources["amap"].render_mapstyle = style_direct
        elif style_env:
            sources["amap"].render_mapstyle = os.environ.get(style_env, "")

    return AppConfig(sources=sources, limits=LimitsConfig(**limits_raw),
                     keys=keys_raw)
