"""影像分类与二值化（重分类规则、 矢量生产流程）。

输出规则：
- 道路：背景→黑(0)，道路（含描边）→白(255)；RGBA，背景全透明
- 建筑：建筑（不含描边）→白(255)，其余→黑(0)；RGBA，背景全透明
- 层数扫描：单通道灰度，多轮结果取最大值镶嵌

numpy/PIL 延迟导入。
"""

from __future__ import annotations

from pathlib import Path


def _load(path_or_bytes):
    import numpy as np
    from PIL import Image

    if isinstance(path_or_bytes, (str, Path)):
        im = Image.open(path_or_bytes)
    else:
        import io
        im = Image.open(io.BytesIO(path_or_bytes))
    return np.asarray(im.convert("RGBA"))


def _save_rgba(arr, out: str | Path) -> Path:
    from PIL import Image
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(arr, "RGBA").save(out)
    return out


def binarize_road(img, out: str | Path, *, threshold: int = 120) -> Path:
    """道路二值化（darkblue 底 + 白路网；道路含描边→255，背景全透明）。

    阈值依据（虹口 z17 实测灰度直方图）：darkblue 样式的背景网格纹理
    亮度主峰在 48-64、上限 ~112；道路描边抗锯齿边缘从 128 起、支路
    128-160、主干纯白 255。112-128 之间存在干净空白带，阈值取 120
    可同时滤净网格与保留支路。
    """
    import numpy as np

    arr = _load(img)
    # darkblue 样式下道路为亮色：亮度阈值判定
    luminance = arr[:, :, 0].astype(int) + arr[:, :, 1].astype(int) + arr[:, :, 2].astype(int)
    mask = luminance > threshold * 3
    out_arr = np.zeros((*mask.shape, 4), dtype="uint8")
    out_arr[mask] = [255, 255, 255, 255]   # 前景道路：纯白不透明
    # 背景：RGBA 全 0（完全透明）
    return _save_rgba(out_arr, out)


def binarize_building(img, out: str | Path, *, threshold: int = 40) -> Path:
    """建筑二值化（blue 样式建筑→255；不含描边，背景全透明）。

    阈值依据（虹口 z17 实测直方图，四峰分布，**形状验证确认**）：
    - 建筑主体 RGB(0,47,64)，lum3 90-119 —— 暗色，形状为清晰建筑块
    - 背景     RGB(3,52,71)，lum3 120-149 —— 比建筑略亮
    - 网格纹理 RGB(47,87,103)/(86,118,131)，lum3 210-359 —— 最亮
    ⚠️ blue 样式下**建筑比背景暗**（与直觉相反）：threshold 为亮度**上限**，
    选中 lum3 < threshold×3 的暗色像素即建筑主体，排除亮背景与网格线。
    透明像素（RGBA alpha=0）一律排除，避免反向阈值误选透明黑边。
    """
    import numpy as np

    arr = _load(img)
    luminance = arr[:, :, :3].astype(int).sum(axis=2)
    alpha = arr[:, :, 3]
    mask = (luminance < threshold * 3) & (alpha > 0)
    out_arr = np.zeros((*mask.shape, 4), dtype="uint8")
    out_arr[mask] = [255, 255, 255, 255]
    return _save_rgba(out_arr, out)


def max_mosaic(images: list, out: str | Path) -> Path:
    """层数扫描多轮结果取最大值镶嵌（单通道灰度）。"""
    import numpy as np
    from PIL import Image

    stack = None
    for img in images:
        arr = _load(img)[:, :, 0]  # 单通道
        stack = arr if stack is None else np.maximum(stack, arr)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(stack, "L").save(out)
    return out


def _color_masks(arr, *, green_gap: int = 30, blue_gap: int = 30,
                 min_chroma: int = 60) -> dict:
    """按「通道主导」判定绿地/水系（供 binarize_background_layer 使用）。

    以绿/蓝通道的**相对大小**划界，不依赖具体样式配色：
    - 绿地：g > b（绿为最大通道）且明显高于红通道（g >= r + green_gap）
    - 水系：b >= g（蓝不小于绿）且明显高于红通道（b >= r + blue_gap）
    实测自定义样式（绿地 #0efb80=g 主导、水系 #00f5ff=青色 b 略大于 g）与
    高德默认样式均适用；r/g/b 先转 int 避免uint8 溢出。
    - min_chroma：目标通道最低亮度（滤掉近黑背景与灰色网格）
    """
    arr = arr.astype(int)
    r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
    green = (g > b) & (g >= r + green_gap) & (g >= min_chroma)
    water = (b >= g) & (b >= r + blue_gap) & (b >= min_chroma)
    return {"green": green, "water": water}


def binarize_background_layer(img, out: str | Path, *, layer: str,
                              green_gap: int = 30, blue_gap: int = 30,
                              min_chroma: int = 60) -> Path:
    """背景渲染图 → 单一要素二值图（layer: 'green' | 'water'， 背景要素分离）。

    对应 render 的 water/green 模板后处理：features:['bg'] 渲染出的彩图按
    「通道主导」提取绿地或水系（绿色/蓝色通道显著高于其余通道），输出 RGBA
    二值（要素纯白不透明、其余全透明），可直接矢量化。默认样式与自定义样式
    （自定义地图发布的任意绿/蓝配色）均适用。
    """
    import numpy as np

    arr = _load(img)[:, :, :3].astype(int)
    masks = _color_masks(arr, green_gap=green_gap, blue_gap=blue_gap,
                         min_chroma=min_chroma)
    if layer not in masks:
        raise ValueError(f"未知背景要素: {layer!r}（仅 green/water）")
    out_arr = np.zeros((*masks[layer].shape, 4), dtype="uint8")
    out_arr[masks[layer]] = [255, 255, 255, 255]
    return _save_rgba(out_arr, out)
