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
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import install  # noqa: E402

ROOT_NAME = "Colab-yoga"

# Never in a package, wherever they sit in the tree.
FORBIDDEN_NAMES = {"colab_kernel_runners.json", "settings.json", "keeper.json"}
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
- First time: open "Connection & Account" and bind your Google account.
- To start models, colab-gpu-llm-service's colab_kernel.py needs to export its
  colab_kernel_runners.json (it holds your API keys) next to Colab-yoga.exe. This package
  ships without it, on purpose.
- Delete the folder to uninstall; delete %LOCALAPPDATA%\\Colab-yoga to forget settings.

Colab-yoga {version}（Windows x64，免安装）

运行 Colab-yoga.exe 即可，所需的 Python 运行时、依赖、bridge 和 server 都在这个文件夹里。
整个文件夹可以放到任何位置，但不要把 exe 单独拿出去。

- 设置和状态保存在 %LOCALAPPDATA%\\Colab-yoga，不在这个文件夹里。
- 第一次使用：在「连接与账号」页绑定 Google 账号。
- 要启动模型，需要 colab-gpu-llm-service 的 colab_kernel.py 把 colab_kernel_runners.json
  （含你的 API 密钥）导出到 Colab-yoga.exe 旁边。本包不带这个文件，是故意的。
- 卸载：删除这个文件夹；想连设置一起清掉，再删 %LOCALAPPDATA%\\Colab-yoga。

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
    for name, text in (("README.txt", README.format(version=version)),):
        (target / name).write_text(text, encoding="utf-8", newline="\r\n")
    for name in ("LICENSE", "NOTICE"):
        shutil.copy2(paths.repo / name, target / name)
    return target


def zip_tree(src: Path, dest: Path) -> list[str]:
    """Zip src as ROOT_NAME/..., skipping caches and bytecode. Returns the entry names."""
    names: list[str] = []
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for f in sorted(p for p in src.rglob("*") if p.is_file()):
            rel = f.relative_to(src).as_posix()
            if re.search(r"(^|/)__pycache__(/|$)", rel) or rel.endswith((".pyc", ".pyo")):
                continue
            arc = f"{ROOT_NAME}/{rel}"
            z.write(f, arc)
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
        if not exe or not exe.is_file():
            raise SystemExit("no Colab-yoga.exe to package (Go missing? pass --exe)")
        staged = stage(paths, exe, args.embed_zip, version)
        zip_path = out / f"Colab-yoga-{version}-windows-amd64.zip"
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
    (out / (zip_path.name + ".sha256")).write_text(f"{digest} *{zip_path.name}\n", encoding="ascii")
    print(f"package: {zip_path} ({zip_path.stat().st_size / (1024 * 1024):.1f} MB, {len(names)} files)")
    print(f"package: sha256 {digest}")
    print("package: audit OK (no runners/settings/keeper/token/credential files, no .pyc, no secret patterns)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
