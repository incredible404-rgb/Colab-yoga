# Contributing / 参与贡献

[English](#english) | [中文](#中文)

## English

Issues and pull requests are welcome. This is a small project with one maintainer, so a reply can take a few days.

### Before a large change

Open an issue first and say what you want to change. A new platform, a new dependency or a change to the bridge commands and the server API is easier to agree on before the code exists.

### Set up and check

Prerequisites are in the README under "Install from source" (Go, and a Python 3.10+ on Windows; Go on macOS).

```bash
git clone https://github.com/incredible404-rgb/Colab-yoga.git
cd Colab-yoga
python tools/install.py --root <a temp dir> --check
```

`--check` prepares the bundled runtime in that temp dir and runs everything CI runs: syntax, `go vet`, `go test`, and the bridge and server tests. `--root` keeps it away from a real install. Drop `--check` to also build the console and lay out a full install there.

### Pull requests

- One topic per pull request, branched from `main`.
- CI runs the check on Windows and macOS. A pull request is merged when it is green and reviewed.
- Add or update tests for behavior you change. Tests must not call Google, Colab or Tailscale.
- When behavior changes, update `README.md`, `README.en.md`, `docs/architecture.md` and `skills/colab-yoga/` in the same pull request.
- Commit subjects follow `feat: ...`, `fix: ...`, `docs: ...`, `test: ...`.
- Leave "Allow edits by maintainers" on, so small fixes can be pushed to your branch.

### Keys

`colab_kernel_runners.json`, `settings.json`, `keeper.json`, tokens and credentials never go into a commit, an issue or a log you paste. `tools/package.py` refuses to build a release that contains them.

### Security

Report vulnerabilities privately through [Security advisories](https://github.com/incredible404-rgb/Colab-yoga/security/advisories/new), not in a public issue.

### License

Contributions are licensed under Apache License 2.0, the license of this repository.

## 中文

欢迎提 issue 和 pull request。项目只有一位维护者，回复可能要几天。

### 改动较大时

先开 issue 说明想改什么。新增平台、新增依赖、改 bridge 命令或 server API，这类事在写代码之前对齐更省事。

### 环境与检查

前置条件见 README「从源码安装」（Windows 需要 Go 和 Python 3.10+；macOS 需要 Go）。

```bash
git clone https://github.com/incredible404-rgb/Colab-yoga.git
cd Colab-yoga
python tools/install.py --root <临时目录> --check
```

`--check` 会在这个临时目录里准备自带运行时，并跑 CI 跑的全部内容：语法、`go vet`、`go test`、bridge 与 server 测试。`--root` 让它不碰真实安装。去掉 `--check` 会继续构建管理台并在该目录组出完整安装。

### Pull request

- 一个 PR 只做一件事，从 `main` 拉分支。
- CI 会在 Windows 和 macOS 上跑检查，通过并审过之后合并。
- 行为有变时补上或更新测试。测试不得访问 Google、Colab 或 Tailscale。
- 行为有变时，在同一个 PR 里更新 `README.md`、`README.en.md`、`docs/architecture.md` 和 `skills/colab-yoga/`。
- 提交标题用 `feat: ...`、`fix: ...`、`docs: ...`、`test: ...`。
- 保持勾选 “Allow edits by maintainers”，方便维护者直接往你的分支补小修改。

### 密钥

`colab_kernel_runners.json`、`settings.json`、`keeper.json`、令牌和凭据不能出现在提交、issue 或粘贴的日志里。`tools/package.py` 发现包里有这些会拒绝出包。

### 安全问题

请通过 [Security advisories](https://github.com/incredible404-rgb/Colab-yoga/security/advisories/new) 私下报告，不要开公开 issue。

### 许可

贡献内容按本仓库的 Apache License 2.0 授权。
