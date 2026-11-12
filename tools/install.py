#!/usr/bin/env python3
"""
Installer for Colab-yoga.

Builds the console executable, prepares the bundled Python runtime and its packages
(downloaded and pinned, nothing is copied from another application), lays out the install
directory, migrates the old settings once, retires the old EasyColab compatibility layer
if one is found, manages Windows shortcuts, and closes/restarts the console GUI.

Run it with a Python 3.10+ that has pip (python.org); that Python only runs this
script and pip. The installed app runs on its own runtime\\python.exe.

Usage:
  python tools/install.py                 # Full install / sync
  python tools/install.py --check         # Prepare runtime + dev-venv, run AST/go vet/go test/unit tests, stop
  python tools/install.py --dry-run       # Show actions without modifying files
  python tools/install.py --no-restart    # Do not close or launch the GUI
  python tools/install.py --root <dir>    # Redirect all locations under a sandbox root
  python tools/install.py --embed-zip <f> # Use a local copy of the pinned embeddable zip
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

# The runtime: python.org's Windows embeddable CPython. 3.12.10 is the last 3.12 release
# that ships Windows binaries. The SHA-256 was computed from the download, whose MD5
# matches the one python.org lists for it (fe8ef205f2e9c3ba44d0cf9954e1abd3).
PYTHON_VERSION = "3.12.10"
PYTHON_TAG = "312"
PYTHON_EMBED_URL = f"https://www.python.org/ftp/python/{PYTHON_VERSION}/python-{PYTHON_VERSION}-embed-amd64.zip"
PYTHON_EMBED_SHA256 = "4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3"

# What may sit in the old EasyColab dir when it is only the compatibility layer that an
# earlier Colab-yoga installer made there.
LEGACY_TOP = {"easycolab.exe", "_internal", "repo"}
LEGACY_INTERNAL = {"runtime", "site-packages", "colab_bridge.py", "colab_kernel_runners.json"}


def run_ps1(script_content: str, args: list[str] | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
    """Run a PowerShell script via pwsh -NoProfile -File (never powershell.exe)."""
    if shutil.which("pwsh") is None:
        raise SystemExit("PowerShell 7 (pwsh) is required: winget install Microsoft.PowerShell")
    with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False, encoding="utf-8") as tf:
        tf.write(script_content)
        tmp_name = tf.name
    try:
        cmd = ["pwsh", "-NoProfile", "-File", tmp_name] + (args or [])
        return subprocess.run(cmd, capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL)
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
    def __init__(self, root: str | None = None, cache_dir: str | Path | None = None):
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
        self.console_src = self.repo / "console"
        # Downloaded archives are kept here, so a second install does not download again.
        self.cache_dir = Path(cache_dir) if cache_dir else self.target_config / "cache"


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


def host_pythons() -> list[list[str]]:
    """Command lines of Pythons on this PC that may have pip and venv, best first."""
    out: list[list[str]] = [[sys.executable]]
    if shutil.which("py"):
        out.append(["py", "-3"])
    if shutil.which("python"):
        out.append(["python"])
    return out


def python_can(cmd: list[str], code: str) -> bool:
    try:
        r = subprocess.run(cmd + ["-c", code], capture_output=True, stdin=subprocess.DEVNULL, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def find_pip_python() -> list[str] | None:
    for cmd in host_pythons():
        if python_can(cmd, "import pip"):
            return cmd
    return None


def server_test_python(paths: InstallPaths) -> str | None:
    """A Python with fastapi for server/test_server.py. The bundled runtime has neither
    fastapi nor pip, so a dev venv is made once under the config dir from a host Python
    (COLAB_YOGA_DEV_PYTHON, else this one, else the `py` launcher)."""
    venv = paths.target_config / "dev-venv"
    vpy = venv / "Scripts" / "python.exe"
    reqs = paths.repo / "server" / "requirements-dev.txt"
    quiet = dict(stdin=subprocess.DEVNULL, timeout=600)
    if not vpy.is_file():
        base = os.environ.get("COLAB_YOGA_DEV_PYTHON")
        candidates = [[base]] if base else host_pythons()
        cmd = next((c for c in candidates if python_can(c, "import venv, ensurepip")), None)
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
    """The bundled runtime when it exists (it is made first), else the running Python."""
    bundled = paths.target_install / "runtime" / "python.exe"
    return str(bundled) if bundled.is_file() else sys.executable


# ─── Runtime and packages (self-contained: downloaded, hash-pinned) ──────────────

def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download_verified(url: str, sha256: str, dest: Path) -> None:
    """Download url to dest (via a .part file), keeping it only if its SHA-256 matches."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    last_err: Exception | None = None
    for attempt in range(1, 4):
        try:
            print(f"runtime: downloading {url} (attempt {attempt}) ...")
            req = urllib.request.Request(url, headers={"User-Agent": "Colab-yoga-installer"})
            with urllib.request.urlopen(req, timeout=60) as resp, open(part, "wb") as out:
                shutil.copyfileobj(resp, out, 1 << 20)
            got = sha256_of(part)
            if got != sha256:
                raise RuntimeError(f"SHA-256 mismatch for {url}: expected {sha256}, got {got}")
            os.replace(part, dest)
            return
        except Exception as e:  # network errors and hash mismatch both retry, then fail loudly
            last_err = e
            print(f"runtime: download failed: {e}")
            time.sleep(2 * attempt)
    try:
        part.unlink()
    except OSError:
        pass
    raise RuntimeError(f"could not download a verified {dest.name}: {last_err}\n"
                       f"Download it yourself from {url} and pass it with --embed-zip.")


def safe_extract(zip_path: Path, dest: Path) -> None:
    root = dest.resolve()
    with zipfile.ZipFile(zip_path) as z:
        for name in z.namelist():
            target = (dest / name).resolve()
            if root != target and root not in target.parents:
                raise RuntimeError(f"unsafe path in {zip_path.name}: {name}")
        z.extractall(dest)


def ensure_runtime(paths: InstallPaths, dry_run: bool, embed_zip: str | None = None) -> bool:
    """Make <install>\\runtime from the pinned embeddable CPython unless it is there already.
    Returns True when it had to be created."""
    runtime = paths.target_install / "runtime"
    if (runtime / "python.exe").is_file():
        return False
    if dry_run:
        print(f"[dry-run] runtime: would download CPython {PYTHON_VERSION} embeddable into {runtime}")
        return True
    if embed_zip:
        archive = Path(embed_zip)
        got = sha256_of(archive)
        if got != PYTHON_EMBED_SHA256:
            raise RuntimeError(f"{archive} is not the pinned {PYTHON_EMBED_URL.rsplit('/', 1)[1]}: SHA-256 {got}")
    else:
        archive = paths.cache_dir / PYTHON_EMBED_URL.rsplit("/", 1)[1]
        if not (archive.is_file() and sha256_of(archive) == PYTHON_EMBED_SHA256):
            download_verified(PYTHON_EMBED_URL, PYTHON_EMBED_SHA256, archive)
    paths.target_install.mkdir(parents=True, exist_ok=True)
    staging = paths.target_install / "runtime.new"
    if staging.exists():
        _force_rmtree(staging)
    staging.mkdir()
    safe_extract(archive, staging)
    # The embeddable zip ships its ._pth with "import site" commented out. Keep the install
    # dir ("..") and site-packages on the path; site stays on so .pth files are processed.
    (staging / f"python{PYTHON_TAG}._pth").write_text(
        f"python{PYTHON_TAG}.zip\n.\n..\n..\\site-packages\nimport site\n", encoding="ascii"
    )
    if runtime.exists():
        _force_rmtree(runtime)  # an incomplete earlier attempt: it has no python.exe
    os.replace(staging, runtime)
    print(f"runtime: CPython {PYTHON_VERSION} embeddable ready at {runtime}")
    return True


def norm_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def locked_pins(reqs: Path) -> dict[str, str]:
    pins: dict[str, str] = {}
    for line in reqs.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s\\;]+)", line)
        if m:
            pins[norm_name(m.group(1))] = m.group(2)
    return pins


def installed_pins(site_packages: Path) -> dict[str, str]:
    pins: dict[str, str] = {}
    if not site_packages.is_dir():
        return pins
    for d in site_packages.glob("*.dist-info"):
        m = re.match(r"^(.+?)-([^-]+)\.dist-info$", d.name)
        if m:
            pins[norm_name(m.group(1))] = m.group(2)
    return pins


def ensure_site_packages(paths: InstallPaths, dry_run: bool, before_replace=None) -> bool:
    """Make <install>\\site-packages match bridge/requirements.txt, installing with the
    pip of a host Python (cross-targeting CPython 3.12 / win_amd64, wheels only, every
    wheel checked against its pinned hash). Returns True when it had to install."""
    reqs = paths.repo / "bridge" / "requirements.txt"
    sp = paths.target_install / "site-packages"
    want = locked_pins(reqs)
    if not want:
        raise RuntimeError(f"{reqs} lists no pinned packages")
    if installed_pins(sp) == want and (sp / "colab_cli").is_dir():
        return False
    if dry_run:
        print(f"[dry-run] site-packages: would install {len(want)} pinned packages into {sp}")
        return True
    pip_py = find_pip_python()
    if pip_py is None:
        raise RuntimeError(
            "site-packages needs installing but no Python with pip was found. Run this script "
            "with a Python 3.10+ that has pip (e.g. 'py -3 tools/install.py') or put one on PATH."
        )
    staging = paths.target_install / "site-packages.new"
    if staging.exists():
        _force_rmtree(staging)
    paths.target_install.mkdir(parents=True, exist_ok=True)
    cmd = pip_py + [
        "-m", "pip", "install", "--disable-pip-version-check", "--no-compile",
        "--target", str(staging),
        "--platform", "win_amd64", "--python-version", f"{PYTHON_TAG[0]}.{PYTHON_TAG[1:]}",
        "--only-binary=:all:", "--no-deps", "--require-hashes", "-r", str(reqs),
    ]
    print(f"site-packages: installing {len(want)} pinned packages with {' '.join(pip_py)} -m pip ...")
    r = subprocess.run(cmd, stdin=subprocess.DEVNULL)
    if r.returncode != 0:
        if staging.exists():
            _force_rmtree(staging)
        raise RuntimeError("pip install of bridge/requirements.txt failed (see the output above)")
    # pip --target also writes console-script launchers into bin\; the bridge uses none.
    for junk in ("bin", "__pycache__"):
        if (staging / junk).exists():
            _force_rmtree(staging / junk)
    got = installed_pins(staging)
    if got != want:
        _force_rmtree(staging)
        raise RuntimeError(f"pip installed {len(got)} packages, expected {len(want)} from the lock")
    if before_replace:
        before_replace()
    if sp.exists():
        _force_rmtree(sp)
    os.replace(staging, sp)
    print(f"site-packages: {len(want)} packages installed at {sp}")
    return True


# ─── Step (a): Check ─────────────────────────────────────────────────────────────

def check_ast(repo: Path) -> bool:
    ok = True
    py_files: list[Path] = []
    for d in (repo / "bridge", repo / "server", repo / "tools"):
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
        r = subprocess.run([py, "-X", "utf8", str(bridge_test)], stdin=subprocess.DEVNULL)
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
        r = subprocess.run([spy, "-X", "utf8", str(server_test)], stdin=subprocess.DEVNULL)
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
            if src.read_bytes() == dst.read_bytes():
                return False
    if dry_run:
        print(f"[dry-run] copy: {src} -> {dst}")
        return True
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return True


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


def verify_bridge(install_dir: Path, local_appdata: Path | None, write_bytecode: bool = True) -> str:
    """Import the bridge with the install's own runtime; returns its one-line OK message."""
    py = install_dir / "runtime" / "python.exe"
    bridge_dir = install_dir / "bridge"
    flags = ["-X", "utf8"] + ([] if write_bytecode else ["-B"])
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]); import colab_yoga_bridge; "
        "print('bridge verified OK (Python %s)' % sys.version.split()[0])"
    )
    env = dict(os.environ)
    if local_appdata is not None:
        env["LOCALAPPDATA"] = str(local_appdata)
    if not write_bytecode:
        env["PYTHONDONTWRITEBYTECODE"] = "1"
    r = subprocess.run([str(py)] + flags + ["-c", code, str(bridge_dir)],
                       capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL)
    if r.returncode != 0:
        raise RuntimeError(f"Runtime verification failed:\nstdout: {r.stdout}\nstderr: {r.stderr}")
    return r.stdout.strip()


def layout_install_dir(paths: InstallPaths, built_exe: Path | None, dry_run: bool, write_bytecode: bool = True):
    print(f"install: laying out target at {paths.target_install}...")
    if not dry_run:
        paths.target_install.mkdir(parents=True, exist_ok=True)

    # 1. Colab-yoga.exe
    if built_exe and built_exe.is_file():
        copy_file_if_changed(built_exe, paths.target_exe, dry_run)
    elif not paths.target_exe.is_file():
        print("warning: Colab-yoga.exe not present and not built")

    # 2. bridge/ and 3. server/ (*.py except tests)
    for sub in ("bridge", "server"):
        src_dir = paths.repo / sub
        if not src_dir.is_dir():
            continue
        if not dry_run:
            (paths.target_install / sub).mkdir(parents=True, exist_ok=True)
        for py in src_dir.glob("*.py"):
            if py.name.startswith("test_"):
                continue
            copy_file_if_changed(py, paths.target_install / sub / py.name, dry_run)

    # 4. The bundled runtime must run the bridge.
    if not dry_run:
        msg = verify_bridge(paths.target_install, paths.local_appdata if paths.is_sandbox else None, write_bytecode)
        print(f"install: {msg}")


# ─── Step (d): Migrate config once ───────────────────────────────────────────────

def migrate_config(paths: InstallPaths, dry_run: bool):
    """One-time: settings.json and keeper.json of an earlier EasyColab install are copied
    into the Colab-yoga config dir when it has none of its own. Nothing reads the old
    dir afterwards."""
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


# ─── Step (e): Retire the old EasyColab compatibility layer ──────────────────────

def is_junction(path: Path) -> bool:
    """True for any reparse point (junction or symlink); os.path.isjunction needs 3.12."""
    try:
        attrs = os.lstat(path).st_file_attributes
    except (OSError, AttributeError):
        return False
    return bool(attrs & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT


def remove_link(path: Path):
    """Remove a junction/symlink itself. os.rmdir on a directory reparse point deletes the
    link and never its target; rmtree must not be used on a path that may be one."""
    if not is_junction(path):
        raise RuntimeError(f"refusing to unlink {path}: not a link")
    try:
        os.rmdir(path)
    except OSError:
        os.unlink(path)


def _force_rmtree(path: Path):
    def _onerror(func, p, exc_info):
        try:
            os.chmod(p, 0o777)
            func(p)
        except Exception:
            pass
    shutil.rmtree(path, onerror=_onerror)


def classify_legacy(legacy: Path) -> tuple[bool, str]:
    """(True, "") if the dir holds nothing but the compatibility layer that earlier
    Colab-yoga installers made: EasyColab.exe (a copy), _internal with two links to the
    runtime and packages, the bridge forwarder and the runner scripts, and a link to the
    repo. An original EasyColab install (real runtime and site-packages dirs, anything
    else) is not that."""
    for item in legacy.iterdir():
        low = item.name.lower()
        if low not in LEGACY_TOP:
            return False, f"unexpected entry {item.name}"
        if low == "repo" and not is_junction(item):
            return False, "repo is a real directory"
        if low == "easycolab.exe" and (item.is_dir() or is_junction(item)):
            return False, "EasyColab.exe is not a file"
        if low == "_internal":
            if is_junction(item) or not item.is_dir():
                return False, "_internal is not a plain directory"
            for sub in item.iterdir():
                sl = sub.name.lower()
                if sl not in LEGACY_INTERNAL:
                    return False, f"unexpected entry _internal/{sub.name}"
                if sl in ("runtime", "site-packages") and not is_junction(sub):
                    return False, f"_internal/{sub.name} is a real directory"
    return True, ""


def migrate_runners(src: Path, dst: Path, dry_run: bool):
    """Keep the newer colab_kernel_runners.json (it holds API keys: never printed)."""
    if not src.is_file():
        return
    if dst.is_file() and dst.stat().st_mtime >= src.stat().st_mtime:
        print("runners: the install's colab_kernel_runners.json is current; the old copy is dropped")
        return
    if dry_run:
        print(f"[dry-run] runners: would move {src} -> {dst}")
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    if sha256_of(src) != sha256_of(dst):
        raise RuntimeError("runners: the copy differs from the original; the old dir is kept")
    print(f"runners: colab_kernel_runners.json ({dst.stat().st_size} bytes) moved to {dst}")


def retire_legacy(paths: InstallPaths, dry_run: bool):
    """Migrate the runner scripts, then delete the old Programs\\EasyColab compatibility
    layer. Its links are unlinked first, so the real runtime and packages they point at
    are never touched."""
    legacy = paths.legacy_install
    if not legacy.is_dir():
        return
    ok, why = classify_legacy(legacy)
    if not ok:
        print(f"legacy: {legacy} is not a Colab-yoga compatibility layer ({why}); left in place. "
              "Nothing uses it any more; delete it yourself if you do not need EasyColab.")
        return
    new_ready = (paths.target_install / "runtime" / "python.exe").is_file() and \
        (paths.target_install / "site-packages" / "colab_cli").is_dir() and \
        (paths.target_install / "bridge" / "colab_yoga_bridge.py").is_file()
    if not new_ready and not dry_run:
        print("legacy: the new install is not complete; the old directory is kept")
        return
    migrate_runners(legacy / "_internal" / "colab_kernel_runners.json",
                    paths.target_install / "colab_kernel_runners.json", dry_run)
    if dry_run:
        print(f"[dry-run] legacy: would unlink its links and delete {legacy}")
        return
    for link in (legacy / "repo", legacy / "_internal" / "runtime", legacy / "_internal" / "site-packages"):
        if os.path.lexists(link):
            remove_link(link)
    if not (paths.target_install / "runtime" / "python.exe").is_file() or \
            not (paths.target_install / "site-packages" / "colab_cli").is_dir():
        raise RuntimeError("the install lost its runtime or packages while unlinking; stopping")
    for root, dirs, files in os.walk(legacy):  # nothing left may be a link
        for n in dirs + files:
            if is_junction(Path(root) / n):
                raise RuntimeError(f"{Path(root) / n} is still a link; not deleting {legacy}")
    _force_rmtree(legacy)
    if legacy.exists():
        raise RuntimeError(f"could not delete {legacy} (a file in it is in use? close EasyColab.exe)")
    print(f"legacy: removed the compatibility layer {legacy}")


# ─── Step (f): Shortcuts ─────────────────────────────────────────────────────────

def setup_shortcuts(paths: InstallPaths, dry_run: bool):
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

    # Shortcuts that point into the retired EasyColab directory would dangle.
    ps_prune = """
param([string]$LegacyDir, [string[]]$Dirs)
$sh = New-Object -ComObject WScript.Shell
foreach ($d in $Dirs) {
    if (-not (Test-Path $d)) { continue }
    foreach ($f in Get-ChildItem -Path $d -Filter *.lnk -File) {
        $t = $sh.CreateShortcut($f.FullName).TargetPath
        if ($t -and $t.StartsWith($LegacyDir, [System.StringComparison]::OrdinalIgnoreCase)) {
            Remove-Item -Force $f.FullName
            "removed $($f.FullName)"
        }
    }
}
"""
    if dry_run:
        print(f"[dry-run] shortcut: remove shortcuts pointing into {paths.legacy_install}")
    else:
        res = run_ps1(ps_prune, [str(paths.legacy_install), str(paths.start_menu), str(paths.desktop)])
        for line in res.stdout.splitlines():
            if line.strip():
                print(f"shortcut: {line.strip()}")


# ─── Step (g): Restart console & keeper ──────────────────────────────────────────

def stop_keeper(paths: InstallPaths, dry_run: bool):
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


# EasyColab.exe is the copy the old compatibility layer kept; it must not be running when
# that directory is deleted.
CONSOLE_EXES = ["Colab-yoga.exe", "EasyColab.exe"]


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
    parser.add_argument("--check", action="store_true", help="Prepare the embedded runtime and the dev-venv, run AST, go vet, go test "
                        "and the bridge & server unit tests, then exit")
    parser.add_argument("--dry-run", action="store_true", help="Show actions without modifying files")
    parser.add_argument("--no-restart", action="store_true", help="Do not close or restart running GUI")
    parser.add_argument("--root", type=str, default=None, help="Sandbox root directory")
    parser.add_argument("--embed-zip", type=str, default=None,
                        help="Local copy of the pinned CPython embeddable zip (SHA-256 is still checked)")
    args = parser.parse_args(argv)

    paths = InstallPaths(args.root)
    print(f"Colab-yoga installer: target={paths.target_install}, sandbox={paths.is_sandbox}")

    state = {"closed": False, "was_running": False}

    def quiesce():
        """Close the console and stop the keeper once, before anything in use is replaced."""
        if state["closed"] or args.check:
            return
        state["closed"] = True
        state["was_running"] = close_consoles(paths, args.no_restart, args.dry_run)
        stop_keeper(paths, args.dry_run)

    try:
        # The checks run on the bundled runtime, so it and its packages come first.
        ensure_runtime(paths, args.dry_run, args.embed_zip)
        ensure_site_packages(paths, args.dry_run, before_replace=quiesce)

        # (a) Check
        if not run_all_checks(paths):
            print("Check phase FAILED")
            return 1
        if args.check:
            print("Check phase PASSED (--check stops here)")
            return 0

        # (b) Build console
        built_exe = build_console(paths, args.dry_run)

        # (c) Lay out install directory (running consoles closed first, relaunched at the end)
        quiesce()
        layout_install_dir(paths, built_exe, args.dry_run)

        # (d) Migrate config once
        migrate_config(paths, args.dry_run)

        # (e) Retire the old EasyColab compatibility layer, if any
        retire_legacy(paths, args.dry_run)

        # (f) Shortcuts
        setup_shortcuts(paths, args.dry_run)
    finally:
        if not args.check:
            relaunch_console(paths, state["was_running"])

    rt_mb = get_dir_size_mb(paths.target_install / "runtime")
    sp_mb = get_dir_size_mb(paths.target_install / "site-packages")
    print(f"\nInstalled size: runtime={rt_mb:.1f} MB, site-packages={sp_mb:.1f} MB, total={rt_mb + sp_mb:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
