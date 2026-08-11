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

- **用量卡片** — 本月 / 今日 / 近 1 小时的输入（含缓存读取）、输出与缓存命中率，一眼看清消耗节奏
- **费用估算** — 按 [Kimi 开放平台刊例价](https://platform.kimi.com/docs/pricing/chat)折算本月等效 API 费用，
  缓存命中 / 未命中 / 输出三项构成 + 分模型本月与上月对比，卡片内 ⓘ 图标可查计算方式
- **套餐回本率** — 自动识别会员档位（kimi web 在线时读取档位价），计算回本进度与月底预估
- **用量趋势** — 24 小时 / 7 天 / 30 天 / 本月范围切换，分模型堆叠柱状图，
  悬浮十字线 + 明细提示框（DeepSeek 用量页风格），选择记忆在本机浏览器
- **WebUI 侧栏组件** — 浏览器扩展，在 Kimi Code WebUI 侧栏实时显示本月 token 用量，点击直达看板

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
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--port` | 8321 | 监听端口 |
| `--host` | 127.0.0.1 | 监听地址（仅本机回环） |
| `--plan-price` | 自动 | 会员月费；不显式指定时优先自动识别（需 kimi web 在线），识别失败按 199 计 |
| `--no-open` | 关 | 启动后不自动打开浏览器 |

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
- 价目硬编码在 `kimi_board.py` 顶部的 `PRICING` 表，官方调价后请手动更新
  （当前：k3 / k3-256k = 缓存命中 ¥2、未命中 ¥20、输出 ¥100 每百万 token;
  kimi-for-coding ≈ K2.7 Code = ¥1.3 / ¥6.5 / ¥27)。
- 模型与平台商品名的映射是人工假设（如 `k3-256k` 按 `kimi-k3` 计价），
  官方折算口径不同时数字会有出入。

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
