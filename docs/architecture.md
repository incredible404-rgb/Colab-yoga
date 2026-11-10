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
runtime\python.exe, runtime\pythonw.exe   embedded CPython 3.12 (python312._pth: python312.zip, ".", "..", "..\site-packages")
site-packages\                           only what the bridge imports
bridge\colab_yoga_bridge.py
server\colab_yoga_server.py, server\vm_boot.py
```

Config and state: `%LOCALAPPDATA%\Colab-yoga\settings.json`, `keeper.json` (migrated once
from `%LOCALAPPDATA%\EasyColab\`).

Runner scripts (with real keys, never committed): `colab_kernel_runners.json`. Looked up at
`<install>\colab_kernel_runners.json` and the legacy `%LOCALAPPDATA%\Programs\EasyColab\_internal\colab_kernel_runners.json`
(where colab-gpu-llm-service's `colab_kernel.py` exports it); the newer of the two wins.

### Legacy path shim (`%LOCALAPPDATA%\Programs\EasyColab`)

colab-gpu-llm-service's `colab_cli.py easycolab …` and `colab_kernel.py` hard-code the old
path, and that project is not edited from here. The old dir keeps only:

- `_internal\runtime`, `_internal\site-packages`: directory junctions to the new ones;
- `_internal\colab_bridge.py`: a forwarder that runs `bridge\colab_yoga_bridge.py` with the same arguments (`--app-py` ignored);
- `_internal\colab_kernel_runners.json`: left where `colab_kernel.py` writes it;
- `EasyColab.exe`: a copy of `Colab-yoga.exe` (it finds the real install when its own dir has no `bridge\`).

Retire the shim once colab-gpu-llm-service points at the new path.

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
