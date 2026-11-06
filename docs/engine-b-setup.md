# 引擎 B 环境配置指南（Firefox + geckodriver）

引擎 B（`mapfetch render` / `mapfetch floors`）用 **Selenium 4 + Firefox + geckodriver** 驱动高德 JS API 页面做要素渲染截图。运行前需满足三项前置条件，缺一不可：

1. **selenium** Python 包（已包含在 `requirements.txt` / `environment.yml` 中）；
2. **Firefox** 浏览器（64 位）；
3. **geckodriver** 驱动（与 Firefox 版本匹配）。

本文按操作系统分别说明安装、路径指定、版本兼容与验证方法。

---

## 0. 版本兼容性（先看这一节，避免踩坑）

geckodriver 是"浏览器与 Selenium 之间的翻译器"。版本对应遵循两条原则：

- **向后兼容**：新版 geckodriver 通常能驱动较新乃至较旧的 Firefox；
- **最低版本门槛会随 geckodriver 升高**：老 geckodriver 无法驱动"比它新很多"的 Firefox（协议/特性对不上，表现为启动失败或功能异常）。

**推荐做法：Firefox 和 geckodriver 都装最新版**，最省心。若 Firefox 因故固定在某个老版本，再按下表选对应 geckodriver：

| geckodriver 版本 | 支持的 Firefox 版本 |
|---|---|
| 0.37.x（最新） | 147+（完整特性需 152+） |
| 0.35.x | 115+ |
| 0.33.x | 102+ |
| 0.30.x | 90+ |

> 架构注意：macOS Apple Silicon（M 系列）要下 **arm64/aarch64** 版 geckodriver，Intel Mac 下 x64 版；Linux 自 geckodriver 0.37 起只提供 x86_64。

---

## 1. Windows

### 1.1 安装 Firefox

- 官网下载 64 位安装包：<https://www.mozilla.org/firefox/all/>，或命令行：
  ```powershell
  winget install Mozilla.Firefox
  ```
- 默认安装路径：`C:\Program Files\Mozilla Firefox\firefox.exe`（在 PATH 之外的默认目录也能被 Selenium 按注册表找到）。

> ⚠️ 常见错误：把 `...\firefox.exe` **文件路径**加进 PATH——PATH 条目必须是**目录**（`C:\Program Files\Mozilla Firefox`），放文件路径不会让 `firefox` 命令生效。不想改 PATH 就改用 §4 的 `MAPFETCH_FIREFOX_BINARY`（这个变量才是填完整文件路径）。

### 1.2 安装 geckodriver

1. 下载最新版：<https://github.com/mozilla/geckodriver/releases>，选择 `geckodriver-v{版本}-win64.zip`；
2. 解压得到 `geckodriver.exe`，放到一个固定目录，例如 `C:\WebDriver\`；
3. 把该目录加入系统 PATH（**这一步是 `doctor` 检测通过的关键**，因为检测逻辑是"在 PATH 里找"）：
   ```powershell
   # 图形界面：设置 → 系统 → 关于 → 高级系统设置 → 环境变量 → Path → 新建 C:\WebDriver
   # 或管理员 PowerShell 一行完成（当前用户级）：
   [Environment]::SetEnvironmentVariable("Path", $env:Path + ";C:\WebDriver", "User")
   ```
   也可以用 winget 自动装到 PATH：`winget install Mozilla.GeckoDriver`。

### 1.3 验证

新开一个终端（使 PATH 生效），运行：

```powershell
firefox --version        # 或 geckodriver --version
geckodriver --version
```

---

## 2. macOS

### 2.1 安装 Firefox

```bash
brew install --cask firefox
# 或官网下载 dmg：https://www.mozilla.org/firefox/all/
```

默认可执行文件路径：`/Applications/Firefox.app/Contents/MacOS/firefox`。

### 2.2 安装 geckodriver

```bash
brew install geckodriver      # 自动装到 /opt/homebrew/bin（Apple Silicon）或 /usr/local/bin（Intel），已在 PATH
```

或手动：下载 `geckodriver-v{版本}-macos-{aarch64|x64}.tar.gz`，解压后 `sudo mv geckodriver /usr/local/bin/`。

### 2.3 验证

```bash
which firefox && firefox --version
geckodriver --version
```

> 若首次启动 Firefox 触发 macOS Gatekeeper，需在"系统设置 → 隐私与安全性"里允许。

---

## 3. Linux

### 3.1 安装 Firefox

```bash
# Debian / Ubuntu
sudo apt update && sudo apt install -y firefox
# Fedora
sudo dnf install -y firefox
```

默认可执行文件路径：`/usr/bin/firefox`。

> ⚠️ Ubuntu 22.04 起的 Firefox 是 snap 容器包，与 geckodriver 0.37 有已知的启动挂起问题（容器内文件系统隔离导致 profile 目录访问异常）。若遇到启动 hang，优先改用发行版仓库的 deb 版（`sudo apt install firefox` 的 apt 版）或 Mozilla 官网 tar 包。

### 3.2 安装 geckodriver

```bash
# 下载 linux64 版并解压到 /usr/local/bin（已在 PATH）
VERSION=0.37.1
wget "https://github.com/mozilla/geckodriver/releases/download/v${VERSION}/geckodriver-v${VERSION}-linux64.tar.gz"
tar -xzf geckodriver-v${VERSION}-linux64.tar.gz
sudo mv geckodriver /usr/local/bin/
sudo chmod +x /usr/local/bin/geckodriver
```

### 3.3 验证

```bash
which firefox && firefox --version
geckodriver --version
```

---

## 4. 非默认安装路径：用环境变量显式指定

如果 Firefox 或 geckodriver 装在非默认位置、又不想改系统 PATH，可直接用两个环境变量显式指定完整路径（引擎 B 已支持）：

| 环境变量 | 含义 | 示例 |
|---|---|---|
| `MAPFETCH_FIREFOX_BINARY` | Firefox 可执行文件的完整路径 | `C:\Mozilla\firefox.exe` |
| `MAPFETCH_GECKODRIVER` | geckodriver 可执行文件的完整路径 | `D:\tools\geckodriver.exe` |

各系统设置方法（**持久化**，写进 shell 配置文件）：

**Windows（PowerShell，用户级）：**
```powershell
[Environment]::SetEnvironmentVariable("MAPFETCH_FIREFOX_BINARY", "C:\Mozilla\firefox.exe", "User")
[Environment]::SetEnvironmentVariable("MAPFETCH_GECKODRIVER", "D:\tools\geckodriver.exe", "User")
```

**macOS / Linux（写入 `~/.zshrc` 或 `~/.bashrc` 后 `source` 生效）：**
```bash
echo 'export MAPFETCH_FIREFOX_BINARY="/opt/firefox/firefox"' >> ~/.zshrc
echo 'export MAPFETCH_GECKODRIVER="/opt/drivers/geckodriver"' >> ~/.zshrc
source ~/.zshrc
```

> 设置这两个环境变量后，`mapfetch doctor` 会同时检查"PATH 里有"或"环境变量指向的文件存在"，任一满足即判定通过；引擎 B 启动时也会优先使用它们。

---

## 5. 高德安全密钥（JS API 2.0 自定义样式必需）

2021-12-02 之后申请的高德 Key 属于「新版鉴权」，**必须同时配置安全密钥（securityJsCode）**：

- 不配置时：官方预置样式（如 darkblue）能正常显示，但**自定义样式不生效**（回退默认样式）；
- 获取：高德控制台 → 我的应用 → 对应 amap_js Key → 复制 `securityJsCode`；
- 配置（二选一，keys.txt 推荐）：

  ```text
  # configs/keys.txt
  amap_security=你的安全密钥
  ```

  或环境变量 `MAPFETCH_SECURITY_AMAP`（Windows：`setx MAPFETCH_SECURITY_AMAP "密钥"`）。

引擎 B 会在渲染页面里自动注入 `window._AMapSecurityConfig`（位于 AMap 加载之前），无需手动改模板。

另外注意：**mapStyle 必须是完整格式 `amap://styles/<样式ID>`**——只填裸样式 ID 会被忽略（回退默认样式）。`amap_mapstyle` 配置里两种写法现在都兼容（代码自动补前缀）。

> **样式 ID 固定不变**：免费版自定义地图只能发布 1 个样式，`amap_mapstyle` 填一次即可。
> 每次根据要获取的要素，在高德控制台「自定义地图」里改样式设置（如获取绿地：其余内容设为不显示、绿地设为显示、用系统默认颜色），样式 ID 不变，直接用 `--template water` / `--template green` 分别获取对应图层。

## 6. 验证方法

### 5.1 重新运行资源检测

```bash
mapfetch doctor
```

预期两项从 `[FAIL]` 变为 `[OK]`：

```
[OK ] Firefox
[OK ] geckodriver
```

### 5.2 最小启动测试（确认引擎 B 真能拉起 Firefox）

保存为 `test_engine_b.py` 并运行，能打印出浏览器版本即通过：

```python
import os
from selenium import webdriver
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.firefox.service import Service

opts = Options()
opts.add_argument("-headless")                       # 无窗口验证；要看到窗口可去掉这行
fb = os.environ.get("MAPFETCH_FIREFOX_BINARY", "")
if fb:
    opts.binary_location = fb
gd = os.environ.get("MAPFETCH_GECKODRIVER", "")
service = Service(executable_path=gd) if gd else Service()

driver = webdriver.Firefox(options=opts, service=service)
driver.get("about:blank")
print("Firefox 启动成功，版本：", driver.capabilities.get("browserVersion"))
driver.quit()
```

```bash
python test_engine_b.py
# 期望输出：Firefox 启动成功，版本：15x.x
```

通过后，即可运行引擎 B 真实命令（需先配置高德 JS Key，见 `installation.md` §4）：

```bash
mapfetch render --aoi rect:120.30,36.05,120.42,36.12 --template rd --zoom 17 --headless --out run/demo
```

---

## 6. 常见问题速查

| 现象 | 原因 / 处理 |
|---|---|
| `doctor` 里 Firefox/geckodriver 为 FAIL | 未安装，或不在 PATH。装好并加入 PATH，或设 `MAPFETCH_*_BINARY` 环境变量后重开终端 |
| 启动报 `cannot find geckodriver` | geckodriver 不在 PATH 且未设 `MAPFETCH_GECKODRIVER`；补 PATH 或环境变量 |
| `Expected browser binary location` 报错 | Selenium 找不到 Firefox；设 `MAPFETCH_FIREFOX_BINARY` 指向 `firefox.exe`/`firefox` |
| 启动后窗口一闪即退 / 连接超时 | geckodriver 与 Firefox 版本不匹配；升级 geckodriver 或换对应版本 |
| Linux snap 版 Firefox 启动挂起 | 改用 apt 版或官网 tar 包（见 §3.1 提示） |
