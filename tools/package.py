#!/usr/bin/env python3
"""
Builds the release package: a zip that runs from wherever it is unpacked, no installer.

  Colab-yoga-<version>-windows-amd64.zip
    Colab-yoga/
      Colab-yoga.exe
      runtime/            CPython embeddable (pinned and hash-checked by install.py)
      site-packages/      the bridge's pinned packages (bridge/requirements.txt)
      bridge/colab_yoga_bridge.py
      server/colab_yoga_server.py, vm_boot.py
      skills/colab-yoga/    the agent skill (SKILL.md, references/api.md), ready to copy
      README.txt, LICENSE, NOTICE

The package is staged exactly like an install (same code as tools/install.py, in a
temporary sandbox), then zipped. It never carries anything personal: runner scripts, settings,
keeper state, tokens or compiled .pyc files. The zip is audited for that before it is kept.

Usage:
  python tools/package.py --out <dir>             # build the exe, stage, zip, audit
  python tools/package.py --out <dir> --exe <f>   # use an already built Colab-yoga.exe
  python tools/package.py --out <dir> --embed-zip <f>
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import stat
import sys
import tempfile
import time
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import install  # noqa: E402

ROOT_NAME = "Colab-yoga"

# Never in a package, wherever they sit in the tree.
FORBIDDEN_NAMES = {
    "colab_kernel_runners.json",
    "settings.json",
    "keeper.json",
    "install.json",
    "local_mcp_bridge.json",
}
FORBIDDEN_PATTERNS = (
    re.compile(r"(^|/)\.env($|\.)", re.I),
    re.compile(r"(^|/)[^/]*token[^/]*\.json$", re.I),
    re.compile(r"(^|/)[^/]*credentials?[^/]*\.json$", re.I),
    re.compile(r"\.py[co]$", re.I),
    re.compile(r"(^|/)__pycache__(/|$)"),
)
# High-signal secret shapes (Google OAuth access/refresh tokens, Tailscale keys, Hugging Face
# and API keys, private keys). Scanned in every non-binary-looking file of the package.
SECRET_RES = (
    re.compile(rb"ya29\.[0-9A-Za-z_-]{20,}"),
    re.compile(rb"1//[0-9A-Za-z_-]{40,}"),
    re.compile(rb"tskey-[a-z]+-[0-9A-Za-z]{10,}"),
    re.compile(rb"\bhf_[A-Za-z0-9]{30,}"),
    re.compile(rb"\bsk-[A-Za-z0-9_-]{32,}"),
    re.compile(rb"AIza[0-9A-Za-z_-]{35}"),
)
# A whole PEM private key block. Only checked outside site-packages: libraries there carry the
# marker in code and tornado ships a public test key fixture; our own files never should.
PEM_RE = re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----\s*[A-Za-z0-9+/=\s]{80,}")

README = """\
Colab-yoga {version} (Windows x64, no installer)

Run Colab-yoga.exe. Everything it needs is in this folder: the exe, runtime\\ (Python),
site-packages\\, bridge\\ and server\\. Move or copy the whole folder anywhere; do not
move the exe out of it.

- Settings and state are kept in %LOCALAPPDATA%\\Colab-yoga (not in this folder).
- First time: the window opens on a sign-in page; sign in with your Google account there.
- To start models, run `python -X utf8 scripts/colab_kernel.py --all` in
  colab-gpu-llm-service: it writes colab_kernel_runners.json (with your API keys) to
  %LOCALAPPDATA%\\Colab-yoga automatically, so this folder works from anywhere without copying
  files by hand (a file next to Colab-yoga.exe overrides it). This package ships without it.
- For an AI agent (Claude Code, Codex, ...): copy skills\\colab-yoga\\ (the whole folder) into
  the agent's skills directory, e.g. ~/.claude/skills/ or ~/.agents/skills/. It tells the
  agent how to bind, wake, check and release, and references\\api.md has the server API.
- Delete the folder to uninstall; delete %LOCALAPPDATA%\\Colab-yoga to forget settings.

Colab-yoga {version}（Windows x64，免安装）

运行 Colab-yoga.exe 即可，所需的 Python 运行时、依赖、bridge 和 server 都在这个文件夹里。
整个文件夹可以放到任何位置，但不要把 exe 单独拿出去。

- 设置和状态保存在 %LOCALAPPDATA%\\Colab-yoga，不在这个文件夹里。
- 第一次使用：窗口打开就是登录页，在那里用 Google 账号登录。
- 要启动模型，在 colab-gpu-llm-service 中运行 `python -X utf8 scripts/colab_kernel.py --all`：
  它会自动把 colab_kernel_runners.json（含你的 API 密钥）写入 %LOCALAPPDATA%\\Colab-yoga，
  免安装包放在任意位置都能直接读取，无需手动拷文件（放在 Colab-yoga.exe 旁的同名文件优先）。
  本包不带这个文件。
- 给 AI agent（Claude Code、Codex 等）用：把 skills\\colab-yoga\\ 整个文件夹复制到 agent 的
  技能目录（如 ~/.claude/skills/ 或 ~/.agents/skills/）。技能讲清绑定、唤醒、查看、释放怎么做，
  references\\api.md 是服务端接口说明。
- 卸载：删除这个文件夹；想连设置一起清掉，再删 %LOCALAPPDATA%\\Colab-yoga。

Source and docs / 源码与文档: https://github.com/incredible404-rgb/Colab-yoga
License / 许可: Apache License 2.0 (LICENSE, NOTICE)
"""

README_MACOS = """\
Colab-yoga {version} (macOS Apple Silicon arm64, no installer)

Open Colab-yoga.app. Everything it needs is in this folder: Colab-yoga.app, runtime/ (Python),
site-packages/, bridge/ and server/. Move or copy the whole folder anywhere; do not move
Colab-yoga.app out of this folder on its own.

- If macOS Gatekeeper blocks a browser-downloaded zip ("unidentified developer"), run:
    xattr -cr Colab-yoga
  on this unpacked folder, then open Colab-yoga.app again.
- Settings and state are kept in ~/Library/Application Support/Colab-yoga (not in this folder).
- First time: the window opens on a sign-in page; sign in with your Google account there.
- To start models, run `python3 -X utf8 scripts/colab_kernel.py --all` in
  colab-gpu-llm-service: it writes colab_kernel_runners.json (with your API keys) to
  ~/Library/Application Support/Colab-yoga automatically, so this folder works from anywhere
  without copying files by hand (a file next to Colab-yoga.app overrides it). This package ships
  without it.
- For an AI agent (Claude Code, Codex, ...): copy skills/colab-yoga/ (the whole folder) into
  the agent's skills directory, e.g. ~/.claude/skills/ or ~/.agents/skills/.
- Delete the folder to uninstall; delete ~/Library/Application Support/Colab-yoga to forget settings.

Colab-yoga {version}（macOS Apple Silicon arm64，免安装）

双击打开 Colab-yoga.app 即可，所需的 Python 运行时、依赖、bridge 和 server 都在这个文件夹里。
整个文件夹可以放到任何位置，但不要把 Colab-yoga.app 单独拖出这个文件夹。

- 若从浏览器下载后首次打开被 macOS Gatekeeper 拦截，在终端对解压出的文件夹执行一次：
    xattr -cr Colab-yoga
  再双击打开 Colab-yoga.app 即可。
- 设置和状态保存在 ~/Library/Application Support/Colab-yoga，不在这个文件夹里。
- 第一次使用：窗口打开就是登录页，在那里用 Google 账号登录。
- 要启动模型，在 colab-gpu-llm-service 中运行 `python3 -X utf8 scripts/colab_kernel.py --all`：
  它会自动把 colab_kernel_runners.json（含你的 API 密钥）写入 ~/Library/Application Support/Colab-yoga，
  免安装包放在任意位置都能直接读取，无需手动拷文件（放在 Colab-yoga.app 旁的同名文件优先）。
  本包不带这个文件。
- 给 AI agent（Claude Code、Codex 等）用：把 skills/colab-yoga/ 整个文件夹复制到 agent 的
  技能目录（如 ~/.claude/skills/ 或 ~/.agents/skills/）。
- 卸载：删除这个文件夹；想连设置一起清掉，再删 ~/Library/Application Support/Colab-yoga。

Source and docs / 源码与文档: https://github.com/incredible404-rgb/Colab-yoga
License / 许可: Apache License 2.0 (LICENSE, NOTICE)
"""


def console_version(repo: Path) -> str:
    return json.loads((repo / "console" / "mygo.json").read_text(encoding="utf-8"))["version"]


def forbidden(rel: str) -> str | None:
    low = rel.lower()
    if low.rsplit("/", 1)[-1] in FORBIDDEN_NAMES:
        return "personal/config file"
    for pat in FORBIDDEN_PATTERNS:
        if pat.search(low):
            return "credential-like or compiled file"
    return None


def stage(paths: install.InstallPaths, exe: Path, embed_zip: str | None, version: str) -> Path:
    """Lay out the package in the sandbox install dir and return it."""
    install.ensure_runtime(paths, False, embed_zip)
    install.ensure_site_packages(paths, False)
    install.layout_install_dir(paths, exe, False, write_bytecode=False)
    target = paths.target_install
    readme_tpl = README_MACOS if sys.platform == "darwin" else README
    readme_nl = "\n" if sys.platform == "darwin" else "\r\n"
    for name, text in (("README.txt", readme_tpl.format(version=version)),):
        with open(target / name, "w", encoding="utf-8", newline=readme_nl) as f:
            f.write(text)
    for name in ("LICENSE", "NOTICE"):
        shutil.copy2(paths.repo / name, target / name)
    skill_src = paths.repo / "skills" / "colab-yoga"
    if not (skill_src / "SKILL.md").is_file():
        raise SystemExit(f"package: {skill_src} has no SKILL.md")
    shutil.copytree(
        skill_src, target / "skills" / "colab-yoga", dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    return target


def zip_tree(src: Path, dest: Path) -> list[str]:
    """Zip src as ROOT_NAME/..., skipping caches and bytecode while preserving POSIX symlinks
    and executable permissions. Returns the entry names."""
    entries: list[Path] = []
    for root, dirs, files in os.walk(src, followlinks=False):
        root_p = Path(root)
        kept_dirs: list[str] = []
        for d in sorted(dirs):
            if d == "__pycache__":
                continue
            dp = root_p / d
            if dp.is_symlink():
                entries.append(dp)
            else:
                kept_dirs.append(d)
        dirs[:] = kept_dirs
        for fn in sorted(files):
            if fn.endswith((".pyc", ".pyo")):
                continue
            entries.append(root_p / fn)
    entries.sort(key=lambda p: p.relative_to(src).as_posix())

    names: list[str] = []
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for p in entries:
            rel = p.relative_to(src).as_posix()
            if re.search(r"(^|/)__pycache__(/|$)", rel) or rel.endswith((".pyc", ".pyo")):
                continue
            arc = f"{ROOT_NAME}/{rel}"
            if p.is_symlink():
                st = os.lstat(p)
                zi = zipfile.ZipInfo(arc, time.localtime(st.st_mtime)[:6])
                zi.create_system = 3  # Unix
                zi.external_attr = (stat.S_IFLNK | 0o777) << 16
                z.writestr(zi, os.readlink(p).encode("utf-8"), compress_type=zipfile.ZIP_STORED)
            else:
                z.write(p, arc)
            names.append(arc)
    return names


def audit(zip_path: Path) -> list[str]:
    """Problems found in the zip: forbidden names, or secret-shaped content."""
    problems: list[str] = []
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            rel = info.filename.split("/", 1)[1] if "/" in info.filename else info.filename
            why = forbidden(rel)
            if why:
                problems.append(f"{info.filename}: {why}")
                continue
            if info.file_size > 8 * 1024 * 1024:
                continue
            data = z.read(info)
            if b"\0" in data[:4096]:
                continue  # binary
            for pat in SECRET_RES + (() if "/site-packages/" in "/" + rel else (PEM_RE,)):
                if pat.search(data):
                    problems.append(f"{info.filename}: matches secret pattern {pat.pattern[:30]!r}")
                    break
    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Build the Colab-yoga release zip")
    parser.add_argument("--out", required=True, help="Output directory (outside the repo)")
    parser.add_argument("--exe", default=None, help="Use this Colab-yoga.exe instead of building one")
    parser.add_argument("--embed-zip", default=None, help="Local copy of the pinned CPython embeddable zip")
    args = parser.parse_args(argv)

    repo = Path(__file__).resolve().parent.parent
    out = Path(args.out).resolve()
    if repo == out or repo in out.parents:
        raise SystemExit(f"--out must be outside the repo ({repo})")
    out.mkdir(parents=True, exist_ok=True)
    version = console_version(repo)

    with tempfile.TemporaryDirectory(prefix="colab-yoga-pkg-") as tmp:
        paths = install.InstallPaths(tmp, cache_dir=out / ".cache")
        if args.exe:
            exe = Path(args.exe).resolve()
        else:
            exe = install.build_console(paths, False)
        if not exe or not install.app_binary_exists(exe):
            want = "Colab-yoga.app" if sys.platform == "darwin" else "Colab-yoga.exe"
            raise SystemExit(f"no {want} to package (Go missing? pass --exe)")
        staged = stage(paths, exe, args.embed_zip, version)
        plat_tag = f"darwin-{platform.machine()}" if sys.platform == "darwin" else "windows-amd64"
        zip_path = out / f"Colab-yoga-{version}-{plat_tag}.zip"
        if zip_path.exists():
            zip_path.unlink()
        names = zip_tree(staged, zip_path)

    problems = audit(zip_path)
    if problems:
        zip_path.unlink()
        print("package: audit FAILED, zip removed:")
        for p in problems:
            print("  " + p)
        return 1
    digest = install.sha256_of(zip_path)
    with open(out / (zip_path.name + ".sha256"), "w", encoding="ascii", newline="\n") as f:  # LF, so `sha256sum -c` works on every OS
        f.write(f"{digest} *{zip_path.name}\n")
    print(f"package: {zip_path} ({zip_path.stat().st_size / (1024 * 1024):.1f} MB, {len(names)} files)")
    print(f"package: sha256 {digest}")
    print("package: audit OK (no runners/settings/keeper/token/credential files, no .pyc, no secret patterns)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
