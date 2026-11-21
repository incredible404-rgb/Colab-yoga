# Colab-yoga and colab-gpu-llm-service: what they share

The two repositories ship separately but run on the same machine against the same Colab account.
This file lists every place where one relies on the other. A change to any row is a change to
both repositories, made in one pull request each and released together; nothing outside this list
may be relied on. colab-gpu-llm-service's AGENTS.md points here.

## Shared with the official colab CLI (google-colab-cli)

| What | Where | Rule |
|---|---|---|
| colab-cli release | Colab-yoga `bridge/requirements.txt`; colab-gpu-llm-service `scripts/colab_path.py` `COLAB_CLI_VERSION` | Always the same version. colab-gpu-llm-service's preflight warns when the uv tool or the Colab-yoga install differ. |
| Google OAuth token | OS keyring entry of colab-cli (`colab_cli.auth.KEYRING_SERVICE_NAME` / `KEYRING_USERNAME`): Windows Credential Manager, macOS Keychain | Read and written only through that entry. A `~/.config/colab-cli/token.json` from colab-cli 0.7.5 or older is moved in on first read and deleted, as colab-cli does. Never write a token file. |
| Named sessions | `~/.config/colab-cli/sessions.json` through `colab_cli.common.state.store` | Colab-yoga's session is `easycolab` (colab-gpu-llm-service also accepts `colab-yoga`). Binding the same account again keeps every record; another account drops only records whose endpoint it does not hold. |
| colab-cli internals used directly | Colab-yoga bridge: `auth` (scopes, redirect URI, keyring names), `client` (Accelerator, Variant, Shape, resolve_assign_shape, PostAssignmentResponse, ColabRequestError), `common.state`, `state.SessionState`, `runtime.ColabRuntime`, `contents.ContentsClient`. colab-gpu-llm-service: the `colab` commands `new`, `exec`, `stop`, `sessions`, `usage`, plus, in the keep-alive daemon, `common.State`, `common._apply_proxy_info`, `client.ColabRequestError`, `utils.get_status_code`, `runtime.ColabRuntime` (the in-process heartbeat; its `execute_code` timeout is an idle limit since 0.8.0) and `auth.KEYRING_SERVICE_NAME` / `KEYRING_USERNAME` | These are not a public API. Bumping colab-cli means checking each of them against the new release before the pin moves. |

## colab-gpu-llm-service → Colab-yoga

| What | Where | Rule |
|---|---|---|
| Runners file | `colab_kernel_runners.json` exported by `colab_kernel.py` into the Colab-yoga install or `%LOCALAPPDATA%\Colab-yoga` (`~/Library/Application Support/Colab-yoga`) | Fields `slug`, `alias`, `title`, `name`, `tags`, `note`, `root_dir`, `files`, `code`. Colab-yoga knows no model of its own; see "The model list" in [architecture.md](architecture.md). |
| Local MCP helper | `local_mcp.py start\|status\|stop`, found through `COLAB_LOCAL_MCP_SCRIPT`, `local_mcp_bridge.json` (`script`) or the agent skill folders | The last stdout line is JSON without a token. Config and token stay in `~/.config/colab-cli/local-mcp.json`. |
| Keep-alive daemon state | `~/.config/colab-cli/keepalive-<session>.json` (`pid`, `endpoint`) | Colab-yoga's keeper treats a live `pid` on the same endpoint as covered and stands down; `stop_all` ends that `pid`. |

## Colab-yoga → colab-gpu-llm-service

| What | Where | Rule |
|---|---|---|
| Bridge commands | `<install>/runtime/python` `-u bridge/colab_yoga_bridge.py auth_status \| auth_url \| auth_code <code> \| wake --gpu G \| stop_all` | `wake` prints `WAKE_URL:` or `WAKE_ERROR:` lines; the others print one JSON object. The install is found through `COLAB_YOGA_DIR`, then `appDir` in `install.json`, then the default install folder. |
| In-VM status | `GET http://127.0.0.1:7860/health` inside the VM | The keep-alive daemon's beat reads it (2 s limit, failure ignored) to count downloads, loads and an extended idle timer as activity. Its fields are in [api.md](../skills/colab-yoga/references/api.md). |

## Bumping colab-cli

1. Read the upstream changes since the current pin and check every row of "colab-cli internals used directly".
2. Colab-yoga: change `bridge/requirements.txt` (new wheels hash-pinned) and run `python tools/install.py --root <temp> --check`.
3. colab-gpu-llm-service: change `COLAB_CLI_VERSION` and the documented install commands, run the unit tests.
4. Merge both, then on each machine update the uv tool (`python -m uv tool install --force "google-colab-cli==<version>"`) and Colab-yoga in the same sitting; a 0.7.5 install next to a newer one loses the token.
