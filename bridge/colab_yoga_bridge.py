#!/usr/bin/env python3
"""
Colab-yoga Bridge: manages Google Colab GPU runtimes, server deployment,
URL discovery, and detached heartbeat.
Derived from EasyColab's colab_bridge.py (original author wy51ai, Apache 2.0).

Supports subcommands:
  - auth_status  : Check OAuth login state, CU balance, hourly burn rate, and active VMs
  - auth_url     : Generate Google OAuth2 authorization URL
  - auth_code    : Exchange Google authorization code and save token
  - auth_logout  : Disconnect and clear local tokens and sessions
  - wake         : Provision/reuse Colab GPU VM, upload colab_yoga_server.py & runners, run vm_boot.py
  - stop_all     : Terminate and unassign all active Colab VMs on account
  - discover_url : Read running server tunnel URL from VM log without redeploying
  - keeper       : Detached heartbeat daemon that keeps the Colab runtime alive while active
"""
import os
import re
import sys
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


def cmd_auth_code(code: str):
    try:
        client_config = _load_client_config()
        flow = InstalledAppFlow.from_client_config(client_config, PUBLIC_SCOPES)
        flow.redirect_uri = REMOTE_REDIRECT_URI
        if os.path.exists(PKCE_VERIFIER_PATH):
            with open(PKCE_VERIFIER_PATH, "r", encoding="utf-8") as f:
                flow.code_verifier = f.read().strip()
        flow.fetch_token(code=code.strip())
        creds = flow.credentials
        _clear_local_session_store()
        for p in (ACCOUNT_PROFILE_CACHE_PATH, PKCE_VERIFIER_PATH):
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass
        os.makedirs(os.path.dirname(TOKEN_CONFIG_PATH), exist_ok=True)
        with open(TOKEN_CONFIG_PATH, "w", encoding="utf-8") as f:
            f.write(creds.to_json())
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

        info = state.client.get_consumption_user_info()
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


def resolve_runners_path() -> str | None:
    """
    Look up colab_kernel_runners.json per architecture contract:
    Newer of <install>/colab_kernel_runners.json and the legacy
    %LOCALAPPDATA%/Programs/EasyColab/_internal/colab_kernel_runners.json
    """
    install_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    install_path = os.path.join(install_dir, "colab_kernel_runners.json")

    local_app_data = (
        os.environ.get("LOCALAPPDATA")
        or os.environ.get("APPDATA")
        or os.path.expanduser("~/AppData/Local")
    )
    legacy_path = os.path.join(
        local_app_data, "Programs", "EasyColab", "_internal", "colab_kernel_runners.json"
    )

    has_install = os.path.isfile(install_path)
    has_legacy = os.path.isfile(legacy_path)

    if has_install and has_legacy:
        return (
            install_path
            if os.path.getmtime(install_path) >= os.path.getmtime(legacy_path)
            else legacy_path
        )
    if has_install:
        return install_path
    if has_legacy:
        return legacy_path
    return None


def cmd_wake(gpu_type: str = "G4", server_py_path: str = None, app_py_path: str = None):
    """
    Wake/reuse Colab GPU VM, upload colab_yoga_server.py & runners, execute vm_boot.py.
    """
    if not _has_valid_token():
        print("WAKE_ERROR: 尚未授权登录 Google 账号，请先点击「🔑 绑定账号」", flush=True)
        return

    if not server_py_path:
        server_py_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "server", "colab_yoga_server.py"
        )
    server_py_path = os.path.abspath(server_py_path)

    try:
        session_name = "easycolab"
        print("WAKE_LOG: 🔍 正在检查账号下已有的 Colab 实例...", flush=True)
        local_sessions, assignments, synced = _safe_sync_sessions(with_status=True)
        if not synced:
            print("WAKE_ERROR: 暂时无法确认账号下的 Colab 实例（网络或授权问题），为避免重复开卡已停止，请稍后重试。", flush=True)
            return
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
                        "请稍后再点「唤醒实例并部署」；确定不用了再点「释放全部实例」。",
                        flush=True,
                    )
                    return

        if not target_session:
            target_session = _allocate_fresh_session(session_name, gpu_type)
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

        vm_boot_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "server", "vm_boot.py"
        )
        if not os.path.isfile(vm_boot_path):
            print(f"WAKE_ERROR: vm_boot.py 未找到: {vm_boot_path}", flush=True)
            return

        with open(vm_boot_path, "r", encoding="utf-8") as f:
            boot_code = f.read()

        def _on_kid(kid):
            target_session.kernel_id = kid
            state.store.add(target_session)

        def _on_sid(sid):
            target_session.session_id = sid
            state.store.add(target_session)

        hook_called = False

        def _handle_output_item(out):
            nonlocal hook_called
            hook_called = True
            if isinstance(out, dict):
                if "text" in out:
                    for line in str(out["text"]).splitlines():
                        print(line, flush=True)
                elif out.get("output_type") == "error":
                    print("WAKE_ERROR: " + str(out.get("evalue", "Execution error")), flush=True)

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

    except Exception as e:
        print(f"WAKE_ERROR: {str(e)}", flush=True)


_DISCOVER_CODE = r"""
import json, os, re, subprocess
_u = None
if os.path.exists('/content/colab_yoga_server.log'):
    _m = re.findall(r'COLAB_YOGA_URL=(https://\S+)', open('/content/colab_yoga_server.log', errors='replace').read())
    if _m:
        _u = _m[-1].strip()
_alive = subprocess.run(['pgrep', '-f', 'python.*/content/colab_yoga_server.py'], capture_output=True).returncode == 0
print('EC_DISCOVER:' + json.dumps({'url': _u, 'app_alive': _alive}))
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
        found.update(ok=True, active_assignments=len(assignments), accelerator=s.accelerator)
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
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or os.path.expanduser("~/AppData/Local")
    path = os.path.join(base, "Colab-yoga")
    os.makedirs(path, exist_ok=True)
    return path


def keeper_state_path() -> str:
    return os.path.join(_colab_yoga_config_dir(), "keeper.json")


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
    except OSError:
        return False


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


def _track_a_covers(endpoint: str) -> bool:
    """colab-gpu-llm-service's own keep-alive daemon already beats this runtime."""
    import glob

    for f in glob.glob(os.path.join(os.path.expanduser("~/.config/colab-cli"), "keepalive-*.json")):
        try:
            with open(f, "r", encoding="utf-8") as fh:
                ka = json.load(fh)
            if ka.get("endpoint") == endpoint and _pid_alive(int(ka.get("pid") or 0)):
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
    sess = None
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
                time.sleep(KEEPER_BEAT_S)
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
            _keeper_save(st)
            time.sleep(max(1.0, KEEPER_BEAT_S - (time.time() - cycle)))
    except Exception as e:
        st["exit_reason"] = f"crash {str(e)[:160]}"
    finally:
        st["ended"] = time.time()
        st["exit_reason"] = st.get("exit_reason") or "loop_ended"
        _keeper_save(st)


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
        cmd_wake(args.gpu, server_py_path=args.server_py, app_py_path=args.app_py)
    elif args.cmd == "stop_all":
        cmd_stop_all()
    elif args.cmd == "discover_url":
        cmd_discover_url()
    elif args.cmd == "keeper":
        cmd_keeper()


def run_cli(argv=None):
    try:
        main(argv)
    finally:
        for stream in (sys.stdout, sys.stderr):
            try:
                if stream is not None:
                    stream.flush()
            except Exception:
                pass
        os._exit(0)


if __name__ == "__main__":
    run_cli()
