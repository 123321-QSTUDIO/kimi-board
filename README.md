<div align="center">

<img src="extension/icons/128.png" alt="Kimi Board logo" width="88" />

# Kimi Board

**Kimi Code 本机 token 用量看板**

本地运行 · 零云端依赖 · 零第三方库

[![Release](https://img.shields.io/github/v/release/Pierre1231/kimi-board?style=flat-square)](../../releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-2e6fe8?style=flat-square)](LICENSE)
![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-2e6fe8?style=flat-square)
![Platform](https://img.shields.io/badge/platform-Windows%20%C2%B7%20macOS-lightgrey?style=flat-square)

![Kimi Code 用量看板](img_library/Kimi%20Code%20%E7%94%A8%E9%87%8F%E7%9C%8B%E6%9D%BF.png)

</div>

## ✨ 功能特性

- **用量卡片** — 本账期 / 今日 / 近 1 小时的输入（含缓存读取）、输出与缓存命中率，一眼看清消耗节奏
- **费用估算** — 按官方刊例价自动折算本账期等效 API 费用，
  缓存命中 / 未命中 / 输出三项构成 + 分模型本账期与上账期对比，卡片内 ⓘ 图标可查计算方式
- **套餐回本率** — 自动识别会员档位（kimi web 在线时读取档位价），计算回本进度与账期预估
- **官方限额** — 自动同步 **5 小时限额 / 周限额**（官方接口），显示已用 / 限额 / 剩余 / 重置时间，
  并按当前消耗节奏推算预计触顶时间
- **月额度（官网订阅）** — 同步官网 `GetSubscriptionStats` 的**月额度已用比例 / 重置时间 / 官方提示**，
  三种接入方式：内置 WebView 登录（默认，点「连接 Kimi」弹出窗口登录一次即可）、
  浏览器扩展自动同步（凭据不出浏览器）、手动粘贴 Token（救援）
- **可视化设置页** — `/settings` 网页配置：会员档位、**计费周期起算（精确到分钟）**、
  价格来源与手动覆盖、配额同步开关，无需改命令行
- **价目自动同步** — 默认抓 [Kimi 官方刊例](https://platform.kimi.com/docs/pricing/chat-k3)（元/1M），
  或改从 [models.dev](https://models.dev)（USD 按汇率折元）；离线时回退上次快照，仍可手动覆盖
- **用量趋势** — 24 小时 / 7 天 / 30 天 / 本账期范围切换，分模型堆叠柱状图，
  悬浮十字线 + 明细提示框（DeepSeek 用量页风格），选择记忆在本机浏览器
- **WebUI 侧栏组件** — 浏览器扩展，在 Kimi Code WebUI 侧栏实时显示本账期 token 用量，点击直达看板

数据全部来自本机 `~/.kimi-code` 会话记录，统计在本机完成，不上传任何东西。

## 🚀 快速开始（Windows)

1. 在 [Releases](../../releases) 下载 `kimi-board-vX.Y.Z-windows-x64.zip`，解压到任意位置
2. 双击 **`install-autostart.bat`**（推荐）：立即启动服务并注册开机静默自启，
   以后每次开机自动运行，无需再手动启动；`uninstall-autostart.bat` 可取消自启
   - 只想临时用一次：双击 **`start.bat`**，服务在后台启动并打开看板，
     关掉终端窗口不影响运行；停止用 `taskkill /im kimi-board.exe /f`
3. **推荐：安装浏览器扩展**（见下方 [🧩 浏览器扩展](#-浏览器扩展推荐）)，
   在 Kimi Code WebUI 侧栏实时显示用量，点击直达看板

## 🍎 快速开始（macOS）

1. 按芯片选择下载：Intel 选 `kimi-board-vX.Y.Z-darwin-x64.zip`,
   M 系列选 `kimi-board-vX.Y.Z-darwin-arm64.zip`，解压到任意位置
2. 首次运行前在终端执行一次（二进制未签名，需解除浏览器下载附加的隔离标记）:

   ```bash
   cd 解压目录
   chmod +x kimi-board *.sh
   xattr -dr com.apple.quarantine .
   ```
3. 终端执行 **`./install-autostart.sh`**（推荐）：立即启动服务并注册登录自启（launchd，
   无需 root），以后每次开机自动运行；`./uninstall-autostart.sh` 可取消自启
   - 只想临时用一次：终端执行 **`./start.sh`**，服务在后台启动并打开看板，
     关掉终端窗口不影响运行；停止用 `pkill -f kimi-board`
4. 浏览器扩展同样可用（见下方 [🧩 浏览器扩展](#-浏览器扩展推荐）)

## 🧩 浏览器扩展（推荐）

在 Kimi Code WebUI(`kimi web`）侧栏底部显示实时用量卡片：

1. Chrome / Edge 打开 `chrome://extensions`，开启**开发人员模式**
2. **加载已解压的扩展程序** → 选择解压目录里的 `extension/` 文件夹
3. 打开 Kimi Code WebUI，侧栏底部（设置按钮上方）会出现用量卡片，
   每 60 秒自动刷新，点击打开看板

> 扩展本身无法读取本地文件，看板服务（exe）必须在运行才会有数据；
> 服务未启动时卡片显示"看板未启动"，点击会给出提示。

## 🐍 从源码运行（任意平台）

要求 Python 3.8+（仅标准库，无第三方依赖）:

```bash
python kimi_board.py                      # 默认 127.0.0.1:8321，自动打开浏览器
python kimi_board.py --port 9000 --plan-price 99 --no-open
python kimi_board.py --cycle-day 5 --cycle-hour 9 --cycle-minute 30
python kimi_board.py --price-source modelsdev
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--port` | 8321 | 监听端口 |
| `--host` | 127.0.0.1 | 监听地址（仅本机回环） |
| `--plan-price` | 自动 | 会员月费（元）；不显式指定时优先自动识别（需 kimi web 在线），识别失败按 199 计 |
| `--plan-tier` | 自动 | 指定会员档位名（adagio / andante / moderato / allegretto / allegro） |
| `--price-source` | kimi | 价目来源：`kimi`=官方刊例(元/1M) · `modelsdev`=models.dev(USD 按汇率折元) · `manual`=手动 |
| `--cycle-day` / `--cycle-hour` / `--cycle-minute` | 1 / 0 / 0 | 计费周期起算（每月 1 日 00:00 为默认） |
| `--usd-cny` | 自动 | USD→CNY 汇率（modelsdev 来源时；留空自动获取，失败按 7.25） |
| `--k3-256k-half` | 关 | kimi-code/k3-256k 按 k3 生效价的 50% 计价（默认与 k3 同价） |
| `--no-quota` | 开 | 关闭官方限额同步 |
| `--no-open` | 关 | 启动后不自动打开浏览器 |

### ⚙️ 可视化设置页

启动后访问 **`http://127.0.0.1:8321/settings`**（主页面右上角「设置」按钮）即可网页配置：

- **会员档位**：自动识别 / 指定档位 / 自定义月费
- **计费周期**：每月起算日 + 时分（精确到分钟），"本账期"卡片与回本率随之变化
- **价目表**：来源切换（官方 / models.dev / 手动）、USD 汇率、各模型手动覆盖单价、
  "k3-256k 按 k3 半价"开关（官方称 k3-256k 约为 k3 的一半但口径未知，按需开启）
- **月额度**：「连接 Kimi」内置 WebView 登录（推荐）、浏览器扩展自动同步、手动 Token 三种方式，
  无需知道 JWT / Cookie / F12；数据来源一键同步
- **官方限额**：同步开关 + 数据来源（自动 / 仅本地 / 仅云端），一键立即同步

设置保存在 `~/.kimi-code/kimi-board.json`，重启服务不丢失。CLI 参数优先级高于配置文件。

另有命令行版统计工具：

```bash
python kimi_usage.py [--hours N | --month YYYY-MM | --all] [--by-workdir]
```

## 📊 数据口径与费用说明

- 数据源：`$KIMI_CODE_HOME/sessions/*/*/agents/*/wire.jsonl`（默认 `~/.kimi-code/sessions`）
  中 `type=usage.record` 且 `usageScope=turn` 的记录（含子 agent；
  不含 session 级汇总记录，避免重复计数）。
- 服务按文件 mtime/size 缓存解析结果，刷新开销很小。
- 费用为**刊例价估算**，非实际账单；订阅会员是额度制，与 API 按量计费是两套体系。
- 价目默认自动同步：`kimi` 来源抓 [官方定价页](https://platform.kimi.com/docs/pricing/chat-k3)（元/1M），
  `modelsdev` 来源抓 [models.dev](https://models.dev)（USD 折元）；抓取结果快照在
  `~/.kimi-code/kimi-board-cache.json`，离线时自动回退上次快照，仍可在设置页手动覆盖单价。
- 模型与平台商品名的映射是人工假设（如 `k3-256k` 默认按 `kimi-k3` 同价计价，
  可在设置页开启"按 k3 半价"），官方折算口径不同时数字会有出入。

## 📈 官方限额说明

- 5 小时 / 周限额与 `used` / `limit` / `reset_at` 全部来自 kimi-code 官方接口：
  优先调本地 kimi web 的 `GET /api/v1/oauth/usage`（`~/.kimi-code/server.token` 认证，
  无需额外配置）；本地服务未运行时直连 `https://api.kimi.com/coding/v1/usages`
  （自动读取/刷新 `~/.kimi-code/credentials/kimi-code.json` 的 OAuth token）。
- 同步结果 30 秒缓存，刷新页面即更新；"预计触顶"按窗口内平均消耗速率推算，仅供参考。
- 若官方未返回限额（账号 / 地区不适用），卡片会显示原因，不影响其他统计。

## 📅 月额度（官网订阅）说明

`/usages` 只给周 / 5 小时限额，**月额度比例**来自官网 `https://www.kimi.com/.../GetSubscriptionStats`，
该接口要官网网页登录态（`app_id: kimi` 的 JWT），kimi-code 本地 OAuth token 用不了。
主页「官方限额」卡片整合展示：官网（百分比两位小数）→ 无官网登录自动回退 KimiCode（整数百分比），
**每 30 秒自动刷新**。设置页同一张「官方限额」卡提供三种接入：

1. **内置 WebView 登录（默认）**：设置页「官方限额」→「连接 Kimi」，登录一次即可。
   需要 `pip install pywebview`（Windows exe 版已内置；网页版源码运行需手动装一次）。
   登录态保存在 WebView 自己的**持久 profile** 里（Cookie/JWT/localStorage 都在其中），
   登录后窗口自动隐藏，看板**每 30 秒用该持久会话后台实时刷新**；看板重启后自动重连隐藏窗口，
   Kimi 官网自己的续期逻辑继续生效，无需复制任何 token。
2. **浏览器扩展同步（可选）**：扩展声明 `www.kimi.com` 权限，用浏览器里已登录的
   Kimi 会话直接请求该接口，把解析后的数据推给本机看板——**凭据始终留在浏览器**。
3. **手动 Token（救援）**：WebView / 验证码 / 平台兼容出问题时，把浏览器 `kimi-auth`
   cookie 的值（`eyJ…`）粘贴到设置页，看板自行请求。Token 约 30 天有效。

月额度接口只给**已用比例**（如 90.52%）与重置时间，不给绝对 token 数；看板据此展示
月额度使用率、KimiCode 占比与官方提示（如"月额度已不足 10%"）。

## 🔒 安全与隐私

- **本地接口防护**：服务默认只绑 `127.0.0.1`；每次启动生成随机 secret 并注入页面，
  CORS 不再放行 `*`，只对同源 / kimi web 本机来源 / `chrome-extension://`（需携带 secret）
  回显来源头；校验 Host 与 Origin，恶意网页无法探测或调用本看板。
- **凭据不出看板**：WebView 的 Cookie/JWT 只存在 WebView 自己的**持久 profile**
  （`~/.kimi-code/webview-profile/`，`private_mode=False` 确保落盘，随 `KIMI_CODE_HOME` 走），
  不导出到 `kimi-board.json` / 缓存 / 日志；后端只保存归一化后的额度结果（比例 / 重置时间 / 提示）。
- **手动 Token**：标记为"高级 / 救援"，**默认不持久化**（仅本次运行内存）；勾选后保存到
  Windows 凭据管理器，从不写入配置文件，也不会回显到设置页。
- **清除登录数据**：设置页「清除 Kimi 登录数据」会一并清掉 WebView 的 kimi.com
  Cookie / localStorage / sessionStorage 与看板缓存的额度数据。
- **不打印敏感信息**：不 dump 请求头，日志不含任何 `Authorization`。

> 本项目是**非官方**集成，读取的是 Kimi 官网页面接口（`GetSubscriptionStats`），
> 接口路径 / 字段 / 鉴权可能随官网改版而变化，届时请升级看板或改用 `/usages` 的
> 周 / 5 小时限额。公开发布前请留意 Kimi 相关服务条款。

## ❓ 常见问题

**看板动效（粒子背景 / 字符场）不动？**
系统关闭了动画效果（Windows 设置 → 辅助功能 → 视觉效果）时会自动静止。
访问一次 `http://127.0.0.1:8321/?motion=on` 即可强制开启（记住在本机浏览器）,`?motion=off` 还原。

**扩展装了但没反应？**
确认看板服务在运行（访问 `http://127.0.0.1:8321` 能打开），并在 `chrome://extensions` 刷新一次扩展。

## 🛠 自己打包 exe

```bash
pip install pyinstaller
pyinstaller --onefile --name kimi-board kimi_board.py   # 产物在 dist/
```

打 `v*` 标签推送到 GitHub 会自动构建 exe 并发布 release
（见 `.github/workflows/release.yml`)。

## 📄 License

[MIT](LICENSE)
