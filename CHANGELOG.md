# Changelog

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)（预发布阶段允许破坏性变更）。

## [Unreleased]

### 已实现

- 数据源层：天地图 / 高德 / 百度三源适配器（`sources/`），含已知地标离线自检（百度层级一致性检查）
- 坐标与瓦片内核：`core/geo`（crs / tile / geometry）+ 单元测试（WGS84↔GCJ02↔BD09 往返误差 < 2 m）
- 引擎 A（瓦片直下）：令牌桶 / 并发控制 / 日配额熔断 / AIMD 调速 / 退避重试 / 缓存 / 断点续跑
- 栅格输出：流式拼接 + GeoTIFF（EPSG:4490，内部金字塔，LZW）
- GUI（PySide6 + QtWebEngine + Leaflet）：AOI 四种选择方式（行政区 / 矩形 / 多边形 / 文件导入）+ 地图绘制回接 + 瓦片预估
- 引擎 B（JS API 渲染）：扫描工作流（ScanPlanner / RenderScanJob / FloorScanJob），六类渲染模板，PGW 纠偏标签一致
- 矢量化管线：`process/pipeline.py`（road / building / background / floors → GPKG / SHP / GeoJSON）
- 合规模块：`compliance/`（署名 / manifest / README 自动生成）
- 打包：pyproject pip 包配置；覆盖核心模块的单元测试全部通过
