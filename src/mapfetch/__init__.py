"""MapFetch · 多元地图数据下载工具。

天地图（默认，CGCS2000）/ 高德 / 百度三源，
瓦片下载（引擎 A）+ JS 渲染截图（引擎 B）+ 栅格反演矢量化。
"""

__version__ = "0.1.0.dev0"

# 成果默认坐标系：CGCS2000，EPSG:4490
DEFAULT_OUTPUT_CRS = "EPSG:4490"
# 内部基准坐标系：WGS84 存储与展示
INTERNAL_CRS = "EPSG:4326"
