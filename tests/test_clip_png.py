"""PNG AOI 裁剪测试（render/floors 对齐 fetch 的 _aoi 第二份输出）。"""
from __future__ import annotations

import numpy as np
import pytest
from PIL import Image

from mapfetch.core.aoi import AOI
from mapfetch.export.worldfile import clip_png_to_aoi, write_worldfile


def _make_png(tmp_path, name, arr, *, west=121.0, north=31.0, res=0.001):
    src = tmp_path / name
    Image.fromarray(arr).save(src)
    write_worldfile(src, west=west, north=north, res_x=res, res_y=res)
    return src


class TestClipPngToAoi:
    def test_clip_rect_rgba(self, tmp_path):
        arr = np.full((10, 10, 4), 255, dtype=np.uint8)
        src = _make_png(tmp_path, "src.png", arr)
        aoi = AOI.from_rect(121.003, 30.993, 121.007, 30.997)
        dst = tmp_path / "dst.png"
        clip_png_to_aoi(src, aoi, dst)

        out = np.asarray(Image.open(dst))
        assert out.shape[:2] == (4, 4)              # 收缩到 AOI bbox 窗口
        assert (out[:, :, 3] == 255).all          # AOI 内全不透明

        # 新世界文件：窗口原点平移（west=121.003, north=30.997）
        pgw = [float(v) for v in dst.with_suffix(".pgw").read_text().strip().split("\n")]
        assert pgw[4] == pytest.approx(121.0035)   # 像素中心 x = 121.003 + 0.0005
        assert pgw[5] == pytest.approx(30.9965)    # 像素中心 y = 30.997 - 0.0005

    def test_clip_circle_corners_transparent(self, tmp_path):
        arr = np.full((20, 20, 4), 255, dtype=np.uint8)
        src = _make_png(tmp_path, "src.png", arr, res=0.0005)
        aoi = AOI.from_center_radius(121.005, 30.995, 200)  # 圆 → bbox 四角在圆外
        dst = tmp_path / "dst.png"
        clip_png_to_aoi(src, aoi, dst)

        out = np.asarray(Image.open(dst))
        h, w = out.shape[:2]
        assert out[h // 2, w // 2, 3] == 255        # 圆心不透明
        assert out[0, 0, 3] == 0                    # bbox 四角在圆外 → 透明
        assert out[0, w - 1, 3] == 0
        assert out[h - 1, 0, 3] == 0
        assert out[h - 1, w - 1, 3] == 0

    def test_clip_grayscale_preserves_values(self, tmp_path):
        arr = np.full((10, 10), 6, dtype=np.uint8)  # floors 层数档位值 6
        src = _make_png(tmp_path, "src.png", arr)
        aoi = AOI.from_rect(121.003, 30.993, 121.007, 30.997)
        dst = tmp_path / "dst.png"
        clip_png_to_aoi(src, aoi, dst)

        out = np.asarray(Image.open(dst))
        assert out.ndim == 2                        # 保持灰度（不升维）
        assert (out == 6).all                     # AOI 内层数值不变

    def test_missing_pgw_raises(self, tmp_path):
        arr = np.full((10, 10, 4), 255, dtype=np.uint8)
        src = tmp_path / "src.png"
        Image.fromarray(arr).save(src)              # 无 .pgw
        aoi = AOI.from_rect(121.0, 30.99, 121.01, 31.0)
        import pytest
        with pytest.raises(FileNotFoundError):
            clip_png_to_aoi(src, aoi, tmp_path / "dst.png")
