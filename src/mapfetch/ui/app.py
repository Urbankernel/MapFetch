"""GUI 主程序（PySide6 + QtWebEngine/Leaflet）。

AOI 三种方式：
    1. 手动输入规格（rect:/circle:/file:）
    2. 地图绘制范围（Leaflet 矩形框选 / 多边形绘制 → 回传 → rect:/file: 规格）
    3. 行政区（名称/adcode → admin: 规格）

R-4 合规设计：在线静态底图默认开启（可取消勾选回退本地缓存预览）；
启动不再弹「使用限制」确认框（合规要点见 docs/compliance.md 与 `mapfetch doctor`）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ASSETS = Path(__file__).resolve().parent / "assets"


def run_gui(config_dir: str | None = None) -> int:
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError as exc:
        # 区分"未安装"与"装了但 DLL 加载失败"（后者是 conda-forge ICU 与
        # pip PySide6 的符号版本冲突，见 docs/installation.md §6 常见问题）
        hint = ""
        if "DLL load failed" in str(exc) or "找不到指定的程序" in str(exc):
            hint = ("\n\n[DLL 加载失败] 可能原因：conda 环境用 pip 安装的 PySide6 "
                    "与 conda-forge 的 ICU 版本符号不兼容。\n"
                    "  修复：pip install \"PySide6==6.8.3\"（Qt 以 -no-icu 构建，不依赖 ICU），"
                    "或 conda install -c conda-forge pyside6。\n"
                    "  详见 docs/installation.md §6。")
        print(f"GUI 无法启动：{exc}{hint}", file=sys.stderr)
        return 2

    from PySide6.QtWidgets import (QCheckBox, QComboBox, QFileDialog,
                                   QFormLayout, QHBoxLayout, QLabel,
                                   QLineEdit, QMainWindow, QMessageBox,
                                   QPushButton, QVBoxLayout, QWidget)

    from ..config import load_config
    from .mapview import MapView

    cfg = load_config(config_dir)

    app = QApplication(sys.argv)
    app.setApplicationName("MapFetch")

    win = QMainWindow()
    win.setWindowTitle("MapFetch · 多元地图数据下载工具")
    win.resize(1280, 820)

    # ---------------- 参数面板 ----------------
    aoi_mode = QComboBox()
    aoi_mode.addItems(["手动输入规格", "地图绘制范围", "行政区（名称/adcode）", "从文件导入"])
    aoi_edit = QLineEdit("rect:121.495,31.280,121.508,31.287@gcj02")
    admin_edit = QLineEdit("310109")
    admin_edit.setVisible(False)

    src_box = QComboBox()
    for name, scfg in cfg.sources.items():
        if scfg.enabled:
            src_box.addItem(name)
    style_box = QComboBox()
    level_edit = QLineEdit("15-17")
    _default_out = str(Path.cwd() / "run" / "demo")
    out_edit = QLineEdit()                      # 默认空，占位符显示示例路径
    out_edit.setPlaceholderText(_default_out)
    online_preview = QCheckBox("在线底图（天地图静态地图，默认开启；取消勾选回退本地缓存预览）")
    online_preview.setChecked(True)  # 打开即加载在线静态底图，无需手动勾选

    def refresh_styles():
        style_box.clear()
        style_box.addItems(list(cfg.sources[src_box.currentText()].styles.keys()))

    src_box.currentTextChanged.connect(refresh_styles)
    refresh_styles()

    _prev_aoi_mode = ["手动输入规格"]

    def _apply_mode_ui(text):
        is_admin = text.startswith("行政区")
        admin_edit.setVisible(is_admin)
        aoi_edit.setVisible(not is_admin)
        # 地图工具栏的「矩形框选/多边形绘制」仅在「地图绘制范围」模式下可用
        mapview.set_draw_enabled(text == "地图绘制范围")

    def on_mode_change(text):
        if text == "从文件导入":
            # 「从文件导入」是瞬时动作：弹文件选择框，选完后回退到上一选项
            path, _ = QFileDialog.getOpenFileName(
                win, "选择 AOI 文件", str(Path.cwd()),
                "AOI 文件 (*.geojson *.json *.gpkg *.shp);;所有文件 (*)")
            aoi_mode.blockSignals(True)
            aoi_mode.setCurrentText(_prev_aoi_mode[0])
            aoi_mode.blockSignals(False)
            _apply_mode_ui(_prev_aoi_mode[0])
            if path:
                import_aoi_file(path)
            return
        _prev_aoi_mode[0] = text
        _apply_mode_ui(text)

    aoi_mode.currentTextChanged.connect(on_mode_change)

    panel = QFormLayout()
    panel.addRow("AOI 方式", aoi_mode)
    panel.addRow("AOI 规格", aoi_edit)
    panel.addRow("行政区", admin_edit)
    panel.addRow("数据源", src_box)
    panel.addRow("图层样式", style_box)
    panel.addRow("层级", level_edit)
    panel.addRow("输出目录", out_edit)
    panel.addRow(online_preview)

    estimate_label = QLabel("瓦片预估：未计算")
    estimate_label.setWordWrap(True)
    show_aoi_btn = QPushButton("在地图中显示AOI范围")
    take_aoi_btn = QPushButton("从地图取回 AOI")
    estimate_btn = QPushButton("计算瓦片预估")
    run_btn = QPushButton("开始抓取")

    left = QVBoxLayout()
    left.addLayout(panel)
    left.addWidget(show_aoi_btn)
    left.addWidget(take_aoi_btn)
    left.addWidget(estimate_btn)
    left.addWidget(estimate_label)
    left.addWidget(run_btn)
    left.addStretch(1)

    # 天地图 tk 与在线底图样式（预览用；未配置时在线预览无图，本地缓存预览不受影响）
    try:
        tsc = cfg.sources["tianditu"]
        tk = tsc.get_key()
        basemap_style = tsc.preview_style
        basemap_fallback = tsc.preview_fallback_style
    except (RuntimeError, KeyError):
        tk = ""
        basemap_style = "vec_w"
        basemap_fallback = "img_w"

    mapview = MapView(cache_root=cfg.limits.cache.dir,
                      online=online_preview.isChecked(),
                      source=src_box.currentText(),
                      style=style_box.currentText(),
                      tk=tk,
                      basemap_style=basemap_style,
                      basemap_fallback=basemap_fallback)
    online_preview.toggled.connect(mapview.set_online)

    # 初始 AOI 方式为「手动输入规格」→ 地图框选/绘制默认禁用（选择「地图绘制范围」后启用）
    mapview.set_draw_enabled(aoi_mode.currentText() == "地图绘制范围")

    def on_layer_change():
        mapview.set_layer(src_box.currentText(), style_box.currentText())

    src_box.currentTextChanged.connect(on_layer_change)
    style_box.currentTextChanged.connect(on_layer_change)

    # ---------------- AOI 取回（绘制完成自动捕获） ----------------
    state = {"aoi_spec": None, "last_geom": None}

    def current_aoi_spec() -> str:
        mode = aoi_mode.currentText()
        if mode.startswith("行政区"):
            return f"admin:{admin_edit.text().strip()}"
        return state["aoi_spec"] or aoi_edit.text().strip()

    def apply_drawn_aoi(geom: dict, mode: str, save_gpkg: bool = False) -> None:
        """把绘制几何写入 AOI 规格（矩形→rect:，多边形→file:），可选落盘 gpkg。

        自动捕获只更新规格（save_gpkg=False），不导出数据文件；仅「从地图取回 AOI」
        时（save_gpkg=True）才把 aoi.gpkg 保存到输出目录。
        """
        out_dir = Path(out_edit.text().strip() or _default_out)
        aoi_dir = out_dir / "aoi"
        aoi_dir.mkdir(parents=True, exist_ok=True)
        if mode == "rect":
            coords = geom["coordinates"][0]
            lons = [c[0] for c in coords]
            lats = [c[1] for c in coords]
            state["aoi_spec"] = (f"rect:{min(lons)},{min(lats)},"
                                 f"{max(lons)},{max(lats)}")
        else:
            # 多边形：落 GeoJSON 到输出目录 aoi/（file: 规格需引用该文件，
            # 属规格而非最终数据；最终数据 aoi.gpkg 由「取回」时才落盘）
            tmp = aoi_dir / "mapfetch_aoi_drawn.geojson"
            fc = {"type": "Feature", "properties": {"source": "gui_polygon"},
                  "geometry": geom}
            tmp.write_text(json.dumps(fc, ensure_ascii=False), encoding="utf-8")
            state["aoi_spec"] = f"file:{tmp}"
        aoi_edit.setText(state["aoi_spec"])
        if save_gpkg:
            try:
                from ..export.vector import write_vector
                aoi_path = aoi_dir / "aoi.gpkg"
                write_vector([{"geometry": geom, "source": "gui"}],
                             aoi_path, layer_name="aoi", crs_epsg=4490)
                estimate_label.setText(f"AOI 已保存到：{aoi_path}")
            except Exception as exc:
                estimate_label.setText(f"AOI 已获取，但保存 gpkg 失败：{exc}")
        else:
            estimate_label.setText("AOI 已获取（未保存；点「从地图取回 AOI」导出 aoi.gpkg）")

    def on_take_aoi():
        def handle(js):
            if not js or js == "null":
                QMessageBox.warning(win, "AOI", "地图上还没有绘制的区域")
                return
            data = json.loads(js)
            geom = data.get("geometry")
            if not geom:
                QMessageBox.warning(win, "AOI", "地图上还没有绘制的区域")
                return
            # 矩形/多边形由地图实际绘制工具决定（lastDrawMode），不再依赖下拉框
            apply_drawn_aoi(geom, data.get("mode") or "polygon", save_gpkg=True)

        mapview.get_aoi_state(handle)

    take_aoi_btn.clicked.connect(on_take_aoi)

    # 在地图中显示 AOI（手动输入规格 / 行政区名称或 adcode 解析后展示）
    def on_show_aoi():
        try:
            from argparse import Namespace

            from .cli import parse_aoi

            aoi = parse_aoi(Namespace(aoi=current_aoi_spec(), config=None))
            mapview.set_aoi_geometry(aoi.to_geojson()["geometry"])
            estimate_label.setText(f"已在图中显示 AOI（面积 {aoi.area_km2:.1f} km²）")
        except Exception as exc:
            estimate_label.setText(f"显示 AOI 失败：{exc}")

    show_aoi_btn.clicked.connect(on_show_aoi)

    # 从文件导入 AOI（GeoJSON Polygon/MultiPolygon，与 CLI file: 口径一致；
    # 由「AOI 方式」下拉选择「从文件导入」触发，选完即回退到上一选项）
    def import_aoi_file(path: str) -> None:
        try:
            from argparse import Namespace

            from .cli import parse_aoi

            aoi = parse_aoi(Namespace(aoi=f"file:{path}", config=None))
        except Exception as exc:
            QMessageBox.warning(win, "AOI", f"文件解析失败：{exc}")
            return
        state["aoi_spec"] = f"file:{path}"
        aoi_edit.setText(state["aoi_spec"])
        mapview.set_aoi_geometry(aoi.to_geojson()["geometry"])

    # 绘制完成后自动捕获 AOI：定时轮询 Leaflet 当前绘制状态，
    # 几何一旦变化即写入规格（无需再点「取回 AOI」），使「计算瓦片预估」/
    # 「开始抓取」直接使用绘制范围而非默认规格。
    def poll_aoi():
        def handle(js):
            if not js or js == "null":
                return
            data = json.loads(js)
            geom = data.get("geometry")
            mode = data.get("mode")
            # mode 为空 = 程序化显示（手动输入/行政区），不触发自动捕获，避免覆盖规格
            if not geom or not mode:
                return
            key = json.dumps(geom, sort_keys=True)
            if key == state.get("last_geom"):
                return
            state["last_geom"] = key
            apply_drawn_aoi(geom, mode)

        mapview.get_aoi_state(handle)

    from PySide6.QtCore import QTimer
    poll_timer = QTimer()
    poll_timer.timeout.connect(poll_aoi)
    poll_timer.start(500)

    # ---------------- 瓦片预估 ----------------
    def on_estimate():
        try:
            from argparse import Namespace

            from ..core.aoi import AOI  # noqa: F401
            from ..sources import create_adapter
            from .cli import parse_aoi, parse_levels

            spec = current_aoi_spec()
            if spec.startswith("admin:"):
                estimate_label.setText("行政区 AOI 需联网查询边界，预估在抓取时进行")
                return
            aoi = parse_aoi(Namespace(aoi=spec, config=None))
            adapter = create_adapter(src_box.currentText(),
                                     cfg.sources[src_box.currentText()])
            levels = parse_levels(level_edit.text().strip())
            est = adapter.estimate_tiles(aoi, levels)
            total = sum(est.values())
            th = cfg.limits.task_thresholds
            warn = " ⚠️ 超阈值" if total > th.max_tiles else ""
            estimate_label.setText(
                f"面积 {aoi.area_km2:.1f} km²；瓦片 {est}；合计 {total}{warn}")
        except Exception as exc:
            estimate_label.setText(f"预估失败: {exc}")

    estimate_btn.clicked.connect(on_estimate)

    # ---------------- 抓取 ----------------
    def on_run():
        from .cli import main as cli_main
        out_dir = out_edit.text().strip() or _default_out
        argv = ["fetch", "--aoi", current_aoi_spec(),
                "--source", src_box.currentText(),
                "--style", style_box.currentText(),
                "--levels", level_edit.text().strip(),
                "--out", out_dir,
                "--project", Path(out_dir).name or "gui_project"]
        code = cli_main(argv)
        if code == 0:
            QMessageBox.information(
                win, "完成",
                f"数据已下载完成。\n输出目录：{out_dir}\n"
                f"（AOI 边界已保存到该目录的 aoi/aoi.gpkg，详见控制台输出）")
        else:
            QMessageBox.warning(
                win, "失败",
                f"抓取未完成（退出码 {code}），请查看控制台输出的错误信息。")

    run_btn.clicked.connect(on_run)

    root = QWidget()
    layout = QHBoxLayout(root)
    leftw = QWidget()
    leftw.setLayout(left)
    leftw.setFixedWidth(360)
    layout.addWidget(leftw)
    layout.addWidget(mapview, 1)
    win.setCentralWidget(root)
    win.show()
    return app.exec()
