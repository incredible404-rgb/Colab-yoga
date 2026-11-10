# <img src="console/resources/icon.png" width="36" height="36" align="top" alt="Colab-yoga" /> Colab-yoga

[简体中文](README.md) | English

A system tray console for self-hosted LLMs on paid Google Colab GPUs: wake or release the instance in one click, keep an eye on your balance and on whether each endpoint is reachable, and let it shut down on its own when nobody is using it.

Run your own large language models on paid Google Colab GPUs (default: G4 · RTX PRO 6000 96 GB) and expose `llama-server :8081` over Tailscale to web chat, Magpie, and the MCP novel-directory bridge. Chat traffic does not pass through Colab-yoga; Colab-yoga only manages the GPU: how much it costs, which model is running, whether each entrypoint is reachable, keeping the instance alive while in use, and releasing it automatically when idle.

![Colab-yoga UI & Non-linear Motion](docs/motion.gif)

| Dark theme (default) | Light theme (Oklab crossfade) |
|---|---|
| ![Dark dashboard](docs/dashboard-dark.png) | ![Light dashboard](docs/dashboard-light.png) |

The model runtime itself (`colab_kernel` startup script, model catalog, Chinese thinking-template patches, and novel MCP service) lives in the `colab-gpu-llm-service` repository, not here.

## Components

| Directory | Runs on | Role |
|---|---|---|
| `console/` | Local machine | `Colab-yoga.exe`: native UI built with Go + [MyGo](https://github.com/egoist/mygo), resident in the system tray |
| `bridge/` | Local machine | `colab_yoga_bridge.py`: Google account auth, wake, endpoint discovery, release, and background keepalive (`keeper`) |
| `server/` | Colab VM | `colab_yoga_server.py`: HTTP API, idle watchdog, and MCP forwarding; `vm_boot.py`: executed inside the VM kernel on wake |
| `tools/` | Local machine | `install.py`: checks, build, runtime and dependency setup, installation, shortcuts, and config migration; `package.py`: builds the portable release zip |

Conventions shared across components (directory layout, APIs, state fields, and keepalive rules) are documented in [docs/architecture.md](docs/architecture.md).

## Deployment

Two ways, and neither depends on another application's install directory: Colab-yoga brings its own Python runtime and dependencies.

### Option 1: download the portable package

1. Download `Colab-yoga-<version>-windows-amd64.zip` (with its `.sha256` next to it) from [Releases](https://github.com/incredible404-rgb/Colab-yoga/releases).
2. Unzip it anywhere and run `Colab-yoga.exe`. No separate Python install is needed. The whole folder can be moved as a unit; do not take the exe out of it.
3. Settings and state live in `%LOCALAPPDATA%\Colab-yoga`, not in the unzipped folder. The package creates no shortcuts and writes nothing to the registry; delete the folder to uninstall.
4. To start models, put `colab_kernel_runners.json` (exported by colab-gpu-llm-service's `colab_kernel.py`, contains API keys) next to `Colab-yoga.exe`. The package does not ship it.

### Option 2: install from source

Prerequisites: Windows 10/11 x64; [Go](https://go.dev/dl/) 1.27+; [PowerShell 7](https://learn.microsoft.com/powershell/scripting/install/installing-powershell-on-windows) (`pwsh`, used for shortcuts and process handling); and [python.org](https://www.python.org/downloads/windows/) Python 3.12 with pip (it only runs the installer and pip).

```bash
git clone https://github.com/incredible404-rgb/Colab-yoga.git
cd Colab-yoga
py -3.12 tools/install.py
```

That one command:

- runs the checks (syntax, `go vet`, Go tests, and the unit tests for bridge and server), then builds `Colab-yoga.exe`;
- downloads python.org's Windows embeddable CPython 3.12.10 into `%LOCALAPPDATA%\Programs\Colab-yoga\runtime` and verifies its SHA-256; an existing `runtime\` is not downloaded again (offline, point `--embed-zip <file>` at the same archive);
- installs the dependencies into `site-packages\` with the host Python's pip, from [bridge/requirements.txt](bridge/requirements.txt): pinned versions, every wheel hash-checked, binary wheels only, targeting CPython 3.12 / win_amd64, so the host Python need not be 3.12 (uv is not used);
- places `bridge\` and `server\`, creates Start menu and desktop shortcuts, and smoke-tests the bridge with the bundled runtime.

Running it again is an upgrade: dependencies are not reinstalled when their versions match, and a running console is closed first and reopened afterwards. Add `--dry-run` to see what it would do, or `--root <dir>` to try it elsewhere.

When moving over from the earlier EasyColab, the installer once copies the old `settings.json`, moves the newer `colab_kernel_runners.json` from the old directory into the new install, and then deletes the old compatibility layer in `%LOCALAPPDATA%\Programs\EasyColab` (only when it contains nothing but links, the forwarder script and the exe copy; anything else is left alone).

### Build the portable package yourself

```bash
py -3.12 tools/package.py --out <a directory outside the repo>
```

It stages the package the same way an install does, in a temporary directory, zips it to `Colab-yoga-<version>-windows-amd64.zip` (plus a `.sha256`), and audits the result: no `colab_kernel_runners.json`, `settings.json`, `keeper.json`, token or credential files, `.pyc` files, or key-shaped strings. If anything turns up, the zip is deleted and the build fails.

## Daily Usage

- Launch **Colab-yoga** from the Start menu or desktop shortcut. Only a single instance runs: launching it again brings the existing window to the front.
- Closing the window hides it to the system tray without affecting the GPU. The tray tooltip shows billing status and current CU balance; the right-click menu provides shortcuts to open web chat, extend time, release the GPU (opens a confirmation prompt first), and quit.
- First-time setup: bind your Google account on the **Connection & Account** page. Clicking **Bind** opens your browser; after authorizing, paste the authorization code back into the console.
- Instance keepalive is handled by an independent `keeper` process, so closing or exiting the console does not interrupt a running GPU. The cloud watchdog counts activity across web chat, Magpie, and MCP as active usage, releasing the instance only after the configured idle timeout (default: 30 minutes), with a system notification 5 minutes before release.

## Development

The source can live anywhere (the working copy on the maintainer's PC is `%LOCALAPPDATA%\Programs\Colab-yoga\repo`). Requires Go 1.27+ (the MyGo CLI version is pinned in `console/go.mod`).

```bash
# Run all checks: AST, go vet, go test, and unit tests for bridge and server (prepares the runtime, dependencies and the server test venv on first run)
py -3.12 tools/install.py --check
# Check, build, install locally, and reopen the running console
py -3.12 tools/install.py
```

Once installed you can also run the installer with the bundled runtime: `runtime/python.exe -X utf8 tools/install.py` (it has no pip of its own, so this only works while the dependencies are already in place).

- Visual snapshot self-check: `cd console && SNAP_DIR=<dir> go test -run Snapshots` outputs PNG snapshots across dark/light themes, window sizes, and UI states.
- Motion lives in `console/motion.go`: movement rides springs (a second click mid-flight turns with its velocity), colors and opacity ease on short curves, and movement jumps to its place when the desktop asks for less motion. Pulses and shimmers run only during wake, release, model load and inference; an idle window draws no frames (guarded by `TestIdleWindowIsStill`). Mid-animation screenshots: `cd console && SNAP_DIR=<dir> go test -run MotionSnapshots`.
- Icons: `cd console && go run ./cmd/makeicon` renders `resources/icon.png` and `tray.png` using MyGo's canvas API.
- Changes under `server/` take effect on the next wake or via **Redeploy Server** on the **Connection & Account** page, which reuses the running VM without interrupting the model or MCP forwarding on `:8081`.

## License

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
