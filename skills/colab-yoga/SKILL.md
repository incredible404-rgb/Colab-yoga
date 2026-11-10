---
name: colab-yoga
description: Run and manage a paid Google Colab GPU that serves your own LLMs with Colab-yoga on Windows - install it, bind the Google account, wake or release the runtime, check balance, burn rate and which model runs, switch models, extend the idle timer. Use when the user wants a Colab GPU up or down, asks what it costs or why chat is unreachable, or wants Colab-yoga installed. 中文关键词：托盘、管理台、唤醒、释放、余额、CU、续时、切换模型、Colab 实例。Starting a fresh instance and adding a new model go to colab-gpu-llm-service (its `start`); the tray wakes the runtime and deploys its management server, switches between models already exported (`load_model`), extends the idle timer, views and releases.
---

# Colab-yoga

Colab-yoga is a tray console (`Colab-yoga.exe`) plus a command-line bridge that manage one Colab GPU runtime: what it costs, which model runs on llama-server `:8081`, whether each way in works, and releasing it when idle. Conversations never pass through it.

Division of labour with **colab-gpu-llm-service**: that project owns the kernel and the models (first boot, adding or archiving a model, its `start`, exporting `colab_kernel_runners.json`). Colab-yoga (tray and bridge) owns waking the runtime and deploying its management server (`wake`), switching between the models already exported (`load_model`), extending the idle timer (续时), viewing, and releasing. To start a fresh instance for a new model or add a model, use colab-gpu-llm-service (`start`); come here for the rest. The tray's model list is read from `colab_kernel_runners.json`, so colab-gpu-llm-service must first export that file (`colab_kernel.py`) next to `Colab-yoga.exe`, or into the standard install `%LOCALAPPDATA%\Programs\Colab-yoga\`. Without it the tray lists no models.

`<app>` below is the folder holding `Colab-yoga.exe`, `runtime\` and `bridge\` (the release zip also holds this skill under `skills\colab-yoga\`): `%LOCALAPPDATA%\Programs\Colab-yoga` for an install, or wherever the release zip was unpacked. Settings and state live in `%LOCALAPPDATA%\Colab-yoga\`.

## Money gate

Waking a runtime starts billing; switching models interrupts live chats; releasing ends every chat on it; `config_idle` with `idle_timeout_minutes: 0` turns the automatic release off, so the runtime bills until someone releases it. Before `wake`, `stop_all`, `load_model`, `unload_model`, `disconnect` or `config_idle`, state the action and its cost or effect to the user and wait for a clear yes. Reads (`auth_status`, `discover_url`, `GET /api/status`) and `touch` need no confirmation.

## Install

1. Prefer the release: download `Colab-yoga-<version>-windows-amd64.zip` and its `.sha256` from https://github.com/incredible404-rgb/Colab-yoga/releases, check the hash, unpack anywhere, and run `Colab-yoga.exe`. Done when the tray icon shows. No Python install is needed; keep the folder whole. If the Releases page has no zip yet, install from source (step 2).
2. From source (needs Go 1.27+, PowerShell 7, a Python 3.10+ with pip): `git clone` the repo, then `py -3 tools/install.py`. Done when it prints `bridge verified OK`.
3. Loading models needs `colab_kernel_runners.json` (exported by colab-gpu-llm-service's `colab_kernel.py`; it holds API keys) beside `Colab-yoga.exe`. Without it the console still binds, wakes and releases, but lists no models and cannot start one.

## Bind the Google account (needs the human)

Run `auth_status` first. If it already says `"logged_in": true`, do not bind again: the token `~/.config/colab-cli/token.json` is shared with colab-gpu-llm-service, which is already using it. Bind only when `logged_in` is false, or the user wants a different account.

1. `"<app>\runtime\python.exe" -X utf8 "<app>\bridge\colab_yoga_bridge.py" auth_url` prints `{"ok": true, "auth_url": ...}`. Give the user that link; they authorise in their browser and copy the code shown.
2. Pass the code they paste back: `... colab_yoga_bridge.py auth_code <code>`. Done when it answers `"logged_in": true`. The code is single-use and secret: pass it straight through, never log or repeat it. Authorising the same account again keeps the running sessions; for a different account only the session records it does not hold are dropped.

The console's **连接与账号 / Connection & Account** page does the same with a button.

## Operate

`<py>` is `<app>\runtime\python.exe -X utf8`; `<bridge>` is `<app>\bridge\colab_yoga_bridge.py`. Every bridge command prints one JSON object as its last line (read `ok` first, then the fields), with two exceptions: `wake` prints text lines (below), and `keeper` is a long-running daemon that prints JSON only when one is already running.

| Want | Command (`<py> <bridge> …`) | Read |
|---|---|---|
| Account, balance, billing | `auth_status` | `logged_in`, `paid_balance` (CU), `burn_rate_hourly`, `active_assignments`, `sessions[].accelerator` |
| Start a GPU and deploy the server (money gate) | `wake --gpu G4` (or `H100`, `A100`, `L4`, `T4`) | streams `WAKE_LOG:` lines; match by prefix: success is a line starting `WAKE_URL:` (the URL follows it, no space) and exit code 0, failure one starting `WAKE_ERROR:` and a non-zero exit code |
| Find a lost server URL and token | `discover_url` | `url`, `app_alive`, `token_saved` |
| Release every runtime (money gate) | `stop_all` | `active_assignments` back to 0, `burn_rate_hourly` 0 |

Every current model needs a G4 (about 96 GB of VRAM); T4 and L4 are for testing only. `wake` keeps an instance of the requested type and reuses it, but if the account already holds an instance of a different type it first releases that one (ending its chats) and then allocates the requested type; that release is part of the money gate, so say so before running it. If `wake` allocated a new instance and the deployment then fails, it releases that instance again before printing `WAKE_ERROR:`; a reused instance is never released on failure.

A wake takes minutes; run it in the background and watch for the `WAKE_URL:` prefix. By the time that line shows, `wake` has also started a detached keeper if none was alive, so the runtime stays up without the console.

With the server URL, the runtime answers JSON over HTTP: `GET /api/status` (GPU, VRAM, model on `:8081`, idle seconds left, load progress; no token needed), and `POST /api/touch` (reset the idle countdown) / `load_model` / `unload_model` / `config_idle` / `disconnect`. Every POST needs `Authorization: Bearer <token>`; the token is the `token` field of `%LOCALAPPDATA%\Colab-yoga\server_token.json`, written by `wake` and refreshed by `discover_url`. A 401 means that file is stale (the server was redeployed from elsewhere): run `discover_url`, then retry. Never print or log the token. Methods, bodies, answers and the status fields are in [references/api.md](references/api.md) in this skill folder; read it before calling anything but `status` and `touch`.

## Liveness

A detached keeper (`colab_yoga_bridge.py keeper`) keeps the runtime alive with a short kernel execution every 30 s and releases it once the server's idle countdown reaches zero; closing the console does not stop it. Both the console and `wake` start it. Chats from the web UI, Magpie and MCP count as use. If the user says a runtime vanished, check `auth_status` then the keeper state in `%LOCALAPPDATA%\Colab-yoga\keeper.json` (`exit_reason`).

## Verify

After any action, re-read: `auth_status` for billing, `GET /api/status` for the model and idle timer. Report the numbers you read, not the command's success.
