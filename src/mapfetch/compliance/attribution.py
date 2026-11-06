"""署名文件与项目 README 生成（manifest）。

每个项目目录自动生成：
- ATTRIBUTION.txt：数据来源署名 + 使用限制（模板）
- README.md：任务摘要 + 精度声明+ 使用限制
- manifest.json：任务元数据（来源/层级/坐标系/配额/告警）
"""

from __future__ import annotations

import json
import time
from pathlib import Path

PRECISION_NOTES = [
    "GCJ-02 ↔ WGS84 转换残差 < 2 m",
    "BD-09 转换残差 < 3 m",
    "z17 影像几何精度（相对）~1–3 m（取决于源数据）",
    "建筑轮廓矢量化精度 ~0.5–1.5 m（z17，1.02 m/px + 抗锯齿）",
    "绝对定位精度受源数据制约，不保证优于 10 m；涉密/法定测绘项目不得使用",
]

USAGE_LIMIT_CLAUSES = [
    "本数据集仅限本项目内部研究使用，不得对外分发、转售或作为服务提供。",
    "不得用于模型或算法训练及相关数据集构建（高德开放平台服务协议 3.4）。",
    "不得用于法定测绘、权属界定等需要法定资质与精度的用途。",
    "在任何展示本数据的成果（图件、报告、网页）中须保留上述数据来源标注。",
]


def write_attribution(project_dir: str | Path, *, sources_attribution: list[str],
                      aoi_wkt_or_desc: str, levels: list[int],
                      crs_note: str, project_name: str) -> Path:
    """生成 ATTRIBUTION.txt（模板）。"""
    now = time.strftime("%Y-%m-%d")
    lines = list(dict.fromkeys(sources_attribution))  # 去重保序
    body = [
        *lines,
        "",
        f"获取时间：{now}",
        f"获取范围：{aoi_wkt_or_desc}",
        f"获取层级：{', '.join('z' + str(z) for z in levels)}",
        f"坐标系：{crs_note}",
        "",
        "使用限制：",
        *[f"{i + 1}. {c}" for i, c in enumerate(USAGE_LIMIT_CLAUSES)],
        "",
    ]
    out = Path(project_dir) / "ATTRIBUTION.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(body), encoding="utf-8")
    return out


def write_project_readme(project_dir: str | Path, *, project_name: str,
                         summary: dict) -> Path:
    """生成项目 README（来源、层级、坐标系、署名、精度声明）。"""
    lines = [
        f"# {project_name} · MapFetch 抓取成果",
        "",
        "## 任务摘要",
        "",
        "| 项 | 值 |",
        "|---|---|",
        *[f"| {k} | {v} |" for k, v in summary.items()],
        "",
        "## 精度声明",
        "",
        *[f"- {n}" for n in PRECISION_NOTES],
        "",
        "## 使用限制",
        "",
        *[f"{i + 1}. {c}" for i, c in enumerate(USAGE_LIMIT_CLAUSES)],
        "",
        "详见 ATTRIBUTION.txt 与 manifest.json。",
    ]
    out = Path(project_dir) / "README.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def write_manifest(project_dir: str | Path, manifest: dict) -> Path:
    """写 manifest.json（字段；可追溯与合规审计）。"""
    manifest = dict(manifest)
    manifest.setdefault("created_at", time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    manifest.setdefault("precision_note", PRECISION_NOTES)
    out = Path(project_dir) / "manifest.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    return out
