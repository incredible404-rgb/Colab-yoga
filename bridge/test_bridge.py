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

# No test may touch the real %LOCALAPPDATA%\Colab-yoga\ledger.json; tests that read it patch their own.
_LEDGER_SANDBOX = Path(tempfile.mkdtemp()) / "ledger.json"
cb.ledger_state_path = lambda: str(_LEDGER_SANDBOX)


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
        # Wake stores the control token and starts a keeper: keep both off the real config
        # dir and the real process table.
        self.cfg = Path(tempfile.mkdtemp())
        self.spawned = 0
        patch(cb, "_colab_yoga_config_dir", lambda: str(self.cfg))

        def spawn():
            self.spawned += 1
            return True

        patch(cb, "spawn_keeper", spawn)
        patch(cb, "local_mcp_script_path", lambda: None)
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
            self.ok = self.cb.cmd_wake("G4", server_py_path="colab_yoga_server.py")
        return self._out.getvalue()

    def test_slow_probe_on_assigned_vm_does_not_release(self) -> None:
        self.sessions = {"easycolab": self._sess("easycolab", "vm1")}
        self.assignments = [self._assign("vm1")]
        self.ready = False
        out = self._wake()
        self.assertEqual(self.calls["unassign"], [])
        self.assertEqual(self.calls["allocate"], 0)
        self.assertIn("WAKE_ERROR", out)
        self.assertFalse(self.ok)

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
        self.assertEqual(self.calls["allocate"], 1)
        # the reclaimed record is never unassigned; only the new instance goes back, because
        # this fixture's upload fails (see test_failed_wake_releases_only_what_it_allocated)
        self.assertEqual(self.calls["unassign"], ["new"])

    def test_explicit_gpu_change_still_releases(self) -> None:
        self.sessions = {"easycolab": self._sess("easycolab", "vm1", acc="A100")}
        self.assignments = [self._assign("vm1", acc="A100")]
        self._wake()
        self.assertEqual(self.calls["unassign"][0], "vm1")  # the old model's instance, on purpose
        self.assertEqual(self.calls["allocate"], 1)

    def test_failed_wake_releases_only_what_it_allocated(self) -> None:
        # upload fails right after a fresh allocation: that instance goes back, the error is
        # printed after the release, and the result says failure
        self.sessions = {}
        self.assignments = []
        out = self._wake()
        self.assertFalse(self.ok)
        self.assertEqual(self.calls["unassign"], ["new"])
        self.assertEqual(self.calls["pruned"], ["easycolab"])
        self.assertIn("WAKE_ERROR", out)
        self.assertLess(out.index("释放本次新分配"), out.index("WAKE_ERROR"))

    def test_failed_wake_on_a_reused_instance_does_not_release_it(self) -> None:
        self.sessions = {"easycolab": self._sess("easycolab", "vm1")}
        self.assignments = [self._assign("vm1")]
        out = self._wake()  # upload fails
        self.assertFalse(self.ok)
        self.assertEqual(self.calls["unassign"], [])
        self.assertIn("WAKE_ERROR", out)

    def _boot_with(self, msgs, fresh: bool):
        """Run wake to the end with a runtime printing msgs; fresh=False reuses vm1."""
        temp_dir = tempfile.mkdtemp()
        server_py = os.path.join(temp_dir, "colab_yoga_server.py")
        vm_boot_py = os.path.join(temp_dir, "vm_boot.py")
        for f in (server_py, vm_boot_py):
            Path(f).write_text("# dummy", encoding="utf-8")

        class FakeContents:
            def __init__(self, _sess):
                pass

            def upload(self, local, remote):
                pass

        class FakeRuntime:
            def __init__(self, *a, **k):
                pass

            def execute_code(self, code, allow_stdin=False, output_hook=None, timeout=None):
                for m in msgs:
                    output_hook(m)
                return list(msgs)

            def stop(self, shutdown_kernel=False):
                pass

        self._saved[(cb, "ContentsClient")] = cb.ContentsClient
        self._saved[(cb, "ColabRuntime")] = cb.ColabRuntime
        self._saved[(cb, "resolve_runners_path")] = cb.resolve_runners_path
        cb.ContentsClient, cb.ColabRuntime = FakeContents, FakeRuntime
        cb.resolve_runners_path = lambda: None
        real_join = cb.os.path.join
        self._saved[(cb.os.path, "join")] = real_join
        cb.os.path.join = lambda *p: vm_boot_py if p[-1] == "vm_boot.py" else real_join(*p)
        if fresh:
            self.sessions, self.assignments = {}, []
        else:
            self.sessions = {"easycolab": self._sess("easycolab", "vm1")}
            self.assignments = [self._assign("vm1")]
        with contextlib.redirect_stdout(self._out):
            self.ok = cb.cmd_wake("G4", server_py_path=server_py)
        return self._out.getvalue()

    def test_boot_error_releases_a_fresh_instance_and_fails(self) -> None:
        out = self._boot_with([{"text": "WAKE_ERROR: 启动超时\n"}], fresh=True)
        self.assertFalse(self.ok)
        self.assertEqual(self.calls["unassign"], ["new"])
        self.assertLess(out.index("释放本次新分配"), out.index("WAKE_ERROR: 启动超时"))
        self.assertEqual(self.spawned, 0)  # no keeper for an instance that was given back

    def test_boot_without_a_url_counts_as_failure(self) -> None:
        out = self._boot_with([{"text": "something else\n"}], fresh=True)
        self.assertFalse(self.ok)
        self.assertEqual(self.calls["unassign"], ["new"])
        self.assertIn("WAKE_ERROR", out)

    def test_boot_error_on_a_reused_instance_keeps_it(self) -> None:
        out = self._boot_with([{"output_type": "error", "evalue": "boom"}], fresh=False)
        self.assertFalse(self.ok)
        self.assertEqual(self.calls["unassign"], [])
        self.assertIn("WAKE_ERROR: boom", out)

    def test_boot_success_returns_true_and_releases_nothing(self) -> None:
        self._boot_with([{"text": "WAKE_URL: https://x.trycloudflare.com\n"}], fresh=True)
        self.assertTrue(self.ok)
        self.assertEqual(self.calls["unassign"], [])

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
        token_on_vm = []

        class FakeContents:
            def __init__(self, _sess):
                pass

            def upload(self, local, remote):
                uploaded.append((local, remote))
                if remote == "content/colab_yoga_token":
                    with open(local, encoding="utf-8") as fh:
                        token_on_vm.append(fh.read())

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
        # The control token goes to the VM as a file, is kept locally, and is never printed.
        self.assertEqual(len(token_on_vm), 1)
        token = token_on_vm[0]
        self.assertGreaterEqual(len(token), 32)
        self.assertEqual(self.cb.load_server_token(), token)
        self.assertNotIn(token, out)
        self.assertFalse(os.path.exists(next(l for l, r in uploaded if r == "content/colab_yoga_token")))
        # No keeper was running, so wake started one, before the URL line was printed.
        self.assertEqual(self.spawned, 1)
        self.assertLess(out.index("已启动后台保活"), out.index("WAKE_URL:"))


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
    """Runner file lookup: next to the install, then the standard install; never EasyColab."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.install_dir = self.tmp / "unzipped"
        self.install_file = self.install_dir / "colab_kernel_runners.json"
        self.bridge_py = self.install_dir / "bridge" / "colab_yoga_bridge.py"
        self.bridge_py.parent.mkdir(parents=True)
        self.local = self.tmp / "local"
        self.standard_file = self.local / "Programs" / "Colab-yoga" / "colab_kernel_runners.json"
        self.old_file = self.local / "Programs" / "EasyColab" / "_internal" / "colab_kernel_runners.json"

    def _put(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")

    def _call_resolve(self, **env) -> str:
        orig_file = cb.__file__
        keys = ("LOCALAPPDATA", "COLAB_YOGA_RUNNERS", "COLAB_YOGA_DIR")
        orig_env = {k: os.environ.get(k) for k in keys}
        try:
            cb.__file__ = str(self.bridge_py)
            for k in keys:
                os.environ.pop(k, None)
            os.environ["LOCALAPPDATA"] = str(self.local)
            os.environ.update(env)
            return cb.resolve_runners_path()
        finally:
            cb.__file__ = orig_file
            for k, v in orig_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    @property
    def cfg_file(self) -> Path:
        return self.local / "Colab-yoga" / "colab_kernel_runners.json"

    def test_missing_falls_back_to_the_config_dir_path(self) -> None:
        self.assertEqual(self._call_resolve(), str(self.cfg_file))
        self.assertFalse(self.cfg_file.exists())

    def test_config_dir_comes_after_the_install_and_before_the_standard_install(self) -> None:
        self._put(self.standard_file)
        self._put(self.cfg_file)
        self.assertEqual(self._call_resolve(), str(self.cfg_file))
        self._put(self.install_file)
        self.assertEqual(self._call_resolve(), str(self.install_file))

    def test_env_overrides_come_first_and_only_when_the_file_exists(self) -> None:
        self._put(self.install_file)
        override = self.tmp / "elsewhere" / "r.json"
        yoga_dir = self.tmp / "yoga"
        self.assertEqual(self._call_resolve(COLAB_YOGA_RUNNERS=str(override),
                                             COLAB_YOGA_DIR=str(yoga_dir)), str(self.install_file))
        self._put(yoga_dir / "colab_kernel_runners.json")
        self.assertEqual(self._call_resolve(COLAB_YOGA_RUNNERS=str(override), COLAB_YOGA_DIR=str(yoga_dir)),
                         str(yoga_dir / "colab_kernel_runners.json"))
        self._put(override)
        self.assertEqual(self._call_resolve(COLAB_YOGA_RUNNERS=str(override), COLAB_YOGA_DIR=str(yoga_dir)),
                         str(override))

    def test_install_copy(self) -> None:
        self._put(self.install_file)
        self.assertEqual(self._call_resolve(), str(self.install_file))

    def test_standard_install_is_the_fallback(self) -> None:
        self._put(self.standard_file)
        self.assertEqual(self._call_resolve(), str(self.standard_file))

    def test_install_copy_wins_over_standard(self) -> None:
        self._put(self.standard_file)
        self._put(self.install_file)
        os.utime(self.install_file, (1000, 1000))  # priority, not age
        self.assertEqual(self._call_resolve(), str(self.install_file))

    def test_old_easycolab_path_is_ignored(self) -> None:
        self._put(self.old_file)
        self.assertEqual(self._call_resolve(), str(self.cfg_file))


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
        patch(cb, "local_mcp_script_path", lambda: None)
        patch(cb, "resolve_runners_path", lambda: str(self.tmp / "colab_kernel_runners.json"))
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


class EnsureKeeperTests(unittest.TestCase):
    """The command-line wake has no console to spawn the keeper, so wake does it itself."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self._saved = (cb._colab_yoga_config_dir, cb.subprocess.Popen)
        cb._colab_yoga_config_dir = lambda: str(self.tmp)
        self.popen_calls = []

        def popen(cmd, **kwargs):
            self.popen_calls.append((cmd, kwargs))
            return types.SimpleNamespace(pid=1)

        cb.subprocess.Popen = popen

    def tearDown(self) -> None:
        cb._colab_yoga_config_dir, cb.subprocess.Popen = self._saved

    def _write_keeper(self, **st) -> None:
        (self.tmp / "keeper.json").write_text(json.dumps(st), encoding="utf-8")

    def test_a_live_keeper_is_left_alone(self) -> None:
        self._write_keeper(pid=os.getpid(), exit_reason="")
        self.assertEqual(cb.ensure_keeper(), "running")
        self.assertEqual(self.popen_calls, [])

    def test_no_keeper_dead_pid_or_finished_keeper_starts_one(self) -> None:
        for st in (None, {"pid": 999999, "exit_reason": ""}, {"pid": os.getpid(), "exit_reason": "idle_release"}):
            self.popen_calls.clear()
            if st is None:
                with contextlib.suppress(FileNotFoundError):
                    (self.tmp / "keeper.json").unlink()
            else:
                self._write_keeper(**st)
            self.assertEqual(cb.ensure_keeper(), "started", st)
            self.assertEqual(len(self.popen_calls), 1, st)

    def test_spawn_is_detached_keeper_on_the_bundled_pythonw(self) -> None:
        install = self.tmp / "app"
        (install / "runtime").mkdir(parents=True)
        (install / "bridge").mkdir()
        (install / "runtime" / "pythonw.exe").write_bytes(b"")
        fake_file = str(install / "bridge" / "colab_yoga_bridge.py")
        saved = cb.__file__
        cb.__file__ = fake_file
        try:
            self.assertTrue(cb.spawn_keeper())
        finally:
            cb.__file__ = saved
        cmd, kw = self.popen_calls[0]
        self.assertEqual(cmd, [str(install / "runtime" / "pythonw.exe"), "-X", "utf8",
                               os.path.abspath(fake_file), "keeper"])
        self.assertEqual(kw["cwd"], str(install))
        self.assertEqual(kw["stdout"], cb.subprocess.DEVNULL)  # must not hold wake's output pipe open
        self.assertEqual(kw["env"]["PYTHONUTF8"], "1")
        if sys.platform == "win32":
            self.assertTrue(kw["creationflags"] & 0x00000008)  # DETACHED_PROCESS
            self.assertTrue(kw["creationflags"] & 0x08000000)  # CREATE_NO_WINDOW

    def test_spawn_retries_without_breakaway_then_reports_failure(self) -> None:
        if sys.platform != "win32":
            self.skipTest("Windows creation flags")
        flags_seen = []

        def popen(cmd, **kwargs):
            flags_seen.append(kwargs["creationflags"])
            raise OSError("refused")

        cb.subprocess.Popen = popen
        self.assertFalse(cb.spawn_keeper())
        self.assertEqual(len(flags_seen), 2)
        self.assertTrue(flags_seen[0] & 0x01000000)
        self.assertFalse(flags_seen[1] & 0x01000000)


class LocalMcpTests(unittest.TestCase):
    """The local MCP is started by wake, kept up while a session is live (keeper) and stopped
    when the last session is released, all through the helper script. Without a helper it is
    an optional part: nothing is printed and nothing fails."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.log = self.tmp / "helper.log"
        self.script = None
        self.now = 1_000_000.0
        self.release_at = float("inf")
        self.extra_assignments = []
        self.settle_checks = True  # each fake sleep waits for in-flight checks, so beats are deterministic
        self.on_sleep = lambda now: None
        self.real_find = getattr(cb, "local_mcp_script_path", None)
        self._saved = {}

        def sleep(sec):
            self.now += sec
            self.on_sleep(self.now)
            if self.settle_checks:
                self._join_checks()

        def sync(with_status=False):
            assigns = [types.SimpleNamespace(endpoint="vm1", runtime_proxy_info=types.SimpleNamespace(
                token="t", url="u", expires_at=lambda: None))] if self.now < self.release_at else []
            return {"easycolab": self.sess}, assigns + self.extra_assignments, True

        def exec_on(s, code, timeout=40):
            payload = {"url": "https://x", "app_alive": True, "idle_remaining": 900,
                       "idle_timeout": 30, "llm_online": True}
            return [{"text": "EC_KEEPER:" + json.dumps(payload)}]

        self.sess = types.SimpleNamespace(name="easycolab", endpoint="vm1", token="", url="", token_expires_at=None)
        self._patch(cb, "keeper_state_path", lambda: str(self.tmp / "keeper.json"))
        self._patch(cb, "_has_valid_token", lambda: True)
        self._patch(cb, "_safe_sync_sessions", sync)
        self._patch(cb, "_track_a_covers", lambda endpoint: False)
        self._patch(cb, "_exec_on_session", exec_on)
        self._patch(cb, "_tfe_keep_alive", lambda endpoint: None)
        self._patch(cb, "state", types.SimpleNamespace(store=types.SimpleNamespace(add=lambda s: None)))
        self._patch(cb, "ledger_poll", lambda endpoint="": None)
        self._patch(cb, "save_server_token", lambda token: None)
        self._patch(cb, "ensure_keeper", lambda: "running")
        self._patch(cb, "local_mcp_script_path", lambda: self.script)
        self.tunnel = True
        self._patch(cb, "local_mcp_tunnel_configured", lambda: self.tunnel)
        self._patch(cb.time, "time", lambda: self.now)
        self._patch(cb.time, "sleep", sleep)

    def tearDown(self) -> None:
        for (obj, name), val in reversed(list(self._saved.items())):
            setattr(obj, name, val)

    def _patch(self, obj, name, val) -> None:
        self._saved[(obj, name)] = getattr(obj, name, None)
        setattr(obj, name, val)

    def test_wake_without_a_reverse_tunnel_does_not_start_the_local_mcp(self) -> None:
        self.script = self._helper({"ok": True, "started": True, "pid": 7})
        self.tunnel = False
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cb._on_wake_url("tok")
        self.assertEqual(self._actions(), [])
        self.assertNotIn("MCP", out.getvalue())

    def test_keeper_without_a_reverse_tunnel_does_not_start_the_local_mcp(self) -> None:
        calls = []
        self._patch(cb, "ensure_local_mcp", lambda: calls.append(1) or {"state": "started", "error": ""})
        self.tunnel = False
        self.release_at = self.now + 300
        cb.cmd_keeper()
        self.assertEqual(calls, [])

    def test_keeper_backs_off_after_failed_starts(self) -> None:
        calls = []
        self._patch(cb, "ensure_local_mcp", lambda: calls.append(cb.time.time()) or {"state": "failed", "error": "x"})
        self.release_at = self.now + 2400  # every 60 s would be 40 tries
        cb.cmd_keeper()
        gaps = [b - a for a, b in zip(calls, calls[1:])]
        self.assertEqual(len(calls), 4, gaps)
        self.assertGreaterEqual(gaps[0], 60)
        self.assertGreaterEqual(gaps[1], 300)
        self.assertGreaterEqual(gaps[2], 1800)

    def test_retry_wait_backs_off_and_resets(self) -> None:
        self.assertEqual([cb.local_mcp_retry_s(n) for n in range(5)],
                         [cb.LOCAL_MCP_CHECK_S, 60, 300, 1800, 1800])

    def test_tunnel_needs_a_real_local_mcp_ip_or_the_auto_switch(self) -> None:
        self._patch(cb, "local_mcp_tunnel_configured", self._saved[(cb, "local_mcp_tunnel_configured")])
        runners = self.tmp / "runners.json"
        self._patch(cb, "resolve_runners_path", lambda: str(runners))
        self.assertFalse(cb.local_mcp_tunnel_configured(env={}))
        runners.write_text(json.dumps({"m": {"code": 'LOCAL_MCP_IP = "<YOUR_TAILSCALE_IP>"\n'}}), encoding="utf-8")
        self.assertFalse(cb.local_mcp_tunnel_configured(env={}))
        self.assertTrue(cb.local_mcp_tunnel_configured(env={"COLAB_LOCAL_MCP_AUTO": "1"}))
        runners.write_text(json.dumps({"m": {"code": 'LOCAL_MCP_IP = "100.64.0.9"\n'}}), encoding="utf-8")
        self.assertTrue(cb.local_mcp_tunnel_configured(env={}))

    def test_script_from_the_config_dirs_local_mcp_bridge_json(self) -> None:
        helper = self.tmp / "svc" / "local_mcp.py"
        helper.parent.mkdir()
        helper.write_text("", encoding="utf-8")
        cfg = self.tmp / "local" / "Colab-yoga"
        cfg.mkdir(parents=True)
        (cfg / "local_mcp_bridge.json").write_text(json.dumps({"script": str(helper)}), encoding="utf-8")
        find = self._saved[(cb, "local_mcp_script_path")]
        found = find(install_dir=str(self.tmp / "app"), env={"LOCALAPPDATA": str(self.tmp / "local")},
                     home=str(self.tmp / "home"))
        self.assertEqual(found, str(helper))

    def _helper(self, reply: dict, exit_code: int = 0) -> str:
        """A stand-in helper: records its action in self.log and prints `reply` as its last line."""
        reply_file = self.tmp / "reply.json"
        reply_file.write_text(json.dumps(reply), encoding="utf-8")
        path = self.tmp / "helper" / "local_mcp.py"
        path.parent.mkdir(exist_ok=True)
        path.write_text(
            "import sys\n"
            f"with open({str(self.log)!r}, 'a', encoding='utf-8') as f: f.write(sys.argv[1] + '\\n')\n"
            "print('helper chatter')\n"
            f"print(open({str(reply_file)!r}, encoding='utf-8').read())\n"
            f"sys.exit({exit_code})\n",
            encoding="utf-8")
        return str(path)

    def _actions(self) -> list:
        return self.log.read_text(encoding="utf-8").split() if self.log.exists() else []

    def _join_checks(self) -> None:
        for t in threading.enumerate():
            if t is not threading.current_thread():
                t.join(30)

    def test_wake_starts_the_local_mcp_and_says_so(self) -> None:
        self.script = self._helper({"ok": True, "started": True, "pid": 7,
                                   "url": "http://127.0.0.1:3426/mcp", "token_file": "t"})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cb._on_wake_url("tok")
        self.assertEqual(self._actions(), ["start"])
        self.assertIn("已启动本机 MCP", out.getvalue())

    def test_wake_without_a_helper_is_silent_and_runs_no_helper(self) -> None:
        self.script = None
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cb._on_wake_url("tok")
        self.assertNotIn("MCP", out.getvalue())
        self.assertEqual(self._actions(), [])

    def test_failed_start_is_reported_with_its_reason_and_never_leaks_the_token(self) -> None:
        self.script = self._helper({"ok": False, "error": "port 3426 is held by another program",
                                   "token": "sekret-123"}, exit_code=1)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cb._on_wake_url("tok")  # the GPU side still succeeds: no exception
        text = out.getvalue()
        self.assertIn("本机 MCP 桥没能启动", text)
        self.assertIn("port 3426 is held by another program", text)
        self.assertNotIn("sekret-123", text)
        self.assertEqual(cb.ensure_local_mcp()["state"], "failed")

    def test_start_states_follow_the_helper_reply(self) -> None:
        self.script = self._helper({"ok": True, "started": False, "pid": 7})
        self.assertEqual(cb.ensure_local_mcp()["state"], "running")
        self.script = self._helper({"unexpected": True})  # no ok: never counted as started
        self.assertEqual(cb.ensure_local_mcp()["state"], "failed")

    def test_helper_is_found_by_env_then_install_config_then_the_skills(self) -> None:
        find = self.real_find
        install = self.tmp / "app"
        install.mkdir()
        home = self.tmp / "home"

        def touch(path: Path) -> str:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("", encoding="utf-8")
            return os.path.abspath(path)

        skill = Path("skills") / "colab-gpu-llm-service" / "scripts" / "local_mcp.py"
        self.assertIsNone(find(str(install), {}, str(self.tmp / "empty")))
        claude = touch(home / ".claude" / skill)
        self.assertEqual(find(str(install), {}, str(home)), claude)
        agents = touch(home / ".agents" / skill)
        self.assertEqual(find(str(install), {}, str(home)), agents)
        configured = touch(self.tmp / "configured" / "mcp.py")
        (install / "local_mcp_bridge.json").write_text(json.dumps({"script": configured}), encoding="utf-8")
        self.assertEqual(find(str(install), {}, str(home)), configured)
        from_env = touch(self.tmp / "env" / "mcp.py")
        self.assertEqual(find(str(install), {"COLAB_LOCAL_MCP_SCRIPT": from_env}, str(home)), from_env)
        missing = str(self.tmp / "gone.py")
        self.assertEqual(find(str(install), {"COLAB_LOCAL_MCP_SCRIPT": missing}, str(home)), configured)

    def test_keeper_keeps_the_local_mcp_up_while_a_session_is_live(self) -> None:
        self.script = self._helper({"ok": True, "started": False, "pid": 7})
        self.release_at = self.now + 600
        cb.cmd_keeper()
        self._join_checks()
        actions = self._actions()
        self.assertEqual(actions[-1], "stop")  # the release at the end stops it
        self.assertGreaterEqual(actions.count("start"), 9)  # about one check a minute, not one per beat
        self.assertLessEqual(actions.count("start"), 11)

    def test_keeper_without_a_live_session_never_starts_the_local_mcp(self) -> None:
        self.script = self._helper({"ok": True, "started": False, "pid": 7})
        self.release_at = self.now  # no assignment from the first look
        cb.cmd_keeper()
        self._join_checks()
        self.assertEqual(self._actions(), ["stop"])

    def test_releasing_one_session_keeps_mcp_for_another_assignment(self) -> None:
        self.script = self._helper({"ok": True, "started": False, "pid": 7})
        self.extra_assignments = [types.SimpleNamespace(endpoint="other-vm")]
        cb.state.client = types.SimpleNamespace(unassign=lambda ep: None)
        cb.state.prune_session = lambda name: None
        self._patch(cb, "keeper_decide", lambda *args: "release")
        cb.cmd_keeper()
        self.assertNotIn("stop", self._actions())

    def test_a_failed_release_does_not_stop_mcp_for_a_live_assignment(self) -> None:
        self.script = self._helper({"ok": True, "started": False, "pid": 7})
        def fail(ep):
            raise RuntimeError("release failed")
        cb.state.client = types.SimpleNamespace(unassign=fail)
        self._patch(cb, "keeper_decide", lambda *args: "release")
        cb.cmd_keeper()
        self.assertNotIn("stop", self._actions())

    def test_a_slow_check_is_not_stacked_and_does_not_stall_the_beats(self) -> None:
        release = threading.Event()
        calls = []

        def slow_ensure():
            calls.append(1)
            release.wait(30)
            return {"state": "running", "error": ""}

        self._patch(cb, "ensure_local_mcp", slow_ensure)
        self.settle_checks = False
        self.release_at = self.now + 300
        # Free the blocked check only at the keeper's next assignment sync after the release (120 s cadence).
        self.on_sleep = lambda now: release.set() if now >= self.release_at + 60 else None
        cb.cmd_keeper()
        self._join_checks()
        self.assertEqual(len(calls), 1)
        self.assertGreaterEqual(json.loads((self.tmp / "keeper.json").read_text(encoding="utf-8"))["beats"], 9)

    def test_stop_only_asks_the_helper_and_never_kills_by_port(self) -> None:
        self.script = self._helper({"ok": True, "started": False, "pid": 7})
        commands = []
        real_run = cb.subprocess.run

        def record(cmd, *args, **kwargs):
            commands.append(list(cmd))
            return real_run(cmd, *args, **kwargs)

        self._patch(cb.subprocess, "run", record)
        cb.stop_local_mcp()
        self.assertEqual(commands, [[cb._local_mcp_python(), "-X", "utf8", self.script, "stop"]])
        self.assertEqual(self._actions(), ["stop"])


class McpStartCommandTests(unittest.TestCase):
    """mcp_start is the console's manual fallback. It starts the local MCP and nothing else: it
    works with no GPU running and must never wake or keep one."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.log = self.tmp / "helper.log"
        self.script = None
        self._saved = {}

        def no_gpu(*args, **kwargs):
            raise AssertionError("mcp_start must not wake or keep a GPU")

        self._patch(cb, "local_mcp_script_path", lambda: self.script)
        self.status_reply = {"ok": True, "running": True, "pid": 7}
        real_run = cb.run_local_mcp

        def run_local_mcp(action):
            return self.status_reply if action == "status" else real_run(action)

        self._patch(cb, "run_local_mcp", run_local_mcp)
        self._patch(cb, "cmd_wake", no_gpu)
        self._patch(cb, "ensure_keeper", no_gpu)
        self._patch(cb, "spawn_keeper", no_gpu)

    def tearDown(self) -> None:
        for (obj, name), val in reversed(list(self._saved.items())):
            setattr(obj, name, val)

    def _patch(self, obj, name, val) -> None:
        self._saved[(obj, name)] = getattr(obj, name, None)
        setattr(obj, name, val)

    def _helper(self, reply: dict, exit_code: int = 0) -> str:
        reply_file = self.tmp / "reply.json"
        reply_file.write_text(json.dumps(reply), encoding="utf-8")
        path = self.tmp / "helper" / "local_mcp.py"
        path.parent.mkdir(exist_ok=True)
        path.write_text(
            "import sys\n"
            f"with open({str(self.log)!r}, 'a', encoding='utf-8') as f: f.write(sys.argv[1] + '\\n')\n"
            "print('helper chatter')\n"
            f"print(open({str(reply_file)!r}, encoding='utf-8').read())\n"
            f"sys.exit({exit_code})\n",
            encoding="utf-8")
        return str(path)

    def _run(self) -> dict:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cb.main(["mcp_start"])
        return json.loads(out.getvalue().strip().splitlines()[-1])

    def test_started_and_already_running_are_both_ok(self) -> None:
        self.script = self._helper({"ok": True, "started": True, "pid": 7})
        self.assertEqual(self._run(), {"ok": True, "state": "started", "error": ""})
        self.script = self._helper({"ok": True, "started": False, "pid": 7})
        self.assertEqual(self._run(), {"ok": True, "state": "running", "error": ""})

    def test_failure_carries_the_reason_and_never_the_token(self) -> None:
        self.script = self._helper({"ok": False, "error": "端口 3426 被其他程序占用", "token": "sekret-9"},
                                  exit_code=1)
        result = self._run()
        self.assertFalse(result["ok"])
        self.assertEqual(result["state"], "failed")
        self.assertIn("端口 3426 被其他程序占用", result["error"])
        self.assertNotIn("sekret-9", json.dumps(result))

    def test_a_start_the_status_does_not_confirm_is_a_failure(self) -> None:
        self.script = self._helper({"ok": True, "started": True, "pid": 7})
        self.status_reply = {"ok": True, "running": False}
        result = self._run()
        self.assertFalse(result["ok"])
        self.assertEqual(result["state"], "failed")
        self.assertIn("状态检查", result["error"])

    def test_no_helper_says_what_to_install(self) -> None:
        self.script = None
        result = self._run()
        self.assertFalse(result["ok"])
        self.assertEqual(result["state"], "absent")
        self.assertIn("local_mcp.py", result["error"])


class ServerTokenTests(unittest.TestCase):
    """The control token: stored locally, found again by discover_url, kept out of output."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self._saved = {}

        def patch(obj, name, val):
            self._saved[(obj, name)] = getattr(obj, name)
            setattr(obj, name, val)

        sess = types.SimpleNamespace(name="easycolab", endpoint="vm1", token="t", url="u", token_expires_at=None,
                                     accelerator="G4", kernel_id=None, session_id=None)
        proxy = types.SimpleNamespace(token="t2", url="u2", expires_at=lambda: None)
        assign = types.SimpleNamespace(endpoint="vm1", runtime_proxy_info=proxy)
        self.alive = True
        self.vm_token = "tok-from-the-vm"

        def exec_on(s, code, timeout=40):
            body = {"url": "https://x.gradio.live", "app_alive": self.alive, "token": self.vm_token}
            return [{"text": "EC_DISCOVER:" + json.dumps(body)}]

        patch(cb, "_colab_yoga_config_dir", lambda: str(self.tmp))
        patch(cb, "_has_valid_token", lambda: True)
        patch(cb, "_safe_sync_sessions", lambda with_status=False: ({"easycolab": sess}, [assign]))
        patch(cb, "_exec_on_session", exec_on)
        patch(cb, "state", types.SimpleNamespace(store=types.SimpleNamespace(add=lambda s: None)))

    def tearDown(self) -> None:
        for (obj, name), val in self._saved.items():
            setattr(obj, name, val)

    def _discover(self) -> dict:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cb.cmd_discover_url()
        if self.vm_token:
            self.assertNotIn(self.vm_token, out.getvalue())
        return json.loads(out.getvalue().strip().splitlines()[-1])

    def test_discover_saves_the_token_and_never_prints_it(self) -> None:
        body = self._discover()
        self.assertTrue(body["ok"])
        self.assertTrue(body["token_saved"])
        self.assertNotIn("token", body)
        self.assertEqual(cb.load_server_token(), "tok-from-the-vm")

    def test_an_old_server_without_a_token_file_leaves_the_local_one_alone(self) -> None:
        cb.save_server_token("older")
        self.vm_token = None
        body = self._discover()
        self.assertFalse(body["token_saved"])
        self.assertEqual(cb.load_server_token(), "older")

    def test_a_dead_server_does_not_overwrite_the_token(self) -> None:
        cb.save_server_token("older")
        self.alive = False
        self.assertFalse(self._discover()["token_saved"])
        self.assertEqual(cb.load_server_token(), "older")

    def test_discover_code_reads_the_token_file(self) -> None:
        import ast

        ast.parse(cb._DISCOVER_CODE)
        self.assertIn("/content/colab_yoga_token", cb._DISCOVER_CODE)

    def test_token_file_round_trip_and_clear(self) -> None:
        self.assertEqual(cb.load_server_token(), "")
        cb.save_server_token("abc")
        self.assertEqual(cb.load_server_token(), "abc")
        self.assertEqual(Path(cb.server_token_path()).parent, self.tmp)
        cb.clear_server_token()
        self.assertEqual(cb.load_server_token(), "")
        cb.clear_server_token()  # already gone: no error


class ExitCodeTests(unittest.TestCase):
    """The console tells a wake failure from WAKE_ERROR, but any other caller (an agent, a
    script) needs the exit status."""

    def _exit_code(self, wake_result):
        calls = []
        saved = (cb.cmd_wake, cb.os._exit)
        cb.cmd_wake = lambda *a, **k: wake_result
        cb.os._exit = lambda c: calls.append(c)
        try:
            cb.run_cli(["wake"])
        finally:
            cb.cmd_wake, cb.os._exit = saved
        return calls

    def test_wake_failure_exits_nonzero_success_zero(self) -> None:
        self.assertEqual(self._exit_code(False), [1])
        self.assertEqual(self._exit_code(True), [0])

    def test_other_commands_still_exit_zero(self) -> None:
        calls = []
        saved = (cb.cmd_auth_status, cb.os._exit)
        cb.cmd_auth_status = lambda: None
        cb.os._exit = lambda c: calls.append(c)
        try:
            cb.run_cli(["auth_status"])
        finally:
            cb.cmd_auth_status, cb.os._exit = saved
        self.assertEqual(calls, [0])


class AuthCodeSessionStoreTests(unittest.TestCase):
    """Re-authorising must not orphan the named sessions colab-gpu-llm-service is using."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self._saved = {}

        def patch(obj, name, val):
            self._saved[(obj, name)] = getattr(obj, name)
            setattr(obj, name, val)

        self.emails = []  # what _get_account_email answers: before the bind, then after it
        self.cleared = 0
        creds = types.SimpleNamespace(to_json=lambda: "{}")

        class FakeFlow:
            credentials = creds

            def fetch_token(self, code):
                pass

        flow_cls = types.SimpleNamespace(from_client_config=lambda *a, **k: FakeFlow())

        def clear():
            self.cleared += 1

        patch(cb, "_load_client_config", lambda: {})
        patch(cb, "InstalledAppFlow", flow_cls)
        patch(cb, "TOKEN_CONFIG_PATH", str(self.tmp / "token.json"))
        patch(cb, "PKCE_VERIFIER_PATH", str(self.tmp / "pkce.txt"))
        patch(cb, "ACCOUNT_PROFILE_CACHE_PATH", str(self.tmp / "profile.json"))
        patch(cb, "_get_account_email", lambda: self.emails.pop(0))
        patch(cb, "_clear_local_session_store", clear)
        patch(cb, "cmd_auth_status", lambda: None)
        # the new account holds vm-new; the store has one record that backs it and one that
        # does not
        self.assigned = [types.SimpleNamespace(endpoint="vm-new")]
        self.list_fails = False
        self.store = {
            "live": types.SimpleNamespace(name="live", endpoint="vm-new"),
            "stale": types.SimpleNamespace(name="stale", endpoint="vm-old"),
        }

        def list_assignments():
            if self.list_fails:
                raise RuntimeError("offline")
            return list(self.assigned)

        patch(cb, "state", types.SimpleNamespace(
            _client="c", _sessions="s",
            client=types.SimpleNamespace(list_assignments=list_assignments),
            store=types.SimpleNamespace(list=lambda: dict(self.store), remove=lambda n: self.store.pop(n, None)),
        ))

    def tearDown(self) -> None:
        for (obj, name), val in self._saved.items():
            setattr(obj, name, val)

    def _bind(self, old: str, new: str) -> None:
        self.emails = [old, new]
        with contextlib.redirect_stdout(io.StringIO()):
            cb.cmd_auth_code("the-code")
        self.assertTrue((self.tmp / "token.json").exists())  # the new token is saved either way

    def test_same_account_keeps_the_named_sessions(self) -> None:
        self._bind("me@example.com", "Me@Example.com")
        self.assertEqual(self.cleared, 0)
        self.assertIsNone(cb.state._client)  # but the client is rebuilt from the new token

    def test_a_different_account_drops_only_sessions_it_does_not_hold(self) -> None:
        self._bind("me@example.com", "other@example.com")
        self.assertEqual(self.cleared, 0)  # never the whole table
        self.assertEqual(list(self.store), ["live"])

    def test_an_unidentifiable_account_prunes_the_same_way(self) -> None:
        self._bind("", "me@example.com")  # first bind, or neither token usable
        self.assertEqual(list(self.store), ["live"])
        self.store["stale2"] = types.SimpleNamespace(name="stale2", endpoint="vm-x")
        self._bind("me@example.com", "")
        self.assertEqual(list(self.store), ["live"])
        self.assertEqual(self.cleared, 0)

    def test_nothing_is_deleted_when_the_assignments_cannot_be_read(self) -> None:
        self.list_fails = True
        self._bind("me@example.com", "other@example.com")
        self.assertEqual(sorted(self.store), ["live", "stale"])

    def test_a_dead_old_token_falls_back_to_the_remembered_email(self) -> None:
        # the old token cannot refresh, so _get_account_email() answers "" before the bind;
        # the profile cache still names the account, and the same one again keeps everything
        (self.tmp / "profile.json").write_text(json.dumps({"fp": "x", "email": "Me@Example.com"}), encoding="utf-8")
        self._bind("", "me@example.com")
        self.assertEqual(sorted(self.store), ["live", "stale"])
        self.assertEqual(self.cleared, 0)
        self.assertFalse((self.tmp / "profile.json").exists())  # and the cache is renewed after


class LedgerTests(unittest.TestCase):
    """ledger.json: settlements as observed in the official balance, per billing session."""

    def setUp(self) -> None:
        self.path = Path(tempfile.mkdtemp()) / "ledger.json"
        self._orig = cb.ledger_state_path
        cb.ledger_state_path = lambda: str(self.path)

    def tearDown(self) -> None:
        cb.ledger_state_path = self._orig

    def _feed(self, readings):
        led = {}
        for now, bal, active in readings:
            led = cb.ledger_observe(led, now, bal, active, 10.05, "vm1" if active else "")
        return led

    def test_session_starts_from_the_idle_balance_and_keeps_only_drops(self) -> None:
        led = self._feed([(0, 100.0, 0), (60, 100.0, 1), (75, 100.0, 1), (300, 99.6, 1), (315, 99.6, 1),
                          (600, 98.76, 1)])
        s = led["session"]
        self.assertTrue(s["exact"])
        self.assertEqual(s["start_balance"], 100.0)
        self.assertEqual(s["points"], [[300, 99.6], [600, 98.76]])
        self.assertEqual((led["balance"], led["active"], led["official_rate"]), (98.76, 1, 10.05))

    def test_settlement_before_the_first_reading_still_counts_when_the_idle_balance_is_known(self) -> None:
        led = self._feed([(0, 100.0, 0), (400, 99.2, 1)])
        self.assertTrue(led["session"]["exact"])
        self.assertEqual(led["session"]["start_balance"], 100.0)

    def test_unknown_or_stale_idle_balance_starts_from_the_first_reading(self) -> None:
        self.assertFalse(self._feed([(10, 99.0, 1)])["session"]["exact"])
        led = self._feed([(0, 100.0, 0), (cb.LEDGER_PRE_MAX_AGE_S + 1, 99.0, 1)])
        self.assertFalse(led["session"]["exact"])
        self.assertEqual(led["session"]["start_balance"], 99.0)

    def test_top_up_is_not_a_charge(self) -> None:
        led = self._feed([(0, 100.0, 0), (300, 99.2, 1), (600, 98.4, 1), (700, 198.4, 1), (900, 197.6, 1)])
        s = led["session"]
        self.assertAlmostEqual(s["start_balance"] - led["balance"], 2.4)
        self.assertEqual(s["points"], [[900, 197.6]])

    def test_release_ends_the_session_and_the_next_one_starts_fresh(self) -> None:
        led = self._feed([(0, 100.0, 0), (300, 99.2, 1), (450, 98.9, 0), (500, 98.9, 0), (600, 98.9, 1)])
        s = led["session"]
        self.assertNotIn("ended_ts", s)
        self.assertEqual((s["start_balance"], s["points"], s["exact"]), (98.9, [], True))
        ended = self._feed([(0, 100.0, 0), (300, 99.2, 1), (450, 98.9, 0)])["session"]
        self.assertEqual((ended["ended_ts"], ended["points"][-1]), (450, [450, 98.9]))

    def test_points_are_capped(self) -> None:
        led = self._feed([(0, 1000.0, 0)] + [(i * 300, 1000.0 - i, 1) for i in range(1, 100)])
        self.assertEqual(len(led["session"]["points"]), cb.LEDGER_POINTS_MAX)

    def test_record_writes_and_poll_survives_a_failed_read(self) -> None:
        cb.ledger_record(50.0, 0, 0.0, now=1.0)
        cb.ledger_record(49.5, 1, 10.05, "vm1", now=2.0)
        on_disk = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["session"]["start_balance"], 50.0)
        saved = cb.state
        try:
            cb.state = types.SimpleNamespace(client=types.SimpleNamespace(
                get_consumption_user_info=lambda: (_ for _ in ()).throw(RuntimeError("offline"))))
            self.assertIsNone(cb.ledger_poll("vm1"))
        finally:
            cb.state = saved
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), on_disk)


class KeeperLedgerTests(unittest.TestCase):
    """The keeper reads the ledger every LEDGER_POLL_S between its 30 s beats, and once after release."""

    def setUp(self) -> None:
        self._saved = {}
        self.tmp = Path(tempfile.mkdtemp())
        self.now = 1_000_000.0
        self.reads = []
        self.beats = 0

        def patch(obj, name, val):
            self._saved[(obj, name)] = getattr(obj, name)
            setattr(obj, name, val)

        def sleep(sec):
            self.now += sec

        def info():
            self.reads.append(self.now)
            active = 0 if self.beats >= 40 else 1
            # a settlement every 300 s at 10.05 CU/h
            bal = 500.0 - int((self.now - 1_000_000.0) // 300) * 10.05 / 12
            return types.SimpleNamespace(paid_compute_units_balance=bal, consumption_rate_hourly=10.05 if active else 0,
                                         assignments_count=active)

        proxy = types.SimpleNamespace(token="t", url="u", expires_at=lambda: None)
        assign = types.SimpleNamespace(endpoint="vm1", runtime_proxy_info=proxy)
        sess = types.SimpleNamespace(name="easycolab", endpoint="vm1", token="", url="", token_expires_at=None)

        def exec_on(s, code, timeout=40):
            self.beats += 1
            payload = {"app_alive": True, "idle_remaining": 0 if self.beats >= 40 else 900, "idle_timeout": 30}
            return [{"text": "EC_KEEPER:" + json.dumps(payload)}]

        patch(cb, "keeper_state_path", lambda: str(self.tmp / "keeper.json"))
        patch(cb, "ledger_state_path", lambda: str(self.tmp / "ledger.json"))
        patch(cb, "_has_valid_token", lambda: True)
        patch(cb, "_safe_sync_sessions", lambda with_status=False: ({"easycolab": sess}, [assign], True))
        patch(cb, "_track_a_covers", lambda endpoint: False)
        patch(cb, "_exec_on_session", exec_on)
        patch(cb, "_tfe_keep_alive", lambda endpoint: None)
        patch(cb, "state", types.SimpleNamespace(
            store=types.SimpleNamespace(add=lambda s: None),
            client=types.SimpleNamespace(get_consumption_user_info=info, unassign=lambda ep: None),
            prune_session=lambda name: None))
        patch(cb, "local_mcp_script_path", lambda: None)
        patch(cb.time, "time", lambda: self.now)
        patch(cb.time, "sleep", sleep)

    def tearDown(self) -> None:
        for (obj, name), val in self._saved.items():
            setattr(obj, name, val)

    def test_reads_every_15_s_and_closes_the_session_after_release(self) -> None:
        cb.ledger_record(500.0, 0, 0.0, now=self.now - 5)
        cb.cmd_keeper()
        st = json.loads((self.tmp / "keeper.json").read_text(encoding="utf-8"))
        self.assertEqual(st["exit_reason"], "idle_release")
        gaps = [b - a for a, b in zip(self.reads, self.reads[1:-1])]
        self.assertTrue(gaps and all(abs(g - cb.LEDGER_POLL_S) < 1.0 for g in gaps), gaps[:8])
        led = json.loads((self.tmp / "ledger.json").read_text(encoding="utf-8"))
        s = led["session"]
        self.assertTrue(s["exact"])
        self.assertIn("ended_ts", s)
        self.assertEqual(led["active"], 0)
        drops = [p[0] for p in s["points"]]
        self.assertGreaterEqual(len(drops), 3)
        # each settlement is seen within one poll of when it happened
        self.assertTrue(all((t - 1_000_000.0) % 300 < cb.LEDGER_POLL_S + 1 for t in drops), drops)


class AuthStatusLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.path = Path(tempfile.mkdtemp()) / "ledger.json"
        self._saved = {}
        self.active = 0

        def patch(obj, name, val):
            self._saved[(obj, name)] = getattr(obj, name)
            setattr(obj, name, val)

        info = lambda: types.SimpleNamespace(paid_compute_units_balance=80.0, consumption_rate_hourly=0.0,
                                             assignments_count=self.active)
        patch(cb, "ledger_state_path", lambda: str(self.path))
        patch(cb, "state", types.SimpleNamespace(client=types.SimpleNamespace(get_consumption_user_info=info)))
        patch(cb, "_has_valid_token", lambda: True)
        patch(cb, "_safe_sync_sessions", lambda: ({}, []))
        patch(cb, "_get_account_email", lambda: "me@example.com")

    def tearDown(self) -> None:
        for (obj, name), val in self._saved.items():
            setattr(obj, name, val)

    def _status(self):
        with contextlib.redirect_stdout(io.StringIO()):
            cb.cmd_auth_status()

    def test_zero_assignments_record_the_next_sessions_start(self) -> None:
        self._status()
        led = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual((led["idle_balance"], led["active"]), (80.0, 0))

    def test_a_corrupt_ledger_neither_breaks_the_status_nor_stays_corrupt(self) -> None:
        self.path.write_text(json.dumps({"checked_ts": "soon", "balance": "x",
                                         "session": {"start_balance": "?", "points": 3}}), encoding="utf-8")
        self.active = 1
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cb.cmd_auth_status()
        self.assertTrue(json.loads(out.getvalue())["ok"])
        led = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual((led["balance"], led["active"], led["session"]["start_balance"]), (80.0, 1, 80.0))

    def test_a_fresh_keeper_reading_is_left_alone_while_billing(self) -> None:
        cb.ledger_record(81.0, 1, 10.05, "vm1")
        self.active = 1
        self._status()
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["balance"], 81.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
