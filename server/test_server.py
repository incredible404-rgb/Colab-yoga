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
        self.client = TestClient(srv.create_app())

    def fake_probe(self):
        if self.probe_alias is None:
            return None
        return {"model_id": srv.KERNEL_ALIAS_TO_ID[self.probe_alias], "served_alias": self.probe_alias,
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


if __name__ == "__main__":
    unittest.main()
