#!/usr/bin/env python3
"""Cut a release: bump VERSION / backend/pyproject.toml / frontend/package.json, turn the `## [Unreleased]` section of
CHANGELOG.md into `## [<version>] - <today>`, commit, tag, and (optionally) write the tarball.

    scripts/release.py patch            # 0.10.1 -> 0.10.2
    scripts/release.py minor            # 0.10.2 -> 0.11.0
    scripts/release.py 1.0.0            # explicit
    scripts/release.py patch --tar /mnt/user-data/outputs   # also writes avdlab-v<version>.tar.gz there
    scripts/release.py patch --tar DIR --pdf                # ... and avdlab-<version>-documentation.pdf (scripts/docs-pdf)

Rule of the house (0.10.1+): every fix that ships gets its own patch number, so the version badge in the sidebar
(and GET /api/meta) says exactly what is deployed. Put the entry under `## [Unreleased]` while working; this script
renames it.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXCLUDE = ("node_modules", "dist", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", ".venv", ".git", ".work")
EXCLUDE_SUFFIX = (".pyc", ".tsbuildinfo", ".DS_Store")


def current() -> str:
    return (ROOT / "VERSION").read_text().strip()


def bump(kind: str) -> str:
    cur = current()
    if re.fullmatch(r"\d+\.\d+\.\d+", kind):
        return kind
    major, minor, patch = (int(x) for x in cur.split("."))
    if kind == "patch":
        return f"{major}.{minor}.{patch + 1}"
    if kind == "minor":
        return f"{major}.{minor + 1}.0"
    if kind == "major":
        return f"{major + 1}.0.0"
    raise SystemExit(f"unknown bump {kind!r}: patch | minor | major | X.Y.Z")


def write_versions(new: str) -> None:
    (ROOT / "VERSION").write_text(new + "\n")
    p = ROOT / "backend" / "pyproject.toml"
    p.write_text(re.sub(r'^version = "[^"]+"', f'version = "{new}"', p.read_text(), count=1, flags=re.M))
    p = ROOT / "frontend" / "package.json"
    data = json.loads(p.read_text())
    data["version"] = new
    p.write_text(json.dumps(data, indent=2) + "\n")


def roll_changelog(new: str) -> bool:
    p = ROOT / "CHANGELOG.md"
    s = p.read_text()
    if f"## [{new}]" in s:
        return False
    today = dt.date.today().isoformat()
    if "## [Unreleased]" in s:
        s = s.replace("## [Unreleased]", f"## [{new}] - {today}", 1)
    else:
        marker = "\n## ["
        i = s.find(marker)
        s = s[:i] + f"\n## [{new}] - {today}\n\n### Fixed\n- (describe the fix)\n" + s[i:]
    p.write_text(s)
    return True


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True, text=True).stdout.strip()


def make_tar(new: str, out_dir: Path) -> Path:
    out = out_dir / f"avdlab-v{new}.tar.gz"

    def skip(ti: tarfile.TarInfo) -> tarfile.TarInfo | None:
        parts = Path(ti.name).parts
        return None if any(x in EXCLUDE for x in parts) or ti.name.endswith(EXCLUDE_SUFFIX) else ti

    with tarfile.open(out, "w:gz") as tf:
        tf.add(ROOT, arcname="avdlab", filter=skip)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("bump", help="patch | minor | major | X.Y.Z")
    ap.add_argument("--tar", type=Path, help="directory for avdlab-v<version>.tar.gz")
    ap.add_argument("--no-commit", action="store_true")
    ap.add_argument("--pdf", action="store_true", help="also build the documentation PDF next to the tarball (needs --tar)")
    ap.add_argument("-m", "--message", default="", help="extra commit message line")
    a = ap.parse_args()
    new = bump(a.bump)
    write_versions(new)
    roll_changelog(new)
    print(f"version {current()} -> {new}")
    if not a.no_commit:
        git("add", "-A")
        msg = f"release {new}" + (f"\n\n{a.message}" if a.message else "")
        if git("status", "--porcelain"):
            git("commit", "-q", "-m", msg)
        git("tag", "-f", f"v{new}")
        print(f"committed and tagged v{new}")
    if a.tar:
        print("wrote", make_tar(new, a.tar))
        if a.pdf:
            out = a.tar / f"avdlab-{new}-documentation.pdf"
            subprocess.run([sys.executable, str(ROOT / "scripts/docs-pdf/build.py"), "--out", str(out)], check=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
