"""引擎 B：JS API 渲染截图。

的核心机制：
- Selenium 4 + Firefox + geckodriver 控制 AMap JS API 2.0 页面
- 5 套 HTML 模板（rd/bld/water/green/bld3d）靠 features + mapStyle 分离要素
- 两阶段：逐窗 panBy 截图（阶段1）→ 分块拼接（阶段2，merge_size 4×4）
- 建筑层数扫描（模式 3）：3D 模式注入 AMap.Polygon(extrusionHeight=n)
  靠遮挡关系逐层判定

⚠️ getBounds 取坐标：优先公开属性 .lng/.lat，回退内部压缩属性
（kT/KL 内部参数 随时可能变化， 警示）。

重依赖（selenium/PIL/numpy）延迟导入：未安装时仅本模块不可用。
"""

from __future__ import annotations

import io
import logging
import os
import shutil
import string
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# 模板目录（HTML 模板集；随包分发）
TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

# 窗口尺寸基线
DEFAULT_WINDOW = 2000
FLOOR_SCAN_WINDOW = 500   # 模式 3 小窗口，减少透视影响
MERGE_SIZE = 4            # 4×4 分块拼接（merge_size）


@dataclass
class RenderParams:
    """渲染任务参数（命令集 w/z/t/f）。"""

    zoom: int = 17                    # z 命令：14–20，建筑需 ≥17
    wait_sec: float = 1.0             # t 命令：窗口移动后等待加载
    window_size: int = DEFAULT_WINDOW
    floor_min: int = 0                # f 命令：层数扫描范围（仅 bld3d）
    floor_max: int = 36
    floor_step: int = 3
    slow_content_extra: float = 1.5   # 模式 0/7 额外延迟修正

    def validate(self) -> None:
        if not 14 <= self.zoom <= 20:
            raise ValueError("缩放级别须在 14–20 之间")
        if self.floor_min < 0 or self.floor_max <= self.floor_min:
            raise ValueError("层数扫描范围非法：最低 ≥0 且最高 > 最低（f 命令）")
        if (self.floor_max - self.floor_min) % self.floor_step != 0:
            raise ValueError("层数间隔必须整除最高与最低之差（f 命令）")


# ---------------------------------------------------------------------------
# HTML 模板（提取的模板， 要素-样式映射表）
# 变量：$KEY / $CENTER_LON / $CENTER_LAT / $ZOOM / $EXTRUDE（仅 bld3d）
# 公开模板：rd / bld / water / green；bld3d 为 floors 内部专用。
# 所有模板用 CSS 隐藏 .amap-logo / .amap-copyright（DOM 覆盖层，钉在视口
# 左下/右下角）：它们浮在 WebGL 画布之上，每窗截图必然带上；拼接后在每个
# 窗口边界重复出现，且在 floors 镶嵌中「每层都可见且 >150」会被覆写成最高
# 层数档 → 污染成果。数据来源署名由 manifest.json attribution 字段承担
# （docs/compliance.md §4），不在截图中显示。
# ---------------------------------------------------------------------------
_TEMPLATES: dict[str, str] = {
    "rd": """<!doctype html><html><head><meta charset="utf-8">
<style>html,body,#c{width:100%;height:100%;margin:0}
.amap-logo,.amap-copyright,.amap-logo img{display:none!important}</style></head><body>
<div id="c"></div>
<script src="https://webapi.amap.com/maps?v=2.0&key=$KEY"></script>
<script>var map=new AMap.Map('c',{viewMode:'2D',zoom:$ZOOM,features:['road'],
mapStyle:'$MAPSTYLE',center:[$CENTER_LON,$CENTER_LAT]});
map.on('complete',function(){window.__mapReady=1;});</script>
</body></html>""",
    "bld": """<!doctype html><html><head><meta charset="utf-8">
<style>html,body,#c{width:100%;height:100%;margin:0}
.amap-logo,.amap-copyright,.amap-logo img{display:none!important}</style></head><body>
<div id="c"></div>
<script src="https://webapi.amap.com/maps?v=2.0&key=$KEY"></script>
<script>var map=new AMap.Map('c',{viewMode:'2D',zoom:$ZOOM,features:['building'],
mapStyle:'$MAPSTYLE',center:[$CENTER_LON,$CENTER_LAT]});
map.on('complete',function(){window.__mapReady=1;});</script>
</body></html>""",
    "bld3d": """<!doctype html><html><head><meta charset="utf-8">
<style>html,body,#c{width:100%;height:100%;margin:0}
.amap-logo,.amap-copyright,.amap-logo img{display:none!important}</style></head><body>
<div id="c"></div>
<script src="https://webapi.amap.com/maps?v=2.0&key=$KEY"></script>
<script>
var map=new AMap.Map('c',{viewMode:'3D',zoom:17,features:['building'],
center:[$CENTER_LON,$CENTER_LAT]});
map.on('complete',function(){window.__mapReady=1;});
function setProbe(h){ // 3D 遮挡法探针（模式 3）
  if(window._probe){map.remove(window._probe);}
  var b=map.getBounds();
  window._probe=new AMap.Polygon({
    path:[[b.southWest.lng,b.southWest.lat],[b.northEast.lng,b.southWest.lat],
          [b.northEast.lng,b.northEast.lat],[b.southWest.lng,b.northEast.lat]],
    fillColor:'#000000',extrusionHeight:h,roofColor:'#000000',
    wallColor:'#000000',strokeWeight:0});
  map.add(window._probe);
}
setProbe($EXTRUDE);
</script></body></html>""",
}

# 背景渲染 HTML（features:['bg'] + mapStyle 占位）—— water / green 共用；
# 渲染后由 cmd_render 按 RGB 色域分别二值化成独立图层（背景要素分离：
# water/green 两个单独内容）。mapStyle 用 keys.txt 的 amap_mapstyle（样式 ID
# 固定不变）：获取水系/绿地时在高德控制台改样式设置（如绿地显示、其余不显示、
# 用系统默认颜色），即可与 rd 一致地单独获取对应图层。
_BG_HTML = """<!doctype html><html><head><meta charset="utf-8">
<style>html,body,#c{width:100%;height:100%;margin:0}
.amap-logo,.amap-copyright,.amap-logo img{display:none!important}</style></head><body>
<div id="c"></div>
<script src="https://webapi.amap.com/maps?v=2.0&key=$KEY"></script>
<script>var map=new AMap.Map('c',{viewMode:'2D',zoom:$ZOOM,features:['bg'],
mapStyle:'$MAPSTYLE',center:[$CENTER_LON,$CENTER_LAT]});
map.on('complete',function(){window.__mapReady=1;});</script>
</body></html>"""

_TEMPLATES["water"] = _BG_HTML
_TEMPLATES["green"] = _BG_HTML


# 各模板的默认 mapStyle（keys.txt 的 amap_mapstyle 为空时回退到这里）。
# ⚠️ rd：官方预置 darkblue 的道路配色为暗色系，与背景对比度低（提取效果差）；
# 推荐在高德控制台「自定义地图」基于 darkblue 制作样式（背景纯黑、道路纯白，
# 发布后把样式 ID 填入 keys.txt 的 amap_mapstyle）。免费版只能发布 1 个样式，
# 样式 ID 保持不变；获取水系/绿地时改样式设置（如绿地显示、其余不显示、默认颜色）即可。
# bld：固定用官方预置 blue——免费版自定义地图无法设置建筑物图层，blue 的
# 建筑为亮色填充、对比度好；cmd_render 对 bld 不传自定义样式。
# water/green：未指定样式时回退 normal（默认配色，保留绿/蓝供色域分离）。
MAPSTYLE_DEFAULTS: dict[str, str] = {
    "rd": "amap://styles/darkblue",
    "bld": "amap://styles/blue",
}


def render_template(name: str, *, key: str, center_lon: float, center_lat: float,
                    zoom: int, extrude: float = 0.01,
                    mapstyle: str = "", security_js_code: str = "") -> str:
    """按模板生成 HTML（string.Template 变量替换， 模板生成层）。

    mapstyle：rd/water/green 模板的底图样式（来自 keys.txt 的 amap_mapstyle；
    bld 固定官方 blue、bld3d 为 3D 模式，不受此参数影响）；裸样式 ID 自动补
    amap://styles/ 前缀；留空时用该模板的内置默认。
    security_js_code：高德 JS API 2.0 安全密钥（2021-12 后申请的 Key 必配）——
    不配置时**自定义样式不生效**（官方预置样式不受影响）；须在 AMap 加载前声明。
    """
    if name not in _TEMPLATES:
        raise ValueError(f"未知模板: {name!r}，可用: {list(_TEMPLATES)}")
    style = (mapstyle or "").strip()
    if style and "://" not in style:
        style = "amap://styles/" + style  # 裸样式 ID 自动补协议前缀
    if not style:
        style = MAPSTYLE_DEFAULTS.get(name, "amap://styles/normal")
    html = string.Template(_TEMPLATES[name]).substitute(
        KEY=key, CENTER_LON=center_lon, CENTER_LAT=center_lat,
        ZOOM=zoom, EXTRUDE=extrude, MAPSTYLE=style,
    )
    sec = (security_js_code or "").strip().replace("'", "")
    if sec:
        marker = '<script src="https://webapi.amap.com/maps'
        html = html.replace(marker, (
            "<script>window._AMapSecurityConfig="
            "{securityJsCode:'" + sec + "'}</script>" + marker), 1)
    return html


# JS 表达式（优先公开属性，回退内部压缩属性并告警）
_JS_NE_LON = ("var b=map.getBounds();"
              "return (b.northEast.lng!==undefined)?b.northEast.lng:b.northEast.kT;")
_JS_NE_LAT = ("var b=map.getBounds();"
              "return (b.northEast.lat!==undefined)?b.northEast.lat:b.northEast.KL;")
_JS_SW_LON = ("var b=map.getBounds();"
              "return (b.southWest.lng!==undefined)?b.southWest.lng:b.southWest.kT;")
_JS_SW_LAT = ("var b=map.getBounds();"
              "return (b.southWest.lat!==undefined)?b.southWest.lat:b.southWest.KL;")
# 地图中心（getCenter 为 2.0 公开 API，无需内部属性回退）
_JS_CENTER_LON = "return map.getCenter().lng;"
_JS_CENTER_LAT = "return map.getCenter().lat;"


class RenderDriver:
    """Selenium 渲染驱动（控制原语集封装）。

    用法：
        with RenderDriver(key, template="bld", params=RenderParams()) as d:
            d.set_center(lon, lat)
            png_bytes = d.screenshot()
    """

    def __init__(self, key: str, *, template: str = "rd",
                 params: RenderParams | None = None, headless: bool = False,
                 mapstyle: str = "", security_js_code: str = ""):
        from selenium import webdriver  # 延迟导入（引擎 B 专用依赖）
        from selenium.webdriver.firefox.options import Options
        from selenium.webdriver.firefox.service import Service

        self.params = params or RenderParams()
        self.params.validate()
        self.key = key
        self.template = template
        self._mapstyle = (mapstyle or "").strip()
        self._security_js_code = (security_js_code or "").strip()

        opts = Options()
        if headless:
            opts.add_argument("-headless")
        # Firefox 非默认安装目录：用 MAPFETCH_FIREFOX_BINARY 指定 firefox 可执行文件
        # 完整路径（否则 Selenium 按平台默认路径/注册表查找）。
        _fb = os.environ.get("MAPFETCH_FIREFOX_BINARY", "").strip()
        if _fb:
            opts.binary_location = _fb
        self._webdriver_module = webdriver
        self._opts = opts
        # geckodriver 定位顺序：MAPFETCH_GECKODRIVER 环境变量 → PATH
        # （shutil.which，与 doctor 检测口径一致）→ Selenium Manager 兜底。
        # 不直接依赖 Selenium Manager：它需联网探测 Firefox 版本，失败时报
        # NoSuchDriverException，而 geckodriver 明明就在 PATH 里。
        _gd = os.environ.get("MAPFETCH_GECKODRIVER", "").strip() \
            or shutil.which("geckodriver") or ""
        self._service = Service(executable_path=_gd) if _gd else Service()
        self.driver = None

    def start(self, center_lon: float, center_lat: float) -> None:
        self.driver = self._webdriver_module.Firefox(
            options=self._opts, service=self._service)
        self._fit_viewport(self.params.window_size)
        html = render_template(
            self.template, key=self.key, center_lon=center_lon,
            center_lat=center_lat, zoom=self.params.zoom,
            mapstyle=self._mapstyle, security_js_code=self._security_js_code)
        TEMPLATE_DIR.mkdir(parents=True, exist_ok=True)
        page = TEMPLATE_DIR / f"_runtime_{self.template}.html"
        page.write_text(html, encoding="utf-8")
        self.driver.get(page.as_uri())
        time.sleep(self.params.wait_sec + 2.0)  # 首屏加载余量

    def stop(self) -> None:
        if self.driver:
            self.driver.quit()  #  e 命令：避免残留进程
            self.driver = None

    def _fit_viewport(self, target: int) -> None:
        """校准窗口外框，使视口（innerWidth/innerHeight）精确等于 target。

        Firefox 的 set_window_size 设置的是**外框**尺寸：标题栏/边框会占掉
        一部分（实测 headless 下 2000×2000 外框 → 2000×1915 视口，差 85px）。
        若不校准，每窗截图比窗框矮一截，分块拼接会出现透明横缝，且地图
        中心相对窗框上偏（半缝高），引入数十像素的纵向错位。
        """
        self.driver.set_window_size(target, target)
        for _ in range(4):
            w, h = self.window_inner_size()
            if w == target and h == target:
                return
            # 边框占用量恒定，按差值补外框尺寸即可收敛
            self.driver.set_window_size(target + (target - w),
                                        target + (target - h))
        w, h = self.window_inner_size()
        logger.warning("视口校准未收敛：实际 %sx%s（目标 %s×%s），"
                       "拼接可能出现细缝", w, h, target, target)

    def __enter__(self) -> "RenderDriver":
        return self

    def __exit__(self, *_exc) -> None:
        self.stop()

    # ----------------  控制原语 ----------------
    def get_bounds_gcj02(self) -> tuple[float, float, float, float]:
        """(min_lon, min_lat, max_lon, max_lat)，GCJ-02。"""
        sw_lon = self.driver.execute_script(_JS_SW_LON)
        sw_lat = self.driver.execute_script(_JS_SW_LAT)
        ne_lon = self.driver.execute_script(_JS_NE_LON)
        ne_lat = self.driver.execute_script(_JS_NE_LAT)
        return float(sw_lon), float(sw_lat), float(ne_lon), float(ne_lat)

    def set_zoom_and_center(self, z: int, lon: float, lat: float,
                            immediately: bool = False) -> None:
        imm = "true" if immediately else "false"
        self.driver.execute_script(
            f"map.setZoomAndCenter({z}, [{lon}, {lat}], {imm});")

    def pan_by(self, dx: int, dy: int) -> None:
        self.driver.execute_script(f"map.panBy({dx}, {dy});")

    def ensure_center(self, target_lon: float, target_lat: float, *, zoom: int,
                      timeout: float = 6.0, tol_deg: float = 1e-5) -> bool:
        """等待 panBy 平滑动画到位；超时则立即强制定位兜底。

        AMap 2.0 的 panBy 是**带动画**的（数百 ms 缓动）；headless 下渲染帧
        可能被节流，动画偶尔停在中途——此时截图内容相对窗框整体偏移数十
        至数百像素，分块/拼接全部错位（症状：窗间随机偏移、无固定量）。
        轮询 getCenter 直到与目标差 < tol_deg（≈1px @z17），超时用
        setZoomAndCenter(..., immediately=true) 无动画直接跳到目标。

        返回 True=动画自然到位，False=走了强制兜底。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            lon = float(self.driver.execute_script(_JS_CENTER_LON))
            lat = float(self.driver.execute_script(_JS_CENTER_LAT))
            if (abs(lon - target_lon) < tol_deg
                    and abs(lat - target_lat) < tol_deg):
                return True
            time.sleep(0.15)
        logger.warning("panBy %.0fs 未到位，强制 setZoomAndCenter 对齐", timeout)
        self.set_zoom_and_center(zoom, target_lon, target_lat,
                                 immediately=True)
        time.sleep(0.3)
        return False

    def window_inner_size(self) -> tuple[int, int]:
        w = self.driver.execute_script("return window.innerWidth;")
        h = self.driver.execute_script("return window.innerHeight;")
        return int(w), int(h)

    def screenshot(self) -> bytes:
        return self.driver.get_screenshot_as_png()

    def wait_loaded(self, *, slow_content: bool = False) -> None:
        """窗口移动后等待加载（模式 0/7 额外 +1.5s）。"""
        t = self.params.wait_sec
        if slow_content:
            t += self.params.slow_content_extra
        time.sleep(t)

    def wait_map_ready(self, timeout: float = 30.0) -> bool:
        """等待地图首屏资源加载完成（模板在 complete 事件置 __mapReady=1）。

        首窗截图的帧差法盲区：地图从零开始渲染，若矢量瓦片仍在网络往返中，
        画面连续两帧均为"空白网格"，帧差 <0.2% 被误判稳定 → 截图空白
        （症状：拼接图左上角首窗成片缺路网）。complete 是 AMap 官方的
        "首屏加载完成"信号，比固定 sleep 与帧差法都可靠；panBy 后续窗口
        不触发该事件，仍走 wait_loaded + _wait_render_stable。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            ready = self.driver.execute_script(
                "return window.__mapReady === 1;")
            if ready:
                return True
            time.sleep(0.2)
        logger.warning("地图首屏 %ss 未就绪（complete 未触发），继续执行", timeout)
        return False

    # ---------------- 建筑层数扫描（模式 3） ----------------
    def set_probe_height(self, meters: float) -> None:
        self.driver.execute_script(f"setProbe({meters});")

    def scan_floors(self, *, floor_min: int, floor_max: int,
                    floor_step: int) -> dict[int, bytes]:
        """逐层截图，返回 {层数: png_bytes}。调用方负责镶嵌取最大值。"""
        out: dict[int, bytes] = {}
        floors = list(range(floor_min, floor_max + 1, floor_step))
        for f in floors:
            self.set_probe_height(float(f * 3))  # 按每层 3m
            self.wait_loaded()
            out[f] = self.screenshot()
        return out


def decode_png(png_bytes: bytes):
    """PNG 字节 -> PIL Image（延迟导入，供调用方转 np.array 处理）。"""
    from PIL import Image
    return Image.open(io.BytesIO(png_bytes))


# ---------------------------------------------------------------------------
# ：扫描规划与工作流（两阶段：逐窗截图 → 分块拼接）
# ---------------------------------------------------------------------------

def _lonlat_to_global_px(lon: float, lat: float, z: int) -> tuple[float, float]:
    """经纬度 -> 全局 Web Mercator 像素坐标（z 级，256px 瓦片基准）。"""
    import math
    n = 2.0 ** z
    px = (lon + 180.0) / 360.0 * 256 * n
    lat = max(min(lat, 85.05112878), -85.05112878)
    lat_rad = math.radians(lat)
    py = (1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * 256 * n
    return px, py


def _global_px_to_lonlat(px: float, py: float, z: int) -> tuple[float, float]:
    """全局像素坐标 -> 经纬度（_lonlat_to_global_px 的逆）。"""
    import math
    n = 2.0 ** z
    lon = px / (256 * n) * 360.0 - 180.0
    v = math.pi * (1 - 2 * py / (256 * n))
    lat = math.degrees(math.atan(math.sinh(v)))
    return lon, lat


@dataclass
class ScanWindow:
    """单个扫描窗口（全局像素空间 + 该源坐标系中心点）。"""

    col: int
    row: int
    center_lon: float   # 源坐标系（高德为 GCJ-02）
    center_lat: float
    px0: float          # 全局像素四至
    py0: float
    px1: float
    py1: float


class ScanPlanner:
    """工作范围 -> 窗口网格规划（纯函数，可离线测试）。

    坐标约定：输入 bounds 为**源坐标系**（高德 GCJ-02）的
    (min_lon, min_lat, max_lon, max_lat)；窗口在全局像素空间均匀划分，
    保证相邻窗口边缘严丝合缝（消除 panBy 亚像素误差， 裁剪说明）。
    遍历顺序：行优先蛇形，便于断点后续跑方向一致。
    """

    def __init__(self, bounds: tuple[float, float, float, float], z: int,
                 window_px: int = DEFAULT_WINDOW):
        self.bounds = bounds
        self.z = z
        self.window_px = window_px
        min_lon, min_lat, max_lon, max_lat = bounds
        self._px0, self._py1 = _lonlat_to_global_px(min_lon, min_lat, z)  # 西南角
        self._px1, self._py0 = _lonlat_to_global_px(max_lon, max_lat, z)  # 东北角
        import math
        self.cols = max(1, math.ceil((self._px1 - self._px0) / window_px))
        self.rows = max(1, math.ceil((self._py1 - self._py0) / window_px))

    @property
    def total(self) -> int:
        return self.cols * self.rows

    def window(self, col: int, row: int) -> ScanWindow:
        px0 = self._px0 + col * self.window_px
        py0 = self._py0 + row * self.window_px
        px1, py1 = px0 + self.window_px, py0 + self.window_px
        cx, cy = (px0 + px1) / 2, (py0 + py1) / 2
        lon, lat = _global_px_to_lonlat(cx, cy, self.z)
        return ScanWindow(col, row, lon, lat, px0, py0, px1, py1)

    def iter_windows(self):
        """行优先蛇形遍历。"""
        for row in range(self.rows):
            cols = range(self.cols) if row % 2 == 0 else range(self.cols - 1, -1, -1)
            for col in cols:
                yield self.window(col, row)

    def window_bounds_source(self, w: ScanWindow) -> tuple[float, float, float, float]:
        """窗口四至（源坐标系经纬度），用于 PGW 配准。"""
        min_lon, min_lat = _global_px_to_lonlat(w.px0, w.py1, self.z)
        max_lon, max_lat = _global_px_to_lonlat(w.px1, w.py0, self.z)
        return min_lon, min_lat, max_lon, max_lat


class RenderScanJob:
    """引擎 B 扫描任务（两阶段；输出视觉一致）。

    关键区别：PGW 坐标由 GCJ-02 纠偏为 WGS84 后写出，
    SRS 标签与数值严格一致（可选 CGCS2000）。
    """

    def __init__(self, driver: RenderDriver, planner: ScanPlanner, *,
                 slow_content: bool = False, merge_size: int = MERGE_SIZE):
        self.driver = driver
        self.planner = planner
        self.slow_content = slow_content  # 模式 0/7 额外 +1.5s
        self.merge_size = merge_size
        self._content_fn = self._content_fn_for()

    def _shot_arr(self):
        """当前视口截图 -> RGB int16 数组（供逐帧对比）。"""
        import numpy as np
        from PIL import Image
        return np.asarray(
            Image.open(io.BytesIO(self.driver.screenshot())).convert("RGB"),
            dtype=np.int16)

    def _wait_render_stable(self, *, max_wait: float = 20.0,
                            settle: float = 0.6,
                            change_tol: float = 0.002,
                            content_fn=None) -> bool:
        """截图对比法等待渲染稳定（矢量瓦片加载绘制完成后画面不再变化）。

        固定 sleep 等待在矢量源上不可靠：panBy 2000px 后新视野的矢量瓦片
        需逐块请求绘制，1s 内常只画出一部分（症状：窗内容成片空白、露出
        底图网格），拼接后表现为"位置不对"。改为连续两帧对比：变化像素
        比例 < change_tol 视为稳定。

        content_fn（内容感知判定，可选）：帧差法存在**根本盲区**——它无法
        区分「渲染完成」与「尚未开始渲染」：白屏（画布未绘制）、黑网格
        （样式背景已应用、矢量未画）两种静止画面同样两帧全等 → 误判稳定
        （症状：拼接图首窗成片空白/纯白）。content_fn(arr) 以前景像素特征
        判定"看得到内容"（如 rd 的白路网占比），返回 False 时视为未稳定、
        继续等待；超时返回 False（照常截图，由调用方警告链路提示）。
        """
        import numpy as np
        prev = self._shot_arr()
        deadline = time.time() + max_wait
        while time.time() < deadline:
            time.sleep(settle)
            cur = self._shot_arr()
            if prev.shape == cur.shape:
                frac = float((np.abs(cur - prev) > 8).mean())
                if frac < change_tol:
                    if content_fn is None or content_fn(cur):
                        return True
                    # 画面静止但无前景内容：尚未开始渲染，继续等待
            prev = cur
        return False

    def _content_fn_for(self):
        """按模板构造前景内容判定函数（供 _wait_render_stable 补盲区）。

        阈值依据（虹口/青岛 z17 实测）：
        - rd：白路网 lum>360（正常窗 14-24%；白屏/黑网格 ≈0）
        - bld：blue 样式**建筑比背景暗**——建筑主体 RGB(0,47,64) lum3 90-119，
          背景 120-149、网格线 210-359；判据 lum3<120 ≥0.8%
        - water/green：复用 _color_masks 通道主导色域（绿/蓝通道主导）
        前景占比下限：路网/建筑 0.8%；水/绿取 0.001%（真实低占比场景实测
        可低至 0.015%，下限只拦"完全零前景"的未渲染画面）。真实无前景的
        窗口（纯路网空洞区）会等到超时后照常截图，仅多耗时不出错。
        """
        template = self.driver.template
        if template == "rd":
            return lambda a: float((a.sum(axis=2) > 360).mean()) >= 0.008
        if template == "bld":
            return lambda a: float((a.sum(axis=2) < 120).mean()) >= 0.008
        if template in ("water", "green"):
            from ..process.classify import _color_masks

            def _check(arr, _t=template):
                masks = _color_masks(arr[:, :, :3].astype(int))
                return float(masks[_t].mean()) >= 0.00001
            return _check
        return None  # bld3d 等：无内容判定，仅帧差

    def run(self, out_dir: Path, *, name_prefix: str,
            progress_cb=None, resume_from: int = 0,
            merge: bool = False) -> list[Path]:
        """逐窗截图并按 merge_size×merge_size 分块写出 PNG+PGW。

        merge=True 时改为输出**单张完整拼接图**（不分块）。断点续跑由
        resume_from（已完成窗口序号）支持。
        """
        from PIL import Image
        from ..core.geo import crs as crsmod
        from ..export.worldfile import write_worldfile

        out_dir.mkdir(parents=True, exist_ok=True)
        blocks: dict[tuple[int, int], dict] = {}
        block_paths: list[Path] = []
        win_px = self.planner.window_px
        windows = list(self.planner.iter_windows())  # 蛇形序列物化一次
        full = (Image.new("RGBA",
                          (self.planner.cols * win_px,
                           self.planner.rows * win_px),
                          (0, 0, 0, 0)) if merge else None)

        for idx, w in enumerate(windows):
            if idx < resume_from:
                continue
            if idx == 0:
                # 首窗：setZoomAndCenter 定位（无动画）；后续窗口 panBy 平移
                self.driver.set_zoom_and_center(self.planner.z,
                                                w.center_lon, w.center_lat,
                                                immediately=True)
                # 等首屏矢量瓦片加载完成（complete 事件）——首窗从零渲染，
                # 帧差法在瓦片未到达时两帧全等会被误判稳定 → 左上角空白
                self.driver.wait_map_ready()
            else:
                prev = windows[idx - 1]
                dx = int(round((w.px0 + w.px1) / 2 - (prev.px0 + prev.px1) / 2))
                dy = int(round((w.py0 + w.py1) / 2 - (prev.py0 + prev.py1) / 2))
                self.driver.pan_by(dx, dy)
                # panBy 为平滑动画，headless 下可能停在中途 → 轮询校验，
                # 未到位则强制对齐（否则截图内容相对窗框随机偏移）
                self.driver.ensure_center(w.center_lon, w.center_lat,
                                          zoom=self.planner.z)
            self.driver.wait_loaded(slow_content=self.slow_content)
            # 等渲染稳定（矢量瓦片加载绘制完成）再截，防止窗内容成片空白；
            # content_fn 补帧差法盲区：静止但无前景内容（白屏/黑网格）继续等
            if not self._wait_render_stable(content_fn=self._content_fn):
                logger.warning("窗口 (%s, %s) 渲染 %ss 未稳定，照常截图",
                               w.col, w.row, 20)
            png = self.driver.screenshot()
            img = self._paste(png, w, blocks, win_px)
            if full is not None:
                full.paste(img, (w.col * win_px, w.row * win_px))
            if progress_cb:
                progress_cb(idx + 1, self.planner.total, "render")

        if merge:
            merged_png = out_dir / f"{name_prefix}.png"
            full.save(merged_png)
            bounds_src = self._full_bounds_source()
            # GCJ-02 -> WGS84 纠偏（纠正错标签做法）
            sw = crsmod.gcj02_to_wgs84(bounds_src[0], bounds_src[1])
            ne = crsmod.gcj02_to_wgs84(bounds_src[2], bounds_src[3])
            res_x = (ne[0] - sw[0]) / full.width
            res_y = (ne[1] - sw[1]) / full.height
            write_worldfile(merged_png, west=sw[0], north=ne[1],
                            res_x=res_x, res_y=res_y, crs="CGCS2000")
            return [merged_png]

        # 阶段 2：分块写出 + 配准（纠偏为 WGS84）
        for (bc, br), blk in sorted(blocks.items()):
            block_png = out_dir / f"{name_prefix}_{bc}_{br}.png"
            blk["image"].save(block_png)
            bounds_src = self._block_bounds_source(bc, br)
            # GCJ-02 -> WGS84 纠偏（纠正错标签做法）
            sw = crsmod.gcj02_to_wgs84(bounds_src[0], bounds_src[1])
            ne = crsmod.gcj02_to_wgs84(bounds_src[2], bounds_src[3])
            res_x = (ne[0] - sw[0]) / blk["image"].width
            res_y = (ne[1] - sw[1]) / blk["image"].height
            write_worldfile(block_png, west=sw[0], north=ne[1],
                            res_x=res_x, res_y=res_y, crs="CGCS2000")
            block_paths.append(block_png)
        return block_paths

    def _prev_window(self, idx: int) -> ScanWindow:
        """调试辅助：取蛇形序列中前一个窗口。"""
        windows = list(self.planner.iter_windows())
        return windows[max(0, idx - 1)]

    def _paste(self, png: bytes, w: ScanWindow, blocks: dict, win_px: int):
        from PIL import Image

        img = Image.open(io.BytesIO(png)).convert("RGBA")
        # 窗口内实际内容可能含浏览器 UI 偏差，按窗口尺寸居中裁剪（cropped_img）
        img = self._center_crop(img, win_px)
        bc, br = w.col // self.merge_size, w.row // self.merge_size
        key = (bc, br)
        if key not in blocks:
            side = self.merge_size * win_px
            blocks[key] = {"image": Image.new("RGBA", (side, side), (0, 0, 0, 0))}
        ox = (w.col % self.merge_size) * win_px
        oy = (w.row % self.merge_size) * win_px
        blocks[key]["image"].paste(img, (ox, oy))
        return img

    @staticmethod
    def _center_crop(img, win_px: int):
        if img.width == win_px and img.height == win_px:
            return img
        x0 = max(0, (img.width - win_px) // 2)
        y0 = max(0, (img.height - win_px) // 2)
        return img.crop((x0, y0, x0 + win_px, y0 + win_px))

    def _block_bounds_source(self, bc: int, br: int) -> tuple[float, float, float, float]:
        c0 = bc * self.merge_size
        r0 = br * self.merge_size
        w_nw = self.planner.window(c0, r0)
        c1 = min(c0 + self.merge_size - 1, self.planner.cols - 1)
        r1 = min(r0 + self.merge_size - 1, self.planner.rows - 1)
        w_se = self.planner.window(c1, r1)
        b_nw = self.planner.window_bounds_source(w_nw)
        b_se = self.planner.window_bounds_source(w_se)
        return b_nw[0], b_se[1], b_se[2], b_nw[3]

    def _full_bounds_source(self) -> tuple[float, float, float, float]:
        """整图覆盖范围（源坐标系）；与 _block_bounds_source 同逻辑但覆盖全部窗口。"""
        w_nw = self.planner.window(0, 0)
        w_se = self.planner.window(self.planner.cols - 1, self.planner.rows - 1)
        b_nw = self.planner.window_bounds_source(w_nw)
        b_se = self.planner.window_bounds_source(w_se)
        return b_nw[0], b_se[1], b_se[2], b_nw[3]


class FloorScanJob:
    """建筑层数扫描（模式 3； 保留）。

    原理：3D 模式注入黑色探针体块，按 extrusionHeight 逐层截图，
    同一窗口的多层结果取最大值镶嵌为单通道灰度。
    默认 0–36 层、间隔 3（f 命令约束由 RenderParams.validate 保证）。
    merge=True（默认）时输出**单张完整灰度图**，否则按窗口分文件。
    """

    def __init__(self, driver: RenderDriver, planner: ScanPlanner):
        self.driver = driver
        self.planner = planner

    @staticmethod
    def _center_crop(img, win_px: int):
        if img.width == win_px and img.height == win_px:
            return img
        x0 = max(0, (img.width - win_px) // 2)
        y0 = max(0, (img.height - win_px) // 2)
        return img.crop((x0, y0, x0 + win_px, y0 + win_px))

    @staticmethod
    def _mosaic_floors(shots: dict, win_px: int):
        """3D 遮挡法层数镶嵌（模式 3）——取每像素「最后可见层」。

        原理：探针（纯黑体块）随层增高，画面上遮挡区单调扩张。建筑像素在
        探针低于建筑高度时可见（建筑色），被盖住后变黑——**最后可见层**
        即建筑高度所在档位。

        - 遮挡判定：该层截图中像素接近纯黑（探针 fillColor #000000，<50）
        - 建筑掩膜：各层逐像素取最大值合成「无遮挡原始帧」，其中亮于背景
          （>150）的像素为建筑（AMap 默认样式背景灰 ~122、建筑浅色 ~200+）
        - 掩膜内像素值 = 最后可见层对应的层数档位（0,step,2*step,…）；
          掩膜外（背景/无建筑）= 0
        """
        import numpy as np
        from PIL import Image

        floors = sorted(shots)
        arrs = [np.asarray(Image.open(io.BytesIO(shots[f])).convert("L"))
                for f in floors]
        stack = np.max(np.stack(arrs), axis=0)   # 无遮挡原始画面
        building = stack > 150
        hmap = np.zeros(stack.shape, dtype="uint8")
        for f, arr in zip(floors, arrs):
            visible = arr > 50                   # 非探针黑 = 该层未被遮挡
            hmap[visible & building] = f
        return FloorScanJob._center_crop(Image.fromarray(hmap, "L"), win_px)

    def _full_bounds_source(self) -> tuple[float, float, float, float]:
        w_nw = self.planner.window(0, 0)
        w_se = self.planner.window(self.planner.cols - 1, self.planner.rows - 1)
        b_nw = self.planner.window_bounds_source(w_nw)
        b_se = self.planner.window_bounds_source(w_se)
        return b_nw[0], b_se[1], b_se[2], b_nw[3]

    def run(self, out_dir: Path, *, name_prefix: str, params: RenderParams,
            progress_cb=None, merge: bool = True) -> list[Path]:
        from PIL import Image
        from ..core.geo import crs as crsmod
        from ..export.worldfile import write_worldfile

        params.validate()
        out_dir.mkdir(parents=True, exist_ok=True)
        win_px = self.planner.window_px
        full = (Image.new("L", (self.planner.cols * win_px,
                                self.planner.rows * win_px), 0)
                if merge else None)
        out_paths: list[Path] = []
        for idx, w in enumerate(self.planner.iter_windows()):
            self.driver.set_zoom_and_center(17, w.center_lon, w.center_lat)
            if idx == 0:
                self.driver.wait_map_ready()  # 首屏瓦片加载完成再扫层
            self.driver.wait_loaded()
            shots = self.driver.scan_floors(
                floor_min=params.floor_min, floor_max=params.floor_max,
                floor_step=params.floor_step)
            img = self._mosaic_floors(shots, win_px)
            if full is not None:
                full.paste(img, (w.col * win_px, w.row * win_px))
            else:
                merged = out_dir / f"{name_prefix}_floor_{w.col}_{w.row}.png"
                img.save(merged)
                bounds_src = self.planner.window_bounds_source(w)
                sw = crsmod.gcj02_to_wgs84(bounds_src[0], bounds_src[1])
                ne = crsmod.gcj02_to_wgs84(bounds_src[2], bounds_src[3])
                side = self.planner.window_px
                write_worldfile(merged, west=sw[0], north=ne[1],
                                res_x=(ne[0] - sw[0]) / side,
                                res_y=(ne[1] - sw[1]) / side, crs="CGCS2000")
                out_paths.append(merged)
            if progress_cb:
                progress_cb(idx + 1, self.planner.total,
                            f"floors {params.floor_min}-{params.floor_max}")

        if merge:
            merged_png = out_dir / f"{name_prefix}_floors.png"
            full.save(merged_png)
            bounds_src = self._full_bounds_source()
            sw = crsmod.gcj02_to_wgs84(bounds_src[0], bounds_src[1])
            ne = crsmod.gcj02_to_wgs84(bounds_src[2], bounds_src[3])
            write_worldfile(merged_png, west=sw[0], north=ne[1],
                            res_x=(ne[0] - sw[0]) / full.width,
                            res_y=(ne[1] - sw[1]) / full.height, crs="CGCS2000")
            return [merged_png]
        return out_paths



def floor_to_height_range(floor_value: int, *, step: int = 3,
                          floor_height_m: float = 3.0) -> tuple[float, float]:
    """层数扫描值 -> 估算高度区间（换算规则，按每层 3m）。

    值 0 -> (0, 9]m（1–2 层）；值 v -> [v*3, (v+step)*3)m。
    """
    lo = floor_value * floor_height_m
    hi = (floor_value + step) * floor_height_m
    return lo, hi
