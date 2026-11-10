---
name: colab-yoga
description: Run and manage a paid Google Colab GPU that serves your own LLMs with Colab-yoga on Windows - install it, bind the Google account, wake or release the runtime, check balance, burn rate and which model runs, switch models, extend the idle timer. Use when the user wants a Colab GPU up or down, asks what it costs or why chat is unreachable, or wants Colab-yoga installed. Models and kernels themselves live in colab-gpu-llm-service.
---

# Colab-yoga

Colab-yoga is a tray console (`Colab-yoga.exe`) plus a command-line bridge that manage one Colab GPU runtime: what it costs, which model runs on llama-server `:8081`, whether each way in works, and releasing it when idle. Conversations never pass through it.

`<app>` below is the folder holding `Colab-yoga.exe`, `runtime\` and `bridge\`: `%LOCALAPPDATA%\Programs\Colab-yoga` for an install, or wherever the release zip was unpacked. Settings and state live in `%LOCALAPPDATA%\Colab-yoga\`.

## Money gate

Waking a runtime starts billing; switching models interrupts live chats; releasing ends every chat on it. Before `wake`, `stop_all`, `load_model`, `unload_model` or `disconnect`, state the action and its cost or effect to the user and wait for a clear yes. Reads (`auth_status`, `discover_url`, `GET /api/status`) and `touch` need no confirmation.

## Install

1. Prefer the release: download `Colab-yoga-<version>-windows-amd64.zip` and its `.sha256` from https://github.com/incredible404-rgb/Colab-yoga/releases, check the hash, unpack anywhere, and run `Colab-yoga.exe`. Done when the tray icon shows. No Python install is needed; keep the folder whole.
2. From source instead (needs Go 1.27+, PowerShell 7, a Python with pip): `git clone` the repo, then `py -3.12 tools/install.py`. Done when it prints `bridge verified OK`.
3. Loading models needs `colab_kernel_runners.json` (exported by colab-gpu-llm-service's `colab_kernel.py`; it holds API keys) beside `Colab-yoga.exe`. Without it the console still binds, wakes and releases, but cannot start a model.

## Bind the Google account (needs the human)

1. `"<app>\runtime\python.exe" -X utf8 "<app>\bridge\colab_yoga_bridge.py" auth_url` prints `{"ok": true, "auth_url": ...}`. Give the user that link; they authorise in their browser and copy the code shown.
2. Pass the code they paste back: `... colab_yoga_bridge.py auth_code <code>`. Done when it answers `"logged_in": true`. The code is single-use and secret: pass it straight through, never log or repeat it.

The console's **连接与账号 / Connection & Account** page does the same with a button.

## Operate

Every bridge command prints one JSON object as its last line; read `ok` first, then the fields.

| Want | Command (`<py> <bridge> …`) | Read |
|---|---|---|
| Account, balance, billing | `auth_status` | `logged_in`, `paid_balance` (CU), `burn_rate_hourly`, `active_assignments`, `sessions[].accelerator` |
| Start a GPU and deploy the server (money gate) | `wake --gpu G4` (or `H100`, `A100`, `L4`, `T4`) | streams `WAKE_LOG:` lines; success is a `WAKE_URL: https://…` line, failure `WAKE_ERROR:` |
| Find a lost server URL | `discover_url` | `url`, `app_alive` |
| Release every runtime (money gate) | `stop_all` | `active_assignments` back to 0, `burn_rate_hourly` 0 |

`<py>` is `<app>\runtime\python.exe -X utf8`; `<bridge>` is `<app>\bridge\colab_yoga_bridge.py`. A wake takes minutes; run it in the background and watch for `WAKE_URL`.

With the server URL, the runtime answers JSON over HTTP: `GET /api/status` (GPU, VRAM, model on `:8081`, idle seconds left, load progress), `POST /api/touch` (reset the idle countdown), `POST /api/load_model` / `unload_model` / `config_idle` / `disconnect`. Field names and bodies are in `docs/architecture.md` ("HTTP API" and "Status fields"); read it before calling anything but `status` and `touch`.

## Liveness

A detached keeper (`colab_yoga_bridge.py keeper`) keeps the runtime alive with a short kernel execution every 30 s and releases it once the server's idle countdown reaches zero; closing the console does not stop it. Chats from the web UI, Magpie and MCP count as use. If the user says a runtime vanished, check `auth_status` then the keeper state in `%LOCALAPPDATA%\Colab-yoga\keeper.json` (`exit_reason`).

## Verify

After any action, re-read: `auth_status` for billing, `GET /api/status` for the model and idle timer. Report the numbers you read, not the command's success.
