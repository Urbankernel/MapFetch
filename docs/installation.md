# 安装与环境配置

## 1. 系统要求

| 项 | 要求 | 说明 |
|---|---|---|
| 操作系统 | Windows 10 / 11 x64 | 首期仅支持 Windows 桌面 |
| Python | **3.11 或 3.12** | 3.13 暂不支持（依赖 wheel 未完全就绪）；3.10 及以下不支持 |
| 内存 | ≥ 16 GB 推荐 | 大范围拼接已通过 VRT 流式处理优化，目标峰值 < 500 MB |
| Firefox | 最新版 64 位 | 仅引擎 B（要素渲染）需要 |
| geckodriver | 与 Firefox 匹配版本 | 仅引擎 B 需要 |

## 2. 安装

本项目以**源码工具**形式分发：克隆仓库、建环境、`pip install .` 注册命令即可。环境配置文件三份保持同步：`environment.yml`（conda，推荐）、`requirements.txt`（pip 运行时）、`requirements-dev.txt`（开发）。

**方式一 · conda（推荐，rasterio/geopandas 二进制依赖最稳）：**

```bash
git clone https://github.com/Urbankernel/MapFetch mapfetch && cd mapfetch
conda env create -f environment.yml
conda activate mapfetch
pip install .
```

**方式二 · pip + venv：**

```bash
git clone https://github.com/Urbankernel/MapFetch mapfetch && cd mapfetch
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
pip install .
```

**最小安装**（仅核心链路 + 引擎 A，无 GUI/引擎 B/矢量化）：`pip install PyYAML pydantic httpx`

**复制示例配置：**

```bash
cp configs/example.yaml configs/config.yaml   # 任务配置（默认读取）
cp configs/keys.txt.example configs/keys.txt  # 密钥（已由 .gitignore 排除，不入库）
```

> `pip install .` 会注册 `mapfetch` 命令；如未安装（仅想临时运行），也可 `PYTHONPATH=src python -m mapfetch ...` 等价运行。

## 3. 引擎 B 前置条件（Firefox + geckodriver）

仅在需要道路/建筑/背景要素分离或建筑层数扫描时配置：

1. 安装 64 位 Firefox（<https://www.mozilla.org/zh-CN/firefox/all/desktop-release/win64/zh-CN/>）
2. 下载 geckodriver（<https://github.com/mozilla/geckodriver/releases>），将 `geckodriver.exe` 放入 PATH 或 MapFetch 工作目录
3. 验证：运行 `mapfetch doctor`，应报告 Firefox 与 geckodriver 可用

> 完整的分操作系统安装、版本对应、非默认路径指定（`MAPFETCH_FIREFOX_BINARY` /
> `MAPFETCH_GECKODRIVER` 环境变量）与验证方法，见 **[engine-b-setup.md](engine-b-setup.md)**。

## 4. 数据源 Key 申请与配置

### 4.1 各 Key 的使用场景

| keys.txt 字段 | 平台与类型 | 使用场景（哪些功能用到它） | 申请方式 |
|---|---|---|---|
| `tianditu` | 天地图 tk | **引擎 A** 全部天地图瓦片（`img_w`/`vec_w`/`cia_w`/`cva_w`/`ter_w`/`ibo_w`）；GUI 在线预览 | [lbs.tianditu.gov.cn](https://lbs.tianditu.gov.cn) 注册实名 → 控制台创建应用 |
| `amap_js` | 高德 **Web端(JS API)** | **引擎 B** 渲染：`render` 子命令（rd/bld/water/green 模板）与 `floors` 层数扫描——HTML 模板页需要该 Key 加载 AMap JS API | [lbs.amap.com](https://lbs.amap.com) → 控制台 → 应用管理 → 添加 Key，类型选 **Web端(JS API)** |
| `amap_rest` | 高德 **Web服务** | 备用（**行政区边界查询已改用 DataV GeoAtlas**，不再需要此 Key） | 同上，添加 Key 类型选 **Web服务** |
| `baidu` | 百度 浏览器端/服务端 | **引擎 A** 百度瓦片（`street`/`satellite`） | [lbsyun.baidu.com](https://lbsyun.baidu.com) → 控制台 → 创建应用 |

⚠️ 注意事项：

- 高德的 **JS API Key 与 Web服务 Key 是两种类型，不可混用**——`amap_js` 用于引擎 B 页面渲染；`amap_rest` 在行政区查询改用 DataV GeoAtlas 后已无默认用途，保留仅为兼容。
- **行政区边界查询走 DataV GeoAtlas**：`aoi` 与 `admin:` 规格调用阿里云 DataV GeoAtlas（`geo.datav.aliyun.com/areas_v3`），取**详细边界**且无需任何 Key；名称与 6 位国标码（如 `admin:310109`）均可查询，GCJ-02 坐标已自动纠偏为 WGS84。天地图行政区划 API 边界为抽稀版，保留为备用（`TiandituAdapter.query_district`）。
-  Key 粘贴时**不要混入空格或换行**（会导致页面空白或鉴权失败）。
- 配额参考（个人开发者）：天地图 1 万次/日（按图层类别计）；高德、百度依各自控制台显示。工具内置的日配额预算低于官方上限。

### 4.2 秘钥配置方式（二选一，直填优先）

**方式一 · 配置文件直填**：复制示例 `configs/keys.txt.example` 为 `configs/keys.txt` 并填入（后者已被 .gitignore 排除、不会入库，**推荐**）：

```text
tianditu=你的天地图tk
amap_js=你的高德JS_API_Key
baidu=你的百度Key
```

**方式二 · 环境变量**：keys.txt 中对应字段写 `env:` 前缀，或留空回退 sources.yaml 的默认变量名：

```text
tianditu=env:MAPFETCH_TK_TIANDITU
```

```powershell
setx MAPFETCH_TK_TIANDITU "你的天地图tk"
```

**解析顺序**：keys.txt 直填值 → `env:` 指定的环境变量 → sources.yaml `key_env` 默认环境变量 → 报错（含配置提示）。日志中 Key 一律脱敏（仅显示前 4 位）。

**安全建议**：含真实 Key 的文件不要提交到任何仓库。`configs/keys.txt` 已在 `.gitignore` 中排除；`configs/keys.txt.example` 模板只应提交空占位版本。

## 5. 验证安装

```bash
mapfetch --version
mapfetch doctor    # 检查 Python 版本、依赖、Firefox、geckodriver、Key 配置
```

`doctor` 全部通过后，建议先以一个小区域（如某街道，z15）试跑，确认输出 GeoTIFF 在 QGIS/ArcGIS 中落位正确。

