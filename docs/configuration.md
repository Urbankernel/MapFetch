# 配置规格

配置采用 YAML + Pydantic 校验（启动时做类型检查，避免运行时才发现参数错）。三个配置文件职责分离：

## 1. `configs/sources.yaml` — 数据源注册与接口参数

```yaml
sources:
  tianditu:                       # 默认源（合规优先）
    enabled: true
    default: true
    crs: CGCS2000                 # 无加密偏移
    key_env: MAPFETCH_TK_TIANDITU # 环境变量名，不存明文
    referer: "https://lbs.tianditu.gov.cn/"  # 浏览器端 Key 走服务端直连时必填
    subdomains: [t0, t1, t2, t3, t4, t5, t6, t7]
    styles:
      img_w: { name: 影像底图, url: "https://{s}.tianditu.gov.cn/DataServer?T=img_w&x={x}&y={y}&l={z}&tk={tk}", max_level: 18 }
      cia_w: { name: 影像注记, url: "...T=cia_w..." }
      vec_w: { name: 矢量底图, url: "...T=vec_w..." }
      cva_w: { name: 矢量注记, url: "...T=cva_w..." }
      ter_w: { name: 地形晕渲, url: "...T=ter_w..." }
      ibo_w: { name: 全球境界, url: "...T=ibo_w..." }
    attribution: "数据来源：天地图（© 国家基础地理信息中心）"

  amap:
    enabled: true
    crs: GCJ02
    key_env: MAPFETCH_KEY_AMAP_JS
    styles:
      satellite: { name: 影像, url: "https://webst0{s}.is.autonavi.com/appmaptile?style=6&x={x}&y={y}&z={z}", max_level: 18 }
      street:    { name: 街道详图, url: "https://webrd0{s}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}" }
    rest:
      district: "https://restapi.amap.com/v3/config/district"
    render_templates: [rd, bld, water, green, bld3d]   # 引擎 B

  baidu:
    enabled: true
    crs: BD09MC                   # 瓦片编号基准
    key_env: MAPFETCH_KEY_BAIDU
    styles:
      street:    { name: 街道图, url: "http://online{s}.map.bdimg.com/onlinelabel/?qt=tile&x={x}&y={y}&z={z}&styles=pl&udt={udt}" }
      satellite: { name: 卫星, url: "http://shangetu{s}.map.bdimg.com/it/u=x={x};y={y};z={z};v=009;type=sate&fm=46" }
```

未公开参数（如高德 `ltype`、百度 `udt`）集中在本文件维护，启动自检失败时给出明确告警并降级。

### `referer`：浏览器端 Key 的直连前置条件

天地图 Key 分「浏览器端」与「服务端」两类。**浏览器端 Key 由服务端程序直连请求会被拒绝**：

```json
{"msg":"权限类型错误","resolve":"Key权限类型为:浏览器端，请使用浏览器访问！","code":301012}
```

表现为第一条请求即 403、任务秒级中止、`report.done == 0`，因此不会进入拼接环节（项目目录里只剩 `aoi/aoi.geojson`）。

两种处理方式：

| 方案 | 做法 | 说明 |
| --- | --- | --- |
| 配 `referer`（临时） | sources.yaml 填与控制台「Referer 白名单」一致的地址 | 实测带 Referer 后直连可正常返回瓦片；仍受浏览器端 Key 配额约束 |
| 换服务端 Key（推荐） | 控制台新建「服务端」类型应用，tk 写入 keys.txt，删除 `referer` | 服务端批量调用的正规授权方式，配额与服务条款均匹配 |

403 时工具会把服务端返回的 `code / msg / resolve` 原样写进 manifest 的 `warnings`，便于定位。

## 2. `configs/limits.yaml` — 限速与配额

```yaml
rate_limits:
  tianditu: { qps: 1, burst: 2, max_concurrent: 2, daily_budget: 8000 }
  amap_tile: { qps: 2, burst: 5, max_concurrent: 4, daily_budget: 5000 }
  amap_rest: { qps: 1, burst: 2, max_concurrent: 2, daily_budget: 1000 }
  baidu_tile: { qps: 2, burst: 5, max_concurrent: 4, daily_budget: 5000 }
  baidu_rest: { qps: 1, burst: 2, max_concurrent: 2, daily_budget: 1000 }

retry:
  max_attempts: 4
  backoff_base_sec: 1.0
  backoff_max_sec: 60
  jitter: 0.3

circuit_breaker:
  warn_at: 0.8        # 达日预算 80% 告警
  stop_at: 1.0        # 达 100% 熔断
  consecutive_429_threshold: 5

task_thresholds:
  max_tiles: 50000            # 超限需二次确认
  max_duration_hours: 4
  max_disk_gb: 20

cache:
  dir: cache/tiles
  expire_days: 30             # 呼应"不得长期存储"条款；过期标记不自动删除

user_override:
  allow_rate_increase: true   # 允许上调但需二次确认并写日志；不提供一键解除限速
```

## 3. `configs/keys.txt` — Key 配置（直填优先，环境变量备选）

```text
tianditu=      # 天地图 tk：引擎 A 天地图瓦片、GUI 在线预览（行政区边界已改走 DataV，无需 Key）
amap_js=       # 高德 Web端(JS API)：引擎 B 渲染（render/floors）
amap_rest=     # 高德 Web服务：备用（行政区查询已改走 DataV GeoAtlas，无需 Key）
amap_security= # 高德 JS API 2.0 安全密钥（securityJsCode）：自定义样式必需
amap_mapstyle= # 高德自定义地图样式 ID：引擎 B 渲染 rd/water/green 的 mapStyle（免费版仅 1 个样式，ID 固定不变，留空=内置默认）
baidu=         # 百度：引擎 A 百度瓦片
```

**取值三种写法**：`真实Key`（直填）、`env:VAR_NAME`（环境变量）、留空（回退 sources.yaml 的 `key_env` 默认变量名）。

**本机文件**：复制 `configs/keys.txt.example` 为 `configs/keys.txt` 填入真实 Key；`keys.txt` 已在 `.gitignore` 中排除，不会入库。

**解析顺序**：keys.txt 直填 → `env:` 环境变量 → sources.yaml `key_env` 环境变量 → 报错。

**规则**：日志中对 Key 脱敏（仅显示前 4 位）；`configs/keys.txt.example` 模板只应提交空占位版本；高德 JS API 与 Web 服务是两种 Key 类型，不可混用（详见 [installation.md §4](installation.md)）。

## 4. 项目级 `manifest.json`（自动生成，只读）

每个任务输出目录包含任务元数据：`project_name / created_at / tool_version / aoi / sources[] / crs(source, output, method) / limits 实际生效值 / attribution / precision_note / warnings[]`。用于成果可追溯与合规审计。
