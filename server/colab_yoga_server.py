"""Colab-yoga server: runs on the Colab VM next to llama-server :8081.

Derived from EasyColab's app.py (wy51ai, Apache 2.0), reduced to what the Colab-yoga
console needs: an HTTP API (see skills/colab-yoga/references/api.md), live telemetry, the colab_kernel
runner boot, an idle watchdog that releases the runtime, and the detached MCP forwarder.
Conversations never pass through here; web, Magpie and MCP reach llama-server directly.
"""
import ctypes
import inspect
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from collections import deque
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

SERVER_VERSION = "1.1.0"
SERVER_PORT = 7860
RUNNERS_FILE = "/content/colab_kernel_runners.json"
APP_LOG = "/var/colab/app.log"
URL_MARKER = "COLAB_YOGA_URL="

BOOT_TIME = time.time()
LAST_ACTIVE_TIME = time.time()
IDLE_TIMEOUT_MINUTES = 30  # 0 turns the auto-release off
CONTEXT_LENGTH = 262144  # what the console asked for (the runner scripts fix 256K per slot)

# Control API bearer token (see docs/architecture.md, "Control token"). The wake uploads it to
# API_TOKEN_FILE; COLAB_YOGA_TOKEN in the environment wins when set. With neither, the
# server makes one and writes it there, so discover_url can still hand it to the owner.
API_TOKEN_FILE = "/content/colab_yoga_token"
API_TOKEN = ""


def load_api_token() -> str:
    """Set API_TOKEN from the environment, else API_TOKEN_FILE; make one if neither exists."""
    global API_TOKEN
    token = os.environ.get("COLAB_YOGA_TOKEN", "").strip()
    if not token:
        try:
            with open(API_TOKEN_FILE, "r", encoding="utf-8") as f:
                token = f.read().strip()
        except OSError:
            token = ""
    if not token:
        token = secrets.token_urlsafe(32)
        try:
            with open(API_TOKEN_FILE, "w", encoding="utf-8", newline="") as f:
                f.write(token)
        except OSError:
            pass
    try:
        os.chmod(API_TOKEN_FILE, 0o600)
    except OSError:
        pass
    API_TOKEN = token
    return token


def _token_ok(header: str) -> bool:
    scheme, _, cred = (header or "").partition(" ")
    if not API_TOKEN or scheme.lower() != "bearer":
        return False
    return secrets.compare_digest(cred.strip().encode("utf-8"), API_TOKEN.encode("utf-8"))


# The models are whatever RUNNERS_FILE holds: slug -> runner, whose `alias` is the name
# llama-server reports on /v1/models. These are only nicer display names for the models the
# author ships; any other model is shown by its alias.
KNOWN_MODEL_NAMES = {
    "qwen38-huihui-v2": "Qwen3.8-Flash-Next (Huihui v2)",
    "gemma4-31b": "Gemma-4-31B (ARA Heretic Q8_0)",
    "qwen38-27b-rvn": "Qwen3.8-27B (RVN Heretic BF16)",
    "glm53-flash": "GLM-5.3-Flash (320B-A18B 破限)",
}
_RUNNER_ALIAS_MEMO: Dict[str, Any] = {"sig": None, "map": {}}


def runner_alias_to_id() -> Dict[str, str]:
    """alias -> runner slug for every runner in RUNNERS_FILE; {} when it is missing or
    unreadable. Re-read only when the file's mtime or size changes."""
    try:
        st = os.stat(RUNNERS_FILE)
    except OSError:
        _RUNNER_ALIAS_MEMO.update(sig=None, map={})
        return {}
    sig = (RUNNERS_FILE, st.st_mtime_ns, st.st_size)
    if _RUNNER_ALIAS_MEMO["sig"] == sig:
        return _RUNNER_ALIAS_MEMO["map"]
    mapping: Dict[str, str] = {}
    try:
        with open(RUNNERS_FILE, "r", encoding="utf-8") as f:
            runners = json.load(f)
        for slug, info in runners.items():
            alias = str((info or {}).get("alias") or "").strip() if isinstance(info, dict) else ""
            if alias:
                mapping.setdefault(alias, str(slug))
    except Exception:
        mapping = {}
    _RUNNER_ALIAS_MEMO.update(sig=sig, map=mapping)
    return mapping


def model_display_name(model_id: str) -> str:
    return KNOWN_MODEL_NAMES.get(model_id) or next(
        (alias for alias, slug in runner_alias_to_id().items() if slug == model_id), model_id)


# ==============================================================================
# Short-TTL single-flight cache for the /api/status hot path
# ==============================================================================
def _mono() -> float:
    """Monotonic clock for cache ages (tests replace this)."""
    return time.monotonic()


class _TTLCell:
    """One value that is recomputed at most once per `ttl` seconds, however many threads ask.

    Concurrent callers of an expired cell wait for the single computation that is already
    running instead of starting their own (single-flight); `force=True` still shares a
    computation that started after the caller arrived. An exception from `compute` is not
    cached and reaches the caller that ran it."""

    def __init__(self, ttl: float):
        self.ttl = ttl
        self._lock = threading.Lock()
        self._started = 0  # computations begun so far; orders force requests against runs
        self._entry: Optional[Tuple[float, int, Any]] = None  # (clock at start, run number, value)

    def get(self, compute: Callable[[], Any], force: bool = False) -> Any:
        seen = self._started
        entry = self._entry
        if entry is not None and not force and _mono() - entry[0] < self.ttl:
            return entry[2]
        with self._lock:
            entry = self._entry
            if entry is not None and (entry[1] > seen if force else _mono() - entry[0] < self.ttl):
                return entry[2]
            self._started += 1
            started = _mono()
            value = compute()
            self._entry = (started, self._started, value)
            return value

    def invalidate(self) -> None:
        self._entry = None


# ==============================================================================
# Console ring buffer: mirrors stdout/stderr so the console can show the VM's log
# ==============================================================================
CONSOLE_LOCK = threading.Lock()
CONSOLE_LINES: deque = deque(maxlen=120)
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


class ConsoleMirror:
    """Passes writes through to the real stream and keeps the finished lines. A carriage
    return rewrites the line (tqdm style), so only its last state is kept."""

    def __init__(self, orig_stream, lines: deque, lock: threading.Lock):
        self.orig_stream = orig_stream
        self.lines = lines
        self.lock = lock
        # print() writes the text and the newline separately, from any thread.
        self._tls = threading.local()

    def write(self, data: str):
        try:
            self.orig_stream.write(data)
        except Exception:
            pass
        if not data:
            return
        *done, pending = (getattr(self._tls, "partial", "") + _ANSI_RE.sub("", data)).split("\n")
        self._tls.partial = pending
        with self.lock:
            for raw in done:
                shown = [seg.strip() for seg in raw.split("\r") if seg.strip()]
                if shown:
                    self.lines.append(f"[{datetime.now().strftime('%H:%M:%S')}] {shown[-1]}")

    def flush(self):
        try:
            self.orig_stream.flush()
        except Exception:
            pass

    def isatty(self):
        return getattr(self.orig_stream, "isatty", lambda: False)()


def install_console_mirror() -> None:
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        if not isinstance(stream, ConsoleMirror):
            setattr(sys, name, ConsoleMirror(stream, CONSOLE_LINES, CONSOLE_LOCK))


def get_console_lines() -> List[str]:
    with CONSOLE_LOCK:
        return list(CONSOLE_LINES)[-60:]


# ==============================================================================
# Runtime clock and CU estimate
# ==============================================================================
_RUNTIME_ASSIGNED_TS: Optional[float] = None


def _runtime_assigned_ts(app_log: str = APP_LOG) -> Optional[float]:
    """When this runtime was handed to the user, i.e. billing started: the first Jupyter
    "Kernel started" line (every client runs code right after assignment). Not /proc/uptime
    or process start times: Colab boots VMs ahead in a pool, and those were 46-80 min older
    than the assignment on the G4s observed."""
    global _RUNTIME_ASSIGNED_TS
    if _RUNTIME_ASSIGNED_TS is not None:
        return _RUNTIME_ASSIGNED_TS
    with _UPTIME_LOCK:
        if _RUNTIME_ASSIGNED_TS is not None:
            return _RUNTIME_ASSIGNED_TS
        scan = _UPTIME_SCAN
        if scan["path"] != app_log:
            scan.update(path=app_log, offset=0, next_try=0.0)
        now = _mono()
        if now < scan["next_try"]:
            return None
        try:
            ts = _scan_app_log_for_kernel_start(app_log, scan)
        except OSError:
            ts = None
        if ts is not None:
            _RUNTIME_ASSIGNED_TS = ts
            return ts
        scan["next_try"] = now + _UPTIME_RETRY_S
    return None


# The line only appears once, early, so before it shows up the log is followed from where
# the previous look stopped instead of being read again from the top on every status call.
_UPTIME_LOCK = threading.Lock()
_UPTIME_SCAN: Dict[str, Any] = {"path": None, "offset": 0, "next_try": 0.0}
_UPTIME_RETRY_S = 1.0


def _scan_app_log_for_kernel_start(app_log: str, scan: Dict[str, Any]) -> Optional[float]:
    size = os.stat(app_log).st_size
    if size == scan["offset"]:
        return None  # nothing new since the last look: no need to open it
    if size < scan["offset"]:
        scan["offset"] = 0  # truncated or replaced
    with open(app_log, "rb") as f:
        f.seek(scan["offset"])
        for line in f:
            if not line.endswith(b"\n"):
                break  # still being written: look again from here next time
            scan["offset"] += len(line)
            if b"Kernel started" not in line:
                continue
            try:
                stamp = str(json.loads(line).get("time") or "")
                if stamp:
                    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
            except Exception:
                continue
    return None


def _runtime_uptime_seconds(app_log: str = APP_LOG) -> int:
    """Billed time of this runtime, not of this server process (redeploying the server on a
    running VM would otherwise restart the clock and under-report the CU spent)."""
    now = time.time()
    ts = _runtime_assigned_ts(app_log)
    if ts is not None and ts <= now:
        return int(max(now - ts, now - BOOT_TIME))
    return int(now - BOOT_TIME)


def estimate_cu_burn_rate(gpu_name: str) -> float:
    """Colab compute units per hour for a GPU model."""
    name_up = (gpu_name or "").upper()
    if "H100" in name_up:
        return 11.78
    if "PRO 6000" in name_up or "BLACKWELL" in name_up or "G4" in name_up:
        return 10.05
    if "A100" in name_up:
        return 5.37
    if "L4" in name_up:
        return 1.71
    if "T4" in name_up:
        return 1.17
    if "V100" in name_up:
        return 3.80
    return 1.50


# ==============================================================================
# Telemetry (no torch: it would pin GPU memory and take seconds to import)
# ==============================================================================
_GB = 1024 ** 3
NO_GPU_NAME = "CPU Mode (未检测到 GPU)"
NO_GPU_CU_RATE = 0.10
_TELEMETRY_TTL = 2.0
_HARDWARE_CELL = _TTLCell(_TELEMETRY_TTL)


def _query_nvidia_smi() -> Optional[Tuple[str, float, float]]:
    """(name, used GB, total GB) of the first GPU, or None without a usable nvidia-smi."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.used,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout
        name, used_mib, total_mib = [p.strip() for p in out.strip().splitlines()[0].rsplit(",", 2)]
        return name, float(used_mib) / 1024.0, float(total_mib) / 1024.0
    except Exception:
        return None


class _NvmlMemoryV2(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint), ("total", ctypes.c_ulonglong), ("reserved", ctypes.c_ulonglong),
                ("free", ctypes.c_ulonglong), ("used", ctypes.c_ulonglong)]


def _load_nvml():
    for lib_name in ("libnvidia-ml.so.1", "libnvidia-ml.so", "nvml.dll"):
        try:
            return ctypes.CDLL(lib_name)
        except OSError:
            continue
    raise OSError("libnvidia-ml not found")


class _NvmlReader:
    """GPU name and memory from libnvidia-ml, the library nvidia-smi itself reads, so a
    status poll does not fork a process every few seconds. It checks itself against
    nvidia-smi once (same name and total, used within 0.25 GB); any mismatch or missing
    symbol turns it off for good and nvidia-smi keeps being used. A runtime error backs
    off to nvidia-smi for a minute, then the library is opened again."""

    RETRY_S = 60.0
    TOLERANCE_GB = 0.25

    def __init__(self, loader: Callable[[], Any] = _load_nvml):
        self._loader = loader
        self._lock = threading.Lock()
        self._lib: Any = None
        self._handle: Any = None
        self._name = ""
        self._off = False
        self._retry_at = 0.0

    def query(self) -> Optional[Tuple[str, float, float]]:
        """(name, used GB, total GB) or None when the caller must use nvidia-smi."""
        if self._off or _mono() < self._retry_at:
            return None
        with self._lock:
            if self._off:
                return None
            try:
                if self._lib is None:
                    self._open()
                return self._read()
            except Exception:
                self._lib = None
                if not self._off:
                    self._retry_at = _mono() + self.RETRY_S
                return None

    def _open(self) -> None:
        try:
            lib = self._loader()
            mem_fn = lib.nvmlDeviceGetMemoryInfo_v2
            mem_fn.argtypes = [ctypes.c_void_p, ctypes.POINTER(_NvmlMemoryV2)]
            lib.nvmlInit_v2.restype = ctypes.c_int
            lib.nvmlDeviceGetHandleByIndex_v2.argtypes = [ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p)]
            lib.nvmlDeviceGetName.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint]
        except (OSError, AttributeError):
            self._off = True  # no library or too old a driver: nothing to retry
            raise
        if lib.nvmlInit_v2() != 0:
            raise OSError("nvmlInit failed")
        handle = ctypes.c_void_p()
        if lib.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(handle)) != 0:
            raise OSError("no GPU at index 0")
        buf = ctypes.create_string_buffer(96)
        if lib.nvmlDeviceGetName(handle, buf, 96) != 0:
            raise OSError("nvmlDeviceGetName failed")
        self._lib, self._handle, self._name = lib, handle, buf.value.decode("utf-8", errors="replace").strip()
        mine = self._read()
        for attempt in range(3):  # memory may move between the two reads while a model loads
            ref = _query_nvidia_smi()
            if ref is None:
                return  # nothing to compare with; the library answered
            if (ref[0] == self._name and abs(ref[2] - mine[2]) < 0.01
                    and abs(ref[1] - mine[1]) <= self.TOLERANCE_GB):
                return
            mine = self._read()
        self._off = True
        self._lib = None
        raise OSError("nvml disagrees with nvidia-smi")

    def _read(self) -> Tuple[str, float, float]:
        mem = _NvmlMemoryV2()
        mem.version = ctypes.sizeof(_NvmlMemoryV2) | (2 << 24)
        if self._lib.nvmlDeviceGetMemoryInfo_v2(self._handle, ctypes.byref(mem)) != 0:
            raise OSError("nvmlDeviceGetMemoryInfo failed")
        return self._name, mem.used / _GB, mem.total / _GB


_NVML = _NvmlReader()


def _read_gpu() -> Optional[Tuple[str, float, float]]:
    return _NVML.query() or _query_nvidia_smi()


def _read_ram_gb() -> Tuple[float, float]:
    """(total GB, used GB) from /proc/meminfo."""
    total_kb = avail_kb = 0
    try:
        with open("/proc/meminfo", "r") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    total_kb = int(line.split()[1])
                elif line.startswith("MemAvailable:"):
                    avail_kb = int(line.split()[1])
    except Exception:
        return 0.0, 0.0
    return round(total_kb / 1024 ** 2, 1), round(max(0, total_kb - avail_kb) / 1024 ** 2, 1)


def _read_disk_gb() -> Tuple[float, float]:
    """(total GB, used GB) of the volume models are downloaded to."""
    try:
        du = shutil.disk_usage("/content" if os.path.exists("/content") else os.getcwd())
        return round(du.total / _GB, 1), round((du.total - du.free) / _GB, 1)
    except Exception:
        return 0.0, 0.0


def _build_hardware_snapshot() -> Dict[str, Any]:
    gpu = _read_gpu()
    if gpu is None:
        gpu_name, vram_used, vram_total, cu_rate = NO_GPU_NAME, 0.0, 0.0, NO_GPU_CU_RATE
    else:
        gpu_name, vram_used, vram_total = gpu[0], round(gpu[1], 2), round(gpu[2], 2)
        cu_rate = estimate_cu_burn_rate(gpu_name)
    ram_total, ram_used = _read_ram_gb()
    disk_total, disk_used = _read_disk_gb()
    return {
        "gpu_name": gpu_name,
        "vram_used_gb": vram_used,
        "vram_total_gb": vram_total,
        "cu_burn_rate_per_hour": cu_rate,
        "ram_used_gb": ram_used,
        "ram_total_gb": ram_total,
        "disk_used_gb": disk_used,
        "disk_total_gb": disk_total,
    }


def get_hardware_snapshot() -> Dict[str, Any]:
    """Telemetry at most ~2 s old; concurrent callers share one computation."""
    return dict(_HARDWARE_CELL.get(_build_hardware_snapshot))


# ==============================================================================
# Idle clock, Jupyter heartbeat, runtime release
# ==============================================================================
_LAST_JUPYTER_HEARTBEAT_TS = 0.0
_HEARTBEAT_LOCK = threading.Lock()


def _ping_local_colab_jupyter(force: bool = False) -> None:
    """Touch the VM's own Jupyter server (127.0.0.1:8080) now and then. All real traffic
    runs over Tailscale and the tunnel, so without this Colab can see a quiet container."""
    global _LAST_JUPYTER_HEARTBEAT_TS
    with _HEARTBEAT_LOCK:  # watchdog and request threads race here; one ping thread per interval
        now = time.time()
        if not force and (now - _LAST_JUPYTER_HEARTBEAT_TS) < 40.0:
            return
        _LAST_JUPYTER_HEARTBEAT_TS = now

    def _do_ping():
        opener = _local_opener()
        for ep in ("http://127.0.0.1:8080/api/kernels", "http://127.0.0.1:8080/api/status"):
            try:
                req = urllib.request.Request(ep, headers={"X-Colab-Client-Agent": "colab-cli"})
                with opener.open(req, timeout=2.0) as resp:
                    resp.read(256)
            except Exception:
                pass

    threading.Thread(target=_do_ping, daemon=True).start()


def touch_activity() -> None:
    global LAST_ACTIVE_TIME
    LAST_ACTIVE_TIME = time.time()
    _ping_local_colab_jupyter(force=False)


def idle_remaining_seconds() -> int:
    if IDLE_TIMEOUT_MINUTES <= 0:
        return -1
    idle = int(max(0.0, time.time() - LAST_ACTIVE_TIME))
    return max(0, IDLE_TIMEOUT_MINUTES * 60 - idle)


def _unassign_runtime() -> None:
    """Ask Colab to release this VM. google.colab's runtime.unassign() POSTs to the runtime
    manager at $TBE_RUNTIME_ADDR and then asks the notebook frontend to disconnect, which
    fails in a process that is not a kernel; so when the module is missing or raises, the
    same POST is made directly."""
    try:
        from google.colab import runtime
        runtime.unassign()
        return
    except Exception:
        pass
    addr = os.environ.get("TBE_RUNTIME_ADDR")
    if not addr:
        raise RuntimeError("找不到 Colab 运行时管理服务 (TBE_RUNTIME_ADDR)")
    req = urllib.request.Request(f"http://{addr}/unassign", data=b"", method="POST")
    with _local_opener().open(req, timeout=10) as resp:
        if resp.status != 200:
            raise RuntimeError(f"unassign HTTP {resp.status}")


def disconnect_colab_runtime() -> Tuple[bool, str]:
    try:
        _unassign_runtime()
        return True, "🛑 已向 Colab 发送释放运行时指令，实例即将关闭并停止扣除计算单元。"
    except Exception as e:
        return False, f"⚠️ 释放运行时失败: {e}"


# ==============================================================================
# llama-server :8081 (started by the colab_kernel runner, outside this process)
# ==============================================================================
_LOCAL_OPENER: Optional[urllib.request.OpenerDirector] = None


def _local_opener() -> urllib.request.OpenerDirector:
    """One shared no-proxy opener for loopback calls; open() keeps no per-call state, so
    threads can share it and nobody rebuilds the handler chain on every status poll."""
    global _LOCAL_OPENER
    if _LOCAL_OPENER is None:
        _LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return _LOCAL_OPENER


_ALIAS_MEMO: Tuple[float, str] = (0.0, "")  # (when /v1/models last answered, alias)
_ALIAS_RECHECK_S = 10.0


def _probe_kernel_llama_8081() -> Optional[Dict[str, Any]]:
    """Is the colab_kernel llama-server up on :8081, and which model does it serve?

    /health is asked every time; the alias from /v1/models is re-read only every
    _ALIAS_RECHECK_S while /health keeps answering. Swapping models goes through a stop and
    a start, which /health sees, and load/unload in this server forget the memo."""
    global _ALIAS_MEMO
    opener = _local_opener()
    try:
        with opener.open("http://127.0.0.1:8081/health", timeout=1.2) as h_resp:
            if h_resp.status != 200:
                _ALIAS_MEMO = (0.0, "")
                return None
    except Exception:
        _ALIAS_MEMO = (0.0, "")
        return None
    memo_at, memo_alias = _ALIAS_MEMO
    if memo_alias and _mono() - memo_at < _ALIAS_RECHECK_S:
        alias = memo_alias
    else:
        alias = ""
        try:
            with opener.open("http://127.0.0.1:8081/v1/models", timeout=1.5) as m_resp:
                data = json.loads(m_resp.read().decode("utf-8", errors="replace")).get("data") or []
                if data and data[0].get("id"):
                    alias = str(data[0]["id"])
                    _ALIAS_MEMO = (_mono(), alias)
        except Exception:
            pass
    # An alias that is not in the runners file stays unknown ("" model_id): never a guess at
    # one of the known models, which would make a load of that model look already done.
    return {
        "model_id": runner_alias_to_id().get(alias, ""),
        "served_alias": alias,
        "mode_label": f"{alias or '未知模型'} · 256K (svc:qwen :8081)",
    }


# Live :8081 activity. Web UI / Magpie / MCP traffic reaches llama-server over Tailscale
# and never passes through this server, so the idle watchdog has to sample /slots itself
# (same rule as colab-gpu-llm-service's colab_keepalive.py).
LLM_8081_STATE: Dict[str, Any] = {
    "online": False,
    "alias": "",
    "model_id": "",
    "slots_total": 0,
    "slots_busy": 0,
    "n_ctx_per_slot": 0,
    "tokens_seen": 0,
    "last_active_ts": 0.0,
    "checked_ts": 0.0,
}
_LLM_8081_FINGERPRINT: Optional[Tuple[int, Tuple[str, ...]]] = None
_SLOT_TOKEN_KEYS = ("n_past", "n_decoded", "n_prompt_tokens")
# id_task: llama-server gives every completion a new task id (health, models and slots
# queries do not), so two identical short requests between samples still count.
_SLOT_TIME_KEYS = ("id_task", "last_request_time", "t_last_used", "last_used")


def _sample_llama_8081_slots() -> bool:
    """Sample :8081 /slots. True when it shows inference since the previous sample: a busy
    slot, a moved token counter, a changed last-request stamp, or the first answer after
    the server appears."""
    global _LLM_8081_FINGERPRINT
    now = time.time()
    LLM_8081_STATE["checked_ts"] = now
    try:
        with _local_opener().open("http://127.0.0.1:8081/slots", timeout=2.0) as resp:
            slots = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        # Keep the last fingerprint: a flapping :8081 must not count as activity on every
        # recovery and hold the VM past the idle limit.
        LLM_8081_STATE.update(online=False, slots_total=0, slots_busy=0)
        return False
    if isinstance(slots, dict):
        slots = slots.get("slots") or slots.get("data") or []
    if not isinstance(slots, list):
        slots = []
    busy, tokens, stamps, n_ctx = 0, 0, [], 0
    for slot in slots:
        if not isinstance(slot, dict):
            continue
        if slot.get("is_processing"):
            busy += 1
        for key in _SLOT_TOKEN_KEYS:
            val = slot.get(key)
            if isinstance(val, (int, float)):
                tokens += int(val)
                break
        for key in _SLOT_TIME_KEYS:
            val = slot.get(key)
            if val not in (None, ""):
                stamps.append(str(val))
        if isinstance(slot.get("n_ctx"), (int, float)):
            n_ctx = max(n_ctx, int(slot["n_ctx"]))
    fp = (tokens, tuple(stamps))
    active = busy > 0 or _LLM_8081_FINGERPRINT is None or fp != _LLM_8081_FINGERPRINT
    _LLM_8081_FINGERPRINT = fp
    if active:
        LLM_8081_STATE["last_active_ts"] = now
    LLM_8081_STATE.update(
        online=True,
        slots_total=len(slots),
        slots_busy=busy,
        n_ctx_per_slot=n_ctx,
        tokens_seen=tokens,
    )
    return active


LOADED_MODEL_IDS: List[str] = []  # at most one: whatever :8081 serves, as of the last probe
_PROBE_CELL = _TTLCell(2.0)


def _apply_kernel_8081_probe() -> Optional[Dict[str, Any]]:
    k8081 = _probe_kernel_llama_8081()
    if k8081 is None:
        LOADED_MODEL_IDS[:] = []
        LLM_8081_STATE.update(online=False, alias="", model_id="")
        return None
    LLM_8081_STATE.update(online=True, alias=k8081["served_alias"], model_id=k8081["model_id"])
    ident = k8081["model_id"] or k8081["served_alias"]  # a model outside the runners file is listed by alias
    LOADED_MODEL_IDS[:] = [ident] if ident else []  # one assignment: a status reader never sees it half-built
    return k8081


def _sync_kernel_8081_to_loaded_runtimes() -> Optional[Dict[str, Any]]:
    """Probe :8081 now (load and unload decide on this) and refresh what status shows."""
    global _ALIAS_MEMO
    _ALIAS_MEMO = (0.0, "")
    return _PROBE_CELL.get(_apply_kernel_8081_probe, force=True)


def _sync_kernel_8081_if_stale() -> Optional[Dict[str, Any]]:
    """What /api/status uses: the probe result at most ~2 s old, shared by concurrent polls."""
    return _PROBE_CELL.get(_apply_kernel_8081_probe)


def reset_status_caches() -> None:
    """Forget every cached status input (tests; also safe at runtime)."""
    global _ALIAS_MEMO
    _ALIAS_MEMO = (0.0, "")
    _PROBE_CELL.invalidate()
    _HARDWARE_CELL.invalidate()
    _RUNNER_ALIAS_MEMO.update(sig=None, map={})


def get_llm_8081_status() -> Dict[str, Any]:
    st = dict(LLM_8081_STATE)
    last = float(st.pop("last_active_ts") or 0.0)
    st["last_active_age_s"] = int(time.time() - last) if last > 0 else -1
    st.pop("checked_ts", None)
    st["mcp_bridge"] = get_mcp_bridge_status()
    return st


# ==============================================================================
# MCP bridge: the WebUI's MCP server entry is http://127.0.0.1:3426/mcp with useProxy, so
# llama-server connects to 127.0.0.1:3426 on this VM, which must be forwarded through
# tailscaled's SOCKS5 to local_novel_mcp.py on the user's PC. colab_kernel's boot script
# starts that forwarder as a thread, which only survives inside a long-lived Jupyter kernel;
# the runner here is a subprocess that exits. The forwarder therefore runs as its own
# detached process (MCP_BRIDGE_SCRIPT), so redeploying this server does not cut MCP either.
# ==============================================================================
MCP_BRIDGE_STATE: Dict[str, Any] = {
    "listening": False,
    "owner": "",  # "proc" = our forwarder process, "external" = e.g. a notebook kernel
    "reachable": None,  # end-to-end check through the bridge to the PC
    "checked_ts": 0.0,
    "error": "",
}
MCP_BRIDGE_PATH = "/content/colab_yoga_mcp_bridge.py"
MCP_BRIDGE_LOG = "/content/colab_yoga_mcp_bridge.log"
_MCP_TARGET_RE = re.compile(r"inet_aton\(\"([0-9.]+)\"\)\s*\+\s*struct\.pack\('>H',\s*(\d+)\)")
_MCP_SOCKS_RE = re.compile(r"--socks5-server=localhost:(\d+)")

MCP_BRIDGE_SCRIPT = r'''"""Colab-yoga MCP forwarder: 127.0.0.1:<port> -> tailscaled SOCKS5 -> <pc_ip>:<port>.
Usage: python colab_yoga_mcp_bridge.py PC_IP PORT SOCKS_PORT"""
import socket
import struct
import sys
import threading
import time

BUF = 65536


def recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise OSError("connection closed during SOCKS handshake")
        buf += chunk
    return buf


def tune(sock):
    """Small JSON-RPC messages must not wait on Nagle + delayed ACK; keepalive lets a vanished
    peer (the PC going offline) end the connection instead of holding its threads forever."""
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        for name, val in (("TCP_KEEPIDLE", 60), ("TCP_KEEPINTVL", 15), ("TCP_KEEPCNT", 4)):
            opt = getattr(socket, name, None)
            if opt is not None:
                sock.setsockopt(socket.IPPROTO_TCP, opt, val)
    except OSError:
        pass


def socks5_connect(ip, port, socks_port, timeout=10.0):
    up = socket.create_connection(("127.0.0.1", socks_port), timeout=timeout)
    try:
        up.sendall(bytes([5, 1, 0]))
        if recv_exact(up, 2) != bytes([5, 0]):
            raise OSError("SOCKS5 auth refused")
        up.sendall(bytes([5, 1, 0, 1]) + socket.inet_aton(ip) + struct.pack(">H", port))
        rep = recv_exact(up, 10)
        if rep[1] != 0:
            raise OSError("SOCKS5 connect refused (code %d)" % rep[1])
    except Exception:
        up.close()
        raise
    up.settimeout(None)
    tune(up)
    return up


def close_quietly(sock):
    # shutdown first: on Linux close() alone does not wake a thread blocked in recv on it.
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except Exception:
        pass
    try:
        sock.close()
    except Exception:
        pass


class Link:
    """Two sockets and their two directions. A clean end of one direction is passed on as a
    half-close so the other side can still finish its answer; the sockets are closed when
    both directions are done, or at once when either fails."""

    def __init__(self, cli, up):
        self.cli, self.up = cli, up
        self.open_directions = 2
        self.lock = threading.Lock()

    def pipe(self, a, b):
        buf = bytearray(BUF)
        view = memoryview(buf)
        try:
            while True:
                n = a.recv_into(buf)
                if not n:
                    break
                b.sendall(view[:n])
        except Exception:
            close_quietly(self.cli)
            close_quietly(self.up)
            return
        try:
            b.shutdown(socket.SHUT_WR)
        except Exception:
            pass
        with self.lock:
            self.open_directions -= 1
            finished = self.open_directions == 0
        if finished:
            close_quietly(self.cli)
            close_quietly(self.up)


def pipe(a, b):
    """One direction of an unpaired pair (kept for callers that wire sockets themselves)."""
    Link(a, b).pipe(a, b)


def handle(cli, ip, port, socks_port):
    try:
        up = socks5_connect(ip, port, socks_port)
    except Exception as e:
        print("connect failed:", e, flush=True)
        close_quietly(cli)
        return
    tune(cli)
    link = Link(cli, up)
    # This thread carries one direction itself: two threads per connection, not three.
    threading.Thread(target=link.pipe, args=(up, cli), name="mcp-pipe", daemon=True).start()
    link.pipe(cli, up)


def serve(srv, ip, port, socks_port):
    while True:
        try:
            cli, _ = srv.accept()
        except OSError:
            if srv.fileno() == -1:
                return  # listener closed
            time.sleep(0.1)  # e.g. out of file descriptors: keep serving once there is room
            continue
        threading.Thread(target=handle, args=(cli, ip, port, socks_port), name="mcp-conn", daemon=True).start()


if __name__ == "__main__":
    ip, port, socks_port = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(128)
    print("listening on 127.0.0.1:%d" % port, flush=True)
    serve(srv, ip, port, socks_port)
'''


_MCP_TARGET_MEMO: Dict[str, Any] = {"sig": None, "target": None}


def _kernel_mcp_target() -> Optional[Tuple[str, int, int]]:
    """(pc_tailscale_ip, mcp_port, socks_port) as written into the colab_kernel boot scripts.

    The runners file holds every model's boot script, and the watchdog asks every tick, so
    the parsed answer is kept until the file's mtime or size changes."""
    try:
        st = os.stat(RUNNERS_FILE)
    except OSError:
        return None
    sig = (RUNNERS_FILE, st.st_mtime_ns, st.st_size)
    if _MCP_TARGET_MEMO["sig"] == sig:
        return _MCP_TARGET_MEMO["target"]
    target = _parse_mcp_target()
    _MCP_TARGET_MEMO.update(sig=sig, target=target)
    return target


def _parse_mcp_target() -> Optional[Tuple[str, int, int]]:
    try:
        with open(RUNNERS_FILE, "r", encoding="utf-8") as rf:
            runners = json.load(rf)
    except Exception:
        return None
    for info in runners.values():
        code = str((info or {}).get("code") or "")
        m = _MCP_TARGET_RE.search(code)
        if m:
            s = _MCP_SOCKS_RE.search(code)
            return m.group(1), int(m.group(2)), int(s.group(1)) if s else 1055
    return None


def _port_listening(port: int) -> bool:
    try:
        socket.create_connection(("127.0.0.1", port), timeout=2).close()
        return True
    except OSError:
        return False


def _ensure_mcp_bridge() -> None:
    """Idempotent: make sure something listens on 127.0.0.1:<mcp_port> on this VM; if not,
    start the forwarder as a detached process that outlives this server."""
    target = _kernel_mcp_target()
    if target is None:
        return
    ip, port, socks_port = target
    if _port_listening(port):
        if not MCP_BRIDGE_STATE["listening"]:
            MCP_BRIDGE_STATE.update(listening=True, owner=MCP_BRIDGE_STATE["owner"] or "external")
        return
    try:
        with open(MCP_BRIDGE_PATH, "w", encoding="utf-8", newline="\n") as f:
            f.write(MCP_BRIDGE_SCRIPT)
        with open(MCP_BRIDGE_LOG, "a", newline="\n") as log:  # the child keeps its own copy of the handle
            subprocess.Popen(
                [sys.executable, "-u", MCP_BRIDGE_PATH, ip, str(port), str(socks_port)],
                stdout=log,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
    except Exception as e:
        MCP_BRIDGE_STATE.update(listening=False, error=str(e)[:160])
        return
    for _ in range(20):
        if _port_listening(port):
            break
        time.sleep(0.25)
    ok = _port_listening(port)
    MCP_BRIDGE_STATE.update(listening=ok, owner="proc" if ok else "", error="" if ok else "forwarder did not start")
    if ok:
        print(f"[MCP 桥] 已在 127.0.0.1:{port} 常驻（独立进程），经 Tailscale 转发到本机小说目录服务", flush=True)
        _mcp_bridge_selftest()


def _mcp_bridge_selftest() -> None:
    """End-to-end: through the bridge to the PC's MCP server (it answers GET /mcp with 4xx)."""
    target = _kernel_mcp_target()
    MCP_BRIDGE_STATE["checked_ts"] = time.time()
    if target is not None:
        MCP_BRIDGE_STATE["listening"] = _port_listening(target[1])
    if target is None or not MCP_BRIDGE_STATE["listening"]:
        MCP_BRIDGE_STATE["reachable"] = None
        return
    try:
        s = socket.create_connection(("127.0.0.1", target[1]), timeout=8)
        try:
            s.settimeout(8)
            s.sendall(b"GET /mcp HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
            head = s.recv(64)
        finally:
            s.close()
        ok = head.startswith(b"HTTP/")
        MCP_BRIDGE_STATE.update(reachable=ok, error="" if ok else "no HTTP reply from the PC")
    except Exception as e:
        MCP_BRIDGE_STATE.update(reachable=False, error=str(e)[:160] or type(e).__name__)


def get_mcp_bridge_status() -> Dict[str, Any]:
    st = {k: MCP_BRIDGE_STATE[k] for k in ("listening", "owner", "reachable", "error")}
    checked = float(MCP_BRIDGE_STATE["checked_ts"] or 0.0)
    st["checked_age_s"] = int(time.time() - checked) if checked > 0 else -1
    return st


# ==============================================================================
# Model boot: the colab_kernel runner script (download, template patch, llama-server :8081)
# ==============================================================================
TASK_STATE: Dict[str, Any] = {
    "is_downloading": False,
    "is_loading_vram": False,
    "progress": 0.0,
    "message": "等待检查模型状态...",
}
_LOAD_LOCK = threading.Lock()  # held by the worker thread for the whole boot
_RUNNER: Dict[str, Any] = {"proc": None, "cancelled": False}
_PROGRESS_RE = re.compile(r"并发下载进度:\s*(\d+)%")


def _clamp_context_length(val: Optional[int]) -> int:
    try:
        v = int(val) if val is not None else CONTEXT_LENGTH
    except Exception:
        v = CONTEXT_LENGTH
    if v <= 0:
        v = CONTEXT_LENGTH
    return max(4096, min(262144, v))


def _kill_llama_8081() -> None:
    subprocess.run(["pkill", "-f", "llama-server.*8081"], check=False)


def _run_kernel_llm_boot(target_id: str, hf_token: str = "") -> Tuple[bool, str]:
    """Run the one-click boot script exported by colab_kernel.py: parallel download, chat
    template patch, llama-server :8081, Tailscale svc:qwen and the novel-folder bridge."""
    if not os.path.exists(RUNNERS_FILE):
        return False, f"未找到 {RUNNERS_FILE}，请重新唤醒实例以同步脚本。"
    with open(RUNNERS_FILE, "r", encoding="utf-8") as rf:
        runners = json.load(rf)
    if target_id not in runners:
        return False, f"colab_kernel_runners.json 中未包含 {target_id}"

    info = runners[target_id]
    name = model_display_name(target_id)
    k8081 = _probe_kernel_llama_8081()
    if k8081 and k8081["model_id"] == target_id:
        _sync_kernel_8081_to_loaded_runtimes()
        msg = f"{name} 已在 :8081 运行并就绪（{k8081['mode_label']}）！"
        _ensure_mcp_bridge()
        TASK_STATE.update(progress=1.0, message=msg)
        return True, msg

    TASK_STATE.update(progress=0.08, message=f"正在通过 colab_kernel 32 路通道下载并装载 {name}...")
    try:
        # Switching: stop the other model's server and delete the other models' shards (the
        # disk holds one model at a time) but keep llama-dist, the llama.cpp binaries.
        if k8081:
            _kill_llama_8081()
            time.sleep(2)
        target_root = info["root_dir"]
        os.makedirs(target_root, exist_ok=True)
        for other_slug, other_info in runners.items():
            if other_slug == target_id:
                continue
            for base in ("/dev/shm", "/var/colab"):
                shutil.rmtree(os.path.join(base, other_slug), ignore_errors=True)
            old_root = other_info["root_dir"]
            old_dist = os.path.join(old_root, "llama-dist")
            new_dist = os.path.join(target_root, "llama-dist")
            if os.path.isdir(old_dist) and not os.path.exists(new_dist):
                shutil.move(old_dist, new_dist)
            shutil.rmtree(old_root, ignore_errors=True)

        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        if hf_token and hf_token.strip():
            env["HF_TOKEN"] = hf_token.strip()
        proc = subprocess.Popen(
            [sys.executable, "-u", "-c", info["code"]],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            start_new_session=True,  # own process group, so unload can stop its download pool too
        )
        _RUNNER["proc"] = proc
        assert proc.stdout is not None
        for raw_line in proc.stdout:
            ln = raw_line.rstrip("\r\n")
            if not ln:
                continue
            print(ln, flush=True)
            TASK_STATE["message"] = ln
            m_pct = _PROGRESS_RE.search(ln)
            if m_pct:
                TASK_STATE["progress"] = round(0.10 + 0.65 * (int(m_pct.group(1)) / 100.0), 2)
            elif "加载 " in ln and " 到 GPU" in ln:
                TASK_STATE["progress"] = 0.82
            elif "总启动耗时" in ln:
                TASK_STATE["progress"] = 0.98
        rc = proc.wait()
        proc.stdout.close()
        if _RUNNER["cancelled"]:
            return False, f"{name} 装载已取消。"
        if rc != 0:
            return False, f"{name} 启动失败 (exit={rc})，请查看实时控制台日志。"
        k_now = _sync_kernel_8081_to_loaded_runtimes()
        if not k_now:
            return False, f"{name} 脚本已退出但 :8081 未就绪，请检查日志。"
        # The boot script's own MCP forwarder thread died with it; host it here.
        _ensure_mcp_bridge()
        msg = f"{name} 已装载至显存并开启 Tailscale svc:qwen ({k_now['mode_label']})！"
        TASK_STATE.update(progress=1.0, message=msg)
        return True, msg
    except Exception as e:
        err = f"{name} 启动异常: {e}"
        TASK_STATE["message"] = err
        return False, err
    finally:
        _RUNNER["proc"] = None
        touch_activity()


def begin_model_load(target_id: str, hf_token: str) -> bool:
    """Start loading in a thread; False when a load is already running."""
    if not _LOAD_LOCK.acquire(blocking=False):
        return False
    _RUNNER["cancelled"] = False
    TASK_STATE.update(is_downloading=True, is_loading_vram=True, progress=0.0, message="准备装载模型...")

    def worker():
        try:
            ok, msg = _run_kernel_llm_boot(target_id, hf_token)
            TASK_STATE["message"] = msg
            print(("✅ " if ok else "❌ ") + msg, flush=True)
        finally:
            TASK_STATE.update(is_downloading=False, is_loading_vram=False)
            _LOAD_LOCK.release()

    threading.Thread(target=worker, daemon=True).start()
    return True


def unload_model(model_id: str = "") -> str:
    """Stop :8081 and a boot still running. A model_id other than the served one is a no-op."""
    touch_activity()
    proc = _RUNNER["proc"]
    served = _sync_kernel_8081_to_loaded_runtimes()
    if model_id and model_id != "all" and proc is None and (served is None or served["model_id"] != model_id):
        msg = "当前指定模型未驻留显存，无需卸载。"
    elif proc is None and served is None:
        msg = "当前没有运行中的模型，无需卸载。"
    else:
        if proc is not None:
            _RUNNER["cancelled"] = True
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except Exception:
                proc.terminate()
        _kill_llama_8081()
        for _ in range(20):
            if _sync_kernel_8081_to_loaded_runtimes() is None:
                break
            time.sleep(0.5)
        msg = "已停止 :8081 上的 llama-server，显存已释放。"
    TASK_STATE["message"] = msg
    print(f"[显存管理] {msg}", flush=True)
    return msg


# ==============================================================================
# Idle watchdog: release the runtime when nothing has used it for the set time
# ==============================================================================
WATCHDOG_TICK_S = 5  # how soon a chat resets the idle countdown; /slots is a local call
MCP_BRIDGE_CHECK_TICKS = 3  # 15 s
MCP_BRIDGE_SELFTEST_TICKS = 12  # 60 s


def _idle_watchdog_loop() -> None:
    tick = 0
    while True:
        time.sleep(WATCHDOG_TICK_S)
        tick += 1
        # Download/load and any inference on :8081 (web, Magpie, MCP chats go straight to
        # llama-server) count as use and keep the Jupyter heartbeat going.
        llm_active = _sample_llama_8081_slots()
        if LLM_8081_STATE["online"] and tick % MCP_BRIDGE_CHECK_TICKS == 0:
            try:
                _ensure_mcp_bridge()
                if tick % MCP_BRIDGE_SELFTEST_TICKS == 0:
                    _mcp_bridge_selftest()
            except Exception as e:
                MCP_BRIDGE_STATE["error"] = str(e)[:160]
        if llm_active or TASK_STATE["is_downloading"] or TASK_STATE["is_loading_vram"]:
            touch_activity()
            continue
        if IDLE_TIMEOUT_MINUTES <= 0:
            _ping_local_colab_jupyter(force=False)
            continue
        if time.time() - LAST_ACTIVE_TIME >= IDLE_TIMEOUT_MINUTES * 60:
            print(f"🛑 [自动省额度看门狗] 已连续闲置 {IDLE_TIMEOUT_MINUTES} 分钟，正在自动释放 Colab 虚拟机...", flush=True)
            ok, msg = disconnect_colab_runtime()
            print(msg, flush=True)
            if ok:
                break
            time.sleep(45)  # failed: retry, but not every tick
        else:
            _ping_local_colab_jupyter(force=False)


# ==============================================================================
# HTTP API (the contract is in skills/colab-yoga/references/api.md)
# ==============================================================================
class LoadModelRequest(BaseModel):
    model_id: str = ""
    context_length: Optional[int] = None
    hf_token: str = ""


class UnloadModelRequest(BaseModel):
    model_id: str = ""


class IdleConfigRequest(BaseModel):
    idle_timeout_minutes: int = 30


def get_status() -> Dict[str, Any]:
    _sync_kernel_8081_if_stale()
    hw = get_hardware_snapshot()
    uptime = _runtime_uptime_seconds()
    return {
        "server_version": SERVER_VERSION,
        "gpu_name": hw["gpu_name"],
        "vram_used_gb": hw["vram_used_gb"],
        "vram_total_gb": hw["vram_total_gb"],
        "ram_used_gb": hw["ram_used_gb"],
        "ram_total_gb": hw["ram_total_gb"],
        "disk_used_gb": hw["disk_used_gb"],
        "disk_total_gb": hw["disk_total_gb"],
        "uptime_seconds": uptime,
        "session_cu_used": round(uptime / 3600.0 * hw["cu_burn_rate_per_hour"], 3),
        "cu_burn_rate_per_hour": hw["cu_burn_rate_per_hour"],
        "idle_timeout_minutes": IDLE_TIMEOUT_MINUTES,
        "idle_remaining_seconds": idle_remaining_seconds(),
        "loaded_model_ids": list(LOADED_MODEL_IDS),
        "is_downloading": TASK_STATE["is_downloading"],
        "is_loading_vram": TASK_STATE["is_loading_vram"],
        "task_progress": TASK_STATE["progress"],
        "task_message": TASK_STATE["message"],
        "console_lines": get_console_lines(),
        "context_length": CONTEXT_LENGTH,
        "llm_server": get_llm_8081_status(),
    }


def create_app() -> FastAPI:
    app = FastAPI(title="Colab-yoga server", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def require_token(request, call_next):
        # The server is reachable from the internet through its gradio.live tunnel, whose
        # forwarder connects from 127.0.0.1, so a loopback source says nothing about the
        # caller: there is no exemption by address. The keeper only reads /api/status.
        if not (request.method == "GET" and request.url.path == "/api/status"):
            if not _token_ok(request.headers.get("authorization", "")):
                return JSONResponse({"detail": "unauthorized"}, status_code=401,
                                    headers={"WWW-Authenticate": "Bearer"})
        return await call_next(request)

    @app.get("/api/status")
    def api_status():
        # A ready dict straight into JSONResponse skips FastAPI's per-field re-encoding.
        return JSONResponse(get_status())

    @app.post("/api/load_model")
    def api_load_model(req: LoadModelRequest):
        global CONTEXT_LENGTH
        touch_activity()
        if req.context_length is not None:
            CONTEXT_LENGTH = _clamp_context_length(req.context_length)
        # A load already in progress keeps running; its progress shows in the status.
        if not req.model_id.strip():
            return JSONResponse({"ok": False, "message": "model_id is required"}, status_code=400)
        begin_model_load(req.model_id.strip(), req.hf_token)
        return get_status()

    @app.post("/api/unload_model")
    def api_unload_model(req: UnloadModelRequest):
        unload_model(req.model_id)
        return get_status()

    @app.post("/api/touch")
    def api_touch():
        """The console's "keep it running" button: resets the idle countdown."""
        touch_activity()
        return get_status()

    @app.post("/api/config_idle")
    def api_config_idle(req: IdleConfigRequest):
        global IDLE_TIMEOUT_MINUTES
        IDLE_TIMEOUT_MINUTES = max(0, req.idle_timeout_minutes)
        touch_activity()
        return get_status()

    @app.post("/api/disconnect")
    def api_disconnect():
        ok, msg = disconnect_colab_runtime()
        return {"ok": ok, "message": msg}

    return app


# ==============================================================================
# Public tunnel + entry point
# ==============================================================================
def _publish_tunnel() -> None:
    """Open a public HTTPS URL for 127.0.0.1:7860 and log it as COLAB_YOGA_URL=<url>.

    Uses gradio's share tunnel (frp client, https://*.gradio.live) through
    gradio.networking.setup_tunnel, without building any gradio UI. The previous app.py
    already used gradio.live from Colab, so reachability is proven; gradio is a single pip
    package that fetches the small frpc binary itself, and the link needs no account.
    cloudflared quick tunnels would add a binary download from GitHub and promise no
    uptime. gradio writes its TLS certificate to ./.gradio, so the cwd must be writable
    (vm_boot.py starts the server in /content). Retries until it works: the console
    cannot reach this server without it."""
    from gradio import networking

    wanted = {
        "local_host": "127.0.0.1",
        "local_port": SERVER_PORT,
        "share_token": secrets.token_urlsafe(32),
        "share_server_address": None,
        "share_server_tls_certificate": None,
    }
    accepted = inspect.signature(networking.setup_tunnel).parameters  # differs across gradio versions
    kwargs = {k: v for k, v in wanted.items() if k in accepted}
    attempt = 0
    while True:
        attempt += 1
        try:
            url = str(networking.setup_tunnel(**kwargs)).strip()
            break
        except Exception as e:
            print(f"COLAB_YOGA_TUNNEL_ERROR 第 {attempt} 次: {e}", flush=True)
            time.sleep(15)
    url = "https://" + re.sub(r"^https?://", "", url)
    print(f"{URL_MARKER}{url}", flush=True)


def main() -> None:
    install_console_mirror()
    load_api_token()  # before the port opens: no request is ever served without one
    print(f"Colab-yoga server {SERVER_VERSION} 启动中 (port {SERVER_PORT})，控制接口需要 Bearer 令牌", flush=True)
    # llama-server and the MCP forwarder are deliberately left to run on their own.
    threading.Thread(target=_idle_watchdog_loop, daemon=True).start()
    threading.Thread(target=_publish_tunnel, daemon=True).start()
    import uvicorn
    uvicorn.run(create_app(), host="0.0.0.0", port=SERVER_PORT, log_level="warning")


if __name__ == "__main__":
    main()
