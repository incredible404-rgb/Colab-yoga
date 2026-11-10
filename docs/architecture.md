# Colab-yoga architecture

Colab-yoga runs the owner's own LLMs (colab-gpu-llm-service's colab_kernel runners) on a
Colab GPU and serves them on llama-server :8081 over Tailscale to the web UI, Magpie and an
MCP novel-folder bridge. Conversations never pass through Colab-yoga; it manages the GPU:
what it costs, which model runs, whether each way in works, and that the runtime stays alive
exactly as long as it is used. LLM only: no video, image, ComfyUI or Gradio API gateway.

## Parts (one repo)

| Dir | Runs on | What |
|---|---|---|
| `console/` | this PC | `Colab-yoga.exe`, Go + MyGo native UI, tray icon |
| `bridge/` | this PC | `colab_yoga_bridge.py`, run by the bundled Python: Google account, wake, URL recovery, release, the detached keeper |
| `server/` | the Colab VM | `colab_yoga_server.py` (HTTP API + idle watchdog + MCP forwarder), `vm_boot.py` (what wake runs in the VM kernel) |
| `tools/` | this PC | `install.py`: check, build, lay out the install dir, shortcuts, migration |

## Install layout (`%LOCALAPPDATA%\Programs\Colab-yoga`)

```
Colab-yoga.exe
runtime\python.exe, runtime\pythonw.exe   python.org embeddable CPython 3.12.10 (python312._pth: python312.zip, ".", "..", "..\site-packages")
site-packages\                           the packages pinned in bridge/requirements.txt (51 wheels)
bridge\colab_yoga_bridge.py
server\colab_yoga_server.py, server\vm_boot.py
colab_kernel_runners.json                 not part of the repo or a release; exported here by colab-gpu-llm-service
```

The release zip is the same tree under a `Colab-yoga\` folder (plus README.txt, LICENSE, NOTICE,
without `colab_kernel_runners.json`). The exe uses its own directory as the install when
`bridge\colab_yoga_bridge.py` is next to it, so an unzipped package runs where it was unpacked;
otherwise it falls back to `%LOCALAPPDATA%\Programs\Colab-yoga` (a dev build run from
`console\dist`). Config and state stay in `%LOCALAPPDATA%\Colab-yoga` either way.

Self-contained setup (`tools/install.py`, `tools/package.py`): nothing is copied from another
application. `runtime\` is made from the CPython embeddable zip pinned by URL and SHA-256 in
`tools/install.py`; `site-packages\` is installed from `bridge/requirements.txt` (versions and
wheel hashes pinned; `pip install --target --platform win_amd64 --python-version 3.12
--only-binary=:all: --no-deps --require-hashes`) with a host Python's pip. An existing `runtime\`
or a `site-packages\` that already matches the lock is left alone. Bumping Python means changing
the URL and hash in `install.py`; bumping a package means changing `bridge/requirements.txt`.

Config and state: `%LOCALAPPDATA%\Colab-yoga\settings.json`, `keeper.json`. An earlier
install's `%LOCALAPPDATA%\EasyColab\settings.json` and `keeper.json` are copied once, only when
the new ones do not exist; nothing reads the old directory afterwards.

Runner scripts (with real keys, never committed or packaged): `<install>\colab_kernel_runners.json`
only, where colab-gpu-llm-service's `colab_kernel.py` exports it. The console and the bridge look
nowhere else.

### Retired: the EasyColab compatibility layer

Earlier installers kept `%LOCALAPPDATA%\Programs\EasyColab` (junctions to `runtime` and
`site-packages`, a `colab_bridge.py` forwarder, an `EasyColab.exe` copy, a link to the repo) for
colab-gpu-llm-service's old hard-coded path. That project now uses `Programs\Colab-yoga`, so the
installer no longer creates any of it and, when it finds a directory that holds only those
pieces, moves the newer `colab_kernel_runners.json` into the install and deletes it. Links are
removed as links (never recursed into); a directory that is not exactly that layout is left alone.

## VM side

- Files: `/content/colab_yoga_server.py`, its log `/content/colab_yoga_server.log`,
  `/content/colab_kernel_runners.json`, forwarder `/content/colab_yoga_mcp_bridge.py`.
- The server listens on `0.0.0.0:7860` and, once its public tunnel is up, prints one line
  `COLAB_YOGA_URL=https://…` to its log. Wake, discover and the keeper find the URL by that line.
- Wake never kills llama-server :8081 or the MCP forwarder. It kills an older server of either
  name (`/content/colab_yoga_server.py`, legacy `/content/app.py`) so only one idle watchdog runs.
- Colab session name in `~/.config/colab-cli/sessions.json` stays `easycolab` (shared with `colab_cli.py`).

## HTTP API (server, JSON)

| Method | Path | Body | |
|---|---|---|---|
| GET | `/api/status` | | status below |
| POST | `/api/load_model` | `{model_id, context_length, hf_token}` | start the colab_kernel runner (replaces what runs on :8081); returns status |
| POST | `/api/unload_model` | `{model_id}` | stop :8081; returns status |
| POST | `/api/touch` | `{}` | reset the idle countdown; returns status |
| POST | `/api/config_idle` | `{idle_timeout_minutes}` | 0 = off; returns status |
| POST | `/api/disconnect` | `{}` | release this runtime now |

Status fields (the console's `Status` struct reads exactly these): `server_version`,
`gpu_name`, `vram_used_gb`, `vram_total_gb`, `ram_used_gb`, `ram_total_gb`, `disk_used_gb`,
`disk_total_gb`, `uptime_seconds` (since the runtime's first Jupyter "Kernel started"),
`session_cu_used`, `cu_burn_rate_per_hour`, `idle_timeout_minutes`, `idle_remaining_seconds`
(-1 when off), `loaded_model_ids`, `is_downloading`, `is_loading_vram`, `task_progress`,
`task_message`, `console_lines`, `context_length`, `llm_server` = `{online, alias, model_id,
slots_total, slots_busy, n_ctx_per_slot, tokens_seen, last_active_age_s, mcp_bridge:
{listening, owner, reachable, error, checked_age_s}}`.

What `/api/status` costs: GPU/RAM/disk telemetry and the :8081 identity probe are cached for
about 2 s and shared by concurrent polls (one computation, not one per request); the GPU is
read through libnvidia-ml once it has agreed with `nvidia-smi`, and through `nvidia-smi`
otherwise; the model alias is re-read from `/v1/models` every 10 s while `/health` keeps
answering (load and unload always probe fresh); the app log is followed from a remembered
offset until the "Kernel started" line shows up. The idle countdown, load progress and
console lines are never cached. The MCP forwarder passes a half-close on and closes both
sockets when both directions are done; a forwarder that is already running keeps its old code
until it is restarted.

## Liveness

- The keeper (`colab_yoga_bridge.py keeper`, detached pythonw) runs one short kernel
  execution every 30 s, which is what stops Colab reclaiming a CLI runtime after 21–35 min.
  In that execution it reads `/api/status` inside the VM; at `idle_remaining_seconds == 0` it
  releases the runtime via the official API. It stops if the server is gone for 20 min, the
  assignment disappears, a colab_keepalive.py daemon covers the runtime, or after 24 h.
- The server's watchdog counts as use: model download/load, and any inference on :8081
  (`/slots`: busy slot, token counters, `id_task`, last-request stamps), which is how web,
  Magpie and MCP chats keep the runtime.
