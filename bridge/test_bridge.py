#!/usr/bin/env python3
"""
Offline regression tests for Colab-yoga bridge (no network, no Colab).
Ports WakeReuseTests and KeeperTests from windows_app/tools/test_regressions.py,
adapting them to colab_yoga_bridge, plus tests for URL parsing and runner selection.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path

# Ensure bundled site-packages and bridge dir are in sys.path
BRIDGE_DIR = Path(__file__).resolve().parent
if str(BRIDGE_DIR) not in sys.path:
    sys.path.insert(0, str(BRIDGE_DIR))

try:
    import colab_cli  # noqa: F401
except ImportError:
    candidate = os.path.join(os.path.dirname(os.path.dirname(str(BRIDGE_DIR))), "site-packages")  # install dir (repo is in it)
    if os.path.isdir(candidate) and candidate not in sys.path:
        sys.path.insert(0, candidate)

import colab_yoga_bridge as cb


class WakeReuseTests(unittest.TestCase):
    """
    colab_yoga_bridge.cmd_wake must never release a VM that is still assigned
    nor open a second GPU when it cannot see the account's assignments.
    """

    def setUp(self) -> None:
        self.cb = cb
        self.calls: dict = {"unassign": [], "allocate": 0, "upload": 0, "pruned": []}
        self.uploaded_files: list[tuple[str, str]] = []
        self.sessions: dict = {}
        self.assignments: list = []
        self.sync_ok = True
        self.ready = True
        self._saved = {}

        def patch(obj, name, val):
            self._saved[(obj, name)] = getattr(obj, name)
            setattr(obj, name, val)

        def sync():
            if not self.sync_ok:
                raise RuntimeError("offline")
            return dict(self.sessions), list(self.assignments)

        class _Stop(Exception):
            pass

        self._Stop = _Stop

        def contents(_s):
            self.calls["upload"] += 1
            raise _Stop()

        def allocate(name, gpu):
            self.calls["allocate"] += 1
            return types.SimpleNamespace(name=name, endpoint="new", accelerator="G4", url="u", token="t")

        fake_state = types.SimpleNamespace(
            sync_sessions=sync,
            client=types.SimpleNamespace(unassign=lambda ep: self.calls["unassign"].append(ep)),
            prune_session=lambda n: self.calls["pruned"].append(n),
            store=types.SimpleNamespace(add=lambda s: None, list=lambda: list(self.sessions.values())),
            history=None,
        )
        patch(cb, "state", fake_state)
        patch(cb, "_has_valid_token", lambda: True)
        patch(cb, "_wait_for_runtime_ready", lambda s, timeout_s=0: self.ready)
        patch(cb, "_allocate_fresh_session", allocate)
        patch(cb, "ContentsClient", contents)
        patch(cb.time, "sleep", lambda *_: None)
        self._out = io.StringIO()
        self._redirect = contextlib.redirect_stdout(self._out)

    def tearDown(self) -> None:
        for (obj, name), val in self._saved.items():
            setattr(obj, name, val)

    @staticmethod
    def _sess(name, endpoint, acc="G4"):
        return types.SimpleNamespace(
            name=name, endpoint=endpoint, accelerator=acc, url="u", token="t", token_expires_at=None,
            kernel_id=None, session_id=None,
        )

    @staticmethod
    def _assign(endpoint, acc="G4"):
        proxy = types.SimpleNamespace(token="t2", url="u2", expires_at=lambda: None)
        return types.SimpleNamespace(
            endpoint=endpoint, runtime_proxy_info=proxy,
            accelerator=types.SimpleNamespace(value=acc), machine_shape=None,
        )

    def _wake(self) -> str:
        with self._redirect:
            self.cb.cmd_wake("G4", server_py_path="colab_yoga_server.py")
        return self._out.getvalue()

    def test_slow_probe_on_assigned_vm_does_not_release(self) -> None:
        self.sessions = {"easycolab": self._sess("easycolab", "vm1")}
        self.assignments = [self._assign("vm1")]
        self.ready = False
        out = self._wake()
        self.assertEqual(self.calls["unassign"], [])
        self.assertEqual(self.calls["allocate"], 0)
        self.assertIn("WAKE_ERROR", out)

    def test_stale_record_with_live_vm_reuses_live_vm(self) -> None:
        self.sessions = {
            "easycolab": self._sess("easycolab", "dead"),
            "qwen": self._sess("qwen", "vm1"),
        }
        self.assignments = [self._assign("vm1")]
        self._wake()
        self.assertEqual(self.calls["allocate"], 0)
        self.assertEqual(self.calls["unassign"], [])
        self.assertEqual(self.calls["upload"], 1)

    def test_unknown_assignments_never_allocates(self) -> None:
        self.sessions = {"easycolab": self._sess("easycolab", "vm1")}
        self.sync_ok = False
        out = self._wake()
        self.assertEqual(self.calls["allocate"], 0)
        self.assertEqual(self.calls["unassign"], [])
        self.assertIn("WAKE_ERROR", out)

    def test_reclaimed_vm_gets_fresh_allocation_without_release(self) -> None:
        self.sessions = {"easycolab": self._sess("easycolab", "gone")}
        self.assignments = []
        self._wake()
        self.assertEqual(self.calls["unassign"], [])
        self.assertEqual(self.calls["allocate"], 1)

    def test_explicit_gpu_change_still_releases(self) -> None:
        self.sessions = {"easycolab": self._sess("easycolab", "vm1", acc="A100")}
        self.assignments = [self._assign("vm1", acc="A100")]
        self._wake()
        self.assertEqual(self.calls["unassign"], ["vm1"])
        self.assertEqual(self.calls["allocate"], 1)

    def test_upload_and_vm_boot_execution_flow(self) -> None:
        """Test full wake flow: upload server & runners, execute vm_boot.py, stream WAKE_URL."""
        temp_dir = tempfile.mkdtemp()
        server_py = os.path.join(temp_dir, "colab_yoga_server.py")
        runners_json = os.path.join(temp_dir, "colab_kernel_runners.json")
        vm_boot_py = os.path.join(temp_dir, "vm_boot.py")

        with open(server_py, "w", encoding="utf-8") as f:
            f.write("# dummy server")
        with open(runners_json, "w", encoding="utf-8") as f:
            f.write('{"runners": []}')
        with open(vm_boot_py, "w", encoding="utf-8") as f:
            f.write("# dummy boot code")

        uploaded = []

        class FakeContents:
            def __init__(self, _sess):
                pass

            def upload(self, local, remote):
                uploaded.append((local, remote))

        class FakeRuntime:
            def __init__(self, *args, **kwargs):
                self.output_hook = kwargs.get("output_hook")

            def execute_code(self, code, allow_stdin=False, output_hook=None, timeout=None):
                hook = output_hook or self.output_hook
                msg = {"text": "WAKE_URL: https://yoga-test.gradio.live\n"}
                if hook:
                    hook(msg)
                return [msg]

            def stop(self, shutdown_kernel=False):
                pass

        self.sessions = {"easycolab": self._sess("easycolab", "vm1")}
        self.assignments = [self._assign("vm1")]

        setattr(cb, "ContentsClient", FakeContents)
        setattr(cb, "ColabRuntime", FakeRuntime)
        setattr(cb, "resolve_runners_path", lambda: runners_json)

        # Patch vm_boot path relative to bridge
        orig_join = cb.os.path.join

        def fake_join(*parts):
            if len(parts) >= 2 and parts[-1] == "vm_boot.py":
                return vm_boot_py
            return orig_join(*parts)

        setattr(cb.os.path, "join", fake_join)

        out_buf = io.StringIO()
        with contextlib.redirect_stdout(out_buf):
            self.cb.cmd_wake("G4", server_py_path=server_py)

        out = out_buf.getvalue()
        self.assertIn("WAKE_URL: https://yoga-test.gradio.live", out)
        # Verify server upload
        self.assertTrue(any(r == "content/colab_yoga_server.py" for _, r in uploaded))
        # Verify runners upload
        self.assertTrue(any(r == "content/colab_kernel_runners.json" for _, r in uploaded))


class KeeperTests(unittest.TestCase):
    """The detached heartbeat (colab_yoga_bridge keeper): decisions and status."""

    def setUp(self) -> None:
        self.cb = cb

    def test_decisions(self) -> None:
        d, now = self.cb.keeper_decide, 10_000.0
        alive = {"app_alive": True, "idle_timeout": 30}
        self.assertEqual(d(now, {**alive, "idle_remaining": 900}, now), "beat")
        self.assertEqual(d(now, {**alive, "idle_remaining": 0}, now), "release")
        # Auto-release switched off: keep beating (24 h cap applies).
        self.assertEqual(d(now, {"app_alive": True, "idle_timeout": 0, "idle_remaining": -1}, now), "beat")
        # Server not answering: keep beating for grace period, then stop and let Colab reclaim.
        self.assertEqual(d(now, None, now - 60), "beat")
        self.assertEqual(d(now, {"app_alive": False}, now - self.cb.KEEPER_SERVER_GRACE_S - 1), "stop")

    def test_status_reports_dead_pid_as_not_alive(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        orig = self.cb.keeper_state_path
        self.cb.keeper_state_path = lambda: str(tmp / "keeper.json")
        try:
            (tmp / "keeper.json").write_text(json.dumps({"pid": 999999, "exit_reason": ""}), encoding="utf-8")
            self.assertFalse(self.cb.keeper_status()["alive"])

            (tmp / "keeper.json").write_text(json.dumps({"pid": os.getpid(), "exit_reason": ""}), encoding="utf-8")
            self.assertTrue(self.cb.keeper_status()["alive"])

            (tmp / "keeper.json").write_text(json.dumps({"pid": os.getpid(), "exit_reason": "idle_release"}), encoding="utf-8")
            self.assertFalse(self.cb.keeper_status()["alive"])
        finally:
            self.cb.keeper_state_path = orig


class UrlParsingTests(unittest.TestCase):
    """Discover and keeper code find the server by its COLAB_YOGA_URL log line."""

    def setUp(self) -> None:
        self.cb = cb

    def test_regex_parses_colab_yoga_url(self) -> None:
        pattern = r"COLAB_YOGA_URL=(https://\S+)"
        log_text = (
            "2025-01-01 10:00:00 [info] server starting\n"
            "COLAB_YOGA_URL=https://my-colab-session.gradio.live\n"
            "2025-01-01 10:00:05 [info] ready\n"
        )
        matches = cb.re.findall(pattern, log_text)
        self.assertTrue(matches)
        self.assertEqual(matches[-1].strip(), "https://my-colab-session.gradio.live")

    def test_discover_code_syntax_and_structure(self) -> None:
        import ast

        ast.parse(self.cb._DISCOVER_CODE)
        self.assertIn("colab_yoga_server.log", self.cb._DISCOVER_CODE)
        self.assertIn("COLAB_YOGA_URL=", self.cb._DISCOVER_CODE)
        self.assertNotIn("easycolab_server.log", self.cb._DISCOVER_CODE)
        self.assertNotIn("/content/app.py", self.cb._DISCOVER_CODE)
        self.assertIn("colab_yoga_server.py", self.cb._DISCOVER_CODE)

    def test_keeper_code_syntax_and_structure(self) -> None:
        import ast

        ast.parse(self.cb._KEEPER_CODE)
        self.assertIn("colab_yoga_server.log", self.cb._KEEPER_CODE)
        self.assertIn("COLAB_YOGA_URL=", self.cb._KEEPER_CODE)
        self.assertIn("/api/status", self.cb._KEEPER_CODE)
        self.assertIn("colab_yoga_server.py", self.cb._KEEPER_CODE)


class RunnerPathTests(unittest.TestCase):
    """Only the file next to the install counts; nothing is looked up at an older path."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.install_dir = self.tmp / "install"
        self.install_file = self.install_dir / "colab_kernel_runners.json"
        self.bridge_py = self.install_dir / "bridge" / "colab_yoga_bridge.py"
        self.bridge_py.parent.mkdir(parents=True)

    def _call_resolve(self) -> str | None:
        orig_file = cb.__file__
        try:
            cb.__file__ = str(self.bridge_py)
            return cb.resolve_runners_path()
        finally:
            cb.__file__ = orig_file

    def test_missing(self) -> None:
        self.assertIsNone(self._call_resolve())

    def test_install_copy(self) -> None:
        self.install_file.write_text("{}", encoding="utf-8")
        self.assertEqual(self._call_resolve(), str(self.install_file))

    def test_old_easycolab_path_is_ignored(self) -> None:
        old = self.tmp / "local" / "Programs" / "EasyColab" / "_internal"
        old.mkdir(parents=True)
        (old / "colab_kernel_runners.json").write_text("{}", encoding="utf-8")
        orig_env = os.environ.get("LOCALAPPDATA")
        os.environ["LOCALAPPDATA"] = str(self.tmp / "local")
        try:
            self.assertIsNone(self._call_resolve())
        finally:
            if orig_env is None:
                os.environ.pop("LOCALAPPDATA", None)
            else:
                os.environ["LOCALAPPDATA"] = orig_env


class AuthStatusTests(unittest.TestCase):
    """auth_status runs in a fresh process every 40-80 s; its account calls must overlap."""

    def setUp(self) -> None:
        self._saved = {}
        self.barrier = threading.Barrier(3, timeout=3)
        self.fail_info = False

        def patch(obj, name, val):
            self._saved[(obj, name)] = getattr(obj, name)
            setattr(obj, name, val)

        def info():
            self.barrier.wait()
            if self.fail_info:
                raise RuntimeError("ccu-info 401")
            return types.SimpleNamespace(paid_compute_units_balance=12.345, consumption_rate_hourly=1.7,
                                         assignments_count=1)

        def sync():
            self.barrier.wait()
            return {"easycolab": types.SimpleNamespace(name="easycolab", endpoint="vm1")}, [self._assignment()]

        def email():
            self.barrier.wait()
            return "me@example.com"

        fake_state = types.SimpleNamespace(client=types.SimpleNamespace(get_consumption_user_info=info))
        patch(cb, "state", fake_state)
        patch(cb, "_has_valid_token", lambda: True)
        patch(cb, "_safe_sync_sessions", sync)
        patch(cb, "_get_account_email", email)

    def tearDown(self) -> None:
        for (obj, name), val in self._saved.items():
            setattr(obj, name, val)

    @staticmethod
    def _assignment():
        return types.SimpleNamespace(
            endpoint="vm1", accelerator=types.SimpleNamespace(value="G4"),
            variant=types.SimpleNamespace(name="GPU"), machine_shape=types.SimpleNamespace(name="HIGH_RAM"))

    def _run(self) -> dict:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cb.cmd_auth_status()
        return json.loads(out.getvalue())

    def test_the_three_account_calls_are_in_flight_together(self) -> None:
        # The barrier only opens when all three are waiting at once; run one after another
        # they would time out and the status would come back as an error.
        body = self._run()
        self.assertEqual(body, {
            "ok": True, "logged_in": True, "email": "me@example.com", "paid_balance": 12.35,
            "burn_rate_hourly": 1.7, "active_assignments": 1,
            "sessions": [{"name": "easycolab", "endpoint": "vm1", "accelerator": "G4", "variant": "GPU",
                          "shape": "HIGH_RAM"}],
        })

    def test_failure_reports_error_with_email(self) -> None:
        self.fail_info = True
        body = self._run()
        self.assertFalse(body["ok"])
        self.assertTrue(body["logged_in"])
        self.assertEqual(body["email"], "me@example.com")
        self.assertIn("ccu-info 401", body["error"])

    def test_not_logged_in_makes_no_account_calls(self) -> None:
        cb._has_valid_token = lambda: False
        self.assertEqual(self._run(), {"ok": True, "logged_in": False})


class AccountEmailCredsReuseTests(unittest.TestCase):
    def test_email_comes_from_the_creds_just_checked_without_reloading_the_token_file(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        token, profile = tmp / "token.json", tmp / "account_profile.json"
        token.write_bytes(b"{}")
        creds = types.SimpleNamespace(valid=True, expired=False, refresh_token="r" * 30, token="t")
        profile.write_bytes(json.dumps({"fp": ("r" * 30)[-20:], "email": "cached@example.com"}).encode())
        saved = (cb.TOKEN_CONFIG_PATH, cb.ACCOUNT_PROFILE_CACHE_PATH, cb._CHECKED_CREDS, cb.Credentials)
        boom = types.SimpleNamespace(from_authorized_user_file=lambda *a: (_ for _ in ()).throw(AssertionError("reloaded")))
        try:
            cb.TOKEN_CONFIG_PATH, cb.ACCOUNT_PROFILE_CACHE_PATH = str(token), str(profile)
            cb._CHECKED_CREDS, cb.Credentials = creds, boom
            self.assertEqual(cb._get_account_email(), "cached@example.com")
        finally:
            cb.TOKEN_CONFIG_PATH, cb.ACCOUNT_PROFILE_CACHE_PATH, cb._CHECKED_CREDS, cb.Credentials = saved

    def test_has_valid_token_remembers_what_it_validated(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        token = tmp / "token.json"
        token.write_bytes(b"{}")
        creds = types.SimpleNamespace(valid=True, expired=False, refresh_token="r")
        fake_creds_cls = types.SimpleNamespace(from_authorized_user_file=lambda *a: creds)
        saved = (cb.TOKEN_CONFIG_PATH, cb.Credentials, cb._CHECKED_CREDS)
        try:
            cb.TOKEN_CONFIG_PATH, cb.Credentials = str(token), fake_creds_cls
            self.assertTrue(cb._has_valid_token())
            self.assertIs(cb._CHECKED_CREDS, creds)
            token.unlink()
            self.assertFalse(cb._has_valid_token())
            self.assertIsNone(cb._CHECKED_CREDS)
        finally:
            cb.TOKEN_CONFIG_PATH, cb.Credentials, cb._CHECKED_CREDS = saved


class LazyImportTests(unittest.TestCase):
    def test_lazy_stand_in_loads_on_first_use_and_forwards(self) -> None:
        lazy = cb._Lazy("collections", "OrderedDict")
        self.assertIsNone(lazy._real)
        self.assertEqual(list(lazy(a=1)), ["a"])
        self.assertIsNotNone(lazy._real)
        self.assertTrue(callable(lazy.fromkeys))

    def test_importing_the_bridge_leaves_runtime_and_contents_unloaded(self) -> None:
        import subprocess

        code = (
            "import sys; sys.path[:0] = %r; import colab_yoga_bridge as cb; "
            "bad = [m for m in ('colab_cli.runtime', 'colab_cli.contents', 'jupyter_kernel_client') if m in sys.modules]; "
            "print(bad); assert cb.ColabRuntime is not None and cb.ContentsClient is not None"
        ) % ([p for p in sys.path if p],)
        done = subprocess.run([sys.executable, "-X", "utf8", "-c", code], capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(), "[]")

    def test_wake_and_keeper_still_get_the_real_classes(self) -> None:
        self.assertEqual(cb._Lazy("colab_cli.runtime", "ColabRuntime")._load().__name__, "ColabRuntime")
        self.assertEqual(cb._Lazy("colab_cli.contents", "ContentsClient")._load().__name__, "ContentsClient")


class KeeperLoopTests(unittest.TestCase):
    """24 hours of 30 s beats against fakes: it must reach its own limit and not grow."""

    def setUp(self) -> None:
        self._saved = {}
        self.tmp = Path(tempfile.mkdtemp())
        self.now = 1_000_000.0
        self.beats = 0
        self.mem = {}

        def patch(obj, name, val):
            self._saved[(obj, name)] = getattr(obj, name)
            setattr(obj, name, val)

        def sleep(sec):
            self.now += sec

        proxy = types.SimpleNamespace(token="t", url="u", expires_at=lambda: None)
        assign = types.SimpleNamespace(endpoint="vm1", runtime_proxy_info=proxy)
        sess = types.SimpleNamespace(name="easycolab", endpoint="vm1", token="", url="", token_expires_at=None)

        def exec_on(s, code, timeout=40):
            self.beats += 1
            if self.beats in (300, 2800):
                import gc
                import tracemalloc

                gc.collect()
                self.mem[self.beats] = tracemalloc.get_traced_memory()[0]
            payload = {"url": "https://x.gradio.live", "app_alive": True, "idle_remaining": 900,
                       "idle_timeout": 30, "llm_online": True}
            return [{"text": "EC_KEEPER:" + json.dumps(payload)}]

        patch(cb, "keeper_state_path", lambda: str(self.tmp / "keeper.json"))
        patch(cb, "_has_valid_token", lambda: True)
        patch(cb, "_safe_sync_sessions", lambda with_status=False: ({"easycolab": sess}, [assign], True))
        patch(cb, "_track_a_covers", lambda endpoint: False)
        patch(cb, "_exec_on_session", exec_on)
        patch(cb, "_tfe_keep_alive", lambda endpoint: None)
        patch(cb, "state", types.SimpleNamespace(store=types.SimpleNamespace(add=lambda s: None)))
        patch(cb.time, "time", lambda: self.now)
        patch(cb.time, "sleep", sleep)

    def tearDown(self) -> None:
        for (obj, name), val in self._saved.items():
            setattr(obj, name, val)

    def test_runs_to_the_24h_cap_without_growing(self) -> None:
        import tracemalloc

        tracemalloc.start()
        try:
            cb.cmd_keeper()
        finally:
            tracemalloc.stop()
        st = json.loads((self.tmp / "keeper.json").read_text(encoding="utf-8"))
        self.assertEqual(st["exit_reason"], "max_hours")
        self.assertEqual(st["beats"], self.beats)
        self.assertGreaterEqual(self.beats, 2850)
        self.assertLess(self.mem[2800] - self.mem[300], 64 * 1024)

    def test_kernel_lookup_reuses_one_http_session(self) -> None:
        self.assertIs(cb._http(), cb._http())


if __name__ == "__main__":
    unittest.main(verbosity=2)
