"""Offline tests for colab_yoga_server.py (run: python -m unittest server/test_server.py)."""
import contextlib
import http.server
import importlib.util
import io
import json
import os
import re
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from unittest import mock

from fastapi.testclient import TestClient

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("colab_yoga_server", os.path.join(HERE, "colab_yoga_server.py"))
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)

CONTRACT_STATUS_FIELDS = {
    "server_version", "gpu_name", "vram_used_gb", "vram_total_gb", "ram_used_gb", "ram_total_gb",
    "disk_used_gb", "disk_total_gb", "uptime_seconds", "session_cu_used", "cu_burn_rate_per_hour",
    "idle_timeout_minutes", "idle_remaining_seconds", "loaded_model_ids", "is_downloading",
    "is_loading_vram", "task_progress", "task_message", "console_lines", "context_length", "llm_server",
}
CONTRACT_LLM_FIELDS = {
    "online", "alias", "model_id", "slots_total", "slots_busy", "n_ctx_per_slot", "tokens_seen",
    "last_active_age_s", "mcp_bridge",
}
CONTRACT_MCP_FIELDS = {"listening", "owner", "reachable", "error", "checked_age_s"}

HUIHUI_ALIAS = "Huihui-Qwen3.8-Flash-Next-abliterated-v2-UD-Q4_K_XL"
TEST_TOKEN = "test-token-0123456789"
# the models of a typical runners file: slug -> the alias llama-server reports
SHIPPED_ALIASES = {
    "qwen38-huihui-v2": HUIHUI_ALIAS,
    "gemma4-31b": "Gemma-4-31B-it-ARA-Heretic-Q8_0",
    "qwen38-27b-rvn": "Qwen3.8-27B-RVN-Heretic-BF16",
    "glm53-flash": "GLM-5.3-Flash-Uncensored-GSQ-RCO-Q4",
}
ALIAS_TO_SLUG = {alias: slug for slug, alias in SHIPPED_ALIASES.items()}
HARDWARE = {
    "gpu_name": "NVIDIA RTX PRO 6000 Blackwell", "vram_used_gb": 1.5, "vram_total_gb": 95.6,
    "cu_burn_rate_per_hour": 10.05, "ram_used_gb": 10.0, "ram_total_gb": 176.0,
    "disk_used_gb": 20.0, "disk_total_gb": 200.0,
}


class FakeResponse(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    """Stands in for the :8081 opener: maps URL suffix -> JSON body, or an exception."""

    def __init__(self, routes):
        self.routes = routes

    def open(self, url, timeout=None):
        for suffix, body in self.routes.items():
            if url.endswith(suffix):
                if isinstance(body, Exception):
                    raise body
                return FakeResponse(json.dumps(body).encode())
        raise OSError("no route " + url)


def reset_slot_state():
    srv._LLM_8081_FINGERPRINT = None
    srv.LLM_8081_STATE.update(online=False, slots_total=0, slots_busy=0, n_ctx_per_slot=0,
                              tokens_seen=0, last_active_ts=0.0, checked_ts=0.0)


class SlotFingerprintTest(unittest.TestCase):
    def setUp(self):
        reset_slot_state()

    def sample(self, slots):
        with mock.patch.object(srv, "_local_opener", return_value=FakeOpener({"/slots": slots})):
            return srv._sample_llama_8081_slots()

    def idle_slot(self, **extra):
        return {"id": 0, "n_ctx": 262144, "is_processing": False, "n_past": 100, **extra}

    def test_first_answer_counts_then_unchanged_does_not(self):
        self.assertTrue(self.sample([self.idle_slot()]))
        self.assertFalse(self.sample([self.idle_slot()]))
        self.assertEqual(srv.LLM_8081_STATE["n_ctx_per_slot"], 262144)
        self.assertEqual(srv.LLM_8081_STATE["tokens_seen"], 100)

    def test_busy_slot_counts(self):
        self.sample([self.idle_slot()])
        self.assertTrue(self.sample([self.idle_slot(is_processing=True)]))
        self.assertEqual(srv.LLM_8081_STATE["slots_busy"], 1)

    def test_moved_token_counter_counts(self):
        self.sample([self.idle_slot()])
        self.assertTrue(self.sample([self.idle_slot(n_past=250)]))

    def test_n_decoded_used_when_n_past_missing(self):
        slot = {"id": 0, "is_processing": False, "n_decoded": 5}
        self.sample([slot])
        self.assertTrue(self.sample([{**slot, "n_decoded": 9}]))

    def test_id_task_change_counts_for_identical_short_requests(self):
        self.sample([self.idle_slot(id_task=7)])
        self.assertFalse(self.sample([self.idle_slot(id_task=7)]))
        self.assertTrue(self.sample([self.idle_slot(id_task=8)]))

    def test_slots_as_dict_wrapper(self):
        self.assertTrue(self.sample({"slots": [self.idle_slot()]}))
        self.assertEqual(srv.LLM_8081_STATE["slots_total"], 1)

    def test_flapping_server_does_not_count_as_activity_on_recovery(self):
        self.assertTrue(self.sample([self.idle_slot(id_task=3)]))
        with mock.patch.object(srv, "_local_opener", return_value=FakeOpener({"/slots": OSError("down")})):
            self.assertFalse(srv._sample_llama_8081_slots())
        self.assertFalse(srv.LLM_8081_STATE["online"])
        self.assertFalse(self.sample([self.idle_slot(id_task=3)]))
        self.assertTrue(srv.LLM_8081_STATE["online"])


class UptimeTest(unittest.TestCase):
    def setUp(self):
        self._saved = srv._RUNTIME_ASSIGNED_TS
        srv._RUNTIME_ASSIGNED_TS = None
        self.tmp = tempfile.TemporaryDirectory()
        self.log = os.path.join(self.tmp.name, "app.log")

    def tearDown(self):
        srv._RUNTIME_ASSIGNED_TS = self._saved
        self.tmp.cleanup()

    def write_log(self, lines):
        with open(self.log, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines) + "\n")

    def test_first_kernel_started_line_wins(self):
        self.write_log([
            json.dumps({"time": "2025-01-01T00:00:00Z", "msg": "Jupyter booting"}),
            json.dumps({"time": "2025-01-01T10:00:00.123456789Z", "msg": "Kernel started: abc"}),
            json.dumps({"time": "2025-01-01T11:00:00Z", "msg": "Kernel started: def"}),
        ])
        expected = datetime(2025, 1, 1, 10, 0, 0, 123456, tzinfo=timezone.utc).timestamp()
        self.assertAlmostEqual(srv._runtime_assigned_ts(self.log), expected, places=3)

    def test_uptime_is_seconds_since_assignment_not_server_start(self):
        assigned = time.time() - 3600
        stamp = datetime.fromtimestamp(assigned, timezone.utc).isoformat()
        self.write_log([json.dumps({"time": stamp, "msg": "Kernel started"})])
        with mock.patch.object(srv, "BOOT_TIME", time.time() - 60):
            up = srv._runtime_uptime_seconds(self.log)
        self.assertAlmostEqual(up, 3600, delta=3)

    def test_missing_log_falls_back_to_server_boot(self):
        with mock.patch.object(srv, "BOOT_TIME", time.time() - 120):
            up = srv._runtime_uptime_seconds(os.path.join(self.tmp.name, "absent.log"))
        self.assertAlmostEqual(up, 120, delta=3)

    def test_log_without_kernel_started_falls_back(self):
        self.write_log([json.dumps({"time": "2025-01-01T00:00:00Z", "msg": "other"}), "not json"])
        self.assertIsNone(srv._runtime_assigned_ts(self.log))


class CuBurnTest(unittest.TestCase):
    def test_table(self):
        for name, rate in [("NVIDIA H100 80GB", 11.78), ("NVIDIA RTX PRO 6000 Blackwell", 10.05),
                           ("A100-SXM4-40GB", 5.37), ("NVIDIA L4", 1.71), ("Tesla T4", 1.17),
                           ("Tesla V100", 3.80), ("unknown", 1.50)]:
            self.assertEqual(srv.estimate_cu_burn_rate(name), rate, name)


class McpTargetTest(unittest.TestCase):
    CODE = (
        "x = 1\n"
        "sock.sendall(bytes([5, 1, 0, 1]) + socket.inet_aton(\"100.64.0.1\") + struct.pack('>H', 3426))\n"
        "cmd = ['tailscaled', '--socks5-server=localhost:1056', '--state=x']\n"
    )

    def target_from(self, runners):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "runners.json")
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                json.dump(runners, f)
            with mock.patch.object(srv, "RUNNERS_FILE", path):
                return srv._kernel_mcp_target()

    def test_ip_port_and_socks_port(self):
        runners = {"a": {"code": "print(1)"}, "b": {"code": self.CODE}}
        self.assertEqual(self.target_from(runners), ("100.64.0.1", 3426, 1056))

    def test_socks_port_defaults_to_1055(self):
        code = "socket.inet_aton(\"10.0.0.2\") + struct.pack('>H', 3500)"
        self.assertEqual(self.target_from({"a": {"code": code}}), ("10.0.0.2", 3500, 1055))

    def test_no_target_or_no_file(self):
        self.assertIsNone(self.target_from({"a": {"code": "print(1)"}}))
        with mock.patch.object(srv, "RUNNERS_FILE", os.path.join(tempfile.gettempdir(), "no-such-runners.json")):
            self.assertIsNone(srv._kernel_mcp_target())


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeSocks5Server:
    """Accepts one SOCKS5 CONNECT, records the target, then echoes bytes (or refuses)."""

    def __init__(self, reply_code=0):
        self.reply_code = reply_code
        self.target = None
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(4)
        self.port = self.listener.getsockname()[1]
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        while True:
            try:
                conn, _ = self.listener.accept()
            except OSError:
                return
            threading.Thread(target=self.serve, args=(conn,), daemon=True).start()

    def serve(self, conn):
        with conn:
            assert conn.recv(3) == bytes([5, 1, 0])
            conn.sendall(bytes([5, 0]))
            head = self.recv_exact(conn, 4)
            ip = socket.inet_ntoa(self.recv_exact(conn, 4))
            port = struct.unpack(">H", self.recv_exact(conn, 2))[0]
            self.target = (head, ip, port)
            conn.sendall(bytes([5, self.reply_code, 0, 1]) + bytes(6))
            if self.reply_code:
                return
            with contextlib.suppress(OSError):  # the client hangs up when the test ends
                while True:
                    data = conn.recv(4096)
                    if not data:
                        return
                    conn.sendall(data)

    @staticmethod
    def recv_exact(conn, n):
        buf = b""
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                raise OSError("closed")
            buf += chunk
        return buf

    def close(self):
        self.listener.close()


class McpForwarderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        path = os.path.join(cls.tmp.name, "forwarder.py")
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(srv.MCP_BRIDGE_SCRIPT)
        fspec = importlib.util.spec_from_file_location("forwarder", path)
        cls.fwd = importlib.util.module_from_spec(fspec)
        fspec.loader.exec_module(cls.fwd)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_socks5_handshake_sends_connect_to_pc_ip_and_port(self):
        fake = FakeSocks5Server()
        self.addCleanup(fake.close)
        up = self.fwd.socks5_connect("100.64.0.1", 3426, fake.port, timeout=3)
        self.addCleanup(up.close)
        up.sendall(b"ping")
        self.assertEqual(up.recv(4), b"ping")
        self.assertEqual(fake.target, (bytes([5, 1, 0, 1]), "100.64.0.1", 3426))

    def test_socks5_refusal_raises(self):
        fake = FakeSocks5Server(reply_code=5)
        self.addCleanup(fake.close)
        with self.assertRaises(OSError) as ctx:
            self.fwd.socks5_connect("100.64.0.1", 3426, fake.port, timeout=3)
        self.assertIn("code 5", str(ctx.exception))

    def test_forwarder_pipes_a_client_through_socks(self):
        fake = FakeSocks5Server()
        self.addCleanup(fake.close)
        listen = socket.socket()
        listen.bind(("127.0.0.1", 0))
        listen.listen(1)
        self.addCleanup(listen.close)
        def serve_until_closed():
            with contextlib.suppress(OSError):  # the listener is closed at cleanup
                self.fwd.serve(listen, "100.1.2.3", 3426, fake.port)

        threading.Thread(target=serve_until_closed, daemon=True).start()
        with socket.create_connection(listen.getsockname(), timeout=3) as cli:
            cli.sendall(b"hello")
            self.assertEqual(cli.recv(5), b"hello")
        self.assertEqual(fake.target[1:], ("100.1.2.3", 3426))


class BoundStatusTestCase(unittest.TestCase):
    """Starts each test from idle defaults with telemetry, probes and side effects stubbed."""

    served = None  # alias :8081 reports in the probe stub, or None for offline

    def setUp(self):
        reset_slot_state()
        srv.reset_status_caches()
        runners_dir = tempfile.TemporaryDirectory()
        self.addCleanup(runners_dir.cleanup)
        self.models_path = os.path.join(runners_dir.name, "colab_kernel_runners.json")
        self.write_models(SHIPPED_ALIASES)
        p = mock.patch.object(srv, "RUNNERS_FILE", self.models_path)
        p.start()
        self.addCleanup(p.stop)
        srv.LOADED_MODEL_IDS.clear()
        srv.TASK_STATE.update(is_downloading=False, is_loading_vram=False, progress=0.0, message="")
        srv.IDLE_TIMEOUT_MINUTES = 30
        srv.LAST_ACTIVE_TIME = time.time()
        srv.CONTEXT_LENGTH = 262144
        self.probe_alias = self.served
        patches = [
            mock.patch.object(srv, "get_hardware_snapshot", return_value=dict(HARDWARE)),
            mock.patch.object(srv, "_runtime_uptime_seconds", return_value=1800),
            mock.patch.object(srv, "_ping_local_colab_jupyter"),
            mock.patch.object(srv, "_kernel_mcp_target", return_value=None),
            mock.patch.object(srv, "_probe_kernel_llama_8081", side_effect=self.fake_probe),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        srv.API_TOKEN = TEST_TOKEN
        self.addCleanup(setattr, srv, "API_TOKEN", "")
        self.client = TestClient(srv.create_app(), headers={"Authorization": "Bearer " + TEST_TOKEN})

    def write_models(self, aliases):
        """The runners file with one runner per slug -> alias (no scripts worth running)."""
        runners = {slug: {"slug": slug, "alias": alias, "root_dir": "/tmp/" + slug, "code": "pass"}
                   for slug, alias in aliases.items()}
        with open(self.models_path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(runners, f)
        srv.reset_status_caches()

    def fake_probe(self):
        if self.probe_alias is None:
            return None
        return {"model_id": ALIAS_TO_SLUG.get(self.probe_alias, ""), "served_alias": self.probe_alias,
                "mode_label": self.probe_alias + " · 256K"}


class HttpApiTest(BoundStatusTestCase):
    def test_status_fields_equal_contract(self):
        body = self.client.get("/api/status").json()
        self.assertEqual(set(body), CONTRACT_STATUS_FIELDS)
        self.assertEqual(set(body["llm_server"]), CONTRACT_LLM_FIELDS)
        self.assertEqual(set(body["llm_server"]["mcp_bridge"]), CONTRACT_MCP_FIELDS)
        self.assertEqual(body["server_version"], srv.SERVER_VERSION)
        self.assertEqual(body["gpu_name"], HARDWARE["gpu_name"])
        self.assertAlmostEqual(body["session_cu_used"], round(1800 / 3600 * 10.05, 3))
        self.assertEqual(body["loaded_model_ids"], [])
        self.assertFalse(body["llm_server"]["online"])
        self.assertEqual(body["llm_server"]["last_active_age_s"], -1)

    def test_status_reports_served_model(self):
        self.probe_alias = HUIHUI_ALIAS
        body = self.client.get("/api/status").json()
        self.assertEqual(body["loaded_model_ids"], ["qwen38-huihui-v2"])
        self.assertTrue(body["llm_server"]["online"])
        self.assertEqual(body["llm_server"]["alias"], HUIHUI_ALIAS)
        self.assertEqual(body["llm_server"]["model_id"], "qwen38-huihui-v2")

    def test_touch_resets_idle_countdown(self):
        srv.LAST_ACTIVE_TIME = time.time() - 25 * 60
        before = self.client.get("/api/status").json()["idle_remaining_seconds"]
        self.assertLess(before, 6 * 60)
        after = self.client.post("/api/touch", json={}).json()["idle_remaining_seconds"]
        self.assertGreater(after, 29 * 60)

    def test_config_idle_sets_minutes_and_zero_turns_off(self):
        body = self.client.post("/api/config_idle", json={"idle_timeout_minutes": 10}).json()
        self.assertEqual(body["idle_timeout_minutes"], 10)
        self.assertLessEqual(body["idle_remaining_seconds"], 600)
        self.assertGreater(body["idle_remaining_seconds"], 590)
        body = self.client.post("/api/config_idle", json={"idle_timeout_minutes": 0}).json()
        self.assertEqual(body["idle_timeout_minutes"], 0)
        self.assertEqual(body["idle_remaining_seconds"], -1)

    def test_load_model_starts_runner_with_token_and_context(self):
        started = threading.Event()
        release = threading.Event()
        calls = []

        def fake_boot(target_id, hf_token=""):
            calls.append((target_id, hf_token))
            started.set()
            release.wait(5)
            return True, "done"

        with mock.patch.object(srv, "_run_kernel_llm_boot", side_effect=fake_boot):
            body = self.client.post("/api/load_model", json={
                "model_id": "gemma4-31b", "context_length": 65536, "hf_token": "hf_x"}).json()
            self.assertTrue(started.wait(5))
            self.assertTrue(body["is_loading_vram"])
            self.assertEqual(body["context_length"], 65536)
            # A second load while one runs does not start another boot.
            self.client.post("/api/load_model", json={"model_id": "qwen38-27b-rvn"})
            release.set()
            for _ in range(50):
                if not srv.TASK_STATE["is_loading_vram"]:
                    break
                time.sleep(0.05)
        self.assertEqual(calls, [("gemma4-31b", "hf_x")])
        self.assertFalse(srv.TASK_STATE["is_loading_vram"])
        self.assertEqual(srv.TASK_STATE["message"], "done")

    def test_context_length_is_clamped(self):
        with mock.patch.object(srv, "begin_model_load", return_value=True):
            body = self.client.post("/api/load_model", json={"model_id": "gemma4-31b", "context_length": 100}).json()
        self.assertEqual(body["context_length"], 4096)

    def test_unload_stops_8081_and_status_goes_offline(self):
        self.probe_alias = HUIHUI_ALIAS

        def fake_pkill():
            self.probe_alias = None

        with mock.patch.object(srv, "_kill_llama_8081", side_effect=fake_pkill) as kill:
            body = self.client.post("/api/unload_model", json={"model_id": "qwen38-huihui-v2"}).json()
        kill.assert_called_once()
        self.assertEqual(body["loaded_model_ids"], [])
        self.assertFalse(body["llm_server"]["online"])
        self.assertIn("已停止", body["task_message"])

    def test_unload_of_a_model_that_is_not_served_is_a_noop(self):
        self.probe_alias = HUIHUI_ALIAS
        with mock.patch.object(srv, "_kill_llama_8081") as kill:
            body = self.client.post("/api/unload_model", json={"model_id": "gemma4-31b"}).json()
        kill.assert_not_called()
        self.assertEqual(body["loaded_model_ids"], ["qwen38-huihui-v2"])

    def test_disconnect_reports_result(self):
        with mock.patch.object(srv, "disconnect_colab_runtime", return_value=(True, "bye")):
            self.assertEqual(self.client.post("/api/disconnect", json={}).json(), {"ok": True, "message": "bye"})


class RunnerBootTest(BoundStatusTestCase):
    """_run_kernel_llm_boot against a fake runners file; the runner code is a tiny script."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runners_path = os.path.join(self.tmp.name, "runners.json")
        self.roots = {slug: os.path.join(self.tmp.name, slug) for slug in ("qwen38-huihui-v2", "gemma4-31b")}
        self.set_runners(code="print('hello')")
        p = mock.patch.object(srv, "RUNNERS_FILE", self.runners_path)
        p.start()
        self.addCleanup(p.stop)
        for name in ("_ensure_mcp_bridge", "_kill_llama_8081"):
            q = mock.patch.object(srv, name)
            q.start()
            self.addCleanup(q.stop)

    def set_runners(self, code):
        runners = {slug: {"root_dir": root, "code": code} for slug, root in self.roots.items()}
        with open(self.runners_path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(runners, f)

    def test_missing_runner_file_or_id(self):
        os.remove(self.runners_path)
        ok, msg = srv._run_kernel_llm_boot("gemma4-31b")
        self.assertFalse(ok)
        self.assertIn("runners.json", msg)
        self.set_runners("pass")
        ok, msg = srv._run_kernel_llm_boot("nope")
        self.assertFalse(ok)
        self.assertIn("nope", msg)

    def test_parses_progress_and_passes_hf_token(self):
        code = (
            "import os\n"
            "print('HF=' + os.environ.get('HF_TOKEN', ''))\n"
            "print('  并发下载进度: 50% (1 / 2 GB)')\n"
            "print('总启动耗时： 1.0s')\n"
        )
        self.set_runners(code)
        seen = []
        real_print = print

        def fake_sync():
            self.probe_alias = HUIHUI_ALIAS
            return self.fake_probe()

        def spy(*args, **kwargs):
            seen.append((" ".join(map(str, args)), srv.TASK_STATE["progress"]))
            real_print(*args, **kwargs)

        with mock.patch.object(srv, "_sync_kernel_8081_to_loaded_runtimes", side_effect=fake_sync), \
                mock.patch("builtins.print", side_effect=spy):
            ok, msg = srv._run_kernel_llm_boot("qwen38-huihui-v2", hf_token=" hf_secret ")
        self.assertTrue(ok, msg)
        self.assertIn("HF=hf_secret", [t for t, _ in seen])
        # progress as it stood when the line after the 50% line was printed: 0.10 + 0.65 * 0.5, rounded
        self.assertEqual([p for t, p in seen if t.startswith("总启动耗时")][0], 0.43)
        self.assertEqual(srv.TASK_STATE["progress"], 1.0)
        self.assertIn("已装载", msg)

    def test_runner_failure_and_not_ready(self):
        self.set_runners("import sys; sys.exit(3)")
        ok, msg = srv._run_kernel_llm_boot("gemma4-31b")
        self.assertFalse(ok)
        self.assertIn("exit=3", msg)
        self.set_runners("print('ok')")
        ok, msg = srv._run_kernel_llm_boot("gemma4-31b")
        self.assertFalse(ok)
        self.assertIn(":8081 未就绪", msg)

    def test_already_running_target_skips_the_runner(self):
        self.probe_alias = HUIHUI_ALIAS
        self.set_runners("raise SystemExit(9)")
        ok, msg = srv._run_kernel_llm_boot("qwen38-huihui-v2")
        self.assertTrue(ok)
        self.assertIn("已在 :8081 运行", msg)

    def test_switching_kills_server_cleans_other_dirs_and_keeps_llama_dist(self):
        old_root, new_root = self.roots["qwen38-huihui-v2"], self.roots["gemma4-31b"]
        os.makedirs(os.path.join(old_root, "llama-dist"))
        with open(os.path.join(old_root, "llama-dist", "llama-server"), "w", newline="\n") as f:
            f.write("bin")
        with open(os.path.join(old_root, "shard.gguf"), "w", newline="\n") as f:
            f.write("x")
        self.probe_alias = HUIHUI_ALIAS
        with mock.patch.object(srv.time, "sleep"), \
                mock.patch.object(srv, "_sync_kernel_8081_to_loaded_runtimes", return_value=None):
            ok, msg = srv._run_kernel_llm_boot("gemma4-31b")
        self.assertFalse(ok)  # fake runner never brings :8081 up; the cleanup is what is checked
        srv._kill_llama_8081.assert_called_once()
        self.assertFalse(os.path.exists(old_root))
        self.assertTrue(os.path.exists(os.path.join(new_root, "llama-dist", "llama-server")))


class DisconnectTest(unittest.TestCase):
    def test_fallback_posts_unassign_when_google_colab_import_fails(self):
        hits = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                hits.append(self.path)
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        env = {"TBE_RUNTIME_ADDR": "127.0.0.1:%d" % httpd.server_address[1]}
        with mock.patch.dict(sys.modules, {"google.colab": None, "google": None}), mock.patch.dict(os.environ, env):
            ok, msg = srv.disconnect_colab_runtime()
        self.assertTrue(ok, msg)
        self.assertEqual(hits, ["/unassign"])

    def test_no_runtime_manager_reports_failure(self):
        env = {k: v for k, v in os.environ.items() if k != "TBE_RUNTIME_ADDR"}
        with mock.patch.dict(sys.modules, {"google.colab": None, "google": None}), \
                mock.patch.dict(os.environ, env, clear=True):
            ok, msg = srv.disconnect_colab_runtime()
        self.assertFalse(ok)
        self.assertIn("释放运行时失败", msg)


class ConsoleMirrorTest(unittest.TestCase):
    def test_keeps_finished_lines_and_last_carriage_return_state(self):
        from collections import deque
        lines = deque(maxlen=5)
        sink = io.StringIO()
        mirror = srv.ConsoleMirror(sink, lines, threading.Lock())
        mirror.write("hello")
        mirror.write("\n")
        mirror.write("10%\r50%\r\x1b[0m100%\n")
        mirror.write("unfinished")
        texts = [ln.split("] ", 1)[1] for ln in lines]
        self.assertEqual(texts, ["hello", "100%"])
        self.assertIn("unfinished", sink.getvalue())


class VmBootTest(unittest.TestCase):
    def test_is_valid_python_and_prints_the_wake_markers(self):
        import ast
        with open(os.path.join(HERE, "vm_boot.py"), encoding="utf-8") as f:
            src = f.read()
        ast.parse(src)
        self.assertIn("WAKE_URL:", src)
        self.assertIn("WAKE_ERROR:", src)
        # Wake may kill the old server and its orphaned tunnel, never the model or MCP.
        killed = re.findall(r'\["pkill", "-f", "([^"]+)"\]', src)
        self.assertIn("python.*/content/colab_yoga_server.py", killed)
        self.assertIn("python.*/content/app.py", killed)
        for pattern in killed:
            self.assertNotIn("llama", pattern)
            self.assertNotIn("mcp", pattern.lower())


_REAL_PROBE = srv._probe_kernel_llama_8081  # BoundStatusTestCase stubs the module attribute


class FakeClock:
    """Replaces srv._mono so cache ages can be stepped without sleeping."""

    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now


class TTLCellTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        p = mock.patch.object(srv, "_mono", self.clock)
        p.start()
        self.addCleanup(p.stop)

    def test_hit_inside_ttl_and_recompute_after(self):
        cell, calls = srv._TTLCell(2.0), []
        compute = lambda: calls.append(1) or len(calls)
        self.assertEqual(cell.get(compute), 1)
        self.clock.now += 1.9
        self.assertEqual(cell.get(compute), 1)
        self.clock.now += 0.2
        self.assertEqual(cell.get(compute), 2)
        self.assertEqual(len(calls), 2)

    def test_force_recomputes_inside_ttl_and_invalidate_forgets(self):
        cell, calls = srv._TTLCell(2.0), []
        compute = lambda: calls.append(1) or len(calls)
        cell.get(compute)
        self.assertEqual(cell.get(compute, force=True), 2)
        cell.invalidate()
        self.assertEqual(cell.get(compute), 3)

    def test_exception_is_not_cached(self):
        cell = srv._TTLCell(2.0)
        with self.assertRaises(RuntimeError):
            cell.get(mock.Mock(side_effect=RuntimeError("boom")))
        self.assertEqual(cell.get(lambda: "ok"), "ok")

    def test_none_is_a_cacheable_value(self):
        cell, calls = srv._TTLCell(2.0), []
        self.assertIsNone(cell.get(lambda: calls.append(1)))
        self.assertIsNone(cell.get(lambda: calls.append(1)))
        self.assertEqual(len(calls), 1)

    def test_concurrent_callers_share_one_computation(self):
        cell, calls = srv._TTLCell(2.0), []
        gate = threading.Event()

        def slow():
            calls.append(1)
            gate.wait(5)
            return "v"

        results = []
        threads = [threading.Thread(target=lambda: results.append(cell.get(slow))) for _ in range(12)]
        for t in threads:
            t.start()
        time.sleep(0.2)  # everyone is queued behind the one running computation
        gate.set()
        for t in threads:
            t.join(5)
        self.assertEqual(results, ["v"] * 12)
        self.assertEqual(len(calls), 1)

    def test_forced_callers_arriving_during_a_run_share_one_more_run(self):
        cell, calls = srv._TTLCell(2.0), []
        gate = threading.Event()

        def slow():
            calls.append(1)
            gate.wait(5)
            self.clock.now += 0.01
            return len(calls)

        first = threading.Thread(target=lambda: cell.get(slow, force=True))
        first.start()
        time.sleep(0.1)
        self.clock.now += 0.5  # the rest arrive after the first run began
        rest = [threading.Thread(target=lambda: cell.get(slow, force=True)) for _ in range(8)]
        for t in rest:
            t.start()
        time.sleep(0.2)
        gate.set()
        for t in [first] + rest:
            t.join(5)
        self.assertEqual(len(calls), 2)  # not one run per caller


class HardwareSnapshotCacheTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        for p in (mock.patch.object(srv, "_mono", self.clock),
                  mock.patch.object(srv, "_read_ram_gb", return_value=(176.0, 10.0)),
                  mock.patch.object(srv, "_read_disk_gb", return_value=(200.0, 20.0))):
            p.start()
            self.addCleanup(p.stop)
        srv.reset_status_caches()
        self.addCleanup(srv.reset_status_caches)

    def test_gpu_is_read_once_per_ttl_window(self):
        with mock.patch.object(srv, "_read_gpu", return_value=("NVIDIA L4", 1.0, 24.0)) as gpu:
            for _ in range(100):
                snap = srv.get_hardware_snapshot()
            self.assertEqual(gpu.call_count, 1)
            self.assertEqual(snap["gpu_name"], "NVIDIA L4")
            self.assertEqual(snap["cu_burn_rate_per_hour"], 1.71)
            self.clock.now += 2.5
            srv.get_hardware_snapshot()
            self.assertEqual(gpu.call_count, 2)

    def test_callers_get_copies(self):
        with mock.patch.object(srv, "_read_gpu", return_value=None):
            first = srv.get_hardware_snapshot()
            first["gpu_name"] = "mutated"
            self.assertEqual(srv.get_hardware_snapshot()["gpu_name"], srv.NO_GPU_NAME)

    def test_concurrent_polls_run_one_gpu_query(self):
        gate = threading.Event()

        def slow_gpu():
            gate.wait(5)
            return ("NVIDIA L4", 1.0, 24.0)

        with mock.patch.object(srv, "_read_gpu", side_effect=slow_gpu) as gpu:
            threads = [threading.Thread(target=srv.get_hardware_snapshot) for _ in range(10)]
            for t in threads:
                t.start()
            time.sleep(0.2)
            gate.set()
            for t in threads:
                t.join(5)
        self.assertEqual(gpu.call_count, 1)


class FakeNvml:
    """Just enough of libnvidia-ml for _NvmlReader; ctypes byref objects expose ._obj."""

    def __init__(self, name=b"NVIDIA RTX PRO 6000", used=2 * 1024 ** 3, total=96 * 1024 ** 3, v2=True):
        self.name, self.used, self.total = name, used, total
        self.reads = 0
        self.fail_reads = 0
        self.nvmlInit_v2 = self._Fn(lambda: 0)
        self.nvmlDeviceGetHandleByIndex_v2 = self._Fn(self._handle)
        self.nvmlDeviceGetName = self._Fn(self._get_name)
        if v2:
            self.nvmlDeviceGetMemoryInfo_v2 = self._Fn(self._mem)

    class _Fn:
        def __init__(self, fn):
            self.fn = fn
            self.argtypes = None
            self.restype = None

        def __call__(self, *a):
            return self.fn(*a)

    def _handle(self, index, ref):
        ref._obj.value = 1
        return 0

    def _get_name(self, handle, buf, n):
        buf.value = self.name
        return 0

    def _mem(self, handle, ref):
        self.reads += 1
        if self.fail_reads:
            self.fail_reads -= 1
            return 999
        m = ref._obj
        m.total, m.used, m.free, m.reserved = self.total, self.used, self.total - self.used, 0
        return 0


class NvmlReaderTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        p = mock.patch.object(srv, "_mono", self.clock)
        p.start()
        self.addCleanup(p.stop)

    def smi(self, name="NVIDIA RTX PRO 6000", used=2.0, total=96.0):
        return mock.patch.object(srv, "_query_nvidia_smi", return_value=(name, used, total))

    def test_reads_without_nvidia_smi_after_the_one_time_self_check(self):
        fake = FakeNvml()
        reader = srv._NvmlReader(loader=lambda: fake)
        with self.smi() as smi:
            for _ in range(50):
                name, used, total = reader.query()
            self.assertEqual(smi.call_count, 1)
        self.assertEqual((name, round(used, 2), round(total, 2)), ("NVIDIA RTX PRO 6000", 2.0, 96.0))

    def test_disagreeing_with_nvidia_smi_turns_it_off_for_good(self):
        loads = []
        reader = srv._NvmlReader(loader=lambda: loads.append(1) or FakeNvml(used=5 * 1024 ** 3))
        with self.smi(used=2.0):
            self.assertIsNone(reader.query())
        self.clock.now += 3600
        with self.smi(used=2.0) as smi:
            self.assertIsNone(reader.query())
            smi.assert_not_called()
        self.assertEqual(len(loads), 1)

    def test_missing_library_or_symbol_is_permanent(self):
        def missing():
            raise OSError("no lib")

        reader = srv._NvmlReader(loader=missing)
        self.assertIsNone(reader.query())
        self.assertIsNone(reader.query())
        old = srv._NvmlReader(loader=lambda: FakeNvml(v2=False))
        self.assertIsNone(old.query())
        self.clock.now += 3600
        self.assertIsNone(old.query())

    def test_runtime_error_backs_off_then_reopens(self):
        fake = FakeNvml()
        reader = srv._NvmlReader(loader=lambda: fake)
        with self.smi():
            self.assertIsNotNone(reader.query())
            fake.fail_reads = 1
            self.assertIsNone(reader.query())      # error: caller falls back to nvidia-smi
            reads = fake.reads
            self.assertIsNone(reader.query())      # inside the back-off window: not even tried
            self.assertEqual(fake.reads, reads)
            self.clock.now += srv._NvmlReader.RETRY_S + 1
            self.assertIsNotNone(reader.query())

    def test_read_gpu_falls_back_to_nvidia_smi(self):
        with mock.patch.object(srv, "_NVML", mock.Mock(query=mock.Mock(return_value=None))), \
                mock.patch.object(srv, "_query_nvidia_smi", return_value=("g", 1.0, 2.0)) as smi:
            self.assertEqual(srv._read_gpu(), ("g", 1.0, 2.0))
            smi.assert_called_once()
        with mock.patch.object(srv, "_NVML", mock.Mock(query=mock.Mock(return_value=("n", 1.0, 2.0)))), \
                mock.patch.object(srv, "_query_nvidia_smi") as smi:
            self.assertEqual(srv._read_gpu(), ("n", 1.0, 2.0))
            smi.assert_not_called()


class CountingOpener:
    def __init__(self, routes):
        self.inner = FakeOpener(routes)
        self.urls = []

    def open(self, url, timeout=None):
        self.urls.append(url)
        return self.inner.open(url, timeout=timeout)

    def count(self, suffix):
        return sum(u.endswith(suffix) for u in self.urls)


class ProbeAndStatusCacheTest(BoundStatusTestCase):
    served = HUIHUI_ALIAS

    def test_probe_rereads_the_alias_only_every_few_seconds(self):
        clock = FakeClock()
        opener = CountingOpener({"/health": {}, "/v1/models": {"data": [{"id": HUIHUI_ALIAS}]}})
        with mock.patch.object(srv, "_mono", clock), mock.patch.object(srv, "_local_opener", return_value=opener):
            for _ in range(5):
                self.assertEqual(_REAL_PROBE()["served_alias"], HUIHUI_ALIAS)
            self.assertEqual(opener.count("/v1/models"), 1)
            self.assertEqual(opener.count("/health"), 5)
            clock.now += srv._ALIAS_RECHECK_S + 1
            _REAL_PROBE()
            self.assertEqual(opener.count("/v1/models"), 2)

    def test_health_failure_forgets_the_alias_so_a_swapped_model_is_seen(self):
        gemma = "Gemma-4-31B-it-ARA-Heretic-Q8_0"
        up_huihui = CountingOpener({"/health": {}, "/v1/models": {"data": [{"id": HUIHUI_ALIAS}]}})
        down = CountingOpener({"/health": OSError("down")})
        up_gemma = CountingOpener({"/health": {}, "/v1/models": {"data": [{"id": gemma}]}})
        with mock.patch.object(srv, "_local_opener", return_value=up_huihui):
            self.assertEqual(_REAL_PROBE()["model_id"], "qwen38-huihui-v2")
        with mock.patch.object(srv, "_local_opener", return_value=down):
            self.assertIsNone(_REAL_PROBE())
        with mock.patch.object(srv, "_local_opener", return_value=up_gemma):
            self.assertEqual(_REAL_PROBE()["model_id"], "gemma4-31b")

    def test_a_failed_models_call_is_not_remembered(self):
        broken = CountingOpener({"/health": {}, "/v1/models": OSError("busy")})
        with mock.patch.object(srv, "_local_opener", return_value=broken):
            _REAL_PROBE()
            _REAL_PROBE()
        self.assertEqual(broken.count("/v1/models"), 2)

    def test_a_hundred_status_polls_probe_8081_once(self):
        for _ in range(100):
            body = self.client.get("/api/status").json()
        self.assertEqual(srv._probe_kernel_llama_8081.call_count, 1)
        self.assertEqual(body["loaded_model_ids"], ["qwen38-huihui-v2"])

    def test_probe_goes_stale_after_ttl(self):
        clock = FakeClock()
        with mock.patch.object(srv, "_mono", clock):
            self.client.get("/api/status")
            clock.now += 2.1
            self.probe_alias = None
            body = self.client.get("/api/status").json()
        self.assertEqual(body["loaded_model_ids"], [])
        self.assertFalse(body["llm_server"]["online"])

    def test_load_and_unload_decisions_bypass_the_cache(self):
        self.client.get("/api/status")
        self.probe_alias = None
        self.assertIsNone(srv._sync_kernel_8081_to_loaded_runtimes())
        self.assertEqual(self.client.get("/api/status").json()["loaded_model_ids"], [])

    def test_concurrent_status_polls_probe_once(self):
        gate = threading.Event()
        real_fake = self.fake_probe

        def slow_probe():
            gate.wait(5)
            return real_fake()

        srv._probe_kernel_llama_8081.side_effect = slow_probe
        results = []
        threads = [threading.Thread(target=lambda: results.append(srv.get_status())) for _ in range(10)]
        for t in threads:
            t.start()
        time.sleep(0.2)
        gate.set()
        for t in threads:
            t.join(5)
        self.assertEqual(len(results), 10)
        self.assertEqual(srv._probe_kernel_llama_8081.call_count, 1)

    def test_idle_countdown_and_progress_stay_live_while_inputs_are_cached(self):
        self.client.get("/api/status")
        srv.LAST_ACTIVE_TIME -= 120
        srv.TASK_STATE.update(progress=0.5, message="halfway")
        body = self.client.get("/api/status").json()
        self.assertLessEqual(body["idle_remaining_seconds"], 30 * 60 - 119)
        self.assertEqual((body["task_progress"], body["task_message"]), (0.5, "halfway"))
        self.assertEqual(srv._probe_kernel_llama_8081.call_count, 1)

    def test_loaded_model_ids_is_never_seen_empty_while_refreshing(self):
        stop = threading.Event()
        seen_empty = []

        def refresher():
            while not stop.is_set():
                srv._sync_kernel_8081_to_loaded_runtimes()

        srv._sync_kernel_8081_to_loaded_runtimes()
        t = threading.Thread(target=refresher)
        t.start()
        for _ in range(3000):
            if not srv.LOADED_MODEL_IDS:
                seen_empty.append(1)
        stop.set()
        t.join(5)
        self.assertEqual(seen_empty, [])


class UptimeIncrementalTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self._saved = (srv._RUNTIME_ASSIGNED_TS, dict(srv._UPTIME_SCAN))
        srv._RUNTIME_ASSIGNED_TS = None
        srv._UPTIME_SCAN.update(path=None, offset=0, next_try=0.0)
        p = mock.patch.object(srv, "_mono", self.clock)
        p.start()
        self.addCleanup(p.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = os.path.join(self.tmp.name, "app.log")

    def tearDown(self):
        srv._RUNTIME_ASSIGNED_TS = self._saved[0]
        srv._UPTIME_SCAN.update(self._saved[1])

    def append(self, text):
        with open(self.log, "a", encoding="utf-8", newline="\n") as f:
            f.write(text)

    def test_follows_the_log_instead_of_rereading_it(self):
        self.append(json.dumps({"time": "2025-01-01T00:00:00Z", "msg": "boot"}) + "\n")
        self.append('{"msg": "Kernel started", broken\n')   # parsed once, then never again
        self.assertIsNone(srv._runtime_assigned_ts(self.log))
        self.assertEqual(srv._UPTIME_SCAN["offset"], os.path.getsize(self.log))
        self.append(json.dumps({"time": "2025-01-01T10:00:00Z", "msg": "Kernel started: a"}) + "\n")
        self.clock.now += srv._UPTIME_RETRY_S + 0.1
        real_loads = json.loads
        with mock.patch.object(srv.json, "loads", side_effect=real_loads) as loads:
            ts = srv._runtime_assigned_ts(self.log)
        self.assertEqual(loads.call_count, 1)
        self.assertEqual(ts, datetime(2025, 1, 1, 10, tzinfo=timezone.utc).timestamp())

    def test_unfinished_last_line_is_left_for_next_time(self):
        full = json.dumps({"time": "2025-01-01T10:00:00Z", "msg": "Kernel started"})
        self.append(full[:20])
        self.assertIsNone(srv._runtime_assigned_ts(self.log))
        self.assertEqual(srv._UPTIME_SCAN["offset"], 0)
        self.append(full[20:] + "\n")
        self.clock.now += srv._UPTIME_RETRY_S + 0.1
        self.assertIsNotNone(srv._runtime_assigned_ts(self.log))

    def test_misses_are_not_retried_inside_the_retry_window(self):
        with mock.patch.object(srv, "_scan_app_log_for_kernel_start", wraps=srv._scan_app_log_for_kernel_start) as scan:
            for _ in range(50):
                self.assertIsNone(srv._runtime_assigned_ts(self.log))  # the log does not exist yet
            self.assertEqual(scan.call_count, 1)
            self.clock.now += srv._UPTIME_RETRY_S + 0.1
            srv._runtime_assigned_ts(self.log)
            self.assertEqual(scan.call_count, 2)

    def test_log_that_has_not_grown_is_not_opened_again(self):
        self.append(json.dumps({"time": "2025-01-01T00:00:00Z", "msg": "boot"}) + "\n")
        self.assertIsNone(srv._runtime_assigned_ts(self.log))
        self.clock.now += srv._UPTIME_RETRY_S + 0.1
        with mock.patch("builtins.open", side_effect=AssertionError("opened an unchanged log")):
            self.assertIsNone(srv._runtime_assigned_ts(self.log))

    def test_truncated_log_is_scanned_from_the_top(self):
        self.append("x" * 500 + "\n")
        srv._runtime_assigned_ts(self.log)
        with open(self.log, "w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps({"time": "2025-01-01T10:00:00Z", "msg": "Kernel started"}) + "\n")
        self.clock.now += srv._UPTIME_RETRY_S + 0.1
        self.assertIsNotNone(srv._runtime_assigned_ts(self.log))

    def test_found_value_is_final(self):
        self.append(json.dumps({"time": "2025-01-01T10:00:00Z", "msg": "Kernel started"}) + "\n")
        first = srv._runtime_assigned_ts(self.log)
        with mock.patch("builtins.open", side_effect=AssertionError("log must not be opened again")):
            self.assertEqual(srv._runtime_assigned_ts(self.log), first)


class McpTargetMemoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "runners.json")
        p = mock.patch.object(srv, "RUNNERS_FILE", self.path)
        p.start()
        self.addCleanup(p.stop)
        srv._MCP_TARGET_MEMO.update(sig=None, target=None)
        self.addCleanup(lambda: srv._MCP_TARGET_MEMO.update(sig=None, target=None))

    def write(self, ip):
        code = "socket.inet_aton(\"%s\") + struct.pack('>H', 3426)" % ip
        with open(self.path, "w", encoding="utf-8", newline="\n") as f:
            json.dump({"a": {"code": code}}, f)

    def test_parsed_once_until_the_file_changes(self):
        self.write("100.64.0.1")
        with mock.patch.object(srv, "_parse_mcp_target", wraps=srv._parse_mcp_target) as parse:
            for _ in range(20):
                self.assertEqual(srv._kernel_mcp_target(), ("100.64.0.1", 3426, 1055))
            self.assertEqual(parse.call_count, 1)
            self.write("100.64.0.22")  # different size, so a changed signature even within one mtime tick
            self.assertEqual(srv._kernel_mcp_target(), ("100.64.0.22", 3426, 1055))
            self.assertEqual(parse.call_count, 2)

    def test_file_removed_after_being_cached(self):
        self.write("100.64.0.1")
        self.assertIsNotNone(srv._kernel_mcp_target())
        os.remove(self.path)
        self.assertIsNone(srv._kernel_mcp_target())


class HeartbeatRaceTest(unittest.TestCase):
    def test_racing_callers_start_one_ping(self):
        opened = []

        class Opener:
            def open(self, req, timeout=None):
                opened.append(1)
                return FakeResponse(b"{}")

        saved = srv._LAST_JUPYTER_HEARTBEAT_TS
        srv._LAST_JUPYTER_HEARTBEAT_TS = 0.0
        self.addCleanup(lambda: setattr(srv, "_LAST_JUPYTER_HEARTBEAT_TS", saved))
        with mock.patch.object(srv, "_local_opener", return_value=Opener()):
            threads = [threading.Thread(target=srv._ping_local_colab_jupyter) for _ in range(16)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(5)
            for _ in range(50):
                if len(opened) >= 2:
                    break
                time.sleep(0.02)
            time.sleep(0.1)
        self.assertEqual(len(opened), 2)  # one ping = /api/kernels + /api/status


class ForwarderLifecycleTest(unittest.TestCase):
    """Half-close, socket options, thread count and shutdown of the embedded forwarder."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        path = os.path.join(cls.tmp.name, "forwarder.py")
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(srv.MCP_BRIDGE_SCRIPT)
        fspec = importlib.util.spec_from_file_location("forwarder_lifecycle", path)
        cls.fwd = importlib.util.module_from_spec(fspec)
        fspec.loader.exec_module(cls.fwd)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def start_forwarder(self, fake):
        listen = socket.socket()
        listen.bind(("127.0.0.1", 0))
        listen.listen(8)
        self.addCleanup(listen.close)

        def run():
            with contextlib.suppress(OSError):
                self.fwd.serve(listen, "100.1.2.3", 3426, fake.port)

        t = threading.Thread(target=run, daemon=True)
        t.start()
        return listen, t

    @staticmethod
    def mcp_threads():
        return [t for t in threading.enumerate() if t.name in ("mcp-conn", "mcp-pipe")]

    def test_half_close_still_delivers_the_answer(self):
        fake = FakeSocks5Server()
        self.addCleanup(fake.close)
        listen, _ = self.start_forwarder(fake)
        with socket.create_connection(listen.getsockname(), timeout=3) as cli:
            cli.sendall(b"request-body")
            cli.shutdown(socket.SHUT_WR)
            data = b""
            while True:
                chunk = cli.recv(4096)
                if not chunk:
                    break
                data += chunk
        self.assertEqual(data, b"request-body")

    def test_large_payload_round_trips_intact(self):
        fake = FakeSocks5Server()
        self.addCleanup(fake.close)
        listen, _ = self.start_forwarder(fake)
        payload = os.urandom(1_500_000)
        with socket.create_connection(listen.getsockname(), timeout=10) as cli:
            sender = threading.Thread(target=lambda: (cli.sendall(payload), cli.shutdown(socket.SHUT_WR)))
            sender.start()
            got = bytearray()
            while True:
                chunk = cli.recv(65536)
                if not chunk:
                    break
                got += chunk
            sender.join(10)
        self.assertEqual(bytes(got), payload)

    def test_sockets_are_tuned_for_small_messages_and_dead_peers(self):
        fake = FakeSocks5Server()
        self.addCleanup(fake.close)
        up = self.fwd.socks5_connect("100.64.0.1", 3426, fake.port, timeout=3)
        self.addCleanup(up.close)
        self.assertTrue(up.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY))
        self.assertTrue(up.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE))

    def test_two_threads_per_connection_and_none_left_after_close(self):
        fake = FakeSocks5Server()
        self.addCleanup(fake.close)
        listen, _ = self.start_forwarder(fake)
        clients = [socket.create_connection(listen.getsockname(), timeout=3) for _ in range(5)]
        for c in clients:
            c.sendall(b"x")
            self.assertEqual(c.recv(1), b"x")
        self.assertEqual(len(self.mcp_threads()), 10)
        for c in clients:
            c.close()
        for _ in range(100):
            if not self.mcp_threads():
                break
            time.sleep(0.05)
        self.assertEqual(self.mcp_threads(), [])

    def test_serve_returns_when_the_listener_is_closed(self):
        fake = FakeSocks5Server()
        self.addCleanup(fake.close)
        listen, t = self.start_forwarder(fake)
        listen.close()
        t.join(3)
        self.assertFalse(t.is_alive())

    def test_failed_socks_connect_closes_the_client(self):
        fake = FakeSocks5Server(reply_code=5)
        self.addCleanup(fake.close)
        listen, _ = self.start_forwarder(fake)
        with socket.create_connection(listen.getsockname(), timeout=3) as cli:
            cli.settimeout(3)
            self.assertEqual(cli.recv(10), b"")


class VmBootWaitTest(unittest.TestCase):
    """wait_for_url follows the server log from an offset; main() is not run (it kills processes)."""

    def setUp(self):
        with open(os.path.join(HERE, "vm_boot.py"), encoding="utf-8") as f:
            src = f.read().rstrip()
        self.assertTrue(src.endswith("\nmain()"))
        ns = {"__name__": "vm_boot_under_test"}
        exec(compile(src[: -len("main()")], "vm_boot.py", "exec"), ns)
        self.boot = ns
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = os.path.join(self.tmp.name, "server.log")
        ns["LOG"] = self.log

    def run_wait(self, proc, **kw):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.boot["wait_for_url"](proc, poll_s=0.01, **kw)
        return out.getvalue()

    def test_url_split_across_writes_is_found_once_complete(self):
        proc = mock.Mock(poll=mock.Mock(return_value=None))

        def writer():
            for part in ("starting\n", "COLAB_YOGA_URL=https://abc", ".gradio.live\n"):
                time.sleep(0.05)
                with open(self.log, "a", encoding="utf-8", newline="\n") as f:
                    f.write(part)

        t = threading.Thread(target=writer)
        t.start()
        out = self.run_wait(proc, wait_seconds=5)
        t.join(5)
        self.assertEqual(out.strip(), "WAKE_URL:https://abc.gradio.live")

    def test_exit_and_timeout_report_errors_with_the_log_tail(self):
        with open(self.log, "w", encoding="utf-8", newline="\n") as f:
            f.write("Traceback boom\n")
        out = self.run_wait(mock.Mock(poll=mock.Mock(return_value=3), returncode=3), wait_seconds=5)
        self.assertIn("WAKE_ERROR: 服务进程已退出 (exit=3)", out)
        self.assertIn("boom", out)
        out = self.run_wait(mock.Mock(poll=mock.Mock(return_value=None)), wait_seconds=0.1)
        self.assertIn("WAKE_ERROR: 启动超时", out)

    def test_each_poll_reads_only_new_bytes(self):
        with open(self.log, "w", encoding="utf-8", newline="\n") as f:
            f.write("x" * 1000 + "\n")
        sizes = []
        real_open = open

        class Spy:
            def __init__(self, fh):
                self.fh = fh

            def __enter__(self):
                self.fh.__enter__()
                return self

            def __exit__(self, *exc):
                return self.fh.__exit__(*exc)

            def seek(self, pos):
                return self.fh.seek(pos)

            def read(self):
                data = self.fh.read()
                sizes.append(len(data))
                return data

        def spy_open(path, mode="r", *a, **k):
            fh = real_open(path, mode, *a, **k)
            return Spy(fh) if path == self.log and "b" in mode else fh

        proc = mock.Mock(poll=mock.Mock(side_effect=[None, None, None, 1]), returncode=1)
        self.boot["open"] = spy_open
        out = self.run_wait(proc, wait_seconds=5)
        self.assertIn("WAKE_ERROR", out)
        self.assertEqual(sizes[:4], [1001, 0, 0, 0])


class ApiTokenTest(BoundStatusTestCase):
    """The control API is reachable from the internet through the tunnel: every call but
    GET /api/status needs the bearer token."""

    MUTATING = (
        ("post", "/api/touch", {}),
        ("post", "/api/config_idle", {"idle_timeout_minutes": 0}),
        ("post", "/api/load_model", {"model_id": "gemma4-31b"}),
        ("post", "/api/unload_model", {"model_id": ""}),
        ("post", "/api/disconnect", {}),
    )

    def bare(self):
        return TestClient(srv.create_app())  # no Authorization header

    def test_status_is_open_and_everything_else_is_closed_without_a_token(self):
        c = self.bare()
        self.assertEqual(c.get("/api/status").status_code, 200)
        self.assertEqual(c.get("/api/status?model_id=x").status_code, 200)
        with mock.patch.object(srv, "begin_model_load") as load, \
                mock.patch.object(srv, "disconnect_colab_runtime") as disc, \
                mock.patch.object(srv, "unload_model") as unload:
            for method, path, body in self.MUTATING:
                r = getattr(c, method)(path, json=body)
                self.assertEqual(r.status_code, 401, path)
                self.assertEqual(r.headers.get("www-authenticate"), "Bearer")
            load.assert_not_called()
            disc.assert_not_called()
            unload.assert_not_called()
        self.assertEqual(srv.IDLE_TIMEOUT_MINUTES, 30)  # config_idle 0 did not take effect
        self.assertEqual(c.get("/api/nothing").status_code, 401)
        self.assertEqual(c.post("/api/status", json={}).status_code, 401)

    def test_wrong_malformed_or_stale_tokens_are_refused(self):
        c = self.bare()
        for header in ("Bearer nope", "Bearer", "Basic " + TEST_TOKEN, TEST_TOKEN, "Bearer " + TEST_TOKEN + "x",
                       "bearer  "):
            r = c.post("/api/touch", json={}, headers={"Authorization": header})
            self.assertEqual(r.status_code, 401, header)

    def test_the_token_opens_every_call(self):
        c = self.bare()
        h = {"Authorization": "Bearer " + TEST_TOKEN}
        self.assertEqual(c.post("/api/touch", json={}, headers=h).status_code, 200)
        self.assertEqual(c.post("/api/config_idle", json={"idle_timeout_minutes": 45}, headers=h).json()
                         ["idle_timeout_minutes"], 45)
        with mock.patch.object(srv, "disconnect_colab_runtime", return_value=(True, "bye")):
            self.assertEqual(c.post("/api/disconnect", json={}, headers=h).json(), {"ok": True, "message": "bye"})
        # the scheme name is case-insensitive
        self.assertEqual(c.post("/api/touch", json={}, headers={"Authorization": "bearer " + TEST_TOKEN}).status_code, 200)

    def test_no_token_configured_means_closed_not_open(self):
        srv.API_TOKEN = ""
        c = self.bare()
        self.assertEqual(c.get("/api/status").status_code, 200)
        self.assertEqual(c.post("/api/touch", json={}, headers={"Authorization": "Bearer "}).status_code, 401)
        self.assertEqual(c.post("/api/touch", json={}).status_code, 401)

    def test_a_loopback_source_is_not_trusted(self):
        # The tunnel's forwarder connects from 127.0.0.1, so the source address proves nothing.
        c = TestClient(srv.create_app(), client=("127.0.0.1", 50000))
        self.assertEqual(c.post("/api/disconnect", json={}).status_code, 401)

    def test_the_token_never_reaches_status_or_the_console_buffer(self):
        body = json.dumps(self.client.get("/api/status").json())
        self.assertNotIn(TEST_TOKEN, body)
        self.client.post("/api/touch", json={})
        self.assertFalse(any(TEST_TOKEN in line for line in srv.get_console_lines()))


class LoadApiTokenTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "colab_yoga_token")
        for p in (mock.patch.object(srv, "API_TOKEN_FILE", self.path),
                  mock.patch.dict(os.environ, {}, clear=False)):
            p.start()
            self.addCleanup(p.stop)
        os.environ.pop("COLAB_YOGA_TOKEN", None)
        self.addCleanup(setattr, srv, "API_TOKEN", "")

    def test_reads_the_file_the_wake_uploaded(self):
        with open(self.path, "w", encoding="utf-8", newline="") as f:
            f.write("from-wake\n")
        self.assertEqual(srv.load_api_token(), "from-wake")
        self.assertEqual(srv.API_TOKEN, "from-wake")

    def test_environment_wins_over_the_file(self):
        with open(self.path, "w", encoding="utf-8", newline="") as f:
            f.write("from-file")
        with mock.patch.dict(os.environ, {"COLAB_YOGA_TOKEN": " from-env "}):
            self.assertEqual(srv.load_api_token(), "from-env")

    def test_makes_and_stores_one_when_none_was_given(self):
        token = srv.load_api_token()
        self.assertGreaterEqual(len(token), 32)
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(f.read(), token)  # discover_url finds it here
        self.assertEqual(srv.load_api_token(), token)  # and a restart keeps it

    def test_main_loads_the_token_before_serving(self):
        order = []
        with mock.patch.object(srv, "load_api_token", side_effect=lambda: order.append("token")), \
                mock.patch.object(srv, "install_console_mirror"), \
                mock.patch.object(srv.threading, "Thread"), \
                mock.patch.dict(sys.modules, {"uvicorn": mock.Mock(run=lambda *a, **k: order.append("serve"))}):
            srv.main()
        self.assertEqual(order, ["token", "serve"])


class RunnerRegistryTest(BoundStatusTestCase):
    """The model list is the runners file; an unknown alias is never guessed into a model."""

    def probe(self, alias):
        opener = FakeOpener({"/health": {}, "/v1/models": {"data": [{"id": alias}]}})
        with mock.patch.object(srv, "_local_opener", return_value=opener):
            return _REAL_PROBE()

    def test_aliases_come_from_the_file(self):
        self.assertEqual(srv.runner_alias_to_id(), ALIAS_TO_SLUG)
        for slug, alias in SHIPPED_ALIASES.items():
            self.assertEqual(self.probe(alias)["model_id"], slug)
            srv.reset_status_caches()

    def test_a_model_the_user_added_is_found_by_its_alias(self):
        self.write_models({**SHIPPED_ALIASES, "my-70b": "My-70B-Q5"})
        got = self.probe("My-70B-Q5")
        self.assertEqual((got["model_id"], got["served_alias"]), ("my-70b", "My-70B-Q5"))
        self.assertEqual(srv.model_display_name("my-70b"), "My-70B-Q5")  # no curated name: alias
        self.assertEqual(srv.model_display_name("gemma4-31b"), "Gemma-4-31B (ARA Heretic Q8_0)")

    def test_an_unknown_alias_is_not_guessed_and_not_taken_for_running(self):
        got = self.probe("Totally-Other-Model")
        self.assertEqual((got["model_id"], got["served_alias"]), ("", "Totally-Other-Model"))
        self.assertIn("Totally-Other-Model", got["mode_label"])

    def test_unknown_alias_in_status_shows_the_alias_and_no_known_model(self):
        self.probe_alias = "Totally-Other-Model"
        body = self.client.get("/api/status").json()
        self.assertEqual(body["llm_server"]["model_id"], "")
        self.assertEqual(body["llm_server"]["alias"], "Totally-Other-Model")
        self.assertEqual(body["loaded_model_ids"], ["Totally-Other-Model"])
        for slug in SHIPPED_ALIASES:
            self.assertNotIn(slug, body["loaded_model_ids"])

    def test_loading_a_known_model_is_not_skipped_because_an_unknown_one_runs(self):
        self.probe_alias = "Totally-Other-Model"
        with mock.patch.object(srv, "_ensure_mcp_bridge"), mock.patch.object(srv, "_kill_llama_8081") as kill, \
                mock.patch.object(srv.time, "sleep"), \
                mock.patch.object(srv, "_sync_kernel_8081_to_loaded_runtimes", return_value=None):
            ok, msg = srv._run_kernel_llm_boot("qwen38-huihui-v2")
        self.assertNotIn("已在 :8081 运行", msg)
        kill.assert_called_once()  # the unknown model was replaced, not mistaken for the target

    def test_no_runners_file_means_no_known_models(self):
        os.remove(self.models_path)
        srv.reset_status_caches()
        self.assertEqual(srv.runner_alias_to_id(), {})
        self.assertEqual(self.probe(HUIHUI_ALIAS)["model_id"], "")

    def test_unreadable_or_odd_runners_files_do_not_raise(self):
        cases = (("not json", {}), ("[]", {}), ('{"a": 1, "b": {"alias": 7}, "c": {"alias": ""}}', {"7": "b"}))
        for text, want in cases:
            with open(self.models_path, "w", encoding="utf-8", newline="") as f:
                f.write(text)
            srv.reset_status_caches()
            self.assertEqual(srv.runner_alias_to_id(), want, text)

    def test_the_file_is_read_again_only_when_it_changes(self):
        with mock.patch.object(srv.json, "load", wraps=srv.json.load) as load:
            for _ in range(5):
                srv.runner_alias_to_id()
            self.assertEqual(load.call_count, 1)
            self.write_models({"only": "Only-One"})
            self.assertEqual(srv.runner_alias_to_id(), {"Only-One": "only"})
            self.assertEqual(load.call_count, 2)

    def test_load_model_needs_a_model_id(self):
        with mock.patch.object(srv, "begin_model_load") as load:
            r = self.client.post("/api/load_model", json={"model_id": "  "})
            self.assertEqual(r.status_code, 400)
            load.assert_not_called()


if __name__ == "__main__":
    unittest.main()
