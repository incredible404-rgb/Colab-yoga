# <img src="console/resources/icon.png" width="36" height="36" align="top" alt="Colab-yoga" /> Colab-yoga

中文 | [English](README.en.md)

[![CI](https://github.com/incredible404-rgb/Colab-yoga/actions/workflows/ci.yml/badge.svg)](https://github.com/incredible404-rgb/Colab-yoga/actions/workflows/ci.yml) [![Listed on Awesome MyGo](https://awesomemygo.dev/badge.svg)](https://awesomemygo.dev/apps/colab-yoga)

常驻系统托盘的 Colab GPU 管理台：一键唤醒、释放 Google Colab 付费 GPU 上的大模型实例，随时查看余额和各入口是否连通，没人用时自动关停。

在 Google Colab 付费 GPU（默认 G4 · RTX PRO 6000 96 GB）上跑自己的大模型，并通过 Tailscale 把 llama-server :8081 提供给网页对话、Magpie 和本机 MCP 桥。对话不经过 Colab-yoga，它只负责管理 GPU：花了多少钱、跑的是哪个模型、每个入口通不通，以及让实例在有人用时一直活着、没人用时自动释放。

![Colab-yoga 界面与非线性动效](docs/motion.gif)

| 深色模式（默认） | 浅色模式（Oklab 平滑切换） |
|---|---|
| ![深色模式仪表盘](docs/dashboard-dark.png) | ![浅色模式仪表盘](docs/dashboard-light.png) |

模型本身（colab_kernel 启动脚本、模型清单、中文思考补丁、本机 MCP 服务）在 colab-gpu-llm-service 项目里，不在这个仓库。管理台的模型列表就是导出的 `colab_kernel_runners.json` 里有什么就列什么，自己加的模型也会出现。

## 已实现的功能

- GPU 与模型：唤醒、加载与切换模型、续时、释放，独立保活和闲置自动关停。
- 状态与用量：余额、计费速率、显存、推理槽位，以及网页、API、Magpie 和本机 MCP 的连通状态。
- 本机 MCP：唤醒时自动启动，GPU 活跃期间每 60 秒检查并恢复，最后一个会话结束时安全关闭。
- 手动兜底：MCP 卡片的「启动」按钮在没有 GPU 时也可用；后台启动、禁止重复点击，失败显示原因并可重试。按钮复用现有组件和动效。
- 原生界面：系统托盘、深浅配色、弹簧位移、按压反馈、减少动画选项，静止时停止绘制。

## 组成

| 目录 | 运行在 | 作用 |
|---|---|---|
| `console/` | 本机 | `Colab-yoga.exe`（Windows）/ `Colab-yoga.app`（macOS）：Go + [MyGo](https://github.com/egoist/mygo) 原生界面，常驻托盘 / 菜单栏 |
| `bridge/` | 本机 | `colab_yoga_bridge.py`：Google 账号、唤醒、找回地址、释放、后台保活（keeper） |
| `server/` | Colab VM | `colab_yoga_server.py`：HTTP API、闲置看门狗、MCP 转发；`vm_boot.py`：唤醒时在 VM 内核里执行 |
| `tools/` | 本机 | `install.py`：检查、构建、准备运行时与依赖、安装、快捷方式、配置迁移；`package.py`：打免安装包 |

各部分之间的约定（目录布局、API、状态字段、保活规则）见 [docs/architecture.md](docs/architecture.md)。

## 从零开始（4 步跑通第一个模型）

1. **登录**：解压免安装包，运行 `Colab-yoga.exe`（Windows）或打开 `Colab-yoga.app`（macOS），用 Google 账号登录。
2. **配 Tailscale**：按 colab-gpu-llm-service 的说明配置一次 Tailscale Service，之后网页、API 和 MCP 都走这个地址。
3. **导出模型清单**：克隆 [colab-gpu-llm-service](https://github.com/incredible404-rgb/colab-gpu-llm-service)，填好 `.secrets.json` 的 4 个必填项（`api_key`、`ts_client_id`、`ts_client_secret`、`tailnet_fqdn`），运行 `python -X utf8 scripts/colab_kernel.py --all`（macOS 用 `python3`）。它会自动写入 `%LOCALAPPDATA%\Colab-yoga\colab_kernel_runners.json`（macOS 为 `~/Library/Application Support/Colab-yoga/colab_kernel_runners.json`），管理台随即列出模型。
4. **开用**：回到管理台点「唤醒 G4」，加载模型后即可网页对话，或调用 `/v1` API。

不装 colab-gpu-llm-service 也能用 Colab-yoga 单独登录、唤醒、保活和释放实例，只是没有模型可加载。

## 部署

两种方式，都不依赖任何别的软件的安装目录；运行时的 Python 和依赖由 Colab-yoga 自己带。支持 **Windows 10/11 x64** 与 **macOS Apple Silicon (`darwin-arm64`)**。

### 方式一：下载免安装包

1. 在 [Releases](https://github.com/incredible404-rgb/Colab-yoga/releases) 下载对应系统的免安装包：`Colab-yoga-<版本>-windows-amd64.zip` 或 `Colab-yoga-<版本>-darwin-arm64.zip`（旁边有 `.sha256`）。
2. 解压到任意位置，运行里面的 `Colab-yoga.exe`（Windows）或打开 `Colab-yoga.app`（macOS；若从浏览器下载被 Gatekeeper 拦截，在终端执行一次 `xattr -cr Colab-yoga`）。不需要另外安装 Python；整个文件夹可以整体移动，但不要把 `Colab-yoga.exe` / `Colab-yoga.app` 单独拿出去。
3. 设置和状态保存在 `%LOCALAPPDATA%\Colab-yoga`（Windows）或 `~/Library/Application Support/Colab-yoga`（macOS），不在解压目录里。免安装包不建快捷方式、不写注册表，删掉文件夹即卸载。
4. 模型清单 `colab_kernel_runners.json`（colab-gpu-llm-service 的 `colab_kernel.py` 导出，含 API 密钥）和本机 MCP 配置 `local_mcp_bridge.json` 默认都写到上述配置目录，管理台从那里自动读取，所以免安装包放在任意目录都不用手动拷文件；放在可执行程序旁边的同名文件优先。包里不带这两个文件；没有模型清单时，模型列表为空并提示如何导出。

**更新提示。** 管理台每天最多向 GitHub 询问一次最新发布的版本号；有新版本时，左下角账号上方会出现一行「新版本 x.y.z」，点开是该版本的发布页。它只提示，不会自己下载或替换任何文件：下载新包、解压覆盖由你来做，设置和状态不受影响。不想让它联网询问，在 `settings.json` 里加 `"updateCheck": "off"`。

### 方式二：从源码安装

- **Windows 10/11 x64**：[Go](https://go.dev/dl/) 1.27+；[PowerShell 7](https://learn.microsoft.com/powershell/scripting/install/installing-powershell-on-windows)（`pwsh`，建快捷方式和管理进程用）；[python.org](https://www.python.org/downloads/windows/) 的 Python 3.10+（带 pip，只用来跑安装脚本和 pip；管理台运行时用的 3.12 由安装脚本另外下载）。
- **macOS Apple Silicon (`arm64`)**：[Go](https://go.dev/dl/) 1.27+（或设 `GOTOOLCHAIN=auto` 自动拉取）与系统自带或 Homebrew 的 `python3`（独立 CPython 3.12.10 运行时自带 pip，由安装脚本自动下载并校验 SHA-256）。

```bash
git clone https://github.com/incredible404-rgb/Colab-yoga.git
cd Colab-yoga
# Windows:
py -3 tools/install.py
# macOS:
python3 tools/install.py
```

这一条命令会：

- 检查（语法、`go vet`、Go 测试、bridge 与 server 的单测），再构建 `Colab-yoga.exe` / `Colab-yoga.app`；
- 下载独立 CPython 3.12.10 到安装目录的 `runtime/`（Windows 为 `%LOCALAPPDATA%\Programs\Colab-yoga\runtime`，macOS 为 `~/Library/Application Support/Programs/Colab-yoga/runtime`），校验 SHA-256；已有 `runtime/` 时不重复下载（离线时用 `--embed-zip <文件>` 指向同一个压缩包）；
- 用 pip 按 [bridge/requirements.txt](bridge/requirements.txt) 把依赖装进 `site-packages/`：版本锁定、逐个 wheel 校验哈希、只装二进制包，目标固定为 CPython 3.12（`win_amd64` 或 `macosx_arm64`）；
- 放好 `bridge/`、`server/`，建快捷方式（Windows 开始菜单与桌面 `.lnk`，macOS 为 `~/Applications/Colab-yoga.app` 符号链接），并用自带的运行时做一次 bridge 自检。

重复运行就是升级：依赖版本没变就不会重新安装，正在运行的管理台会先关掉、装完再打开。想先看会做什么：加 `--dry-run`；想在别处试一遍：`--root <目录>`。

### 自己打免安装包

```bash
# Windows:
py -3 tools/package.py --out <仓库之外的目录>
# macOS:
python3 tools/package.py --out <仓库之外的目录>
```

按安装同样的流程在临时目录里组好，压成 `Colab-yoga-<版本>-windows-amd64.zip` 或 `Colab-yoga-<版本>-darwin-arm64.zip`（同目录有 `.sha256`），并审计内容：不含 `colab_kernel_runners.json`、`settings.json`、`keeper.json`、token 或凭据类文件、`.pyc`、密钥特征字符串，发现就删掉 zip 并报错。

## 本机 MCP 桥

本机 MCP 和 Magpie 都是可选功能：没装、没配时，对应卡片只给一句安静的说明，不报错、不打扰。

启用本机 MCP 需要 [colab-gpu-llm-service](https://github.com/incredible404-rgb/colab-gpu-llm-service) 的 `scripts/local_mcp.py` 与同目录的 `local_mcp.mjs`，以及 Node.js 和 Pi；云端转发还需要在模型清单里配好 `local_tailscale_ip`。本机 MCP 默认只监听 `127.0.0.1` 和配置的 `local_tailscale_ip`，不对公共局域网开放。

管理台按顺序查找 `COLAB_LOCAL_MCP_SCRIPT`、exe 旁边 `local_mcp_bridge.json` 的 `script` 字段、`%LOCALAPPDATA%\Colab-yoga\local_mcp_bridge.json` 的 `script` 字段（`colab_kernel.py` 导出时自动写入），以及 `~/.agents/skills/colab-gpu-llm-service/scripts/local_mcp.py`（兼容 `~/.claude/skills/`）。一般无需手写；需要手动指定时，内容形如：

```json
{"script": "C:/tools/colab-gpu-llm-service/scripts/local_mcp.py"}
```

唤醒会自动启动本机桥；已经在运行就复用。没启动成功时，在仪表盘的 MCP 卡片点「启动」；不需要先唤醒 GPU。状态检测验证服务身份，其他程序占用 3426 端口时会显示原因，不会误报连通，也不会杀掉该程序。只有云端到本机的探测确认可达，卡片才显示绿色「已连通」；本机就绪、云端未连接或仍在检查时保持灰色。确认释放后会清除旧模型与 MCP 探测缓存，释放前发出的晚到结果也不会恢复旧状态。

`local_mcp` 直接复用 Pi 的 `read`、`write`、`edit`、`bash`，以当前 Windows 用户权限运行；工作目录只是默认位置，工具也能访问其他有权限的路径。Pi 会话里的技能、规则和审批不会自动加载。专用 Bearer 令牌由 helper 存在本机 `~/.config/colab-cli/local-mcp.json`，首次在 WebUI 的客户端私有 MCP 设置中配置 `Authorization`，以后复用。真实令牌不写入 notebook、runner 或 WebUI 的公开默认配置。完整配置见服务仓库的介绍页。

![MCP 端口冲突时显示原因并提供重试](docs/mcp-fallback.png)

## 给 agent 用

[skills/colab-yoga/SKILL.md](skills/colab-yoga/SKILL.md) 是给 Claude Code、Codex 等 agent 的技能：安装、绑定账号、唤醒 / 释放、查余额与模型、切换模型，花钱的动作会先向你确认。把 `skills/colab-yoga` 整个文件夹（含服务端接口说明 `references/api.md`）复制到 agent 的技能目录（如 `~/.claude/skills/` 或 `~/.agents/skills/`）即可，免安装包里同一个文件夹在 `skills\colab-yoga\`；模型本身的部署见 [colab-gpu-llm-service](https://github.com/incredible404-rgb/colab-gpu-llm-service) 自带的技能。

## 日常使用

- 开始菜单或桌面上的「Colab-yoga」。只会运行一个实例：再次打开时，会把已在运行的窗口调出来。
- 关窗口会缩到托盘，GPU 不受影响。托盘图标的提示里显示是否在计费和当前余额；右键菜单可以打开网页对话、续时、释放（会先打开窗口确认）、退出。
- 第一次使用：没绑定 Google 账号时，窗口只有一个登录页。点「用 Google 账号登录」会打开浏览器，授权后把页面上的授权码粘贴回来。暂时不想登录可以点「先不登录，进入控制台」：日志和手填服务地址照常可用，唤醒和查余额要先登录。登录之后账号在左栏最下面，点开可以换号、刷新余额、退出。令牌与 colab-gpu-llm-service 共用（`~/.config/colab-cli/token.json`），已经绑定过就不用再绑；同一个账号重新授权不会动正在运行的会话。
- 一次使用的几步都在顶栏，从左到右：「唤醒 G4」（箭头换 GPU 型号）→「加载模型」→「打开网页对话」→「续时」→「释放」。任何时候只有下一步是实心按钮，做完的步骤留在原位、变暗打勾；现在用不了的按钮是灰的，鼠标停上去会说原因。「加载模型」点第一下会展开模型列表并默认选中上次用的那个，再点一下同一个按钮就开始加载；想换别的，先在列表里点选。唤醒和装载模型的进度在仪表盘的「当前模型」卡里分阶段显示，唤醒失败会停在出错的那一步并写明原因。
- 接入地址（Tailscale Service 域名）会从模型清单的脚本里自动识别（先找 `SERVICE_FQDN`，再找任意 `*.ts.net` 域名）；识别不到就在「连接 → 接入地址」里手填。
- 实例保活由独立的 keeper 进程负责，关掉或退出管理台都不影响；只用命令行 `bridge wake` 唤醒时，wake 成功后也会自己把 keeper 拉起来。服务端的完整状态和控制接口（加载、卸载、改闲置时间、释放）要带随机生成的 Bearer 令牌，由唤醒时下发并保存在 `%LOCALAPPDATA%\Colab-yoga\server_token.json`，管理台会自动携带。只有 `GET /health` 公开最小健康状态，完整 `/api/status` 和日志需要令牌。本地更新后如果仍连接着旧版云端服务，请用「重新部署」同步服务端；下一次唤醒会自动部署匹配版本。云端看门狗把网页、Magpie、MCP 里的对话都算作在用，闲置到设定时间（默认 30 分钟）才会释放实例；剩 5 分钟时会弹系统通知。

## 开发

源码放在哪里都行。需要 Go 1.27+（MyGo CLI 已在 `console/go.mod` 里固定版本）。

```bash
# 全部检查：AST、go vet、go test、bridge 和 server 的单测（运行时、依赖和 server 测试的 venv 第一次会自动准备）
py -3 tools/install.py --check
# 检查、构建、安装到本机，并重新打开正在运行的管理台
py -3 tools/install.py
```

装好之后，也可以直接用安装目录里的运行时跑：`runtime/python.exe -X utf8 tools/install.py`（它自己没有 pip，只在依赖已经就位时可用）。

- 截图自检：`cd console && SNAP_DIR=<目录> go test -run Snapshots`，会输出深色、浅色、多种尺寸、各种状态的 PNG。
- 主页动图：`cd console && FRAMES_DIR=<目录> go test -run ReadmeFrames` 输出一次完整使用过程（唤醒、加载模型、切换配色）的逐帧 PNG，合成后就是 `docs/motion.gif`。
- 动效：都在 `console/motion.go`。位移走弹簧（中途再点会顺着原速度转向），颜色和透明度走短曲线，系统开了「减少动画」时位移直接到位；卡片入场只升起、不淡入（GPU 渲染下大面积的透明度动画会逐帧闪，深色下很明显）；脉冲和流光只在唤醒、释放、装载、推理时出现，静止时窗口不画帧（`TestIdleWindowIsStill` 守着）。动画中间帧截图：`cd console && SNAP_DIR=<目录> go test -run MotionSnapshots`。
- 图标：`cd console && go run ./cmd/makeicon`，用 MyGo 的画笔生成 `resources/icon.png` 和 `tray.png`。
- 改了 `server/` 要重新唤醒才会生效：在「连接」页点「重新部署服务端」，会复用同一台机器，:8081 上的模型和 MCP 转发都不会中断。

## 许可

Apache License 2.0，见 [LICENSE](LICENSE) 和 [NOTICE](NOTICE)。
