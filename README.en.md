# <img src="console/resources/icon.png" width="36" height="36" align="top" alt="Colab-yoga" /> Colab-yoga

[简体中文](README.md) | English

> **When & Why to Use**: For managing Google Colab paid GPU LLM instances from your local desktop—a native Go + MyGo system-tray console and keepalive bridge that wakes/releases GPUs, monitors CU balance and endpoint health, and auto-stops idle instances across web chat, Magpie, and MCP.

Run your own large language models on paid Google Colab GPUs (default: G4 · RTX PRO 6000 96 GB) and expose `llama-server :8081` over Tailscale to web chat, Magpie, and the MCP novel-directory bridge. Chat traffic does not pass through Colab-yoga; Colab-yoga only manages the GPU: how much it costs, which model is running, whether each entrypoint is reachable, keeping the instance alive while in use, and releasing it automatically when idle.

The model runtime itself (`colab_kernel` startup script, model catalog, Chinese thinking-template patches, and novel MCP service) lives in the `colab-gpu-llm-service` repository, not here.

## Components

| Directory | Runs on | Role |
|---|---|---|
| `console/` | Local machine | `Colab-yoga.exe`: native UI built with Go + [MyGo](https://github.com/egoist/mygo), resident in the system tray |
| `bridge/` | Local machine | `colab_yoga_bridge.py`: Google account auth, wake, endpoint discovery, release, and background keepalive (`keeper`) |
| `server/` | Colab VM | `colab_yoga_server.py`: HTTP API, idle watchdog, and MCP forwarding; `vm_boot.py`: executed inside the VM kernel on wake |
| `tools/` | Local machine | `install.py`: checks, build, local installation, shortcuts, and config migration |

Conventions shared across components (directory layout, APIs, state fields, and keepalive rules) are documented in [docs/architecture.md](docs/architecture.md).

## Daily Usage

- Launch **Colab-yoga** from the Start menu or desktop shortcut. Only a single instance runs: launching it again brings the existing window to the front.
- Closing the window hides it to the system tray without affecting the GPU. The tray tooltip shows billing status and current CU balance; the right-click menu provides shortcuts to open web chat, extend time, release the GPU (opens a confirmation prompt first), and quit.
- First-time setup: bind your Google account on the **Connection & Account** page. Clicking **Bind** opens your browser; after authorizing, paste the authorization code back into the console.
- Instance keepalive is handled by an independent `keeper` process, so closing or exiting the console does not interrupt a running GPU. The cloud watchdog counts activity across web chat, Magpie, and MCP as active usage, releasing the instance only after the configured idle timeout (default: 30 minutes), with a system notification 5 minutes before release.

## Development

Source code lives in `%LOCALAPPDATA%\Programs\Colab-yoga\repo`, and the installation directory is its parent folder. Requires Go 1.27+ (the MyGo CLI version is pinned in `console/go.mod`).

```bash
# Run all checks: AST, go vet, go test, and unit tests for bridge and server (creates server test venv on first run)
runtime/python.exe -X utf8 tools/install.py --check
# Check, build, install locally, and reopen the running console
runtime/python.exe -X utf8 tools/install.py
```

- Visual snapshot self-check: `cd console && SNAP_DIR=<dir> go test -run Snapshots` outputs PNG snapshots across dark/light themes, window sizes, and UI states.
- Icons: `cd console && go run ./cmd/makeicon` renders `resources/icon.png` and `tray.png` using MyGo's canvas API.
- Changes under `server/` take effect on the next wake or via **Redeploy Server** on the **Connection & Account** page, which reuses the running VM without interrupting the model or MCP forwarding on `:8081`.

## Legacy Path Compatibility

`colab_cli.py easycolab …` and `colab_kernel.py` in `colab-gpu-llm-service` still reference the legacy path `%LOCALAPPDATA%\Programs\EasyColab`. To stay compatible, the installer maintains a minimal shim there (a directory junction, a forwarding script, and an executable copy; see [docs/architecture.md](docs/architecture.md)). Once `colab-gpu-llm-service` migrates to the new path, this shim can be removed.

## License

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
