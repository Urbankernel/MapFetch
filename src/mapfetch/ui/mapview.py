"""地图预览控件（QtWebEngine + Leaflet；R-4 合规设计）。

- 在线静态底图默认开启（app.py 勾选框默认勾选），取消勾选可回退本地缓存瓦片；
- AOI 交互：Leaflet 矩形/多边形绘制，结果经 runJavaScript 回传 Python。
"""

from __future__ import annotations

import json
import re
import threading
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ASSETS = Path(__file__).resolve().parent / "assets"

# ---- 在线瓦片的性能基础设施（进程级单例，线程安全）----
# 1) 连接池：复用 TCP+TLS 连接。此前每张瓦片新建连接，TLS 握手 200~500ms/张，
#    拖动一次几十张串行握手是卡顿主因；复用后单张降到 ~100ms 内。
# 2) 上游并发信号量：浏览器对同 host 最多 6 并发，这里对齐，防止线程堆积触发
#    天地图限流（限流表现为瓦片批量失败 → errorTileUrl 双倍请求 → 更卡）。
# 3) 内存 LRU 瓦片缓存：回看已浏览区域零网络、零磁盘 IO，拖动不卡。
_UPSTREAM_SEMAPHORE = threading.BoundedSemaphore(6)
_TILE_CACHE: "OrderedDict[str, bytes]" = OrderedDict()
_TILE_CACHE_LOCK = threading.Lock()
_TILE_CACHE_MAX_ITEMS = 400  # ≈ 8~10MB
_HTTP_CLIENT = None
_HTTP_CLIENT_LOCK = threading.Lock()


def _get_http_client():
    """天地图瓦片下载客户端（懒加载；httpx.Client 官方支持跨线程共享）。"""
    global _HTTP_CLIENT
    with _HTTP_CLIENT_LOCK:
        if _HTTP_CLIENT is None:
            import httpx
            _HTTP_CLIENT = httpx.Client(
                limits=httpx.Limits(max_connections=8,
                                    max_keepalive_connections=8),
                timeout=10.0,
                headers={"Referer": "https://lbs.tianditu.gov.cn/",
                         "User-Agent": "mapfetch/0.1 (GUI online preview)"})
        return _HTTP_CLIENT


def _tile_cache_get(key: str):
    with _TILE_CACHE_LOCK:
        if key in _TILE_CACHE:
            _TILE_CACHE.move_to_end(key)
            return _TILE_CACHE[key]
    return None


def _tile_cache_put(key: str, data: bytes) -> None:
    with _TILE_CACHE_LOCK:
        _TILE_CACHE[key] = data
        _TILE_CACHE.move_to_end(key)
        while len(_TILE_CACHE) > _TILE_CACHE_MAX_ITEMS:
            _TILE_CACHE.popitem(last=False)


def _make_tile_handler(html: str, cache_root: Path, tk: str):
    """本地瓦片代理服务的请求处理器（纯 Python，不碰 Qt 对象，线程安全）。

    页面与瓦片统一到同源 http://127.0.0.1，规避 QtWebEngine 对 file:// 页面
    的各种拦截（Leaflet / 本地瓦片 / 在线瓦片全部同源加载）：
      /                → 内联 Leaflet 的预览页
      /cache/{src}/{style}/{z}/{x}/{y}.png  → 本地缓存瓦片（读文件， 目录约定）
      /online/{style}/{z}/{x}/{y}.png       → 在线天地图瓦片（urllib 转发，带 Referer）
      /static?center=..&zoom=..&width=..&height=..&layers=..
                                            → 天地图静态地图（staticimage 单张整图，带 Referer）
    """
    cache_root = cache_root.resolve()

    class _Handler(BaseHTTPRequestHandler):
        # HTTP/1.1 + Content-Length → 浏览器到本地服务的连接复用（keep-alive）
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):  # 静音访问日志
            pass

        def _send(self, data: bytes, ctype: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _fail(self, code: int = 404) -> None:
            try:
                self.send_response(code)
                self.send_header("Content-Length", "0")
                self.end_headers()
            except Exception:
                pass

        def do_GET(self):  # noqa: N802 - http.server 约定方法名
            raw = self.path
            path = raw.split("?", 1)[0]
            try:
                if path in ("/", "/mapview.html"):
                    self._send(html.encode("utf-8"), "text/html; charset=utf-8")
                    return
                if path.startswith("/cache/"):
                    cached = _tile_cache_get(path)  # 本地瓦片也走内存缓存
                    if cached is not None:
                        self._send(cached, "image/png")
                        return
                    rel = path[len("/cache/"):]
                    p = (cache_root / rel).resolve()
                    # 防路径穿越：必须落在缓存目录内且是文件
                    if not str(p).startswith(str(cache_root)) or not p.is_file():
                        self._fail()
                        return
                    data = p.read_bytes()
                    _tile_cache_put(path, data)
                    self._send(data, "image/png")
                    return
                if path.startswith("/online/"):
                    cached = _tile_cache_get(path)
                    if cached is not None:
                        self._send(cached, "image/png")
                        return
                    tail = path[len("/online/"):].rsplit(".png", 1)[0]
                    style, z, x, y = tail.split("/")
                    sub = str(int(x) % 8)  # 子域轮询（官方负载均衡）
                    real = (f"https://t{sub}.tianditu.gov.cn/DataServer"
                            f"?T={style}&x={x}&y={y}&l={z}&tk={tk}")
                    with _UPSTREAM_SEMAPHORE:  # 限制上游并发，防限流
                        resp = _get_http_client().get(real)
                    if resp.status_code != 200:
                        self._fail(resp.status_code)
                        return
                    data = resp.content
                    ctype = resp.headers.get("content-type", "image/png")
                    _tile_cache_put(path, data)
                    self._send(data, ctype)
                    return
                if path.startswith("/static"):
                    # 天地图静态地图服务（staticimage）：一次请求返回整张 PNG，
                    # 相比逐瓦片拼接显著加快加载。参数由前端按当前视口计算，
                    # 经本地代理转发（tk 留在服务端，不进前端）。
                    q = parse_qs(urlparse(raw).query)
                    layers = q.get("layers", ["vec_c,cva_c"])[0]
                    center = q.get("center", [""])[0]
                    zoom = q.get("zoom", [""])[0]
                    width = q.get("width", [""])[0]
                    height = q.get("height", [""])[0]
                    # 白名单校验（本地回环，仍按最小信任处理，防注入/异常参数）
                    if (not re.fullmatch(r"[a-z0-9_,]+", layers)
                            or not re.fullmatch(r"-?\d+(\.\d+)?,-?\d+(\.\d+)?", center)
                            or not re.fullmatch(r"\d{1,2}", zoom)
                            or not re.fullmatch(r"\d{1,4}", width)
                            or not re.fullmatch(r"\d{1,4}", height)):
                        self._fail(400)
                        return
                    real = (f"https://api.tianditu.gov.cn/staticimage"
                            f"?center={center}&zoom={zoom}"
                            f"&width={width}&height={height}"
                            f"&layers={layers}&tk={tk}")
                    with _UPSTREAM_SEMAPHORE:  # 静态图同样限制上游并发，防限流
                        resp = _get_http_client().get(real)
                    if resp.status_code != 200:
                        self._fail(resp.status_code)
                        return
                    data = resp.content
                    ctype = resp.headers.get("content-type", "image/png")
                    self._send(data, ctype)
                    return
                self._fail()
            except Exception:
                self._fail()

    return _Handler


def _start_tile_server(html: str, cache_root: Path, tk: str):
    """启动本地瓦片代理服务（daemon 线程，127.0.0.1 随机端口），返回 (server, port)。"""
    handler = _make_tile_handler(html, cache_root, tk)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, server.server_address[1]


try:  # 模块级只依赖 QtWidgets；QtWebEngine（Addons）延迟到 __init__ 再导入
    from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget
    _QT_WIDGETS_OK = True
except Exception:  # 未安装 / DLL 加载失败：保证模块可导入，实例化时再报错
    _QT_WIDGETS_OK = False

    class QWidget:  # noqa: N801  占位基类，避免类定义阶段 NameError
        def __init__(self, *args, **kwargs):
            raise RuntimeError("PySide6 不可用，MapView 需要 GUI 环境")


class MapView(QWidget):
    """真正的 QWidget 子类（PySide6 的 addWidget 做类型检查，鸭子类型不生效）。

    内部持有一个 QWebEngineView 承载 Leaflet；若环境缺 QtWebEngine，
    退化为提示标签，GUI 其余功能仍可用。
    """

    def __init__(self, parent=None, *, cache_root: str, online: bool = False,
                 source: str = "tianditu", style: str = "vec_w", tk: str = "",
                 basemap_style: str = "vec_w", basemap_fallback: str = "img_w"):
        super().__init__(parent)
        from PySide6.QtCore import QUrl

        self._cache_root = Path(cache_root)
        self._online = online
        self._source = source
        self._style = style
        self._basemap_style = basemap_style
        self._basemap_fallback = basemap_fallback
        self._view = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)

        try:
            from PySide6.QtWebEngineWidgets import QWebEngineView
        except ImportError:
            lay.addWidget(QLabel(
                "未检测到 QtWebEngine（PySide6-Addons），地图预览不可用。\n"
                "AOI 仍可用「手动输入规格」或「行政区」两种方式。\n"
                "修复：pip install \"PySide6==6.8.3\""))
            return

        self._view = QWebEngineView(self)
        lay.addWidget(self._view)
        html = (ASSETS / "mapview.html").read_text(encoding="utf-8")
        # 内联 Leaflet（css/js）：页面经本地服务以 http 加载，不依赖
        # setHtml 的 file:// baseUrl 解析相对资源，彻底可靠。
        css = (ASSETS / "leaflet" / "leaflet.css").read_text(encoding="utf-8")
        js = (ASSETS / "leaflet" / "leaflet.js").read_text(encoding="utf-8")
        html = html.replace('<link rel="stylesheet" href="leaflet/leaflet.css">',
                            "<style>\n" + css + "\n</style>", 1)
        html = html.replace('<script src="leaflet/leaflet.js"></script>',
                            "<script>\n" + js + "\n</script>", 1)
        minz, maxz = self._cache_zoom_range(source, style)
        # 注入配置：在线开关、当前源/样式、已缓存 zoom 范围、在线底图样式与降级样式
        # （minNativeZoom/maxNativeZoom，缩放时拉伸已有瓦片防空白）；
        # tk 与缓存路径留在服务端处理，不进前端（不在页面里暴露 key）。
        inject = (
            "<script>window.MAPFETCH_CONFIG = "
            + json.dumps({
                "online": online,
                "source": source,
                "style": style,
                "minNativeZoom": minz,
                "maxNativeZoom": maxz,
                "basemapStyle": basemap_style,
                "basemapFallback": basemap_fallback,
            }, ensure_ascii=False)
            + ";</script>"
        )
        html = html.replace("</head>", inject + "</head>", 1)
        # 启动本地瓦片代理服务（同源 http://127.0.0.1；tk 不进前端）
        self._server, port = _start_tile_server(html, self._cache_root, tk)
        self._view.load(QUrl(f"http://127.0.0.1:{port}/"))

    def closeEvent(self, event) -> None:
        srv = getattr(self, "_server", None)
        if srv is not None:
            srv.shutdown()
            srv.server_close()
            self._server = None
        super.closeEvent(event)

    def _cache_zoom_range(self, source: str, style: str):
        """扫描缓存目录，返回该源/样式已缓存的 zoom 层级范围 (min, max)，无则 (None, None)。"""
        base = self._cache_root.resolve() / source / style
        zooms = []
        if base.is_dir():
            for d in base.iterdir():
                if d.is_dir() and d.name.isdigit():
                    zooms.append(int(d.name))
        return (min(zooms), max(zooms)) if zooms else (None, None)

    def set_layer(self, source: str, style: str) -> None:
        """数据源/图层样式变化时联动本地缓存层与在线层 URL。"""
        self._source = source
        self._style = style
        if self._view is None:
            return
        minz, maxz = self._cache_zoom_range(source, style)
        self._view.page().runJavaScript(
            f"window.mapfetchSetLayer({json.dumps(source)}, {json.dumps(style)}, "
            f"{json.dumps(minz)}, {json.dumps(maxz)});")

    def set_online(self, on: bool) -> None:
        self._online = on
        if self._view is None:
            return
        self._view.page().runJavaScript(f"window.mapfetchSetOnline({str(on).lower()});")

    def get_aoi_geojson(self, callback) -> None:
        """从 Leaflet 取回当前 AOI（GeoJSON Polygon，WGS84）。"""
        if self._view is None:
            callback(None)
            return
        self._view.page().runJavaScript(
            "JSON.stringify(window.mapfetchGetAOI ? "
            "window.mapfetchGetAOI() : null);", callback)

    def get_aoi_state(self, callback) -> None:
        """取回当前绘制状态 {mode, geometry}（绘制完成后自动捕获 AOI 用）。"""
        if self._view is None:
            callback(None)
            return
        self._view.page().runJavaScript(
            "JSON.stringify(window.mapfetchGetAOIState ? "
            "window.mapfetchGetAOIState() : null);", callback)

    def set_aoi_from_bbox(self, bbox) -> None:
        if self._view is None:
            return
        min_lon, min_lat, max_lon, max_lat = bbox
        js = (f"window.mapfetchSetRect({min_lon}, {min_lat}, "
              f"{max_lon}, {max_lat});")
        self._view.page().runJavaScript(js)

    def set_aoi_geometry(self, geojson: dict) -> None:
        """在地图上显示一个 AOI 几何（手动输入 / 行政区解析后调用）。"""
        if self._view is None:
            return
        self._view.page().runJavaScript(
            "window.mapfetchSetGeometry(" + json.dumps(geojson) + ");")

    def set_draw_enabled(self, enabled: bool) -> None:
        """启用/禁用地图框选与绘制（仅「地图绘制范围」模式下启用）。"""
        if self._view is None:
            return
        self._view.page().runJavaScript(
            f"window.mapfetchSetDrawEnabled({str(enabled).lower()});")
