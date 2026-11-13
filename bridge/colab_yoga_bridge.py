#!/usr/bin/env python3
"""
Colab-yoga Bridge: manages Google Colab GPU runtimes, server deployment,
URL discovery, and detached heartbeat.
Derived from EasyColab's colab_bridge.py (original author wy51ai, Apache 2.0).

Supports subcommands:
  - auth_status  : Check OAuth login state, CU balance, hourly burn rate, and active VMs
  - auth_url     : Generate Google OAuth2 authorization URL
  - auth_code    : Exchange Google authorization code and save token (for a different account
                   only the local session records it does not hold are dropped)
  - auth_logout  : Disconnect and clear local tokens and sessions
  - wake         : Provision/reuse Colab GPU VM, upload colab_yoga_server.py, the runners and a
                   fresh control token, run vm_boot.py, then make sure a keeper is running
  - stop_all     : Terminate and unassign all active Colab VMs on account
  - discover_url : Read the running server's tunnel URL and control token from the VM without
                   redeploying
  - keeper       : Detached heartbeat daemon that keeps the Colab runtime alive while active
"""
import errno
import os
import re
import secrets
import subprocess
import sys
import tempfile
import json
import time
import threading
import argparse

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow


def _ensure_utf8_stdio():
    """Ensure child process streams use UTF-8 on Windows / macOS."""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


_ensure_utf8_stdio()

from colab_cli.auth import PUBLIC_SCOPES, TOKEN_CONFIG_PATH, REMOTE_REDIRECT_URI
from colab_cli.common import state
from colab_cli.client import (
    Accelerator,
    Variant,
    Shape,
    resolve_assign_shape,
    PostAssignmentResponse,
    ColabRequestError,
)
from colab_cli.state import SessionState


class _Lazy:
    """Stands in for a class from a module only wake/stop_all/discover/keeper need, so the
    other commands (auth_status runs in a fresh process every 40-80 s) skip importing it:
    colab_cli.runtime pulls in the Jupyter kernel client and rich, about 80 ms. Calling it or
    reading an attribute loads the real class; replacing the module attribute (tests) works."""

    def __init__(self, module: str, name: str):
        self._module, self._name, self._real = module, name, None

    def _load(self):
        if self._real is None:
            import importlib

            self._real = getattr(importlib.import_module(self._module), self._name)
        return self._real

    def __call__(self, *args, **kwargs):
        return self._load()(*args, **kwargs)

    def __getattr__(self, attr):
        return getattr(self._load(), attr)


ColabRuntime = _Lazy("colab_cli.runtime", "ColabRuntime")
ContentsClient = _Lazy("colab_cli.contents", "ContentsClient")


def _load_client_config() -> dict:
    cfg_path = os.path.expanduser("~/.colab-cli-oauth-config.json")
    if os.path.exists(cfg_path):
        with open(cfg_path, "r", encoding="utf-8") as f:
            return json.load(f)
    from importlib import resources

    res = resources.files("colab_cli").joinpath("oauth_config.json")
    return json.loads(res.read_text(encoding="utf-8"))


_CHECKED_CREDS = None  # what _has_valid_token just validated or refreshed, for the same process to reuse


def _has_valid_token() -> bool:
    global _CHECKED_CREDS
    _CHECKED_CREDS = None
    if not os.path.exists(TOKEN_CONFIG_PATH):
        return False
    try:
        creds = Credentials.from_authorized_user_file(TOKEN_CONFIG_PATH, PUBLIC_SCOPES)
        if creds and creds.valid:
            _CHECKED_CREDS = creds
            return True
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(TOKEN_CONFIG_PATH, "w", encoding="utf-8") as f:
                f.write(creds.to_json())
            _CHECKED_CREDS = creds
            return True
    except Exception:
        return False
    return False


PKCE_VERIFIER_PATH = os.path.expanduser("~/.config/colab-cli/pkce_verifier.txt")
ACCOUNT_PROFILE_CACHE_PATH = os.path.expanduser("~/.config/colab-cli/account_profile.json")


def _clear_local_session_store():
    """Clear cached Colab session records when switching or logging out."""
    try:
        existing = state.store.list()
        if isinstance(existing, dict):
            for name in list(existing.keys()):
                state.store.remove(name)
    except Exception:
        pass
    state._client = None
    state._sessions = None


def _get_account_email() -> str:
    """Get currently bound Google account email (cached by refresh_token fingerprint)."""
    if not os.path.exists(TOKEN_CONFIG_PATH):
        return ""
    try:
        creds = _CHECKED_CREDS
        if creds is None or not creds.valid:
            creds = Credentials.from_authorized_user_file(TOKEN_CONFIG_PATH, PUBLIC_SCOPES)
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(TOKEN_CONFIG_PATH, "w", encoding="utf-8") as f:
                f.write(creds.to_json())
        tok_fp = (creds.refresh_token or creds.token or "")[-20:]
        if tok_fp and os.path.exists(ACCOUNT_PROFILE_CACHE_PATH):
            try:
                with open(ACCOUNT_PROFILE_CACHE_PATH, "r", encoding="utf-8") as cf:
                    cached = json.load(cf)
                if cached.get("fp") == tok_fp and cached.get("email"):
                    return str(cached["email"])
            except Exception:
                pass
        if creds and creds.token:
            r = requests.get(
                "https://www.googleapis.com/oauth2/v2/userinfo",
                headers={"Authorization": f"Bearer {creds.token}"},
                timeout=5,
            )
            if r.status_code == 200:
                email = str(r.json().get("email") or "").strip()
                if email and tok_fp:
                    os.makedirs(os.path.dirname(ACCOUNT_PROFILE_CACHE_PATH), exist_ok=True)
                    with open(ACCOUNT_PROFILE_CACHE_PATH, "w", encoding="utf-8") as cf:
                        json.dump({"fp": tok_fp, "email": email}, cf)
                return email
    except Exception:
        pass
    return ""


def cmd_auth_url():
    client_config = _load_client_config()
    flow = InstalledAppFlow.from_client_config(client_config, PUBLIC_SCOPES)
    flow.redirect_uri = REMOTE_REDIRECT_URI
    auth_url, _ = flow.authorization_url(prompt="select_account consent", token_usage="remote")
    os.makedirs(os.path.dirname(PKCE_VERIFIER_PATH), exist_ok=True)
    if getattr(flow, "code_verifier", None):
        with open(PKCE_VERIFIER_PATH, "w", encoding="utf-8") as f:
            f.write(flow.code_verifier)
    print(json.dumps({"ok": True, "auth_url": auth_url}))


def _same_account(old_email: str, new_email: str) -> bool:
    return bool(old_email) and bool(new_email) and old_email.strip().casefold() == new_email.strip().casefold()


def _reset_state_cache():
    state._client = None
    state._sessions = None


def _cached_account_email() -> str:
    """The email remembered from the last successful profile lookup, whatever token it was
    fetched with. The old token is often unusable exactly when someone rebinds (a failed
    refresh is why auth_status says logged_in:false), so it cannot be the only source."""
    try:
        with open(ACCOUNT_PROFILE_CACHE_PATH, "r", encoding="utf-8") as cf:
            return str(json.load(cf).get("email") or "")
    except Exception:
        return ""


def _prune_sessions_not_assigned():
    """After binding another (or an unidentifiable) account: drop only the named sessions
    whose endpoint the new account does not hold. A session still backed by a live
    assignment is kept, and if the assignments cannot be read nothing is deleted (wake and
    discover_url already ignore records without a live assignment)."""
    _reset_state_cache()
    try:
        endpoints = {a.endpoint for a in state.client.list_assignments()}
        existing = state.store.list()
        if isinstance(existing, dict):
            for name, sess in list(existing.items()):
                if getattr(sess, "endpoint", None) not in endpoints:
                    state.store.remove(name)
    except Exception:
        pass
    _reset_state_cache()


def cmd_auth_code(code: str):
    """Exchange the code and save the token. The named Colab sessions in sessions.json are
    shared with colab-gpu-llm-service and point at live runtimes. Authorising the same
    account again keeps them all; for another account only those the new account does not
    hold are dropped."""
    global _CHECKED_CREDS
    try:
        client_config = _load_client_config()
        flow = InstalledAppFlow.from_client_config(client_config, PUBLIC_SCOPES)
        flow.redirect_uri = REMOTE_REDIRECT_URI
        if os.path.exists(PKCE_VERIFIER_PATH):
            with open(PKCE_VERIFIER_PATH, "r", encoding="utf-8") as f:
                flow.code_verifier = f.read().strip()
        flow.fetch_token(code=code.strip())
        creds = flow.credentials
        # The token file still holds the previous account; if it cannot be used, fall back
        # to the email remembered for it, before that cache is removed below.
        old_email = _get_account_email() or _cached_account_email()
        for p in (ACCOUNT_PROFILE_CACHE_PATH, PKCE_VERIFIER_PATH):
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass
        os.makedirs(os.path.dirname(TOKEN_CONFIG_PATH), exist_ok=True)
        with open(TOKEN_CONFIG_PATH, "w", encoding="utf-8") as f:
            f.write(creds.to_json())
        _CHECKED_CREDS = None
        new_email = _get_account_email()
        if _same_account(old_email, new_email):
            _reset_state_cache()
        else:  # another account, or one of the two could not be told
            _prune_sessions_not_assigned()
        return cmd_auth_status()
    except Exception as e:
        print(json.dumps({"ok": False, "logged_in": False, "error": str(e)}))


def cmd_auth_logout():
    _clear_local_session_store()
    for p in (TOKEN_CONFIG_PATH, PKCE_VERIFIER_PATH, ACCOUNT_PROFILE_CACHE_PATH):
        if os.path.exists(p):
            try:
                os.remove(p)
            except Exception:
                pass
    print(json.dumps({"ok": True, "logged_in": False, "email": ""}))


def _safe_sync_sessions(with_status: bool = False):
    """(sessions, assignments[, ok]). On repeated failure falls back to cached sessions with
    an empty assignment list, which is NOT the same as "no runtime": callers that would
    allocate or release on that basis must ask with_status=True and check ok."""
    for attempt in range(3):
        try:
            sessions, assignments = state.sync_sessions()
            return (sessions, assignments, True) if with_status else (sessions, assignments)
        except Exception:
            if attempt == 2:
                cached = {s.name: s for s in state.store.list()}
                return (cached, [], False) if with_status else (cached, [])
            time.sleep(1.2)


def _run_together(*fns):
    """Run the callables on their own threads and return [(ok, value_or_exception), ...] in
    order. The account calls in auth_status are independent network round trips, so their
    waits overlap instead of adding up."""
    slots = [None] * len(fns)

    def runner(i, fn):
        try:
            slots[i] = (True, fn())
        except BaseException as e:  # SystemExit from colab_cli must not vanish with the thread
            slots[i] = (False, e)

    threads = [threading.Thread(target=runner, args=(i, fn), daemon=True) for i, fn in enumerate(fns[1:], 1)]
    for t in threads:
        t.start()
    runner(0, fns[0])
    for t in threads:
        t.join()
    return slots


def cmd_auth_status():
    if not _has_valid_token():
        print(json.dumps({"ok": True, "logged_in": False}))
        return

    email = None
    try:
        client = state.client  # build the authorized session once, before the calls fan out
        (info_ok, info), (sync_ok, synced), (mail_ok, mail) = _run_together(
            client.get_consumption_user_info, _safe_sync_sessions, _get_account_email)
        email = mail if mail_ok else None
        if not info_ok:
            raise info
        if not sync_ok:
            raise synced
        local_sessions, assignments = synced
        name_by_ep = {s.endpoint: s.name for s in local_sessions.values()}
        sess_list = []
        for a in assignments:
            sess_list.append({
                "name": name_by_ep.get(a.endpoint, "colab-vm"),
                "endpoint": a.endpoint,
                "accelerator": a.accelerator.value,
                "variant": a.variant.name,
                "shape": a.machine_shape.name,
            })

        if email is None:
            email = _get_account_email()
        # The keeper records the ledger every LEDGER_POLL_S while a runtime runs. This covers the
        # zero-assignment balance (the next session's start) and a runtime with no live keeper.
        try:
            stale = time.time() - float(ledger_load().get("checked_ts") or 0) > 3 * LEDGER_POLL_S
        except (TypeError, ValueError):
            stale = True
        if int(info.assignments_count) <= 0 or stale:
            ledger_record(info.paid_compute_units_balance, info.assignments_count, info.consumption_rate_hourly,
                          assignments[0].endpoint if assignments else "")
        print(json.dumps({
            "ok": True,
            "logged_in": True,
            "email": email,
            "paid_balance": round(float(info.paid_compute_units_balance), 2),
            "burn_rate_hourly": round(float(info.consumption_rate_hourly), 2),
            "active_assignments": int(info.assignments_count),
            "sessions": sess_list,
        }))
    except Exception as e:
        print(json.dumps({
            "ok": False,
            "logged_in": True,
            "email": email if email is not None else _get_account_email(),
            "error": str(e),
        }))


def cmd_stop_all():
    if not _has_valid_token():
        print(json.dumps({"ok": False, "error": "尚未登录 Google Colab CLI"}))
        return

    try:
        local_sessions, assignments = state.sync_sessions()
        stopped_count = 0
        for s in list(local_sessions.values()):
            try:
                rt = ColabRuntime(s.url, s.token, kernel_id=s.kernel_id)
                rt.stop(shutdown_kernel=True)
            except Exception:
                pass
            try:
                state.client.unassign(s.endpoint)
                stopped_count += 1
            except Exception:
                pass
            state.store.remove(s.name)

        for a in assignments:
            try:
                state.client.unassign(a.endpoint)
                stopped_count += 1
            except Exception:
                pass

        stop_keeper()
        clear_server_token()

        try:
            import glob
            import subprocess
            cli_dir = os.path.expanduser("~/.config/colab-cli")
            for ka_file in glob.glob(os.path.join(cli_dir, "keepalive-*.json")):
                try:
                    with open(ka_file, "r", encoding="utf-8") as kf:
                        ka_pid = int(json.load(kf).get("pid") or 0)
                    if ka_pid > 0:
                        if sys.platform == "win32":
                            subprocess.run(["taskkill", "/F", "/PID", str(ka_pid)], capture_output=True)
                        else:
                            os.kill(ka_pid, 9)
                except Exception:
                    pass
                try:
                    os.remove(ka_file)
                except Exception:
                    pass
        except Exception:
            pass

        stop_local_mcp_if_unused()
        info = state.client.get_consumption_user_info()
        ledger_record(info.paid_compute_units_balance, info.assignments_count, info.consumption_rate_hourly)
        print(json.dumps({
            "ok": True,
            "stopped_count": stopped_count,
            "paid_balance": round(float(info.paid_compute_units_balance), 2),
            "burn_rate_hourly": round(float(info.consumption_rate_hourly), 2),
            "active_assignments": int(info.assignments_count),
            "message": f"🛑 已彻底释放全部云端实例！当前计费速率: {info.consumption_rate_hourly:.2f} CU/小时",
        }))
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}))


def _wait_for_runtime_ready(session: SessionState, timeout_s: int = 45) -> bool:
    """
    Wait for Colab container Jupyter server (/api/kernels) to be ready.
    If 404/410 detected repeatedly, returns False so instance can be reallocated.
    """
    base_url = session.url.rstrip("/")
    probe_url = f"{base_url}/api/kernels"
    headers = {
        "Accept": "application/json",
        "X-Colab-Client-Agent": "colab-cli",
        "X-Colab-Runtime-Proxy-Token": session.token,
        "Authorization": f"Bearer {session.token}",
    }
    params = {"authuser": "0", "colab-runtime-proxy-token": session.token}
    deadline = time.time() + timeout_s
    not_found_count = 0

    while time.time() < deadline:
        try:
            r = requests.get(probe_url, headers=headers, params=params, timeout=8)
            if r.status_code == 200:
                return True
            if r.status_code in (404, 410):
                not_found_count += 1
                if not_found_count >= 2:
                    return False
        except requests.exceptions.RequestException:
            pass
        time.sleep(2.0)
    return False


GPU_ACCEL_MAP = {
    "h100": Accelerator.H100,
    "g4": Accelerator.G4,
    "a100": Accelerator.A100,
    "l4": Accelerator.L4,
    "t4": Accelerator.T4,
}

SINGLE_SHAPE_GPUS = frozenset({Accelerator.G4, Accelerator.H100, Accelerator.L4})


def _resolve_gpu_accel(gpu_type: str) -> Accelerator:
    return GPU_ACCEL_MAP.get((gpu_type or "g4").strip().lower(), Accelerator.G4)


def _new_uuid():
    import uuid

    return uuid.uuid4()


def _allocate_fresh_session(session_name: str, gpu_type: str) -> SessionState:
    accel = _resolve_gpu_accel(gpu_type)
    shape = None if accel in SINGLE_SHAPE_GPUS else resolve_assign_shape(accel, high_mem=True)
    gpu_desc = "G4 (RTX PRO 6000 · 96GB)" if accel == Accelerator.G4 else accel.value
    print(f"WAKE_LOG: 🚀 正在向 Google Colab 申请分配 {gpu_desc} 实例...", flush=True)
    ledger_poll()  # the balance right before billing starts: this session's exact starting point
    try:
        res = state.client.assign(_new_uuid(), variant=Variant.GPU, accelerator=accel, shape=shape)
    except ColabRequestError as req_err:
        if shape is not None and getattr(getattr(req_err, "response", None), "status_code", None) == 400:
            shape = None
            res = state.client.assign(_new_uuid(), variant=Variant.GPU, accelerator=accel, shape=None)
        else:
            raise
    if isinstance(res, PostAssignmentResponse):
        token = res.runtime_proxy_info.token
        url = res.runtime_proxy_info.url
        endpoint = res.endpoint
        exp = res.runtime_proxy_info.expires_at()
    else:
        token = res.runtime_proxy_info.token if hasattr(res, "runtime_proxy_info") else getattr(res, "runtime_proxy_token", "")
        url = res.runtime_proxy_info.url if hasattr(res, "runtime_proxy_info") else ""
        endpoint = res.endpoint
        exp = res.runtime_proxy_info.expires_at() if hasattr(res, "runtime_proxy_info") else None

    s = SessionState(
        name=session_name,
        token=token,
        url=url,
        endpoint=endpoint,
        token_expires_at=exp,
        variant=Variant.GPU.value,
        accelerator=accel.value,
        machine_shape=Shape.HIGH_RAM.name if shape == Shape.HIGH_RAM else Shape.STANDARD.name,
    )
    state.store.add(s)
    print(f"WAKE_LOG: ✅ 成功分配 {accel.value} 云端虚拟机！正在等待容器内核就绪...", flush=True)
    return s


def _local_app_data(env=None, home: str = None) -> str:
    env = os.environ if env is None else env
    if env.get("LOCALAPPDATA") or env.get("APPDATA"):
        return env.get("LOCALAPPDATA") or env.get("APPDATA")
    home = os.path.expanduser("~") if home is None else home
    if sys.platform == "darwin":
        return os.path.join(home, "Library", "Application Support")
    if sys.platform != "win32":
        return env.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share")
    return os.path.join(home, "AppData", "Local")


def resolve_runners_path() -> str:
    """colab_kernel_runners.json: the first existing of $COLAB_YOGA_RUNNERS,
    $COLAB_YOGA_DIR/, next to this install (<install>/), the config dir
    %LOCALAPPDATA%/Colab-yoga/ (where colab-gpu-llm-service's colab_kernel.py exports it by
    default), then the standard install %LOCALAPPDATA%/Programs/Colab-yoga/. The same order
    as runnersCandidates in console/backend.go. When none exists, the config dir's path (which
    callers check with os.path.isfile)."""
    name = "colab_kernel_runners.json"
    install_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    local_app_data = _local_app_data()
    cfg_file = os.path.join(local_app_data, "Colab-yoga", name)
    candidates = []
    if os.environ.get("COLAB_YOGA_RUNNERS"):
        candidates.append(os.environ["COLAB_YOGA_RUNNERS"])
    if os.environ.get("COLAB_YOGA_DIR"):
        candidates.append(os.path.join(os.environ["COLAB_YOGA_DIR"], name))
    candidates += [
        os.path.join(install_dir, name),
        cfg_file,
        os.path.join(local_app_data, "Programs", "Colab-yoga", name),
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return cfg_file


# How a runner boot script names the PC's tailnet address for the reverse tunnel. An unset
# local_tailscale_ip leaves a <YOUR_...> placeholder (or nothing) there.
_LOCAL_MCP_IP_RE = re.compile(r"""LOCAL_MCP_IP\s*=\s*["']([^"']*)["']""")
_LOCAL_MCP_ATON_RE = re.compile(r"inet_aton\(\"([0-9.]+)\"\)")
_TUNNEL_CFG_CACHE: tuple = ("", -1, -1, False)


def local_mcp_tunnel_configured(env=None) -> bool:
    """True when the cloud is set up to reach this PC's MCP: a runner names a real LOCAL_MCP_IP
    (not empty, not a <YOUR_ placeholder), or COLAB_LOCAL_MCP_AUTO=1. Only then do wake and the
    keeper start the local MCP on their own; a manual start is never gated by this."""
    global _TUNNEL_CFG_CACHE
    env = os.environ if env is None else env
    if env.get("COLAB_LOCAL_MCP_AUTO") == "1":
        return True
    path = resolve_runners_path()
    if not path:
        return False
    try:
        st = os.stat(path)
    except OSError:
        return False
    key = (path, st.st_mtime_ns, st.st_size)
    if _TUNNEL_CFG_CACHE[:3] == key:
        return _TUNNEL_CFG_CACHE[3]
    try:
        with open(path, "r", encoding="utf-8") as f:
            runners = json.load(f)
    except Exception:
        return False
    configured = False
    if isinstance(runners, dict):
        for info in runners.values():
            code = str(info.get("code") or "") if isinstance(info, dict) else ""
            for ip in _LOCAL_MCP_IP_RE.findall(code) + _LOCAL_MCP_ATON_RE.findall(code):
                ip = ip.strip()
                if ip and not ip.startswith("<YOUR_"):
                    configured = True
                    break
            if configured:
                break
    _TUNNEL_CFG_CACHE = (*key, configured)
    return configured


SERVER_TOKEN_REMOTE = "content/colab_yoga_token"


def _upload_server_token(contents, token: str) -> None:
    """Put the token on the VM as /content/colab_yoga_token (the server reads it at start).
    Goes through a private temp file that is removed right after."""
    fd, tmp = tempfile.mkstemp(prefix="colab-yoga-tok-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(token)
        for attempt in range(4):
            try:
                contents.upload(tmp, SERVER_TOKEN_REMOTE)
                return
            except Exception:
                if attempt == 3:
                    raise
                time.sleep(2.5)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _on_wake_url(token: str) -> None:
    """The server is up. Before the URL line reaches the console or an agent: store the
    token it needs for POST calls, and make sure a keeper is keeping the runtime alive (the
    command line alone has no console to spawn one)."""
    try:
        save_server_token(token)
    except Exception as e:
        print(f"WAKE_LOG: ⚠️ 本机保存控制令牌失败：{type(e).__name__}", flush=True)
    result = ensure_keeper()
    if result == "failed":
        print("WAKE_LOG: ⚠️ 后台保活没能启动：请手动运行 colab_yoga_bridge.py keeper，否则实例约 21-35 分钟后会被回收。", flush=True)
    elif result == "started":
        print("WAKE_LOG: 已启动后台保活（独立进程，关掉这个命令也不会中断）", flush=True)

    if not local_mcp_tunnel_configured():
        return  # no reverse tunnel: the local MCP is optional and starts only by hand
    mcp = ensure_local_mcp()
    if mcp["state"] == "started":
        print("WAKE_LOG: 已启动本机 MCP 桥", flush=True)
    elif mcp["state"] == "failed":
        print(f"WAKE_LOG: ⚠️ 本机 MCP 桥没能启动（{mcp['error']}）。GPU 不受影响；网页里用本机工具会报错，修好后重新 wake 即可。", flush=True)


def _release_fresh_session(session: SessionState):
    """Give back an instance this wake just allocated and could not deploy to, so a failed
    wake does not leave an idle GPU billing. Never called for a reused instance."""
    print(f"WAKE_LOG: ↩️ 部署失败，正在释放本次新分配的实例 [{session.accelerator}]，避免空转计费...", flush=True)
    try:
        state.client.unassign(session.endpoint)
    except Exception as e:
        print(f"WAKE_LOG: ⚠️ 释放新实例失败（{type(e).__name__}），请在 Colab 页面或点「释放」确认它已停。", flush=True)
    try:
        state.prune_session(session.name)
    except Exception:
        pass


def cmd_wake(gpu_type: str = "G4", server_py_path: str = None, app_py_path: str = None) -> bool:
    """
    Wake/reuse Colab GPU VM, upload colab_yoga_server.py & runners, execute vm_boot.py.
    Returns True only when the server printed its WAKE_URL. An instance allocated by this
    call is released if the deployment then fails; a reused one is left alone.
    """
    if not _has_valid_token():
        print("WAKE_ERROR: 尚未授权登录 Google 账号，请先点击「🔑 绑定账号」", flush=True)
        return False

    if not server_py_path:
        server_py_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "server", "colab_yoga_server.py"
        )
    server_py_path = os.path.abspath(server_py_path)

    fresh_session = None  # set only when this call allocated the instance
    try:
        session_name = "easycolab"
        print("WAKE_LOG: 🔍 正在检查账号下已有的 Colab 实例...", flush=True)
        local_sessions, assignments, synced = _safe_sync_sessions(with_status=True)
        if not synced:
            print("WAKE_ERROR: 暂时无法确认账号下的 Colab 实例（网络或授权问题），为避免重复开卡已停止，请稍后重试。", flush=True)
            return False
        ep_map = {a.endpoint: a for a in assignments}

        named = [local_sessions.get(session_name)]
        live = [s for s in named if s] + list(local_sessions.values())
        target_session = next((s for s in live if s.endpoint in ep_map), None)
        if target_session is None and not assignments:
            target_session = next((s for s in live), None)
        if target_session:
            session_name = target_session.name

        if not target_session and assignments:
            a = assignments[0]
            target_session = SessionState(
                name=session_name,
                token=a.runtime_proxy_info.token,
                url=a.runtime_proxy_info.url,
                endpoint=a.endpoint,
                token_expires_at=a.runtime_proxy_info.expires_at(),
                variant=Variant.GPU.value,
                accelerator=a.accelerator.value,
                machine_shape=Shape.HIGH_RAM.name if a.machine_shape == Shape.HIGH_RAM else Shape.STANDARD.name,
            )
            state.store.add(target_session)

        requested_accel = _resolve_gpu_accel(gpu_type)
        if target_session:
            cur_acc = (target_session.accelerator or "").strip().upper()
            if cur_acc != requested_accel.value.upper():
                print(
                    f"WAKE_LOG: 🔄 当前已分配实例型号为 [{cur_acc or 'CPU'}]，正在释放并切换为您选择的 [{requested_accel.value}]...",
                    flush=True,
                )
                try:
                    state.client.unassign(target_session.endpoint)
                except Exception:
                    pass
                state.prune_session(target_session.name)
                target_session = None
            elif target_session.endpoint not in ep_map:
                print("WAKE_LOG: ⚠️ 旧实例已不在账号的分配列表里（已被回收），正在重新分配新实例...", flush=True)
                state.prune_session(target_session.name)
                target_session = None
            else:
                info_proxy = ep_map[target_session.endpoint].runtime_proxy_info
                target_session.token = info_proxy.token
                target_session.url = info_proxy.url
                target_session.token_expires_at = info_proxy.expires_at()
                state.store.add(target_session)
                print(f"WAKE_LOG: ♻️ 正在检查已有实例 [{target_session.name}] ({target_session.accelerator}) 连通性...", flush=True)
                if not _wait_for_runtime_ready(target_session, timeout_s=90):
                    print(
                        "WAKE_ERROR: 已有实例仍在计费，但暂时连不上；为免误删上面正在运行的模型，没有自动释放。"
                        "请稍后再点「重新部署」；确定不用了再点「释放」。",
                        flush=True,
                    )
                    return False

        if not target_session:
            target_session = _allocate_fresh_session(session_name, gpu_type)
            fresh_session = target_session
            if not _wait_for_runtime_ready(target_session, timeout_s=50):
                print("WAKE_LOG: ⏳ 容器仍在初始化，继续尝试连接内核...", flush=True)

        print("WAKE_LOG: 正在上传 Colab-yoga 服务端代码 (colab_yoga_server.py)...", flush=True)
        contents = ContentsClient(target_session)
        for up_attempt in range(4):
            try:
                contents.upload(server_py_path, "content/colab_yoga_server.py")
                break
            except Exception as up_err:
                if up_attempt == 3:
                    raise up_err
                time.sleep(2.5)

        runners_path = resolve_runners_path()
        if runners_path and os.path.isfile(runners_path):
            try:
                contents.upload(runners_path, "content/colab_kernel_runners.json")
            except Exception:
                pass

        # A fresh bearer for the server's control API, delivered as a file the server reads
        # at start. Never printed, never put in the code sent to the kernel (the runtime
        # keeps a history of that) or in the URL line.
        api_token = secrets.token_urlsafe(32)
        print("WAKE_LOG: 正在下发控制接口令牌...", flush=True)
        _upload_server_token(contents, api_token)

        vm_boot_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "server", "vm_boot.py"
        )
        if not os.path.isfile(vm_boot_path):
            raise RuntimeError(f"vm_boot.py 未找到: {vm_boot_path}")

        with open(vm_boot_path, "r", encoding="utf-8") as f:
            boot_code = f.read()

        def _on_kid(kid):
            target_session.kernel_id = kid
            state.store.add(target_session)

        def _on_sid(sid):
            target_session.session_id = sid
            state.store.add(target_session)

        hook_called = False
        got_url = False
        boot_errors: list[str] = []  # printed after a fresh instance is released, not before

        def _handle_output_item(out):
            nonlocal hook_called, got_url
            hook_called = True
            if isinstance(out, dict):
                if "text" in out:
                    for line in str(out["text"]).splitlines():
                        if line.startswith("WAKE_ERROR:"):
                            boot_errors.append(line)
                            continue
                        if line.startswith("WAKE_URL:"):
                            got_url = True
                            _on_wake_url(api_token)
                        print(line, flush=True)
                elif out.get("output_type") == "error":
                    boot_errors.append("WAKE_ERROR: " + str(out.get("evalue", "Execution error")))

        def _run_boot_on_runtime(use_saved_kernel: bool):
            rt = ColabRuntime(
                target_session.url,
                target_session.token,
                session_name=target_session.name,
                history=state.history,
                kernel_id=target_session.kernel_id if use_saved_kernel else None,
                session_id=target_session.session_id if use_saved_kernel else None,
                on_kernel_started=_on_kid,
                on_session_started=_on_sid,
            )
            try:
                return rt.execute_code(
                    boot_code,
                    allow_stdin=False,
                    output_hook=_handle_output_item,
                    timeout=300,
                )
            finally:
                try:
                    rt.stop(shutdown_kernel=False)
                except Exception:
                    pass

        try:
            outputs = _run_boot_on_runtime(use_saved_kernel=True)
        except Exception:
            target_session.kernel_id = None
            target_session.session_id = None
            outputs = _run_boot_on_runtime(use_saved_kernel=False)

        if not hook_called and outputs:
            for out in outputs:
                _handle_output_item(out)

        if got_url:  # the server is up and its URL went out; late errors are only reported
            for line in boot_errors:
                print(line, flush=True)
            return True
        if not boot_errors:
            boot_errors.append("WAKE_ERROR: 部署脚本已结束，但没有拿到服务地址（WAKE_URL）")
        if fresh_session is not None:
            _release_fresh_session(fresh_session)
        for line in boot_errors:
            print(line, flush=True)
        return False

    except Exception as e:
        if fresh_session is not None:
            _release_fresh_session(fresh_session)
        print(f"WAKE_ERROR: {str(e)}", flush=True)
        return False


_DISCOVER_CODE = r"""
import json, os, re, subprocess
_u = None
if os.path.exists('/content/colab_yoga_server.log'):
    _m = re.findall(r'COLAB_YOGA_URL=(https://\S+)', open('/content/colab_yoga_server.log', errors='replace').read())
    if _m:
        _u = _m[-1].strip()
_alive = subprocess.run(['pgrep', '-f', 'python.*/content/colab_yoga_server.py'], capture_output=True).returncode == 0
_t = None
if os.path.exists('/content/colab_yoga_token'):
    _t = open('/content/colab_yoga_token', errors='replace').read().strip() or None
print('EC_DISCOVER:' + json.dumps({'url': _u, 'app_alive': _alive, 'token': _t}))
"""


def cmd_discover_url():
    """
    Find public URL of server already running on the active runtime.
    Reads server log on the VM instead of redeploying.
    """
    if not _has_valid_token():
        print(json.dumps({"ok": False, "logged_in": False}))
        return
    try:
        local_sessions, assignments = _safe_sync_sessions()
        ep_map = {a.endpoint: a for a in assignments}
        live = [s for s in local_sessions.values() if s.endpoint in ep_map]
        if not live and assignments:
            a0 = assignments[0]
            s_auto = SessionState(
                name="easycolab",
                token=a0.runtime_proxy_info.token,
                url=a0.runtime_proxy_info.url,
                endpoint=a0.endpoint,
                token_expires_at=a0.runtime_proxy_info.expires_at(),
                variant=Variant.GPU.value,
                accelerator=a0.accelerator.value,
                machine_shape=Shape.HIGH_RAM.name if a0.machine_shape == Shape.HIGH_RAM else Shape.STANDARD.name,
            )
            state.store.add(s_auto)
            live = [s_auto]
        if not live:
            print(json.dumps({"ok": True, "url": None, "app_alive": False, "active_assignments": 0}))
            return
        s = live[0]
        info_proxy = ep_map[s.endpoint].runtime_proxy_info
        s.token = info_proxy.token
        s.url = info_proxy.url
        s.token_expires_at = info_proxy.expires_at()
        state.store.add(s)

        outputs = _exec_on_session(s, _DISCOVER_CODE, timeout=30)
        found = None
        for out in outputs:
            for line in str(out.get("text") or "").splitlines():
                if line.startswith("EC_DISCOVER:"):
                    found = json.loads(line[len("EC_DISCOVER:") :])
        if found is None:
            print(json.dumps({"ok": False, "error": "no discover output", "active_assignments": len(assignments)}))
            return
        # The token stays out of the printed answer: it goes to the local token file.
        token = found.pop("token", None)
        token_saved = False
        if token and found.get("app_alive"):
            try:
                save_server_token(str(token))
                token_saved = True
            except Exception:
                pass
        found.update(ok=True, active_assignments=len(assignments), accelerator=s.accelerator,
                     token_saved=token_saved)
        print(json.dumps(found))
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}))


KEEPER_BEAT_S = 30
KEEPER_ASSIGN_CHECK_S = 120
KEEPER_SERVER_GRACE_S = 20 * 60
KEEPER_MAX_HOURS = 24

_KEEPER_CODE = r"""
import json, os, re, subprocess, urllib.request
_o = {'url': None, 'app_alive': False}
if os.path.exists('/content/colab_yoga_server.log'):
    _m = re.findall(r'COLAB_YOGA_URL=(https://\S+)', open('/content/colab_yoga_server.log', errors='replace').read())
    if _m:
        _o['url'] = _m[-1].strip()
_o['app_alive'] = subprocess.run(['pgrep', '-f', 'python.*/content/colab_yoga_server.py'], capture_output=True).returncode == 0
try:
    _opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    _res = _opener.open('http://127.0.0.1:7860/api/status', timeout=15).read()
    _d = json.loads(_res)
    _o.update(idle_remaining=_d.get('idle_remaining_seconds'), idle_timeout=_d.get('idle_timeout_minutes'),
              llm_online=(_d.get('llm_server') or {}).get('online'))
except Exception as _e:
    _o['status_error'] = type(_e).__name__
print('EC_KEEPER:' + json.dumps(_o))
"""


def _colab_yoga_config_dir() -> str:
    base = _local_app_data()
    path = os.path.join(base, "Colab-yoga")
    os.makedirs(path, exist_ok=True)
    return path


def keeper_state_path() -> str:
    return os.path.join(_colab_yoga_config_dir(), "keeper.json")


def server_token_path() -> str:
    return os.path.join(_colab_yoga_config_dir(), "server_token.json")


def save_server_token(token: str) -> None:
    """The server's control token, for this PC's console and for agents that POST to its
    API. Lives in the per-user config dir, never in the repo or a package."""
    path = server_token_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"token": token, "saved": time.time()}, f)
    os.replace(tmp, path)


def load_server_token() -> str:
    try:
        with open(server_token_path(), "r", encoding="utf-8") as f:
            return str(json.load(f).get("token") or "")
    except Exception:
        return ""


def clear_server_token() -> None:
    try:
        os.remove(server_token_path())
    except OSError:
        pass


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
        k32.CloseHandle(h)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError as e:
        return e.errno == errno.EPERM


def keeper_status() -> dict:
    try:
        with open(keeper_state_path(), "r", encoding="utf-8") as f:
            st = json.load(f)
    except Exception:
        return {"alive": False}
    st["alive"] = not st.get("exit_reason") and _pid_alive(int(st.get("pid") or 0))
    return st


def _keeper_save(st: dict) -> None:
    path = keeper_state_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)
    os.replace(tmp, path)


# ─── Official ledger (what the account was actually charged) ──────────────────
# Colab settles the CU balance about every 5 minutes, each step roughly the advertised
# consumptionRateHourly times the time since the previous one. The keeper reads it every
# LEDGER_POLL_S and ledger.json keeps the readings where the balance dropped, so the console
# shows what was really deducted and a rate measured from those drops, not uptime x price.
LEDGER_POLL_S = 15
LEDGER_POINTS_MAX = 60
# The balance only moves while something is assigned, so the last zero-assignment reading is the
# session's starting balance. Older than this, a session nobody watched may have run in between.
LEDGER_PRE_MAX_AGE_S = 2 * 3600
_LEDGER_EPS = 1e-6


def ledger_state_path() -> str:
    return os.path.join(_colab_yoga_config_dir(), "ledger.json")


def ledger_load() -> dict:
    try:
        with open(ledger_state_path(), "r", encoding="utf-8") as f:
            led = json.load(f)
        return led if isinstance(led, dict) else {}
    except Exception:
        return {}


def _ledger_save(led: dict) -> None:
    path = ledger_state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(led, f, ensure_ascii=False)
    os.replace(tmp, path)


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def ledger_observe(led: dict, now: float, balance: float, active: int, official_rate: float,
                   endpoint: str = "") -> dict:
    """Fold one official ledger reading into led and return it.

    A session is a stretch of readings with active > 0. Its start_balance is the last
    zero-assignment balance when that was read within LEDGER_PRE_MAX_AGE_S (exact), else the
    session's first reading (not exact: whatever was charged before it is unknown). points are
    [ts, balance] for every reading where the balance dropped, i.e. observed settlements. A
    rise is a top-up or refund, not a charge: it moves start_balance up by the same amount and
    restarts the points so no measured rate spans it."""
    led = dict(led or {})
    prev = led.get("balance")
    sess = dict(led["session"]) if isinstance(led.get("session"), dict) else None
    if active <= 0:
        led["idle_balance"], led["idle_ts"] = balance, now
        if sess and not sess.get("ended_ts"):
            if _is_num(prev) and balance < prev - _LEDGER_EPS:
                sess["points"] = (list(sess.get("points") or []) + [[now, balance]])[-LEDGER_POINTS_MAX:]
            sess["ended_ts"] = now
            led["session"] = sess
    else:
        if sess is None or sess.get("ended_ts"):
            pre, pre_ts = led.get("idle_balance"), led.get("idle_ts")
            exact = (_is_num(pre) and _is_num(pre_ts) and 0 <= now - pre_ts <= LEDGER_PRE_MAX_AGE_S
                     and pre >= balance - _LEDGER_EPS)
            sess = {"start_balance": pre if exact else balance, "start_ts": now, "exact": bool(exact),
                    "points": [], "endpoint": endpoint}
        else:
            points = list(sess.get("points") or [])
            if _is_num(prev) and balance < prev - _LEDGER_EPS:
                points.append([now, balance])
            elif _is_num(prev) and balance > prev + _LEDGER_EPS:
                sess["start_balance"] = float(sess.get("start_balance") or 0) + (balance - prev)
                points = []
            sess["points"] = points[-LEDGER_POINTS_MAX:]
            if endpoint and not sess.get("endpoint"):
                sess["endpoint"] = endpoint
        sess["official_rate"] = official_rate
        led["session"] = sess
    led.update(balance=balance, checked_ts=now, active=int(active), official_rate=official_rate,
               poll_s=LEDGER_POLL_S)
    return led


def ledger_record(balance: float, active: int, official_rate: float, endpoint: str = "",
                  now: float = None):
    """Fold a reading into ledger.json. Never raises: auth_status and stop_all call it, and a
    bookkeeping problem must not turn their answers into errors. A file that cannot be folded
    into (hand-edited, half-written by an old build) is started over."""
    try:
        args = (time.time() if now is None else now, float(balance), int(active), float(official_rate), endpoint)
        try:
            led = ledger_observe(ledger_load(), *args)
        except Exception:
            led = ledger_observe({}, *args)
        _ledger_save(led)
        return led
    except Exception:
        return None


def ledger_poll(endpoint: str = ""):
    """One ccu-info read folded into ledger.json; None when the read failed."""
    try:
        info = state.client.get_consumption_user_info()
        return ledger_record(info.paid_compute_units_balance, info.assignments_count,
                             info.consumption_rate_hourly, endpoint)
    except Exception:
        return None


def _keeper_wait(until: float, endpoint: str, next_ledger: float) -> float:
    """Sleep until `until`, reading the ledger whenever next_ledger comes due; returns the
    next due time so the cadence carries across beats."""
    while True:
        if time.time() >= next_ledger:
            ledger_poll(endpoint)
            next_ledger = time.time() + LEDGER_POLL_S
        now = time.time()
        if now >= until:
            return next_ledger
        time.sleep(max(0.05, min(until, next_ledger) - now))


def _track_a_covers(endpoint: str | None) -> bool:
    """Whether a live CLI keeper covers this endpoint, or any endpoint when None."""
    import glob

    for f in glob.glob(os.path.join(os.path.expanduser("~/.config/colab-cli"), "keepalive-*.json")):
        try:
            with open(f, "r", encoding="utf-8") as fh:
                ka = json.load(fh)
            if (endpoint is None or ka.get("endpoint") == endpoint) and _pid_alive(int(ka.get("pid") or 0)):
                return True
        except Exception:
            continue
    return False


_HTTP = None


def _http():
    """One requests.Session for the keeper's every-30-s kernel lookups, so the TLS connection
    to the Colab proxy is reused between beats instead of re-negotiated each time."""
    global _HTTP
    if _HTTP is None:
        _HTTP = requests.Session()
    return _HTTP


def _exec_on_session(s, code: str, timeout: int = 40) -> list:
    """Run code in the runtime's existing kernel (the execution itself is the heartbeat)."""
    base_url = s.url.rstrip("/")
    headers = {
        "Accept": "application/json",
        "X-Colab-Client-Agent": "colab-cli",
        "X-Colab-Runtime-Proxy-Token": s.token,
        "Authorization": f"Bearer {s.token}",
    }
    params = {"authuser": "0", "colab-runtime-proxy-token": s.token}
    kid = s.kernel_id
    try:
        r = _http().get(f"{base_url}/api/kernels", headers=headers, params=params, timeout=10)
        if r.status_code == 200:
            known = [k.get("id") for k in (r.json() if r.text else []) if k.get("id")]
            if known and kid not in known:
                kid = known[0]
    except Exception:
        pass

    def _save_kid(k, _s=s):
        _s.kernel_id = k
        state.store.add(_s)

    rt = ColabRuntime(
        s.url,
        s.token,
        session_name=s.name,
        kernel_id=kid,
        session_id=s.session_id,
        on_kernel_started=_save_kid,
    )
    try:
        return rt.execute_code(code, allow_stdin=False, timeout=timeout)
    finally:
        try:
            rt.stop(shutdown_kernel=False)
        except Exception:
            pass


def _tfe_keep_alive(endpoint: str) -> None:
    """Same cheap TFE ping the browser and colab_keepalive.py send."""
    try:
        url = f"{state.client.colab_domain.rstrip('/')}/tun/m/{endpoint}/keep-alive/"
        state.client.session.get(
            url,
            headers={"X-Colab-Tunnel": "Google", "Accept": "application/json", "X-Colab-Client-Agent": "colab-cli"},
            params={"authuser": "0"},
            timeout=10,
        )
    except Exception:
        pass


def keeper_decide(now: float, vm, last_server_ok: float) -> str:
    """'beat' | 'release' (server's idle limit reached) | 'stop' (server gone too long)."""
    if vm and vm.get("app_alive") and vm.get("idle_remaining") is not None:
        tmo = vm.get("idle_timeout")
        if isinstance(tmo, (int, float)) and tmo > 0 and vm["idle_remaining"] <= 0:
            return "release"
        return "beat"
    if now - last_server_ok > KEEPER_SERVER_GRACE_S:
        return "stop"
    return "beat"


def cmd_keeper():
    cur = keeper_status()
    if cur.get("alive") and int(cur.get("pid") or 0) != os.getpid():
        print(json.dumps({"ok": True, "already_running": True, "pid": cur.get("pid")}))
        return
    st = {"pid": os.getpid(), "started": time.time(), "beats": 0, "beat_fails": 0, "exit_reason": ""}
    _keeper_save(st)
    if not _has_valid_token():
        st["exit_reason"] = "not_logged_in"
        _keeper_save(st)
        return
    start = time.time()
    last_server_ok = start
    last_assign_check = 0.0
    next_ledger = 0.0
    sess = None
    mcp_check = None  # the in-flight local MCP check; a thread, so a slow helper cannot stall a beat
    last_mcp_check = 0.0
    mcp_fails = [0]  # consecutive failed automatic starts, written by the check thread

    def keeper_mcp_check():
        if not local_mcp_tunnel_configured():
            return
        res = ensure_local_mcp()
        mcp_fails[0] = mcp_fails[0] + 1 if res.get("state") == "failed" else 0
    try:
        while True:
            cycle = time.time()
            if cycle - start > KEEPER_MAX_HOURS * 3600:
                st["exit_reason"] = "max_hours"
                break
            if sess is None or cycle - last_assign_check >= KEEPER_ASSIGN_CHECK_S:
                local, assigns, synced = _safe_sync_sessions(with_status=True)
                if synced:
                    last_assign_check = cycle
                    ep_map = {a.endpoint: a for a in assigns}
                    live = sorted(
                        (s for s in local.values() if s.endpoint in ep_map),
                        key=lambda s: 0 if s.name == "easycolab" else 1,
                    )
                    if not live:
                        st["exit_reason"] = "no_assignment"
                        break
                    sess = live[0]
                    proxy = ep_map[sess.endpoint].runtime_proxy_info
                    sess.token, sess.url, sess.token_expires_at = proxy.token, proxy.url, proxy.expires_at()
                    state.store.add(sess)
                    st["endpoint"] = sess.endpoint
            if sess is None:
                next_ledger = _keeper_wait(time.time() + KEEPER_BEAT_S, "", next_ledger)
                continue

            vm = None
            if _track_a_covers(sess.endpoint):
                st["last_detail"] = "track_a_daemon_covers"
            else:
                try:
                    for out in _exec_on_session(sess, _KEEPER_CODE):
                        for line in str(out.get("text") or "").splitlines():
                            if line.startswith("EC_KEEPER:"):
                                vm = json.loads(line[len("EC_KEEPER:"):])
                    st["beats"] += 1
                    st["last_beat_ts"] = time.time()
                    st["last_detail"] = "ok"
                except Exception as e:
                    st["beat_fails"] += 1
                    st["last_detail"] = f"beat_error {str(e)[:120]}"
                _tfe_keep_alive(sess.endpoint)
            if vm and vm.get("app_alive") and vm.get("idle_remaining") is not None:
                last_server_ok = time.time()
                st.update(server_url=vm.get("url"), idle_remaining=vm.get("idle_remaining"),
                          llm_online=vm.get("llm_online"))
            decision = keeper_decide(time.time(), vm, last_server_ok)
            if decision == "release":
                try:
                    state.client.unassign(sess.endpoint)
                    state.prune_session(sess.name)
                except Exception as e:
                    st["last_detail"] = f"unassign_error {str(e)[:120]}"
                st["exit_reason"] = "idle_release"
                break
            if decision == "stop":
                st["exit_reason"] = "server_unreachable"
                break
            # A live session is confirmed here (sess came from the account's assignments).
            if (cycle - last_mcp_check >= local_mcp_retry_s(mcp_fails[0])
                    and (mcp_check is None or not mcp_check.is_alive())):
                last_mcp_check = cycle
                mcp_check = threading.Thread(target=keeper_mcp_check, daemon=True)
                mcp_check.start()
            _keeper_save(st)
            next_ledger = _keeper_wait(time.time() + max(1.0, KEEPER_BEAT_S - (time.time() - cycle)),
                                       sess.endpoint, next_ledger)
    except Exception as e:
        st["exit_reason"] = f"crash {str(e)[:160]}"
    finally:
        if st.get("exit_reason") in ("idle_release", "no_assignment"):
            ledger_poll(st.get("endpoint") or "")  # the release settlement, and the session's end
            if mcp_check is not None:
                mcp_check.join(LOCAL_MCP_TIMEOUT_S)  # a start still in flight must not outlive the stop
            stop_local_mcp_if_unused()
        st["ended"] = time.time()
        st["exit_reason"] = st.get("exit_reason") or "loop_ended"
        _keeper_save(st)


def _keeper_python() -> str:
    """Bundled runtime's Python (pythonw.exe on Windows, bin/python3 on POSIX), else sys.executable."""
    install_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for cand in (
        os.path.join(install_dir, "runtime", "pythonw.exe"),
        os.path.join(install_dir, "runtime", "bin", "python3"),
        os.path.join(os.path.dirname(sys.executable), "pythonw.exe"),
    ):
        if os.path.isfile(cand):
            return cand
    return sys.executable


def spawn_keeper() -> bool:
    """Start `keeper` as a detached process that outlives this one, the way the console's
    SpawnKeeper does: <app>/runtime/pythonw.exe -s -X utf8 <bridge> keeper, own process group,
    no window, outside this process's job when Windows allows."""
    bridge = os.path.abspath(__file__)
    install_dir = os.path.dirname(os.path.dirname(bridge))
    py = _keeper_python()
    # -s keeps the user's own site-packages from shadowing the pinned ones (the macOS runtime
    # reads them); a host Python used for a dev run may have its packages only there.
    bundled = os.path.abspath(py).startswith(os.path.join(install_dir, "runtime") + os.sep)
    cmd = [py] + (["-s"] if bundled else []) + ["-X", "utf8", bridge, "keeper"]
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    kwargs = dict(cwd=install_dir, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                  stderr=subprocess.DEVNULL, close_fds=True)
    if sys.platform == "win32":
        base = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED_PROCESS | NEW_PROCESS_GROUP | NO_WINDOW
        for flags in (base | 0x01000000, base):  # CREATE_BREAKAWAY_FROM_JOB is refused inside some jobs
            try:
                subprocess.Popen(cmd, creationflags=flags, **kwargs)
                return True
            except OSError:
                continue
        return False
    try:
        subprocess.Popen(cmd, start_new_session=True, **kwargs)
        return True
    except OSError:
        return False


def ensure_keeper() -> str:
    """'running' when a live keeper already exists (keeper.json, judged like the console's
    readKeeper: no exit_reason and the pid alive), else 'started' or 'failed'."""
    if keeper_status().get("alive"):
        return "running"
    return "started" if spawn_keeper() else "failed"


def stop_keeper() -> None:
    st = keeper_status()
    pid = int(st.get("pid") or 0)
    if st.get("alive") and pid > 0:
        try:
            if sys.platform == "win32":
                import subprocess
                subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
            else:
                os.kill(pid, 15)
        except Exception:
            pass
    if st.get("pid"):
        st.update(exit_reason=st.get("exit_reason") or "stopped", ended=time.time())
        st.pop("alive", None)
        try:
            _keeper_save(st)
        except Exception:
            pass


# ─── Local MCP bridge (optional) ──────────────────────────────────────────────
# The MCP server on this PC that the cloud model reaches through the VM's forwarder. Its
# process is managed by a helper script found at run time, never hard-coded here: this file
# only runs `start|status|stop` on it. The helper owns the process it starts, so stop never
# touches a server started by hand or by another program, and nothing is killed by port.
LOCAL_MCP_CHECK_S = 60  # while a session is live, the keeper checks the local MCP at most this often
LOCAL_MCP_TIMEOUT_S = 15  # the helper waits up to 8 s for readiness; past this the call counts as failed
LOCAL_MCP_BACKOFF_S = (60, 300, 1800)  # the keeper's wait after 1, 2, 3+ failed starts in a row
LOCAL_MCP_CONFIG = "local_mcp_bridge.json"


def local_mcp_retry_s(failures: int) -> int:
    """How long the keeper waits before its next automatic start: the normal cadence while it
    works, backing off 60 s -> 300 s -> 1800 s while it keeps failing."""
    if failures <= 0:
        return LOCAL_MCP_CHECK_S
    return LOCAL_MCP_BACKOFF_S[min(failures, len(LOCAL_MCP_BACKOFF_S)) - 1]
LOCAL_MCP_SKILL_SCRIPT = os.path.join("skills", "colab-gpu-llm-service", "scripts", "local_mcp.py")


def _install_dir() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def local_mcp_script_path(install_dir: str = None, env=None, home: str = None):
    """The helper script: the first existing of $COLAB_LOCAL_MCP_SCRIPT, the install's
    local_mcp_bridge.json "script", the config dir's %LOCALAPPDATA%/Colab-yoga/
    local_mcp_bridge.json "script" (colab_kernel.py writes it on export), then the
    colab-gpu-llm-service skill under ~/.agents and ~/.claude. Relative paths count from the
    install dir. None when there is none, which is the normal state of an install without the
    local MCP."""
    install_dir = _install_dir() if install_dir is None else install_dir
    env = os.environ if env is None else env
    home = os.path.expanduser("~") if home is None else home
    candidates = [env.get("COLAB_LOCAL_MCP_SCRIPT") or ""]
    local_app_data = _local_app_data(env=env, home=home)
    config_dirs = []
    if env.get("COLAB_YOGA_DIR"):
        config_dirs.append(env["COLAB_YOGA_DIR"])
    config_dirs += [install_dir, os.path.join(local_app_data, "Colab-yoga")]
    for config_dir in config_dirs:
        try:
            with open(os.path.join(config_dir, LOCAL_MCP_CONFIG), "r", encoding="utf-8") as f:
                candidates.append(str(json.load(f).get("script") or ""))
        except Exception:
            pass
    candidates += [os.path.join(home, ".agents", LOCAL_MCP_SKILL_SCRIPT),
                   os.path.join(home, ".claude", LOCAL_MCP_SKILL_SCRIPT)]
    for cand in candidates:
        if cand:
            path = os.path.abspath(os.path.join(install_dir, cand))
            if os.path.isfile(path):
                return path
    return None


def _local_mcp_python() -> str:
    """The bundled runtime's Python, else the interpreter in use."""
    install_dir = _install_dir()
    for cand in (
        os.path.join(install_dir, "runtime", "python.exe"),
        os.path.join(install_dir, "runtime", "bin", "python3"),
    ):
        if os.path.isfile(cand):
            return cand
    return sys.executable


def run_local_mcp(action: str):
    """Run the helper's `start`, `status` or `stop` and return its last JSON line. ok is false
    on a non-zero exit, a timeout, or unreadable output. None when no helper is found. Output
    goes to a temp file, not a pipe: a process the helper starts may inherit a pipe and keep it
    open, which would stall the read past the timeout."""
    script = local_mcp_script_path()
    if script is None:
        return None
    cmd = [_local_mcp_python(), "-X", "utf8", script, action]
    kwargs = {"creationflags": 0x08000000} if sys.platform == "win32" else {}  # CREATE_NO_WINDOW
    with tempfile.TemporaryFile() as out:
        try:
            done = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                  timeout=LOCAL_MCP_TIMEOUT_S, **kwargs)
        except (OSError, subprocess.SubprocessError) as e:
            return {"ok": False, "error": f"helper did not finish ({type(e).__name__})"}
        out.seek(0)
        text = out.read().decode("utf-8", errors="replace")
    lines = [line for line in text.splitlines() if line.strip().startswith("{")]
    result = {}
    if lines:
        try:
            parsed = json.loads(lines[-1])
            if isinstance(parsed, dict):
                result = parsed
        except ValueError:
            pass
    result["ok"] = bool(result.get("ok")) and done.returncode == 0
    if not result["ok"] and not result.get("error"):
        result["error"] = (f"helper reported failure (exit {done.returncode})" if lines
                           else f"helper printed no status line (exit {done.returncode})")
    return result


def ensure_local_mcp() -> dict:
    """{"state": "absent" | "running" | "started" | "failed", "error": str}. Safe to repeat: the
    helper decides whether anything has to start, and says whether this call started it."""
    res = run_local_mcp("start")
    if res is None:
        return {"state": "absent", "error": ""}
    if not res.get("ok"):
        return {"state": "failed", "error": str(res.get("error"))}
    return {"state": "started" if res.get("started") else "running", "error": ""}


def stop_local_mcp():
    """Called when the last session is released. The helper stops only what it owns."""
    return run_local_mcp("stop")


def stop_local_mcp_if_unused():
    """A second manager may still own a session; an unavailable account is not proof of idleness."""
    _, assignments, synced = _safe_sync_sessions(with_status=True)
    if not synced or assignments or _track_a_covers(None):
        return None
    return stop_local_mcp()


def _status_confirms_running(st) -> bool:
    """A start that returned is not proof that the server is up. Only the helper's own read-only
    status is: it confirms with running true, or with a pid that is alive."""
    if not isinstance(st, dict) or st.get("ok") is False or st.get("running") is False:
        return False
    if st.get("running") is True:
        return True
    try:
        return _pid_alive(int(st.get("pid") or 0))
    except (TypeError, ValueError):
        return False


def cmd_mcp_start() -> dict:
    """The console's manual fallback for the local MCP. It starts that server and nothing else:
    no GPU is woken or kept, so it works with no runtime running."""
    mcp = ensure_local_mcp()
    if mcp["state"] == "absent":
        return {"ok": False, "state": "absent",
                "error": f"没找到本机 MCP 桥脚本 local_mcp.py：请安装 colab-gpu-llm-service 的 scripts/local_mcp.py，或在安装目录的 {LOCAL_MCP_CONFIG} 里写明它的路径"}
    if mcp["state"] == "failed":
        return {"ok": False, "state": "failed", "error": mcp["error"]}
    status = run_local_mcp("status")  # the helper's read-only status is the proof, not the start
    if not _status_confirms_running(status):
        why = (status or {}).get("error") or "没有返回运行状态"
        return {"ok": False, "state": "failed", "error": f"启动命令已返回，但状态检查没确认服务在跑：{why}"}
    return {"ok": True, "state": mcp["state"], "error": ""}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Colab-yoga Bridge")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("auth_status")
    sub.add_parser("auth_url")
    sub.add_parser("auth_logout")

    p_code = sub.add_parser("auth_code")
    p_code.add_argument("code", type=str)

    p_wake = sub.add_parser("wake")
    p_wake.add_argument("--gpu", type=str, default="G4")
    p_wake.add_argument(
        "--server-py",
        type=str,
        default=None,
        help="Path to local colab_yoga_server.py (defaults to ../server/colab_yoga_server.py)",
    )
    p_wake.add_argument(
        "--app-py",
        type=str,
        default="",
        help="Legacy argument; ignored in Colab-yoga",
    )

    sub.add_parser("stop_all")
    sub.add_parser("discover_url")
    sub.add_parser("keeper")
    sub.add_parser("mcp_start")

    args = parser.parse_args(argv)
    if args.cmd == "auth_status":
        cmd_auth_status()
    elif args.cmd == "auth_url":
        cmd_auth_url()
    elif args.cmd == "auth_logout":
        cmd_auth_logout()
    elif args.cmd == "auth_code":
        cmd_auth_code(args.code)
    elif args.cmd == "wake":
        # Only wake reports failure through its exit code; the JSON commands always exit 0
        # and carry ok/error in their output, which is what the console parses.
        return 0 if cmd_wake(args.gpu, server_py_path=args.server_py, app_py_path=args.app_py) else 1
    elif args.cmd == "stop_all":
        cmd_stop_all()
    elif args.cmd == "discover_url":
        cmd_discover_url()
    elif args.cmd == "keeper":
        cmd_keeper()
    elif args.cmd == "mcp_start":
        print(json.dumps(cmd_mcp_start(), ensure_ascii=False), flush=True)


def run_cli(argv=None):
    code = 0
    try:
        code = main(argv) or 0
    finally:
        for stream in (sys.stdout, sys.stderr):
            try:
                if stream is not None:
                    stream.flush()
            except Exception:
                pass
        os._exit(code)


if __name__ == "__main__":
    run_cli()
