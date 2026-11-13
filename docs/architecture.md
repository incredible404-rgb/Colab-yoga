# Colab-yoga architecture

Colab-yoga runs the owner's own LLMs (colab-gpu-llm-service's colab_kernel runners) on a
Colab GPU and serves them on llama-server :8081 over Tailscale to the web UI, Magpie and a
local MCP bridge. Conversations never pass through Colab-yoga; it manages the GPU:
what it costs, which model runs, whether each way in works, and that the runtime stays alive
exactly as long as it is used. LLM only: no video, image, ComfyUI or Gradio API gateway.

## Parts (one repo)

| Dir | Runs on | What |
|---|---|---|
| `console/` | this PC / Mac | `Colab-yoga.exe` (Windows) / `Colab-yoga.app` (macOS), Go + MyGo native UI, tray / menu-bar icon |
| `bridge/` | this PC / Mac | `colab_yoga_bridge.py`, run by the bundled Python: Google account, wake, URL and token recovery, release, the detached keeper |
| `server/` | the Colab VM | `colab_yoga_server.py` (HTTP API + idle watchdog + MCP forwarder), `vm_boot.py` (what wake runs in the VM kernel) |
| `tools/` | this PC / Mac | `install.py`: check, build, lay out the install dir, shortcuts, migration; `package.py`: build portable release zip |

## Install layout (`%LOCALAPPDATA%\Programs\Colab-yoga` on Windows, `~/Library/Application Support/Programs/Colab-yoga` on macOS)

```
Colab-yoga.exe  (or Colab-yoga.app on macOS)
runtime\python.exe, runtime\pythonw.exe   Windows: python.org embeddable CPython 3.12.10 (python312._pth: python312.zip, ".", "..", "..\site-packages")
runtime/bin/python3                        macOS arm64: python-build-standalone CPython 3.12.10 (colab_yoga.pth: "../../../..", "../../../../site-packages")
site-packages/                             the packages pinned in bridge/requirements.txt (51 wheels)
bridge/colab_yoga_bridge.py
server/colab_yoga_server.py, server/vm_boot.py
skills/colab-yoga/SKILL.md, references/api.md   release zip only: the agent skill, copy this folder into an agent's skills directory
colab_kernel_runners.json                 not part of the repo or a release; exported here or into ConfigDir by colab-gpu-llm-service
```

The release zip is the same tree under a `Colab-yoga/` folder (plus README.txt, LICENSE, NOTICE,
without `colab_kernel_runners.json`). The binary uses its own directory (or the folder holding
`Colab-yoga.app` on macOS) as the install when `bridge/colab_yoga_bridge.py` is beside it, so an
unzipped package runs where it was unpacked; otherwise it falls back to `<LocalDir>/Programs/Colab-yoga`
(a dev build run from `console/dist`). Config and state stay in `%LOCALAPPDATA%\Colab-yoga` on
Windows and `~/Library/Application Support/Colab-yoga` on macOS either way.

Self-contained setup (`tools/install.py`, `tools/package.py`): nothing is copied from another
application. `runtime\` is made from the CPython embeddable zip pinned by URL and SHA-256 in
`tools/install.py`; `site-packages\` is installed from `bridge/requirements.txt` (versions and
wheel hashes pinned; `pip install --target --platform win_amd64 --python-version 3.12
--only-binary=:all: --no-deps --require-hashes`) with a host Python's pip. An existing `runtime\`
or a `site-packages\` that already matches the lock is left alone. Bumping Python means changing
the URL and hash in `install.py`; bumping a package means changing `bridge/requirements.txt`.

Config and state: `%LOCALAPPDATA%\Colab-yoga\settings.json`, `keeper.json`, `ledger.json` (the
official balance as read, below), `server_token.json` (the control token below; per user, never
in the repo or a package). An earlier
install's `%LOCALAPPDATA%\EasyColab\settings.json` and `keeper.json` are copied once, only when
the new ones do not exist; nothing reads the old directory afterwards.

Runner scripts (with real keys, never committed or packaged): `colab_kernel_runners.json`, where
colab-gpu-llm-service's `colab_kernel.py` exports it. Looked up in `<install>\` first, then in
`%LOCALAPPDATA%\Programs\Colab-yoga\` (which only differs for a package unzipped elsewhere); the
first that exists wins. Nothing under `Programs\EasyColab` is read.

### The model list

The models are the entries of that file: one per slug, with `alias` (the name llama-server
reports on `/v1/models`), `title`, `root_dir`, `files[]` (`size` in bytes) and the boot script
`code`. Both the console and the server build their model list from it; nothing is hard-coded.
A model the author ships (`qwen38-huihui-v2`, `gemma4-31b`, `qwen38-27b-rvn`, `glm53-flash`)
additionally gets a curated display name, tags and a note; any other model is shown by its
alias, with its size summed from `files`. A served alias that is not in the file is shown as
that alias and is never taken for one of the known models. Without the file the console lists
no model and says to export it from colab-gpu-llm-service (`colab_kernel.py`); it re-reads the
file when it changes. The console takes the access address (Tailscale Service host) from the
`SERVICE_FQDN` in the scripts, else from the first `*.ts.net` host in them; when neither is
there, fill it in by hand under "Connection" → "Access addresses".

### Local MCP lifecycle

The PC bridge is supplied by colab-gpu-llm-service: `local_mcp.py start|status|stop`
runs the Pi-native tool server `local_mcp.mjs`. The helper owns config creation, a cross-process
lifecycle lock, authenticated readiness and PID-verified stopping. Its final stdout line is JSON
and contains no token. Discovery uses `COLAB_LOCAL_MCP_SCRIPT`, install-local
`local_mcp_bridge.json` (`script`), then the standard agent skill directories.

Wake starts it after the server URL is known. The keeper checks at most once per 60 seconds
on a background thread, so an 8-second readiness wait cannot stall the kernel heartbeat.
Automatic cleanup rechecks account assignments and live CLI keepers; unknown or still-active
accounts keep the bridge. The dashboard offers an asynchronous start independent of any GPU,
locks the button during a request, then refreshes identity-based `/health` before presenting
the result. A foreign listener is never considered healthy or stopped. A green MCP card requires
a confirmed cloud-to-PC probe; loopback health alone leaves it idle while cloud connectivity
is unknown. A confirmed release invalidates both cached model/MCP health and probes started
before release, then requests fresh access status. Failed, partial or unconfirmed releases
retain the last known runtime instead of reporting that every allocation is gone.

The local tools use a dedicated Bearer token, distinct from the cloud control token below.
It stays in `~/.config/colab-cli/local-mcp.json` and private client settings. Public WebUI
defaults, notebooks and runners carry only the `local_mcp` name and proxy URL. The four tools
run with the current user permissions; cwd is not a sandbox and Pi session policy is not loaded.

### Control token

The server is reachable from the internet through its gradio.live tunnel, so its control calls
need `Authorization: Bearer <token>`. `wake` makes a random token (`secrets.token_urlsafe(32)`),
uploads it to `/content/colab_yoga_token` through the Contents API (not through code sent to the
kernel, whose history is kept) and stores it in `%LOCALAPPDATA%\Colab-yoga\server_token.json`
before it prints `WAKE_URL:`. The server reads `COLAB_YOGA_TOKEN`, else that file; with neither
it makes one and writes the file itself. `discover_url` reads the file back from the VM (the
same place it reads the URL) and stores it locally without printing it. The token never appears
in the log, in the status, or in the `COLAB_YOGA_URL` line, and the console and bridge send it
on every call except `GET /api/status`. There is no exemption for requests from 127.0.0.1: the
tunnel's forwarder connects from there, so a loopback source proves nothing (the keeper only
reads `/api/status` inside the VM). A server from before the token ignores the header, so a
console that sends one keeps working against it. The token stops the URL alone from being
enough; it is not a defence against the tunnel operator, who relays the traffic.

### Retired: the EasyColab compatibility layer

Earlier installers kept `%LOCALAPPDATA%\Programs\EasyColab` (junctions to `runtime` and
`site-packages`, a `colab_bridge.py` forwarder, an `EasyColab.exe` copy, a link to the repo) for
colab-gpu-llm-service's old hard-coded path. That project now uses `Programs\Colab-yoga`, so the
installer no longer creates any of it and, when it finds a directory that holds only those
pieces, moves the newer `colab_kernel_runners.json` into the install and deletes it. Links are
removed as links (never recursed into); a directory that is not exactly that layout is left alone.

## VM side

- Files: `/content/colab_yoga_server.py`, its log `/content/colab_yoga_server.log`,
  `/content/colab_kernel_runners.json`, the control token `/content/colab_yoga_token`,
  forwarder `/content/colab_yoga_mcp_bridge.py`.
- The server listens on `0.0.0.0:7860` and, once its public tunnel is up, prints one line
  `COLAB_YOGA_URL=https://…` to its log. Wake, discover and the keeper find the URL by that line.
- A failed `wake` (upload, boot, tunnel) gives back an instance that this very call
  allocated, then prints `WAKE_ERROR:` and exits 1; a reused instance is left running. A
  successful one exits 0 after `WAKE_URL:`. The console takes the outcome from those two
  line prefixes and does not notify twice for the exit code.
- Wake never kills llama-server :8081 or the MCP forwarder. It kills an older server of either
  name (`/content/colab_yoga_server.py`, legacy `/content/app.py`) so only one idle watchdog runs.
- The Colab session name in `~/.config/colab-cli/sessions.json` is `easycolab`, shared with
  `colab_cli.py` (which also accepts `colab-yoga`). It is an internal identifier, not a path,
  and renaming it would orphan a live runtime's session, so it is kept on purpose.
- The Google token `~/.config/colab-cli/token.json` and `sessions.json` are shared with
  colab-gpu-llm-service. `auth_code` compares the new account's email with the previous
  one (read with the old token, else from the remembered profile in `account_profile.json`,
  because an old token that can no longer refresh is the usual reason to rebind). The same
  account again leaves the running sessions alone; for another or an unidentifiable account
  it removes only the records whose endpoint the new account does not hold, and removes
  nothing when the new account's assignments cannot be read. Check `auth_status` first:
  when `logged_in` is already true there is nothing to bind.

## HTTP API (server, JSON)

The methods, bodies, answers, authentication and the status fields are written down once, in
[`skills/colab-yoga/references/api.md`](../skills/colab-yoga/references/api.md): it sits in the
skill folder so an agent that has only the skill, or the release zip, can read it. Keep it
there and change it together with `server/colab_yoga_server.py` and the console's `Status`
struct (`console/backend.go`); this file does not repeat it.

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

- Who starts the keeper: the console (when it sees a billing runtime without a live keeper) and
  `wake` itself (after the server is up, before it prints `WAKE_URL:`, unless `keeper.json` says
  one is alive), so a command-line-only `wake` is kept alive too. Both start
  `<app>\runtime\pythonw.exe -X utf8 <bridge> keeper` detached.
- The keeper (`colab_yoga_bridge.py keeper`, detached pythonw) runs one short kernel
  execution every 30 s, which is what stops Colab reclaiming a CLI runtime after 21–35 min.
  In that execution it reads `/api/status` inside the VM; at `idle_remaining_seconds == 0` it
  releases the runtime via the official API. It stops if the server is gone for 20 min, the
  assignment disappears, or after 24 h. When a colab_keepalive.py daemon (Track A) already
  covers the runtime it does not stop at once: it skips its own heartbeat and the in-VM
  status read for that cycle, and because it then sees no server status it exits with
  `server_unreachable` once 20 min have passed since it last read one, leaving the daemon
  to keep the runtime alive.
- The server's watchdog counts as use: model download/load, and any inference on :8081
  (`/slots`: busy slot, token counters, `id_task`, last-request stamps), which is how web,
  Magpie and MCP chats keep the runtime. It samples every 5 s, so a chat resets the idle
  countdown within 5 s; the console polls the status every 2.5 s and counts the seconds down
  locally in between, re-anchoring on each poll (for at most 60 s without one).

## Billing

Colab books compute units about every 5 minutes, each step roughly `consumptionRateHourly`
times the time since the previous one; between steps the balance does not move. The console
therefore shows what the ledger deducted, not uptime × rate:

- The keeper reads `ccu-info` every 15 s between its beats (and once after it releases);
  `auth_status` also writes when nothing is assigned, or when no keeper has read for 45 s.
  Both fold the reading into `ledger.json` (`ledger_observe`): `idle_balance` is the last
  balance with nothing assigned, and a session (a stretch with assignments) keeps
  `start_balance` and `points`, the readings where the balance dropped. `wake` reads once
  right before it allocates.
- Spent this session = `start_balance` − balance. It is exact when the session started from an
  `idle_balance` read within 2 h; otherwise it counts from the session's first reading and the
  console shows it as "≥ … 自 HH:MM 起". A rise in the balance is a top-up: it moves
  `start_balance` up and is never counted as negative spending. Next to it the card shows
  "约 X 待结算": the shown rate times the time since the last settlement (or the session start),
  at most 10 minutes' worth, so the first minutes do not read as a bare 0.00. It is labelled as
  an estimate and never added to the deducted figure. Observed twice (1 and 3 minutes): the
  time between the last settlement and a release was never deducted afterwards, so at release
  the pending figure is an upper bound.
- The rate shown is measured from the points once the last 35 min hold at least 3 steps
  (single steps jitter by several percent); until then it is the advertised rate, labelled so.
  The server's `session_cu_used` (uptime × a per-GPU table) is only a fallback while no
  ledger reading exists.
- When a colab_keepalive.py daemon covers the runtime the keeper exits after 20 min (above),
  and the ledger is read by the console's account poll instead (every 40 s with the window
  shown, 80 s from the tray), so settlement times are coarser.
