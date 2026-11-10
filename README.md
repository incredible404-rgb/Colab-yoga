# <img src="console/resources/icon.png" width="36" height="36" align="top" alt="Colab-yoga" /> Colab-yoga

中文 | [English](README.en.md)

常驻系统托盘的 Colab GPU 管理台：一键唤醒、释放 Google Colab 付费 GPU 上的大模型实例，随时查看余额和各入口是否连通，没人用时自动关停。

在 Google Colab 付费 GPU（默认 G4 · RTX PRO 6000 96 GB）上跑自己的大模型，并通过 Tailscale 把 llama-server :8081 提供给网页对话、Magpie 和 MCP 小说目录桥。对话不经过 Colab-yoga，它只负责管理 GPU：花了多少钱、跑的是哪个模型、每个入口通不通，以及让实例在有人用时一直活着、没人用时自动释放。

模型本身（colab_kernel 启动脚本、模型清单、中文思考补丁、小说 MCP 服务）在 colab-gpu-llm-service 项目里，不在这个仓库。

## 组成

| 目录 | 运行在 | 作用 |
|---|---|---|
| `console/` | 本机 | `Colab-yoga.exe`：Go + [MyGo](https://github.com/egoist/mygo) 原生界面，常驻托盘 |
| `bridge/` | 本机 | `colab_yoga_bridge.py`：Google 账号、唤醒、找回地址、释放、后台保活（keeper） |
| `server/` | Colab VM | `colab_yoga_server.py`：HTTP API、闲置看门狗、MCP 转发；`vm_boot.py`：唤醒时在 VM 内核里执行 |
| `tools/` | 本机 | `install.py`：检查、构建、安装、快捷方式、配置迁移 |

各部分之间的约定（目录布局、API、状态字段、保活规则）见 [docs/architecture.md](docs/architecture.md)。

## 日常使用

- 开始菜单或桌面上的「Colab-yoga」。只会运行一个实例：再次打开时，会把已在运行的窗口调出来。
- 关窗口会缩到托盘，GPU 不受影响。托盘图标的提示里显示是否在计费和当前余额；右键菜单可以打开网页对话、续时、释放（会先打开窗口确认）、退出。
- 第一次使用：在「连接与账号」页绑定 Google 账号。点「绑定」后会打开浏览器，授权后把页面上的授权码粘贴回来。
- 实例保活由独立的 keeper 进程负责，关掉或退出管理台都不影响。云端看门狗把网页、Magpie、MCP 里的对话都算作在用，闲置到设定时间（默认 30 分钟）才会释放实例；剩 5 分钟时会弹系统通知。

## 开发

源码在 `%LOCALAPPDATA%\Programs\Colab-yoga\repo`，安装目录是它的上一级。需要 Go 1.27+（MyGo CLI 已在 `console/go.mod` 里固定版本）。

```bash
# 全部检查：AST、go vet、go test、bridge 和 server 的单测（server 测试的 venv 第一次会自动创建）
runtime/python.exe -X utf8 tools/install.py --check
# 检查、构建、安装到本机，并重新打开正在运行的管理台
runtime/python.exe -X utf8 tools/install.py
```

- 截图自检：`cd console && SNAP_DIR=<目录> go test -run Snapshots`，会输出深色、浅色、多种尺寸、各种状态的 PNG。
- 动效：都在 `console/motion.go`。位移走弹簧（中途再点会顺着原速度转向），颜色和透明度走短曲线，系统开了「减少动画」时位移直接到位；脉冲和流光只在唤醒、释放、装载、推理时出现，静止时窗口不画帧（`TestIdleWindowIsStill` 守着）。动画中间帧截图：`cd console && SNAP_DIR=<目录> go test -run MotionSnapshots`。
- 图标：`cd console && go run ./cmd/makeicon`，用 MyGo 的画笔生成 `resources/icon.png` 和 `tray.png`。
- 改了 `server/` 要重新唤醒才会生效：在「连接与账号」页点「重新部署服务端」，会复用同一台机器，:8081 上的模型和 MCP 转发都不会中断。

## 旧路径兼容

colab-gpu-llm-service 的 `colab_cli.py easycolab …` 和 `colab_kernel.py` 仍写死旧路径 `%LOCALAPPDATA%\Programs\EasyColab`。为此，安装脚本在那里保留了一个很小的兼容层：目录链接、一个转发脚本和一个 exe 副本，详见 docs/architecture.md。那边改成新路径后，这个兼容层就可以删掉。

## 许可

Apache License 2.0，见 [LICENSE](LICENSE) 和 [NOTICE](NOTICE)。
