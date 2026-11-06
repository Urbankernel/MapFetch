# MapFetch 使用指南（CLI 命令参考）

> 本文档是 [README](../README.md) 的详细展开，按**引擎分类 → 功能章节**组织。
> 每个章节都给出可直接复制运行的命令、完整参数说明与典型示例，方便按需跳转查阅。
>
> 环境准备（安装依赖、配置 Key）请先看 [README](../README.md) 与 [安装文档](installation.md)。

## 目录

1. [总览：双引擎架构与命令速查](#1-总览双引擎架构与命令速查)
2. [通用前置知识：AOI 规格与通用选项](#2-通用前置知识aoi-规格与通用选项)
3. [引擎 A · 瓦片直下（fetch）](#3-引擎-a--瓦片直下fetch)
4. [引擎 B · JS 渲染截图（render / floors）](#4-引擎-b--js-渲染截图render--floors)
5. [栅格 → 矢量（vectorize）](#5-栅格--矢量vectorize)
6. [辅助功能（aoi / doctor / gui）](#6-辅助功能aoi--doctor--gui)
7. [端到端完整工作流示例](#7-端到端完整工作流示例)
8. [输出物与元数据](#8-输出物与元数据)
9. [常见问题（FAQ）](#9-常见问题faq)
10. [合规提醒](#10-合规提醒)

---

## 1. 总览：双引擎架构与命令速查

MapFetch 用两条引擎获取地图数据，再用一个后处理命令把栅格转为矢量：

```
                        ┌─────────────────────────────────────┐
                        │            AOI 区域选择              │
                        │   rect / circle / admin / file       │
                        └──────────────────┬──────────────────┘
                                           │
              ┌────────────────────────────┼────────────────────────────┐
              │                            │                            │
              ▼                            ▼                            ▼
   ┌────────────────────┐        ┌───────────────────────┐      ┌───────────────────┐
   │  引擎 A · 瓦片直下  │        │  引擎 B · JS 渲染截图  │      │   行政区边界查询   │
   │  (fetch)           │        │  (render / floors)    │      │   (aoi)           │
   │                    │        │                       │      │                   │
   │ HTTP 异步并发下载   │        │ Selenium + Firefox    │      │ DataV GeoAtlas    │
   │ 天地图/高德/百度    │        │ 高德 JS API 2.0        │      │ 详细边界·无需 Key  │
   │                    │        │                       │      │ → GeoPackage      │
   └────────┬───────────┘        └──────────┬────────────┘      └───────────────────┘
            │                             │
            ▼                             ▼
   ┌──────────────────┐        ┌──────────────────────────┐
   │  GeoTIFF（影像）  │        │  要素栅格 PNG            │
   │  CGCS2000        │        │  道路/建筑/水系/绿地/层数 │
   └──────────────────┘        └──────────┬───────────────┘
                                          │
                                          ▼
                                ┌──────────────────┐
                                │   栅格 → 矢量     │
                                │   (vectorize)    │
                                │   → GeoPackage   │
                                └──────────────────┘
```

**命令速查表**：

| 命令 | 所属模块 | 一句话用途 | 章节 |
| --- | --- | --- | --- |
| `fetch` | 引擎 A | 下载瓦片并拼接导出 GeoTIFF | [§3](#3-引擎-a--瓦片直下fetch) |
| `render` | 引擎 B | 高德 JS 渲染道路/建筑/背景/影像要素栅格 | [§4.2](#42-render-渲染要素栅格) |
| `floors` | 引擎 B | 3D 遮挡法扫描建筑层数 | [§4.3](#43-floors-建筑层数扫描) |
| `vectorize` | 后处理 | 栅格 → 矢量（GPKG/SHP/GeoJSON） | [§5](#5-栅格--矢量vectorize) |
| `aoi` | 辅助 | 查询行政区边界并落盘 | [§6.1](#61-aoi-行政区边界查询) |
| `doctor` | 辅助 | 环境自检 | [§6.2](#62-doctor-环境自检) |
| `gui` | 辅助 | 启动图形界面 | [§6.3](#63-gui-图形界面) |

---

## 2. 通用前置知识：AOI 规格与通用选项

### 2.1 AOI 规格（`--aoi` 的四种写法）

`fetch`、`render`、`floors` 三个命令都用 `--aoi` 指定工作区域，共支持四种写法：

| 写法 | 格式 | 说明 |
| --- | --- | --- |
| 矩形框选 | `rect:minLon,minLat,maxLon,maxLat[@坐标系]` | 左下角 + 右上角，默认 WGS84 经纬度 |
| 圆形区域 | `circle:lon,lat,radius_m[@坐标系]` | 中心点 + 半径（米），默认 WGS84 |
| 行政区 | `admin:名称/编码` | 走 **DataV GeoAtlas** 取详细边界（区县级约数百顶点，无需 Key，GCJ02 自动纠偏 WGS84），如 `admin:上海市` / `admin:310109` |
| 文件导入 | `file:路径(.geojson/.gpkg/.shp)` | 导入已有面要素（Polygon/MultiPolygon；`.geojson` 走纯 JSON，`.gpkg`/`.shp` 走 geopandas 读取，非 WGS84 自动转 4326） |

**`rect` / `circle` 的 `坐标系` 后缀（可选）**：

| 后缀 | 含义 | 适用场景 |
| --- | --- | --- |
| `@wgs84`（默认） | 经纬度即 WGS84 | 天地图拾取、GPS、一般坐标 |
| `@gcj02` | 高德/腾讯火星坐标 | 高德·腾讯坐标拾取网页 |
| `@bd09` | 百度经纬度 | 百度地图坐标拾取网页 |
| `@cgcs2000` | 天地图坐标系（与 WGS84 工程等价） | 天地图成果坐标复用 |

**示例**：

```bash
# 矩形框选
--aoi "rect:121.520,31.260,121.545,31.280"

# 矩形框选（高德拾取的 GCJ02 坐标）
--aoi "rect:121.520,31.260,121.545,31.280@gcj02"

# 圆形（百度拾取的 BD09 坐标 + 周边 1 km）
--aoi "circle:121.50,31.25,1000@bd09"

# 行政区（名称或编码）
--aoi admin:310109          # 上海市虹口区（6 位国标码）
--aoi admin:上海市           # 或直接写名称

# 导入文件（GeoJSON / GeoPackage / Shapefile）
--aoi file:my_area.geojson
--aoi file:my_area.gpkg
--aoi file:my_area.shp
```

> **关于引号**：`rect:` / `circle:` 的坐标里如果逗号后带了空格，请加引号包裹；
> 不加引号时工具也会自动容错拼回（见 [FAQ §9.4](#94-不加引号报-unrecognized-arguments)）。

### 2.2 通用选项

| 选项 | 作用 | 适用命令 |
| --- | --- | --- |
| `--out` | 输出目录（可从任务配置读取） | fetch / render / floors / vectorize / aoi |
| `--project` | 项目名（影响导出文件命名） | fetch / render / floors |
| `-y` / `--yes` | 跳过阈值确认（瓦片数 / 窗口数超上限、层数扫描耗时等 `[y/N]` 提示） | fetch / render / floors |
| `-c` / `--config` | 任务配置文件（YAML，见 [§2.3](#23-任务配置文件-c--config)） | fetch / render / floors / vectorize |
| `--config-dir` | 指定配置目录（sources/keys/limits.yaml，默认仓库内置 `configs/`） | 全局 |
| `-v` / `--verbose` | 打印 DEBUG 日志 | 全局 |
| `--version` | 查看版本 | 全局 |

### 2.3 任务配置文件（`-c` / `--config`）

四个数据命令（`fetch` / `render` / `floors` / `vectorize`）默认读 `configs/config.yaml`（也可用 `-c` 指向其他 YAML），**推荐一个项目 = 一份配置**，参数多时比一长串命令行清晰得多。

**结构**：顶层分「公共节 `common`」+ 各命令节，字段与命令行参数一一对应：

```yaml
common:                      # 所有命令共用的公共上下文
  aoi: "admin:310109"
  out: run/demo/             # 项目输出目录
  project: demo              # 导出文件名前缀
  yes: false                 # true = 跳过交互确认

fetch:                       # 引擎 A
  source: tianditu
  style: img_w
  levels: "15-17"            # 或 [15,16,17]
  output_crs: CGCS2000
  no_mosaic: false

render:                      # 引擎 B
  template: bld
  zoom: 17
  window: 2000
  reclassify: true
  threshold: null            # null = 用默认（道路 120 / 建筑 40）

floors:                      # 3D 遮挡法
  floor_min: 0
  floor_max: 36
  floor_step: 3

vectorize:                   # 后处理
  kind: floors
  input: null                # 缺省按 kind 自动定位上游产物（见 §5）
  out: run/demo/vector/demo_floors.gpkg
  min_area: 10.0
  threshold: 100
  simplify: 0.7              # 道路中心线抽稀容差（像素，0=禁用；消除斜向道路台阶锯齿）
  floor_step: 1              # kind=floors 高度换算步长（须与 floors.floor_step 一致；默认 3）
```

完整带注释示例见 [`configs/example.yaml`](../configs/example.yaml)。

**优先级**：命令行同名参数 > 命令节 > `common` 节 > 内置默认。

```bash
mapfetch render                                # 默认读 configs/config.yaml
mapfetch render --zoom 18                      # 命令行覆盖配置的 zoom
mapfetch render --template rd                  # 覆盖配置的 template
```


### 2.4 输出目录约定

所有产物统一写入 `--out` 指定的目录（本工具约定用 `run/` 作为运行时产物根目录，见 [§8](#8-输出物与元数据)）。重复运行同一 `--out` 会复用缓存、断点续跑。

---

## 3. 引擎 A · 瓦片直下（fetch）

**适用场景**：大范围**影像 / 街道底图**，直接下载瓦片并拼接成 GeoTIFF。速度快、内存省，适合整区整市。

### 3.1 命令语法

```bash
mapfetch fetch --aoi <AOI> --source <源> --style <样式> --levels <层级> --out <目录> [选项]
```

**任务配置文件方式（推荐，参数多时更清晰）**：把全部参数写进 `configs/config.yaml`，命令默认读取。

```bash
cp configs/example.yaml configs/config.yaml   # 复制示例后按需修改
mapfetch fetch                                # 默认读 configs/config.yaml
mapfetch fetch --levels 15                    # 命令行参数覆盖 YAML
```

配置字段与命令行参数一一对应，完整示例见 [`configs/example.yaml`](../configs/example.yaml)：

```yaml
common:
  aoi: "admin:310109"    # rect: / circle: / admin: / file:
  out: run/demo/
  project: demo
  yes: false

fetch:
  source: tianditu
  style: img_w
  levels: "15-17"        # 或 [15,16,17]
  output_crs: CGCS2000
  no_mosaic: false
```

### 3.2 参数说明

> 标注 ✅ 的参数也可改由 `-c` 任务配置文件提供（命令行同名参数优先覆盖）。

| 参数 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- |
| `-c` / `--config` | ❌ | — | 任务配置文件（YAML），含下列全部参数 |
| `--aoi` | ✅ | — | AOI 规格（见 [§2.1](#21-aoi-规格--aoi-的四种写法)） |
| `--source` | ❌ | `tianditu` | 数据源：`tianditu` / `amap` / `baidu` |
| `--style` | ✅ | — | 图层样式 key（见下表） |
| `--levels` | ✅ | — | 缩放层级，如 `15-17` 或 `15,16,17` |
| `--out` | ✅ | — | 项目输出目录 |
| `--project` | ❌ | `mapfetch_project` | 项目名（导出文件名前缀） |
| `--output-crs` | ❌ | `EPSG:4490` | 成果坐标系（默认 CGCS2000，可选 WGS84） |
| `--no-mosaic` | ❌ | 关 | 只下载瓦片、不拼接 GeoTIFF |
| `-y` | ❌ | — | 跳过瓦片数超阈值确认（`[y/N]` 提示） |

### 3.3 数据源 × 样式 × 层级对照

层级超出范围会在下载前直接报错（如 `层级 z19 超出 tianditu/img_w 范围 [1, 18]`）。

**天地图（默认，CGCS2000 直出，无偏移）**

| style | 名称 | 层级上限 |
| --- | --- | --- |
| `img_w` | 影像底图 | 18 |
| `cia_w` | 影像注记 | 18 |
| `vec_w` | 矢量底图 | 18 |
| `cva_w` | 矢量注记 | 18 |
| `ter_w` | 地形晕渲 | 17 |
| `ibo_w` | 全球境界 | 10 |

**高德（GCJ02）**

| style | 名称 | 层级上限 |
| --- | --- | --- |
| `satellite` | 影像 | 18 |
| `street` | 街道详图 | 19 |
| `street_lite` | 街道简图 | 19 |
| `road` | 道路纯图 | 19 |

**百度（BD09MC）**

| style | 名称 | 层级上限 |
| --- | --- | --- |
| `street` | 街道图 | 19 |
| `satellite` | 卫星 | 19 |

### 3.4 典型示例

```bash
# ① 行政区 + 天地图影像，z15–17，输出 GeoTIFF（CGCS2000）——最常用
mapfetch fetch --aoi admin:310109 --source tianditu --style img_w --levels 15-17 --out run/demo/

# ② 矩形框选 + 天地图矢量底图（瓦片小、加载快）
mapfetch fetch --aoi "rect:121.520,31.260,121.545,31.280" --source tianditu --style vec_w --levels 15-17 --out run/demo/

# ③ 高德影像
mapfetch fetch --aoi "rect:121.520,31.260,121.545,31.280" --source amap --style satellite --levels 15-17 --out run/demo/ -y

# ④ 只下载瓦片、不拼接（后续可再跑一次拼接）
mapfetch fetch --aoi admin:310109 --source tianditu --style img_w --levels 15-17 --out run/demo/ --no-mosaic
```

### 3.5 输出物

- `raster/tianditu_img_w_z15/export/<project>_z15.tif`（逐层 GeoTIFF，WGS84）
- `aoi/aoi.gpkg`（本次 AOI 边界，WGS84）
- `manifest.json`（元数据与数据来源署名）

---

## 4. 引擎 B · JS 渲染截图（render / floors）

**适用场景**：需要**分要素**的栅格——道路网络、建筑图底、绿地水体背景等。通过高德 JS API 渲染后截图，再交给 `vectorize` 转矢量。

> 引擎 B 依赖 **Firefox + geckodriver**，配置方法见 [引擎 B 配置](engine-b-setup.md)。

### 4.1 渲染模板说明

`render` 命令通过 `--template` 选择渲染什么要素：

| template | 渲染内容 | 适用后续矢量化 |
| --- | --- | --- |
| `rd` | 仅道路 | `vectorize --kind road` |
| `bld` | 仅建筑 | `vectorize --kind building` |
| `water` | 仅水系（自动分离为二值图，通道主导判定） | `vectorize --kind water` |
| `green` | 仅绿地（自动分离为二值图，通道主导判定） | `vectorize --kind green` |

> `bld3d` 是 `floors` 命令内部使用的 3D 模板，不出现在 `render` 的 `--template` 选项中。
> `water`/`green` 共用 `features:['bg']` 渲染（高德默认样式，保留绿地/水系的绿/蓝配色），
> 渲染后自动按「通道主导」（绿/蓝通道显著高于其余通道）分离成水系/绿地二值图
> （`*_bin.png`），无论高德默认样式还是自定义样式的配色均适用，无需 `--reclassify`。

### 4.2 render 渲染要素栅格

```bash
mapfetch render --aoi <AOI> --template <模板> --zoom <层级> --out <目录> [选项]
mapfetch render -c configs/config.yaml                      # 参数改由任务配置提供
```

| 参数 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- |
| `-c` / `--config` | ❌ | — | 任务配置文件（YAML，见 [§2.3](#23-任务配置文件-c--config)） |
| `--aoi` | ✅ | — | AOI 规格（同 fetch；可由任务配置提供） |
| `--template` | ✅ | — | `rd` / `bld` / `water` / `green`（可由任务配置提供） |
| `--zoom` | ❌ | `17` | 缩放层级 14–20，**建筑需 ≥17** |
| `--window` | ❌ | `2000` | 单窗像素边长（越大越省次数，但更吃内存） |
| `--wait` | ❌ | `1.0` | 每次移动窗口后的等待秒数（网络差可调大） |
| `--reclassify` | ❌ | 关 | 道路/建筑二值化输出（黑底白要素，供矢量化） |
| `--threshold` | ❌ | 道路120/建筑40 | 二值化亮度阈值（道路：高于 120×3 为前景；建筑：blue 样式**建筑比背景暗**，低于 40×3 为前景） |
| `--no-merge` | ❌ | 关 | 关闭拼接，按 4×4 窗分块输出（块在 GIS 中按 PGW 叠合；**默认已输出单张完整拼接图**） |
| `--headless` | ❌ | 关 | 无头模式运行 Firefox（不弹窗） |
| `--out` | ✅ | — | 输出目录（可由任务配置提供） |
| `--project` | ❌ | `mapfetch_render` | 项目名 |
| `-y` | ❌ | — | 跳过窗口数超阈值确认（`[y/N]` 提示） |

**典型示例**：

```bash
# ① 道路栅格 + 二值化（供矢量化，最常用）
mapfetch render --aoi "rect:121.520,31.260,121.545,31.280" --template rd --zoom 17 --headless --reclassify --out run/demo/ -y

# ② 建筑栅格 + 二值化（zoom 必须 ≥17）
mapfetch render --aoi admin:310109 --template bld --zoom 17 --headless --reclassify --out run/demo/ -y

# ③ 水系栅格（自动分离为二值图）
mapfetch render --aoi "rect:121.520,31.260,121.545,31.280" --template water --zoom 17 --headless --out run/demo/ -y

# ④ 绿地栅格（自动分离为二值图）
mapfetch render --aoi "rect:121.520,31.260,121.545,31.280" --template green --zoom 17 --headless --out run/demo/ -y
```

**输出**：默认输出单张完整拼接图 `{project}_{template}.png`，并**额外生成按 AOI 裁剪版**
`{project}_{template}_aoi.png`（AOI 外透明，对齐 fetch 的 `_aoi.tif`）。`--reclassify`（rd/bld）
或 water/green 自动分离出的二值图 `*_bin.png` 同样附带 `*_bin_aoi.png` 裁剪版。

> **自定义样式**：`rd` 模板默认用高德官方预置 darkblue（道路为暗色，二值化效果差），
> 建议在高德控制台「自定义地图」制作样式（背景纯黑、道路纯白），把样式 ID 填入
> `configs/keys.txt` 的 `amap_mapstyle`，并配合 `amap_security` 安全密钥（见 [FAQ §9.5](#95-自定义样式不生效)）。
> `bld` 模板固定用官方预置 blue（免费版自定义地图无法设置建筑物图层，blue 的建筑
> 为亮色填充，配合 `binarize_building` 阈值 55 提取）。
>
> **水系/绿地与道路步骤一致**：免费版自定义地图只能发布 1 个样式，因此**样式 ID
> 保持不变**（`amap_mapstyle` 填一次即可）。每次根据要获取的要素，在高德控制台
> 「自定义地图」改样式设置即可——例如获取绿地时：其余内容设为不显示、绿地设为
> 显示、并采用系统默认颜色（后续二值化按通道主导自动提取）。然后用
> `--template water` / `--template green` 单独获取对应图层（不填样式 ID 时回退默认配色）。

### 4.3 floors 建筑层数扫描

通过 3D 遮挡法逐层截图推断建筑层数，**固定 z17**。

```bash
mapfetch floors --aoi <AOI> --floor-min <最小层> --floor-max <最大层> --floor-step <步长> --out <目录> [选项]
mapfetch floors -c configs/config.yaml                      # 参数改由任务配置提供
```

| 参数 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- |
| `-c` / `--config` | ❌ | — | 任务配置文件（YAML，见 [§2.3](#23-任务配置文件-c--config)） |
| `--aoi` | ✅ | — | AOI 规格（可由任务配置提供） |
| `--floor-min` | ❌ | `0` | 起始层数 |
| `--floor-max` | ❌ | `36` | 结束层数 |
| `--floor-step` | ❌ | `3` | 步长（层数间隔） |
| `--wait` | ❌ | `1.0` | 窗口等待秒数 |
| `--headless` | ❌ | 关 | 无头模式 |
| `--no-merge` | ❌ | 关 | 按窗口分文件输出（**默认合并为单张完整层数图**） |
| `--out` | ✅ | — | 输出目录（可由任务配置提供） |
| `--project` | ❌ | `mapfetch_floors` | 项目名 |
| `-y` | ❌ | — | 跳过层数扫描耗时确认（`[y/N]` 提示） |

**典型示例**：

```bash
# 扫描 0–36 层，每 3 层一档（0,3,6,…,36）
mapfetch floors --aoi "rect:121.520,31.260,121.545,31.280" --floor-min 0 --floor-max 36 --floor-step 3 --headless --out run/demo/ -y
```

> 层数扫描耗时较长（每档 × 窗口数），建议先用小范围 `rect:` 试跑。

**输出与数据用法**：

- 输出为**单通道灰度 PNG**（默认单张 `{project}_floors.png` + 同名 `.pgw` 世界文件，WGS84 定位），像素灰度值 = 层数扫描档位（如扫描 0/3/6…/36 时，像素值 3 即该处建筑高度落在第 3 档）。同时生成按 AOI 裁剪版 `{project}_floors_aoi.png`（AOI 外为 0，对齐 fetch 的 `_aoi.tif`）。
- **高度换算**：像素值 v 对应估算建筑高度区间 `[v×3, (v+step)×3) m`（每层按 3 m，见 `floor_to_height_range`）。**矢量化时须用相同步长**：`vectorize --floor-step`（或配置 `vectorize.floor_step`）与 floors 命令的 `--floor-step` 保持一致，否则 `height_max_m` 会按默认步长 3 换算而出错。
- **转为矢量**：用 `vectorize --kind floors` 提取「可见建筑面」并附带参考高度区间：

  ```bash
  mapfetch vectorize --input run/demo/raster/floors_z17/mapfetch_floors_floors.png --kind floors --out run/demo/vector/export/demo_floors.gpkg
  ```

- **在 GIS 中使用**：GPKG 中的面要素含 `floor`（层数档位）、`height_min_m` / `height_max_m`（估算高度区间，单位 m）字段，可直接用于建筑高度可视化等；栅格灰度图也可在 QGIS/ArcGIS 中按灰度分级渲染高度。

---

## 5. 栅格 → 矢量（vectorize）

把引擎 B 产出的要素栅格（PNG/GeoTIFF）反演为矢量要素，输出 GPKG/SHP/GeoJSON。

```bash
mapfetch vectorize --input <栅格> --kind <类型> --out <输出路径> [选项]
mapfetch vectorize -c configs/config.yaml                   # 参数改由任务配置提供
```

| 参数 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- |
| `-c` / `--config` | ❌ | — | 任务配置文件（YAML，见 [§2.3](#23-任务配置文件-c--config)） |
| `--input` | ❌ | 自动定位 | GeoTIFF 或 PNG（**需同名 `.pgw`）；缺省按 `kind` 自动定位上游产物 |
| `--kind` | ✅ | — | `road` / `building` / `water` / `green` / `floors`（可由任务配置提供） |
| `--out` | ✅ | — | 输出 `.gpkg` / `.shp` / `.geojson`（可由任务配置提供） |
| `--min-area` | ❌ | `10.0` | 小面剔除阈值（m²） |
| `--threshold` | ❌ | `100` | 二值化亮度阈值 |
| `--simplify` | ❌ | `0.7` | 道路中心线抽稀容差（像素，`0`=禁用；消除斜向道路的台阶锯齿，仅 kind=`road` 生效） |
| `--floor-step` | ❌ | `3` | kind=`floors` 时的高度换算步长（**须与 floors 命令的 `--floor-step` 一致**） |

**kind 与输入对应关系**：

| kind | 输入来源 | 提取方式 | 输出 |
| --- | --- | --- | --- |
| `road` | `render --template rd --reclassify` | 骨架 → 中心线（抽稀拉直） | 道路 LineString |
| `building` | `render --template bld --reclassify` | 面重构 + 正交化 | 建筑 Polygon |
| `water` | `render --template water`（`*_bin.png`） | 面矢量化 | 水系 Polygon |
| `green` | `render --template green`（`*_bin.png`） | 面矢量化 | 绿地 Polygon |
| `floors` | `floors` 产物 | 层数栅格提取 | 层数面 |

> **道路中心线抽稀（`--simplify`）**：`kind=road` 的道路骨架由像素级逐点连线组成，
> 斜向道路会呈现明显的台阶锯齿。`--simplify` 会在**像素空间**先做 Douglas-Peucker 抽稀，
> 再合并「三点夹角 >170°」的近共线中间点，把台阶压成直线段——容差以**像素**为单位、
> 与缩放层级无关，默认 `0.7`；设 `0` 则完全禁用（保留骨架原始逐像素顶点）。
> 抽稀仅在道路分支生效，`building` / `water` / `green` / `floors` 不受影响。

**典型示例**：

```bash
# 道路栅格 → 道路中心线 GPKG
mapfetch vectorize --input run/demo/raster/render_rd_z17/demo_rd_0_0_bin.png --kind road --out run/demo/vector/export/demo_roads.gpkg

# 建筑栅格 → 建筑面 GPKG
mapfetch vectorize --input run/demo/raster/render_bld_z17/demo_bld_0_0_bin.png --kind building --out run/demo/vector/export/demo_buildings.gpkg
```

**`--input` 自动定位**：不写 `--input`（或配置里 `input: null`）时，按 `kind` 自动到
`common.out`（或 `vectorize.out_dir`）下找 render/floors 的产物：

| kind | 自动定位路径 |
| --- | --- |
| `floors` | `{out}/raster/floors_z17/{project}_floors.png` |
| `road` | `{out}/raster/render_rd_z{zoom}/{project}_rd_bin.png`（无则回退 `_rd.png`） |
| `building` | `{out}/raster/render_bld_z{zoom}/{project}_bld_bin.png`（无则回退 `_bld.png`） |
| `water` | `{out}/raster/render_water_z{zoom}/{project}_water_bin.png` |
| `green` | `{out}/raster/render_green_z{zoom}/{project}_green_bin.png` |

其中 `{project}` 取 `common.project`（缺省 `mapfetch_render` / `mapfetch_floors`）、
`{zoom}` 取 `vectorize.zoom`（缺省 `17`）。一条命令即可完成 floors → 矢量：

```bash
mapfetch vectorize -c configs/config.yaml   # kind: floors，input 自动定位
```


---

## 6. 辅助功能（aoi / doctor / gui）

### 6.1 aoi 行政区边界查询

查询行政区边界并落盘（默认 GeoPackage，也支持 Shapefile / GeoJSON）。数据源为**阿里云 DataV GeoAtlas**——详细边界，公开接口、无需 Key。

```bash
mapfetch aoi --name <名称或编码> --out <输出路径>
```

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `--name` | ✅ | 行政区名称或 6 位国标编码，如 `上海市` / `310109` |
| `--out` | ✅ | 输出 `.gpkg` / `.shp` / `.geojson` |

**示例**：

```bash
mapfetch aoi --name 上海市 --out run/demo/aoi/shanghai.gpkg
mapfetch aoi --name 310109 --out run/demo/aoi/hongkou.gpkg
```

> 边界落盘为 WGS84 , DataV 源坐标为 GCJ-02，工具已自动纠偏，**无需任何 Key**。

### 6.2 doctor 环境自检

检查 Python 版本、依赖、Firefox/geckodriver、各数据源 Key 是否就绪。

```bash
mapfetch doctor
```

输出以 `[OK]` / `[FAIL]` / `[WARN]` 标注，`FAIL` 项需先解决对应问题再跑对应命令。

### 6.3 gui 图形界面

启动 PySide6 图形界面（Leaflet 交互式区域选择 + 瓦片预估 + 一键抓取）。

```bash
mapfetch gui
```

> 启动即默认加载天地图**静态地图服务**在线底图，初始中心点为上海市人民广场（z12）；默认只显示静态底图、不再自动叠加本地缓存瓦片（抓取数据已落盘到输出目录），取消勾选「在线底图」或静态图加载失败时回退本地缓存预览。底图固定矢量（`vec_w`），加载失败时自动降级为影像底图（`img_w`）。

> 地图上绘制矩形框选/多边形后，工具会**自动捕获 AOI**（无需再点按钮），并把边界保存为输出目录下的 `aoi/aoi.gpkg`, 随后「计算瓦片预估」「开始抓取」直接使用绘制的范围。

> 使用「手动输入规格」或「行政区（名称/adcode）」设置 AOI 时，可点「在地图中显示AOI范围」把边界（矩形/圆形/文件多边形/行政区边界）叠加显示到地图上；显示后「从地图取回 AOI / 计算瓦片预估 / 开始抓取」均可正常使用。行政区边界的解析需联网（走 DataV GeoAtlas）。

---

## 7. 端到端完整工作流示例

以「上海市虹口区道路网络矢量」为例，完整链路如下：

```bash
# 第 1 步：环境自检
mapfetch doctor

# 第 2 步（可选）：查行政区边界，确认范围
mapfetch aoi --name 310109 --out run/hongkou/aoi/hongkou.gpkg

# 第 3 步：引擎 B 渲染道路栅格（z17，二值化）
mapfetch render --aoi admin:310109 --template rd --zoom 17 --headless --reclassify --out run/hongkou/

# 第 4 步：栅格 → 道路中心线矢量 GPKG
mapfetch vectorize --input run/hongkou/raster/render_rd_z17/hongkou_rd_0_0_bin.png \
    --kind road --out run/hongkou/vector/export/hongkou_roads.gpkg
```

同样方法可产出**建筑面**（`--template bld` + `--kind building`）、**水系面**（`--template water` + `--kind water`）与**绿地面**（`--template green` + `--kind green`）。

若只需**影像底图**，直接走引擎 A：

```bash
mapfetch fetch --aoi admin:310109 --source tianditu --style img_w --levels 15-17 --out run/hongkou/
```

---

## 8. 输出物与元数据

每次 `fetch` / `render` / `floors` 运行后，`--out` 目录下会生成：

```
run/<项目>/
├── manifest.json          # 任务元数据：来源/层级/坐标系/署名/配额消耗/告警
├── aoi/aoi.gpkg           # AOI 边界（GeoPackage，CGCS2000/EPSG:4490）
├── raster/{source}_{style}_z{n}/export/*.tif   # GeoTIFF（默认 CGCS2000）
│                          #   {project}_z{n}.tif     完整拼接影像
│                          #   {project}_z{n}_aoi.tif 按 AOI 裁剪（外透明）
├── raster/render_{template}_z{n}/*.png          # 引擎 B 要素栅格（默认单张拼接图 + 二值图）
│                          #   {project}_{template}.png     完整拼接图
│                          #   {project}_{template}_aoi.png 按 AOI 裁剪（外透明）
│                          #   {project}_{template}_bin.png 二值图（rd/bld 需 --reclassify；water/green 自动）
│                          #   {project}_{template}_bin_aoi.png 二值图 AOI 裁剪版
├── raster/floors_z17/*.png                      # 层数栅格（默认单张 {project}_floors.png + _floors_aoi.png 裁剪版）
└── vector/export/*.gpkg                         # 矢量要素（GeoPackage）
```

- 所有成果默认坐标系 **CGCS2000（EPSG:4490）**，与国内 GIS 数据可直接叠加。
- `manifest.json` 记录完整溯源信息与数据来源署名（`attribution` 字段）。

---

## 9. 常见问题（FAQ）

### 9.1 层级最高能填多少？

天地图主节点影像/矢量底图**最高 18 级**；高德 `street`/`road`、百度最高 19 级。详见 [§3.3](#33-数据源--样式--层级对照)。

层级每升一级，瓦片数约 **×4**。城市研究通常 17 级已够用，18 级建议缩小到 `rect:` 范围控制瓦片量。

### 9.2 提示「层级 zXX 超出 … 范围」？

填的层级超过了该源/样式上限。查 [§3.3](#33-数据源--样式--层级对照) 对照表，改用合法层级。

### 9.3 不加引号报 `unrecognized arguments`？

`--aoi` 值里逗号后带了空格、又没加引号，shell 会把空格当参数分隔符。两种解法：

```bash
# 解法一：加引号（推荐）
--aoi "rect:121.520, 31.260, 121.545, 31.280"

# 解法二：逗号后不要空格
--aoi rect:121.520,31.260,121.545,31.280
```

> 工具已对「不加引号」做了容错（`--aoi` 支持多段拼接），两种写法现在都能跑通，但加引号更规范。

### 9.4 自定义样式不生效 / 道路看不清？

引擎 B 的 `rd` 模板默认用高德官方预置 darkblue，道路为暗色、与背景对比度低。若已在高德控制台发布了自定义样式但仍不生效，检查两点：

1. **样式 ID 填写**：`configs/keys.txt` 的 `amap_mapstyle` 填入发布后的样式 ID；
2. **安全密钥**：2021-12 后申请的 Key 必须同时配置 `amap_security`（安全密钥 securityJsCode），否则自定义样式回退默认。见 [README §配置 Key](../README.md)。

> 注意：`amap_mapstyle` 作用于 `rd`/`water`/`green` 模板。免费版只能发布 1 个样式，
> 样式 ID **保持不变**（填一次即可）；每次根据要获取的要素在高德控制台「自定义地图」
> 改样式设置（如获取绿地：其余不显示、绿地显示、用默认颜色）即可，无需改 ID。

### 9.5 引擎 B 报 `NoSuchDriverException` / 找不到 geckodriver？

把 geckodriver 放进系统 PATH，或用环境变量 `MAPFETCH_GECKODRIVER` 指定完整路径（Firefox 用 `MAPFETCH_FIREFOX_BINARY`）。详见 [引擎 B 配置](engine-b-setup.md)。

### 9.6 运行报 `Cannot find gdalvrt.xsd` 警告？

这是 GDAL 数据目录未找到的无害警告，工具入口已自动探测补设；如仍出现，可手动设置环境变量 `GDAL_DATA` 指向 GDAL 的 `share/gdal` 目录。

---

## 10. 合规提醒

本工具按 **B+C 合规策略**运行，使用前请注意：

- **默认数据源为天地图**（国家官方平台，合规性最高）；
- 高德/百度的瓦片批量下载能力予以保留，但其平台协议明确限制数据抓取与存储，**相关风险由使用者自行承担**；
- 工具不提供任何规避风控的手段（无 UA 伪装、无代理池、无多 Key 分摊）；
- 下载数据**仅限内部研究使用**，不得再分发、转售或用于模型/算法训练；
- 成果精度受源数据制约，**不得用于法定测绘与涉密用途**。

