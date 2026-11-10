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
    for candidate in [
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Colab-yoga\site-packages"),
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\EasyColab\_internal\site-packages"),
    ]:
        if os.path.isdir(candidate) and candidate not in sys.path:
            sys.path.insert(0, candidate)
            break

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


class RunnerSelectionTests(unittest.TestCase):
    """Tests that runner-file selection picks the newer file per the architecture contract."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.install_dir = self.tmp / "install"
        self.legacy_dir = self.tmp / "legacy" / "Programs" / "EasyColab" / "_internal"
        self.install_dir.mkdir(parents=True)
        self.legacy_dir.mkdir(parents=True)

        self.install_file = self.install_dir / "colab_kernel_runners.json"
        self.legacy_file = self.legacy_dir / "colab_kernel_runners.json"

        self.bridge_py = self.install_dir / "bridge" / "colab_yoga_bridge.py"
        self.bridge_py.parent.mkdir(parents=True)

    def _call_resolve(self) -> str | None:
        orig_file = cb.__file__
        orig_env = os.environ.get("LOCALAPPDATA")
        try:
            cb.__file__ = str(self.bridge_py)
            os.environ["LOCALAPPDATA"] = str(self.tmp / "legacy")
            return cb.resolve_runners_path()
        finally:
            cb.__file__ = orig_file
            if orig_env is None:
                os.environ.pop("LOCALAPPDATA", None)
            else:
                os.environ["LOCALAPPDATA"] = orig_env

    def test_neither_exists(self) -> None:
        self.assertIsNone(self._call_resolve())

    def test_only_install_exists(self) -> None:
        self.install_file.write_text("{}", encoding="utf-8")
        self.assertEqual(self._call_resolve(), str(self.install_file))

    def test_only_legacy_exists(self) -> None:
        self.legacy_file.write_text("{}", encoding="utf-8")
        self.assertEqual(self._call_resolve(), str(self.legacy_file))

    def test_both_exist_install_is_newer(self) -> None:
        self.legacy_file.write_text("{}", encoding="utf-8")
        os.utime(self.legacy_file, (1000, 1000))
        self.install_file.write_text("{}", encoding="utf-8")
        os.utime(self.install_file, (2000, 2000))
        self.assertEqual(self._call_resolve(), str(self.install_file))

    def test_both_exist_legacy_is_newer(self) -> None:
        self.install_file.write_text("{}", encoding="utf-8")
        os.utime(self.install_file, (1000, 1000))
        self.legacy_file.write_text("{}", encoding="utf-8")
        os.utime(self.legacy_file, (2000, 2000))
        self.assertEqual(self._call_resolve(), str(self.legacy_file))


if __name__ == "__main__":
    unittest.main(verbosity=2)
