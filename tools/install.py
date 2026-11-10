#!/usr/bin/env python3
"""
Installer for Colab-yoga (replacing dev_sync.py).
Builds the console executable, lays out the install directory per architecture contract,
migrates settings, sets up legacy compatibility shim (junctions, forwarder colab_bridge.py),
manages Windows shortcuts, and gracefully closes/restarts the console GUI.

Usage:
  python tools/install.py                 # Full install / sync
  python tools/install.py --check         # Validation only (AST, go vet, tests)
  python tools/install.py --dry-run       # Show actions without modifying files
  python tools/install.py --no-restart    # Do not close or launch the GUI
  python tools/install.py --prune-legacy  # Verify new install, then prune legacy EasyColab dir
  python tools/install.py --root <dir>    # Redirect all locations under sandbox root
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

EXCLUDED_SITE_PACKAGES_PREFIXES = (
    "pyside6",
    "shiboken6",
    "pyarrow",
    "__pycache__",
)


def run_ps1(script_content: str, args: list[str] | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
    """Run a PowerShell script via pwsh -NoProfile -File (never powershell.exe)."""
    if shutil.which("pwsh") is None:
        raise SystemExit("PowerShell 7 (pwsh) is required: winget install Microsoft.PowerShell")
    with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False, encoding="utf-8") as tf:
        tf.write(script_content)
        tmp_name = tf.name
    try:
        cmd = ["pwsh", "-NoProfile", "-File", tmp_name] + (args or [])
        return subprocess.run(cmd, capture_output=True, text=True, env=env)
    finally:
        try:
            os.remove(tmp_name)
        except Exception:
            pass


def desktop_folder() -> Path:
    """The real Desktop, which OneDrive or a policy may have moved off %USERPROFILE%."""
    if shutil.which("pwsh") is None:
        raise SystemExit("PowerShell 7 (pwsh) is required: winget install Microsoft.PowerShell")
    r = subprocess.run(
        ["pwsh", "-NoProfile", "-Command", "[Environment]::GetFolderPath('Desktop')"],
        capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30,
    )
    found = r.stdout.strip()
    return Path(found) if r.returncode == 0 and found else Path.home() / "Desktop"


class InstallPaths:
    def __init__(self, root: str | None = None):
        self.repo = Path(__file__).resolve().parent.parent
        self.is_sandbox = root is not None
        if root:
            sandbox = Path(root).resolve()
            self.local_appdata = sandbox / "AppData" / "Local"
            self.target_install = sandbox / "Programs" / "Colab-yoga"
            self.legacy_install = sandbox / "Programs" / "EasyColab"
            self.target_config = self.local_appdata / "Colab-yoga"
            self.legacy_config = self.local_appdata / "EasyColab"
            self.start_menu = sandbox / "StartMenu"
            self.desktop = sandbox / "Desktop"
        else:
            base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
            self.local_appdata = Path(base)
            self.target_install = self.local_appdata / "Programs" / "Colab-yoga"
            self.legacy_install = self.local_appdata / "Programs" / "EasyColab"
            self.target_config = self.local_appdata / "Colab-yoga"
            self.legacy_config = self.local_appdata / "EasyColab"
            self.start_menu = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
            self.desktop = desktop_folder()

        self.target_exe = self.target_install / "Colab-yoga.exe"
        self.legacy_exe = self.legacy_install / "EasyColab.exe"
        self.console_src = self.repo / "console"


def find_go() -> str | None:
    found = shutil.which("go")
    if found:
        return found
    local = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Go" / "bin" / "go.exe"
    if local.is_file():
        go_dir = str(local.parent)
        if go_dir not in os.environ.get("PATH", ""):
            os.environ["PATH"] = go_dir + os.pathsep + os.environ.get("PATH", "")
        return str(local)
    return None


def server_test_python(paths: InstallPaths) -> str | None:
    """A Python with fastapi for server/test_server.py. The bundled runtime has neither
    fastapi nor pip, so a dev venv is made once under the config dir from a system
    Python (COLAB_YOGA_DEV_PYTHON, else the `py` launcher)."""
    venv = paths.target_config / "dev-venv"
    vpy = venv / "Scripts" / "python.exe"
    reqs = paths.repo / "server" / "requirements-dev.txt"
    quiet = dict(stdin=subprocess.DEVNULL, timeout=600)
    if not vpy.is_file():
        base = os.environ.get("COLAB_YOGA_DEV_PYTHON")
        cmd = [base] if base else ([shutil.which("py"), "-3"] if shutil.which("py") else None)
        if not cmd:
            return None
        print(f"Creating the server-test venv at {venv} ...")
        if subprocess.run(cmd + ["-m", "venv", str(venv)], **quiet).returncode != 0:
            return None
    probe = subprocess.run([str(vpy), "-c", "import fastapi, httpx"], capture_output=True, **quiet)
    if probe.returncode != 0:
        r = subprocess.run([str(vpy), "-m", "pip", "install", "-q", "-r", str(reqs)], **quiet)
        if r.returncode != 0:
            return None
    return str(vpy)


def find_python(paths: InstallPaths) -> str:
    """Find bundled or available python executable."""
    candidates = [
        paths.target_install / "runtime" / "python.exe",
        paths.legacy_install / "_internal" / "runtime" / "python.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "EasyColab" / "_internal" / "runtime" / "python.exe",
        Path(sys.executable),
    ]
    for c in candidates:
        if c.is_file():
            return str(c)
    return sys.executable


# ─── Step (a): Check ─────────────────────────────────────────────────────────────

def check_ast(repo: Path) -> bool:
    ok = True
    py_files: list[Path] = []
    for d in (repo / "bridge", repo / "server"):
        if d.is_dir():
            py_files.extend(d.glob("*.py"))
    for src in sorted(py_files):
        try:
            ast.parse(src.read_text(encoding="utf-8"), filename=str(src))
        except SyntaxError as e:
            print(f"SYNTAX ERROR {src.relative_to(repo)}:{e.lineno}:{e.offset}: {e.msg}")
            ok = False
    print(f"AST check: {'OK' if ok else 'FAILED'} ({len(py_files)} files)")
    return ok


def check_go(console_dir: Path) -> bool:
    go = find_go()
    if not console_dir.is_dir():
        return True
    if not go:
        print("console check: skipped (Go not found)")
        return True
    res_vet = subprocess.run([go, "vet", "./..."], cwd=console_dir)
    if res_vet.returncode != 0:
        print("console: go vet FAILED")
        return False
    print("console: go vet OK")
    res_test = subprocess.run([go, "test", "./..."], cwd=console_dir)
    if res_test.returncode != 0:
        print("console: go test FAILED")
        return False
    print("console: go test OK")
    return True


def run_tests(paths: InstallPaths) -> bool:
    py = find_python(paths)
    ok = True
    bridge_test = paths.repo / "bridge" / "test_bridge.py"
    if bridge_test.is_file():
        print(f"Running {bridge_test.relative_to(paths.repo)} with {py}...")
        r = subprocess.run([py, "-X", "utf8", str(bridge_test)])
        if r.returncode != 0:
            ok = False
            print("bridge/test_bridge.py FAILED")
        else:
            print("bridge/test_bridge.py OK")

    server_test = paths.repo / "server" / "test_server.py"
    if server_test.is_file():
        spy = server_test_python(paths)
        if spy is None:
            print("server/test_server.py FAILED: no Python with fastapi (see server/requirements-dev.txt)")
            return False
        print(f"Running {server_test.relative_to(paths.repo)} with {spy}...")
        r = subprocess.run([spy, "-X", "utf8", str(server_test)])
        if r.returncode != 0:
            ok = False
            print("server/test_server.py FAILED")
        else:
            print("server/test_server.py OK")
    return ok


def run_all_checks(paths: InstallPaths) -> bool:
    if not check_ast(paths.repo):
        return False
    if not check_go(paths.console_src):
        return False
    if not run_tests(paths):
        return False
    return True


# ─── Step (b): Build console ─────────────────────────────────────────────────────

def build_console(paths: InstallPaths, dry_run: bool) -> Path | None:
    if not paths.console_src.is_dir():
        print("console: source directory not found, skipping build")
        return None
    go = find_go()
    if not go:
        print("console: skipped build (Go not found)")
        return None
    if dry_run:
        print("console: [dry-run] would run 'go tool mygo build -skip-installer'")
        return None
    cmd = [go, "tool", "mygo", "build", "-skip-installer"]
    print(f"console: building via '{' '.join(cmd)}'...")
    res = subprocess.run(cmd, cwd=paths.console_src)
    if res.returncode != 0:
        raise RuntimeError("console: mygo build failed")
    dist_dir = paths.console_src / "dist" / "windows-amd64"
    exes = list(dist_dir.glob("*.exe"))
    if not exes:
        raise RuntimeError(f"console: no .exe found in {dist_dir}")
    exes.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    built_exe = exes[0]
    print(f"console: successfully built {built_exe.name} ({built_exe.stat().st_size / (1024*1024):.1f} MB)")
    return built_exe


# ─── Step (c): Lay out install dir ───────────────────────────────────────────────

def copy_file_if_changed(src: Path, dst: Path, dry_run: bool) -> bool:
    if dst.is_file():
        if src.stat().st_size == dst.stat().st_size:
            # Check content
            if src.read_bytes() == dst.read_bytes():
                return False
    if dry_run:
        print(f"[dry-run] copy: {src} -> {dst}")
        return True
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return True


def copy_tree_filtered(src_dir: Path, dst_dir: Path, exclude_prefixes: tuple[str, ...], dry_run: bool) -> int:
    copied = 0
    if not src_dir.is_dir():
        return 0
    for root, dirs, files in os.walk(src_dir):
        rel_dir = Path(root).relative_to(src_dir)
        # Check if current dir is excluded
        top_name = rel_dir.parts[0].lower() if rel_dir.parts else ""
        if any(top_name.startswith(p) for p in exclude_prefixes):
            continue
        # Filter subdirs in-place to avoid descending into excluded directories
        dirs[:] = [d for d in dirs if not any(d.lower().startswith(p) for p in exclude_prefixes)]

        for f in files:
            if any(f.lower().startswith(p) for p in exclude_prefixes):
                continue
            src_file = Path(root) / f
            dst_file = dst_dir / rel_dir / f
            if copy_file_if_changed(src_file, dst_file, dry_run):
                copied += 1
    return copied


def get_dir_size_mb(path: Path) -> float:
    if not path.is_dir():
        return 0.0
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except Exception:
                pass
    return total / (1024 * 1024)


def layout_install_dir(paths: InstallPaths, built_exe: Path | None, dry_run: bool):
    print(f"install: laying out target at {paths.target_install}...")
    if not dry_run:
        paths.target_install.mkdir(parents=True, exist_ok=True)

    # 1. Colab-yoga.exe
    if built_exe and built_exe.is_file():
        copy_file_if_changed(built_exe, paths.target_exe, dry_run)
    elif not paths.target_exe.is_file():
        print("warning: Colab-yoga.exe not present and not built")

    # 2. bridge/
    target_bridge = paths.target_install / "bridge"
    if not dry_run:
        target_bridge.mkdir(parents=True, exist_ok=True)
    bridge_src = paths.repo / "bridge"
    if bridge_src.is_dir():
        for py in bridge_src.glob("*.py"):
            if py.name.startswith("test_"):
                continue
            copy_file_if_changed(py, target_bridge / py.name, dry_run)

    # 3. server/ (server/*.py except tests)
    target_server = paths.target_install / "server"
    if not dry_run:
        target_server.mkdir(parents=True, exist_ok=True)
    server_src = paths.repo / "server"
    if server_src.is_dir():
        for py in server_src.glob("*.py"):
            if py.name.startswith("test_"):
                continue
            copy_file_if_changed(py, target_server / py.name, dry_run)

    # 4. runtime\ and site-packages\
    target_runtime = paths.target_install / "runtime"
    target_sp = paths.target_install / "site-packages"

    # Locate source runtime & site-packages (from legacy install or real local install)
    legacy_runtime = paths.legacy_install / "_internal" / "runtime"
    legacy_sp = paths.legacy_install / "_internal" / "site-packages"

    if not legacy_runtime.is_dir() or not legacy_sp.is_dir():
        # Fallback to real install if sandbox didn't supply them
        real_local = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "EasyColab" / "_internal"
        if (real_local / "runtime").is_dir() and (real_local / "site-packages").is_dir():
            legacy_runtime = real_local / "runtime"
            legacy_sp = real_local / "site-packages"

    if not target_runtime.is_dir() and legacy_runtime.is_dir():
        print(f"install: copying runtime from {legacy_runtime}...")
        copy_tree_filtered(legacy_runtime, target_runtime, (), dry_run)
    elif legacy_runtime.is_dir():
        # Update changed files in runtime
        copy_tree_filtered(legacy_runtime, target_runtime, (), dry_run)

    if not target_sp.is_dir() and legacy_sp.is_dir():
        print(f"install: copying site-packages from {legacy_sp} (excluding PySide6, shiboken6, pyarrow)...")
        copy_tree_filtered(legacy_sp, target_sp, EXCLUDED_SITE_PACKAGES_PREFIXES, dry_run)
    elif legacy_sp.is_dir():
        copy_tree_filtered(legacy_sp, target_sp, EXCLUDED_SITE_PACKAGES_PREFIXES, dry_run)

    if not dry_run and target_runtime.is_dir():
        # Verify copied runtime
        py = target_runtime / "python.exe"
        if py.is_file():
            verify_cmd = [
                str(py), "-X", "utf8", "-c",
                f"import sys; sys.path.insert(0, r'{target_bridge}'); import colab_yoga_bridge; print('bridge verified OK')"
            ]
            env = dict(os.environ)
            if paths.is_sandbox:
                env["LOCALAPPDATA"] = str(paths.local_appdata)
            r = subprocess.run(verify_cmd, capture_output=True, text=True, env=env)
            if r.returncode != 0:
                raise RuntimeError(f"Runtime verification failed:\nstdout: {r.stdout}\nstderr: {r.stderr}")
            print(f"install: {r.stdout.strip()}")


# ─── Step (d): Migrate config once ───────────────────────────────────────────────

def migrate_config(paths: InstallPaths, dry_run: bool):
    if not dry_run:
        paths.target_config.mkdir(parents=True, exist_ok=True)
    for fname in ("settings.json", "keeper.json"):
        src = paths.legacy_config / fname
        dst = paths.target_config / fname
        if src.is_file() and not dst.is_file():
            if dry_run:
                print(f"[dry-run] migrate config: {src} -> {dst}")
            else:
                shutil.copy2(src, dst)
                print(f"config: migrated {fname} to {paths.target_config}")


# ─── Step (e): Legacy shim & prune ───────────────────────────────────────────────

def create_junction(link_path: Path, target_path: Path, dry_run: bool):
    if dry_run:
        print(f"[dry-run] junction: {link_path} -> {target_path}")
        return
    link_path.parent.mkdir(parents=True, exist_ok=True)
    ps = """
param([string]$LinkPath, [string]$TargetPath)
$parent = [System.IO.Path]::GetDirectoryName($LinkPath)
if (-not (Test-Path $parent)) {
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
}
if (Test-Path $LinkPath) {
    Remove-Item -Force $LinkPath
}
New-Item -ItemType Junction -Path $LinkPath -Target $TargetPath | Out-Null
"""
    res = run_ps1(ps, [str(link_path), str(target_path)])
    if res.returncode != 0:
        print(f"warning: failed to create junction {link_path} -> {target_path}: {res.stderr}")
    else:
        print(f"junction: {link_path} -> {target_path}")


FORWARDER_CODE = '''#!/usr/bin/env python3
"""
Forwarder for legacy EasyColab callers (e.g. colab-gpu-llm-service).
Redirects to Colab-yoga's bridge/colab_yoga_bridge.py.
"""
import os
import sys

def _find_bridge() -> str:
    legacy_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidate = os.path.normpath(os.path.join(legacy_dir, "..", "Colab-yoga", "bridge", "colab_yoga_bridge.py"))
    if os.path.isfile(candidate):
        return candidate
    local = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/AppData/Local")
    candidate = os.path.join(local, "Programs", "Colab-yoga", "bridge", "colab_yoga_bridge.py")
    if os.path.isfile(candidate):
        return candidate
    raise FileNotFoundError("Could not find colab_yoga_bridge.py at: " + candidate)

if __name__ == "__main__":
    bridge_py = _find_bridge()
    bridge_dir = os.path.dirname(bridge_py)
    if bridge_dir not in sys.path:
        sys.path.insert(0, bridge_dir)
    import colab_yoga_bridge
    colab_yoga_bridge.run_cli()
'''


def verify_new_install_for_prune(paths: InstallPaths) -> bool:
    py = paths.target_install / "runtime" / "python.exe"
    bridge = paths.target_install / "bridge" / "colab_yoga_bridge.py"
    if not py.is_file() or not bridge.is_file():
        print("verify: python or bridge missing in target install")
        return False
    env = dict(os.environ)
    if paths.is_sandbox:
        env["LOCALAPPDATA"] = str(paths.local_appdata)

    # 1. bridge import
    cmd_import = [
        str(py), "-X", "utf8", "-c",
        f"import sys; sys.path.insert(0, r'{paths.target_install / 'bridge'}'); import colab_yoga_bridge"
    ]
    r1 = subprocess.run(cmd_import, capture_output=True, text=True, env=env)
    if r1.returncode != 0:
        print(f"verify: bridge import failed: {r1.stderr}")
        return False

    # 2. auth_status returns JSON
    cmd_auth = [str(py), "-X", "utf8", str(bridge), "auth_status"]
    r2 = subprocess.run(cmd_auth, capture_output=True, text=True, env=env)
    try:
        data = json.loads(r2.stdout.strip().splitlines()[-1])
        if not isinstance(data, dict):
            print("verify: auth_status output is not a JSON dict")
            return False
    except Exception as e:
        print(f"verify: auth_status failed to parse as JSON: {e}, stdout: {r2.stdout}")
        return False
    print("verify: new install successfully verified (bridge import + auth_status JSON)")
    return True


def is_junction(path: Path) -> bool:
    """os.path.isjunction needs Python 3.12; read the reparse-point attribute instead."""
    try:
        attrs = os.lstat(path).st_file_attributes
    except (OSError, AttributeError):
        return False
    return bool(attrs & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT


def replace_dir_with_junction(link: Path, target: Path, dry_run: bool):
    if dry_run:
        print(f"[dry-run] shim: replace real dir {link} with a junction to {target}")
        return
    aside = link.with_name(link.name + ".pre-colab-yoga")
    link.rename(aside)
    create_junction(link, target, dry_run)
    probe = link / "python.exe" if link.name == "runtime" else link
    if is_junction(link) and probe.exists():
        _force_rmtree(aside)
        print(f"shim: replaced {link} with a junction")
        return
    if link.exists() or is_junction(link):
        os.rmdir(link)
    aside.rename(link)
    raise RuntimeError(f"junction {link} -> {target} did not work; the real directory was put back")


def _force_rmtree(path: Path):
    def _onerror(func, p, exc_info):
        try:
            os.chmod(p, 0o777)
            func(p)
        except Exception:
            pass
    shutil.rmtree(path, onerror=_onerror)


def setup_legacy_shim(paths: InstallPaths, prune: bool, dry_run: bool):
    internal = paths.legacy_install / "_internal"
    if not dry_run:
        internal.mkdir(parents=True, exist_ok=True)

    # Copy EasyColab.exe from Colab-yoga.exe
    if paths.target_exe.is_file():
        copy_file_if_changed(paths.target_exe, paths.legacy_exe, dry_run)

    # Write forwarder colab_bridge.py
    forwarder_dst = internal / "colab_bridge.py"
    if dry_run:
        print(f"[dry-run] write forwarder: {forwarder_dst}")
    else:
        forwarder_dst.write_text(FORWARDER_CODE, encoding="utf-8")
        print(f"forwarder: written {forwarder_dst}")

    # Prune legacy if requested
    if prune:
        if not dry_run:
            if not verify_new_install_for_prune(paths):
                raise RuntimeError("Pruning aborted: new install verification failed")

        print("prune: removing legacy files while preserving runners JSON and repo...")
        # Items to preserve in legacy_install
        keep_in_legacy = {"EasyColab.exe", "_internal", "repo"}
        keep_in_internal = {"colab_kernel_runners.json", "colab_bridge.py", "runtime", "site-packages"}

        if paths.legacy_install.is_dir():
            for item in paths.legacy_install.iterdir():
                if item.name in keep_in_legacy:
                    continue
                if dry_run:
                    print(f"[dry-run] prune: remove {item}")
                else:
                    if item.is_dir():
                        _force_rmtree(item)
                    else:
                        try:
                            os.chmod(item, 0o777)
                        except Exception:
                            pass
                        item.unlink()

        if internal.is_dir():
            for item in internal.iterdir():
                if item.name in keep_in_internal:
                    continue
                if dry_run:
                    print(f"[dry-run] prune: remove {item}")
                else:
                    if item.is_dir():
                        _force_rmtree(item)
                    else:
                        try:
                            os.chmod(item, 0o777)
                        except Exception:
                            pass
                        item.unlink()

    # Junctions. A real directory (the pre-Colab-yoga runtime) is only replaced with
    # --prune-legacy, and only after the junction is in place and works.
    for name in ("runtime", "site-packages"):
        link = internal / name
        target = paths.target_install / name
        if not link.exists() or is_junction(link):
            create_junction(link, target, dry_run)
        elif not prune:
            print(f"shim: {link} is still a real directory; left in place (--prune-legacy replaces it)")
        else:
            replace_dir_with_junction(link, target, dry_run)


# ─── Step (f): Shortcuts ─────────────────────────────────────────────────────────

def setup_shortcuts(paths: InstallPaths, prune: bool, dry_run: bool):
    if not paths.target_exe.is_file() and not dry_run:
        return
    ps_create = """
param(
    [string]$LnkPath,
    [string]$TargetPath,
    [string]$WorkingDir,
    [string]$Description,
    [string]$IconLocation
)
$dir = [System.IO.Path]::GetDirectoryName($LnkPath)
if (-not (Test-Path $dir)) {
    New-Item -ItemType Directory -Path $dir -Force | Out-Null
}
$s = (New-Object -ComObject WScript.Shell).CreateShortcut($LnkPath)
$s.TargetPath = $TargetPath
$s.WorkingDirectory = $WorkingDir
$s.Description = $Description
$s.IconLocation = "$IconLocation,0"
$s.Save()
"""
    for shortcut_dir in (paths.start_menu, paths.desktop):
        lnk = shortcut_dir / "Colab-yoga.lnk"
        if dry_run:
            print(f"[dry-run] shortcut: create {lnk}")
        else:
            shortcut_dir.mkdir(parents=True, exist_ok=True)
            res = run_ps1(
                ps_create,
                [str(lnk), str(paths.target_exe), str(paths.target_install), "Colab-yoga Console", str(paths.target_exe)],
            )
            if res.returncode == 0:
                print(f"shortcut: created {lnk}")
            else:
                print(f"warning: failed to create shortcut {lnk}: {res.stderr}")

    if prune:
        ps_prune = """
param([string]$ProgramsDir)
foreach ($name in @("EasyColab Yoga.lnk", "EasyColab.lnk")) {
    $p = Join-Path $ProgramsDir $name
    if (Test-Path $p) {
        Remove-Item -Force $p
    }
}
"""
        if dry_run:
            print(f"[dry-run] shortcut: prune old shortcuts in {paths.start_menu}")
        else:
            run_ps1(ps_prune, [str(paths.start_menu)])


# ─── Step (g): Restart console & keeper ──────────────────────────────────────────

def restart_keeper_if_needed(paths: InstallPaths, dry_run: bool):
    keeper_json = paths.target_config / "keeper.json"
    if not keeper_json.is_file():
        return
    try:
        st = json.loads(keeper_json.read_text(encoding="utf-8"))
    except Exception:
        return
    pid = int(st.get("pid") or 0)
    if pid and not st.get("exit_reason"):
        if dry_run:
            print(f"[dry-run] keeper: would stop PID {pid}")
            return
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
        st.update(exit_reason="install_restart", ended=time.time())
        keeper_json.write_text(json.dumps(st), encoding="utf-8")
        print(f"keeper: stopped PID {pid} (bridge updated; console will respawn)")


def get_gui_pids(exe_names: list[str]) -> list[int]:
    pids = []
    for name in exe_names:
        ps = f"Get-CimInstance Win32_Process -Filter \"Name='{name}'\" | Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
        res = run_ps1(ps)
        out = res.stdout.strip()
        if not out:
            continue
        try:
            data = json.loads(out)
            rows = data if isinstance(data, list) else [data]
            for r in rows:
                pids.append(int(r["ProcessId"]))
        except Exception:
            pass
    return pids


CONSOLE_EXES = ["Colab-yoga.exe", "EasyColab Yoga.exe", "EasyColab-Yoga.exe"]


def close_consoles(paths: InstallPaths, no_restart: bool, dry_run: bool) -> bool:
    """Close running consoles before their exe is replaced (Windows locks a running exe).
    WM_CLOSE first so the window saves its bounds; the app then sits in the tray, so it is
    ended after a moment. The keeper is a separate process and keeps the GPU alive."""
    if no_restart or dry_run or paths.is_sandbox:
        return False
    running_pids = get_gui_pids(CONSOLE_EXES)
    if not running_pids:
        return False
    print(f"console: closing running instances (PIDs: {running_pids})...")
    for pid in running_pids:
        subprocess.run(["taskkill", "/PID", str(pid)], capture_output=True)
    time.sleep(2)
    for pid in running_pids:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
    deadline = time.time() + 10
    while get_gui_pids(CONSOLE_EXES) and time.time() < deadline:
        time.sleep(0.5)
    return True


def relaunch_console(paths: InstallPaths, was_running: bool):
    if was_running and paths.target_exe.is_file():
        print(f"console: launching {paths.target_exe.name}...")
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen([str(paths.target_exe)], cwd=str(paths.target_install), creationflags=flags, close_fds=True)


# ─── Main ────────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Colab-yoga Installer")
    parser.add_argument("--check", action="store_true", help="Perform checks only and exit")
    parser.add_argument("--dry-run", action="store_true", help="Show actions without modifying files")
    parser.add_argument("--no-restart", action="store_true", help="Do not close or restart running GUI")
    parser.add_argument("--prune-legacy", action="store_true", help="Prune the legacy EasyColab directory")
    parser.add_argument("--root", type=str, default=None, help="Sandbox root directory")
    args = parser.parse_args(argv)

    paths = InstallPaths(args.root)
    print(f"Colab-yoga installer: target={paths.target_install}, sandbox={paths.is_sandbox}")

    # (a) Check
    checks_passed = run_all_checks(paths)
    if not checks_passed:
        print("Check phase FAILED")
        return 1
    if args.check:
        print("Check phase PASSED (--check stops here)")
        return 0

    # (b) Build console
    built_exe = build_console(paths, args.dry_run)

    # (c) Lay out install directory (running consoles closed first, relaunched in (g))
    was_running = close_consoles(paths, args.no_restart, args.dry_run)
    layout_install_dir(paths, built_exe, args.dry_run)

    # (d) Migrate config once
    migrate_config(paths, args.dry_run)

    # (e) Legacy shim & prune
    setup_legacy_shim(paths, args.prune_legacy, args.dry_run)

    # (f) Shortcuts
    setup_shortcuts(paths, args.prune_legacy, args.dry_run)

    # (g) Restart keeper & console
    restart_keeper_if_needed(paths, args.dry_run)
    relaunch_console(paths, was_running)

    # Report sizes
    rt_mb = get_dir_size_mb(paths.target_install / "runtime")
    sp_mb = get_dir_size_mb(paths.target_install / "site-packages")
    total_mb = rt_mb + sp_mb
    print(
        f"\nInstalled size: runtime={rt_mb:.1f} MB, site-packages={sp_mb:.1f} MB, "
        f"total={total_mb:.1f} MB (vs legacy 283 MB, saved {283 - total_mb:.1f} MB)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
