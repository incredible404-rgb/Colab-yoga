# Colab-yoga server HTTP API

This file is the single reference for the API (the repo's `docs/architecture.md` points here). It ships inside the skill folder and in the release zip under `skills\colab-yoga\references\`, so an agent can read it without the repo.

The server runs on the Colab VM and is reached through its public tunnel URL. Get the URL from the `WAKE_URL:` line of `wake`, or from `discover_url` (`url`). All bodies and answers are JSON.

## Authentication

Every call except `GET /api/status` needs `Authorization: Bearer <token>`. Without it, or with a wrong token, the answer is `401 {"detail": "unauthorized"}`. `/api/status` is open so a quick check needs nothing.

The token is the `token` field of `%LOCALAPPDATA%\Colab-yoga\server_token.json`, written by `wake` and refreshed by `discover_url`. A 401 with the token you have usually means the server was redeployed from elsewhere: run `discover_url`, re-read the file, retry once. Never print, log or paste the token. A loopback source is not exempt: the public tunnel's forwarder connects from 127.0.0.1.

## Endpoints

| Method | Path | Auth | Body | Effect and answer |
|---|---|---|---|---|
| GET | `/api/status` | none | | The status object below. |
| POST | `/api/load_model` | Bearer | `{"model_id": "<slug>", "context_length": <int, optional>, "hf_token": "<optional>"}` | Starts the runner of `model_id` (a slug in `colab_kernel_runners.json`; empty or missing gives `400 {"ok": false, "message": "model_id is required"}`) and replaces whatever runs on `:8081`. `context_length` is clamped to 4096..262144. `hf_token` is passed to the runner as `HF_TOKEN` for gated downloads. Returns immediately with the status; progress shows in `is_downloading`, `is_loading_vram`, `task_progress`, `task_message`. Interrupts live chats. |
| POST | `/api/unload_model` | Bearer | `{"model_id": "<slug, optional>"}` | Stops `:8081` and a boot still running. A `model_id` that is not the served one is a no-op. Returns the status. |
| POST | `/api/touch` | Bearer | `{}` | Resets the idle countdown. Returns the status. |
| POST | `/api/config_idle` | Bearer | `{"idle_timeout_minutes": <int>}` | Sets the automatic-release timeout (default 30). `0` (negatives become 0) turns it off, so the runtime bills until someone releases it. Also resets the countdown. Returns the status. |
| POST | `/api/disconnect` | Bearer | `{}` | Releases this runtime now, ending every chat on it. Returns `{"ok": true|false, "message": "..."}`. |

## Status fields

`GET /api/status` (and the POSTs that return it) answer one object. The console's `Status` struct reads exactly these:

- `server_version`
- `gpu_name`, `vram_used_gb`, `vram_total_gb`, `ram_used_gb`, `ram_total_gb`, `disk_used_gb`, `disk_total_gb`
- `uptime_seconds` (since the runtime's first Jupyter "Kernel started"), `session_cu_used`, `cu_burn_rate_per_hour` (both from a per-GPU price table, an estimate; what was actually charged is in `ledger.json`, see SKILL.md)
- `idle_timeout_minutes`, `idle_remaining_seconds` (-1 when the automatic release is off)
- `loaded_model_ids`, `context_length`
- `is_downloading`, `is_loading_vram`, `task_progress`, `task_message`, `console_lines`
- `llm_server`: `{online, alias, model_id, slots_total, slots_busy, n_ctx_per_slot, tokens_seen, last_active_age_s, mcp_bridge: {listening, owner, reachable, error, checked_age_s}}`

`llm_server.model_id` is the runner slug whose `alias` llama-server reports on `:8081`, and `""` when that alias is not in the runners file; `loaded_model_ids` then lists the alias instead. `llm_server.online` false means no model is serving.

The GPU/RAM/disk telemetry and the `:8081` probe are cached for about 2 s, so polling faster than that returns the same numbers; the idle countdown, load progress and console lines are live.

## Example (PowerShell)

```powershell
$base = "<WAKE_URL value>"
$tok  = (Get-Content "$env:LOCALAPPDATA\Colab-yoga\server_token.json" -Raw | ConvertFrom-Json).token
Invoke-RestMethod "$base/api/status"                                  # open
Invoke-RestMethod "$base/api/touch" -Method Post -Body '{}' -ContentType 'application/json' `
  -Headers @{Authorization = "Bearer $tok"}                           # needs the token
```
