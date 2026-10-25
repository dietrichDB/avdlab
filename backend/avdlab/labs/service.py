"""Lab directories on disk (standard AVD layout) with a git repository per lab.

All functions are synchronous filesystem/git helpers; API handlers and jobs call them via `asyncio.to_thread`
when they may take a while (commits of big builds).
"""
from __future__ import annotations

import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from git import Actor, Repo

from ..core.config import settings
from ..core.db import Lab

GIT_AUTHOR = Actor("avdlab", "avdlab@localhost")
GITIGNORE = """# avdlab lab repository
*.pyc
__pycache__/
.ansible/
anta/reports/
clab/ceos-config/
*.token
.git-credentials
.avdlab-*
"""
# intended/ and documentation/ stay committed on purpose (useful in the remote)
GITIGNORE_REQUIRED = [ln for ln in GITIGNORE.splitlines() if ln and not ln.startswith("#")]
# files/dirs that are managed by knobs; a hand edit marks the lab's knobs as "custom"
KNOB_MANAGED = ("inventory.yml", "group_vars/")
# path prefixes exposed for reading/editing through the files API
EDITABLE_PREFIXES = ("inventory.yml", "ansible.cfg", "group_vars/", "host_vars/", "playbooks/", "anta/user_catalogs/")
READ_ONLY_PREFIXES = ("intended/", "documentation/", "clab/", "anta/")
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    slug = _SLUG_RE.sub("-", name.lower()).strip("-")
    return slug[:64] or "lab"


def lab_dir(lab: Lab | str) -> Path:
    slug = lab if isinstance(lab, str) else lab.slug
    return settings.labs_dir / slug


def _repo(path: Path) -> Repo:
    return Repo(path)


def init_lab_dir(lab: Lab | str, files: dict[str, str], message: str = "initial render") -> str:
    """Create the directory, `git init`, write files and commit. Returns the commit sha."""
    root = lab_dir(lab)
    root.mkdir(parents=True, exist_ok=True)
    if not (root / ".git").exists():
        repo = Repo.init(root, initial_branch="main")
        with repo.config_writer() as cw:
            cw.set_value("user", "name", GIT_AUTHOR.name)
            cw.set_value("user", "email", GIT_AUTHOR.email)
        (root / ".gitignore").write_text(GITIGNORE)
    for sub in ("group_vars", "playbooks", "anta/user_catalogs", "intended", "documentation", "clab"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return write_files(lab, files, message)


def write_files(lab: Lab | str, files: dict[str, str], message: str, *, prune_group_vars: bool = False) -> str:
    """Write files (relative paths) and commit. With prune_group_vars, group_vars files not in `files` are removed
    (used when a re-render drops a group). Returns the commit sha ('' if nothing changed)."""
    root = lab_dir(lab)
    if prune_group_vars and any(p.startswith("group_vars/") for p in files):  # custom labs render nothing: never prune
        keep = {p for p in files if p.startswith("group_vars/")}
        for f in list((root / "group_vars").rglob("*")):
            rel = str(f.relative_to(root))
            if f.is_file() and rel not in keep:
                f.unlink()
        for d in sorted((root / "group_vars").rglob("*"), reverse=True):
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
    for rel, content in files.items():
        p = _safe(root, rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return commit_all(root, message)


def commit_all(root: Path, message: str) -> str:
    repo = _repo(root)
    repo.git.add(A=True)
    if not repo.is_dirty(untracked_files=True) and repo.head.is_valid():
        return ""
    if repo.head.is_valid() and not repo.index.diff("HEAD") and not repo.untracked_files:
        return ""
    commit = repo.index.commit(message, author=GIT_AUTHOR, committer=GIT_AUTHOR)
    return commit.hexsha


def ensure_gitignore(root: Path) -> bool:
    """Make sure the lab's .gitignore carries every avdlab entry (older labs, imported/cloned repos) and untrack
    files that are now ignored (clab/ceos-config). Does not commit. Returns True when something changed."""
    gi = root / ".gitignore"
    lines = gi.read_text(encoding="utf-8").splitlines() if gi.exists() else []
    missing = [ln for ln in GITIGNORE_REQUIRED if ln not in lines]
    changed = False
    if missing:
        body = "\n".join(lines).rstrip("\n")
        gi.write_text((body + "\n" if body else "") + "# avdlab\n" + "\n".join(missing) + "\n", encoding="utf-8")
        changed = True
    repo = _repo(root)
    if repo.head.is_valid():
        tracked = repo.git.ls_files("clab/ceos-config")
        if tracked.strip():
            repo.git.rm("-r", "--cached", "--quiet", "clab/ceos-config")
            changed = True
    return changed


def _safe(root: Path, rel: str) -> Path:
    p = (root / rel).resolve()
    if root.resolve() not in p.parents and p != root.resolve():
        raise ValueError(f"path escapes lab dir: {rel}")
    if ".git" in Path(rel).parts:
        raise ValueError("refusing to touch .git")
    return p


def read_file(lab: Lab | str, rel: str) -> str:
    p = _safe(lab_dir(lab), rel)
    if not p.is_file():
        raise FileNotFoundError(rel)
    return p.read_text(encoding="utf-8")


def read_files(lab: Lab | str, prefixes: tuple[str, ...] = EDITABLE_PREFIXES) -> dict[str, str]:
    root = lab_dir(lab)
    out: dict[str, str] = {}
    for f in sorted(root.rglob("*")):
        if not f.is_file() or ".git" in f.parts:
            continue
        rel = str(f.relative_to(root))
        if any(rel == p or rel.startswith(p) for p in prefixes):
            out[rel] = f.read_text(encoding="utf-8")
    return out


def file_tree(lab: Lab | str) -> list[dict[str, Any]]:
    """Flat listing [{path, size, editable}] of everything except .git."""
    root = lab_dir(lab)
    out: list[dict[str, Any]] = []
    for f in sorted(root.rglob("*")):
        if not f.is_file() or ".git" in f.parts:
            continue
        rel = str(f.relative_to(root))
        out.append({
            "path": rel,
            "size": f.stat().st_size,
            "editable": any(rel == p or rel.startswith(p) for p in EDITABLE_PREFIXES),
            "knob_managed": any(rel == p or rel.startswith(p) for p in KNOB_MANAGED),
        })
    return out


def is_editable(rel: str) -> bool:
    return any(rel == p or rel.startswith(p) for p in EDITABLE_PREFIXES)


def is_knob_managed(rel: str) -> bool:
    return any(rel == p or rel.startswith(p) for p in KNOB_MANAGED)


def history(lab: Lab | str, limit: int = 50) -> list[dict[str, Any]]:
    root = lab_dir(lab)
    repo = _repo(root)
    if not repo.head.is_valid():
        return []
    out = []
    for c in repo.iter_commits(max_count=limit):
        out.append({
            "sha": c.hexsha,
            "short": c.hexsha[:8],
            "message": c.message.strip(),
            "author": c.author.name,
            "date": datetime.fromtimestamp(c.committed_date, tz=timezone.utc).isoformat(),
            "files": sorted(c.stats.files.keys()),
        })
    return out


def diff(lab: Lab | str, rev_a: str | None, rev_b: str | None = None, path: str | None = None) -> str:
    """Unified diff between two revisions (default: previous commit vs HEAD; rev_b=None means the working tree)."""
    repo = _repo(lab_dir(lab))
    if not repo.head.is_valid():
        return ""
    a = rev_a or "HEAD~1"
    if a == "HEAD~1" and len(repo.head.commit.parents) == 0:
        a = EMPTY_TREE
    args = [a] + ([rev_b] if rev_b else [])
    if path:
        args += ["--", path]
    return repo.git.diff(*args)


EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"  # git's well-known empty tree object


def delete_lab_dir(lab: Lab | str) -> None:
    root = lab_dir(lab)
    if root.exists():
        shutil.rmtree(root)
