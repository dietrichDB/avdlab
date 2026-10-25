"""Custom labs (phase 3 §4): import an AVD inventory from a zip / tar.gz (or a git clone), export a lab as zip.

The archive must contain an Ansible inventory (`inventory.yml|inventory.yaml|hosts.yml`) and `group_vars/`; a single
top-level directory is stripped. `intended/`, `documentation/`, `clab/`, `anta/reports/` and `.git/` are ignored
(rebuilt / regenerated). The inventory is normalised to `inventory.yml` (what the build, editor and executors use).
Invalid AVD inputs do not block the import: the lab is created as `draft` with `settings["import_warnings"]`.
"""
from __future__ import annotations

import asyncio
import io
import shutil
import tarfile
import tempfile
import zipfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from ..core.db import Lab, LabState, SessionLocal
from . import service
from .templates import INVENTORY_NAMES, coerce_knobs, find_inventory, get_template, load_group_vars_dir, \
    load_lab_hostvars, validate_hostvars

Log = Callable[[str], None]
MAX_UNPACKED = 200 * 1024 * 1024
MAX_MEMBERS = 20000
IGNORED_PREFIXES = (".git/", "intended/", "documentation/", "clab/", "anta/reports/", "anta/runs/", "__MACOSX/")
EXPORT_EXCLUDES = (".git", "anta/reports")


class ImportError_(ValueError):  # noqa: N801 - avoid shadowing the builtin
    pass


# ------------------------------------------------------------------------------------------------ extraction
def _members(data: bytes, filename: str) -> list[tuple[str, bytes]]:
    """(posix path, content) of regular files in a zip or tar(.gz) archive; unsafe paths / links refused."""
    out: list[tuple[str, bytes]] = []
    total = 0

    def add(name: str, content: bytes) -> None:
        nonlocal total
        p = PurePosixPath(name)
        if p.is_absolute() or ".." in p.parts or name.startswith("\\") or ":" in (p.parts[0] if p.parts else ""):
            raise ImportError_(f"unsafe path in archive: {name}")
        total += len(content)
        if total > MAX_UNPACKED or len(out) > MAX_MEMBERS:
            raise ImportError_("archive too large (max 200 MB unpacked)")
        out.append((str(p), content))

    if zipfile.is_zipfile(io.BytesIO(data)):
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                if (info.external_attr >> 16) & 0o170000 == 0o120000:  # symlink
                    continue
                if info.file_size > MAX_UNPACKED:
                    raise ImportError_("archive too large (max 200 MB unpacked)")
                add(info.filename, zf.read(info))
        return out
    try:
        tf = tarfile.open(fileobj=io.BytesIO(data), mode="r:*")
    except tarfile.TarError as exc:
        raise ImportError_(f"{filename or 'file'} is not a zip or tar.gz archive") from exc
    with tf:
        for m in tf.getmembers():
            if not m.isfile():
                continue
            if m.size > MAX_UNPACKED:
                raise ImportError_("archive too large (max 200 MB unpacked)")
            fh = tf.extractfile(m)
            add(m.name, fh.read() if fh else b"")
    return out


def _strip_top(members: list[tuple[str, bytes]]) -> list[tuple[str, bytes]]:
    tops = {PurePosixPath(n).parts[0] for n, _ in members if PurePosixPath(n).parts}
    has_root_files = any(len(PurePosixPath(n).parts) == 1 for n, _ in members)
    if len(tops) == 1 and not has_root_files:
        return [(str(PurePosixPath(*PurePosixPath(n).parts[1:])), c) for n, c in members]
    return members


def extract_archive(data: bytes, filename: str, dest: Path) -> list[str]:
    """Write the archive's lab files into `dest` (created). Returns the relative paths written."""
    members = [(n, c) for n, c in _strip_top(_members(data, filename))
               if n and not any(n == p.rstrip("/") or n.startswith(p) for p in IGNORED_PREFIXES)
               and not n.endswith(".DS_Store")]
    names = {n for n, _ in members}
    if not any(n in names for n in INVENTORY_NAMES):
        raise ImportError_(f"no Ansible inventory found (expected one of {', '.join(INVENTORY_NAMES)} at the top level)")
    if not any(n.startswith("group_vars/") for n in names):
        raise ImportError_("no group_vars/ directory found")
    dest.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for rel, content in members:
        p = (dest / rel).resolve()
        if dest.resolve() not in p.parents:
            raise ImportError_(f"unsafe path in archive: {rel}")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        written.append(rel)
    return written


def normalize_inventory(root: Path) -> str | None:
    """Rename inventory.yaml / hosts.yml to inventory.yml. Returns the original name when renamed."""
    inv = find_inventory(root)
    if inv is None:
        raise ImportError_(f"no Ansible inventory found (expected one of {', '.join(INVENTORY_NAMES)})")
    if inv.name == "inventory.yml":
        return None
    inv.rename(root / "inventory.yml")
    return inv.name


# ------------------------------------------------------------------------------------------------ inspection
def detect_fabric_name(root: Path) -> str:
    """First `fabric_name` found in group_vars (groups in sorted order), else FABRIC."""
    try:
        for data in load_group_vars_dir(root).values():
            if isinstance(data, dict) and data.get("fabric_name"):
                return str(data["fabric_name"])
    except yaml.YAMLError:
        pass
    return "FABRIC"


def validate_dir(root: Path) -> list[dict[str, Any]]:
    """pyavd validation of the inventory/group_vars in `root` → violations (load errors become one violation)."""
    try:
        hostvars = load_lab_hostvars(root)
    except Exception as exc:  # noqa: BLE001 - YAML errors, bad inventory shape
        return [{"path": "inventory.yml", "message": f"cannot load inventory/group_vars: {exc}"}]
    if not hostvars:
        return [{"path": "inventory.yml", "message": "no hosts in inventory"}]
    _validated, violations = validate_hostvars(hostvars)
    return violations


# ------------------------------------------------------------------------------------------------ lab creation
def _prepare_repo(root: Path, message: str) -> str:
    """git init (if needed) + avdlab .gitignore + standard dirs, then commit everything. Returns the commit sha."""
    gi = root / ".gitignore"
    own_gitignore = gi.read_text(encoding="utf-8") if gi.is_file() else None
    if not (root / ".git").exists():
        service.init_lab_dir(root.name, {}, message)  # writes avdlab's .gitignore
        if own_gitignore is not None:
            gi.write_text(own_gitignore, encoding="utf-8")  # keep the archive's, ensure_gitignore adds ours
    service.ensure_gitignore(root)
    for sub in ("group_vars", "playbooks", "anta/user_catalogs", "intended", "documentation", "clab"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return service.commit_all(root, message)


async def create_custom_lab(*, name: str, slug: str, host_id: str | None, source: str, log: Log,
                            git_cfg: dict[str, Any] | None = None) -> tuple[Lab, list[dict[str, Any]]]:
    """Files already in `lab_dir(slug)` → normalise, commit, validate, insert the Lab row (template custom)."""
    root = service.lab_dir(slug)
    renamed = await asyncio.to_thread(normalize_inventory, root)
    if renamed:
        log(f"renamed {renamed} → inventory.yml")
    await asyncio.to_thread(_prepare_repo, root, f"import ({source})")
    fabric_name = await asyncio.to_thread(detect_fabric_name, root)
    log(f"fabric_name: {fabric_name}")
    violations = await asyncio.to_thread(validate_dir, root)
    for v in violations[:20]:
        log(f"[violation] {v.get('host', '')}: {v.get('path', '')}: {v['message']}")
    tpl = get_template("custom")
    knobs = coerce_knobs(tpl, {})
    st: dict[str, Any] = {"knobs": knobs, "secret_knobs": {}, "fabric_name": fabric_name, "custom_knobs": [],
                          "custom_files": [], "import_warnings": violations, "imported_from": source}
    if git_cfg:
        st["git"] = git_cfg
    lab = Lab(name=name, slug=slug, template="custom", host_id=host_id, target="clab", state=LabState.draft,
              ceos_version=knobs.get("ceos_version"), settings=st)
    async with SessionLocal() as s:
        s.add(lab)
        await s.commit()
        await s.refresh(lab)
    return lab, violations


def import_archive_files(data: bytes, filename: str, slug: str) -> list[str]:
    """Blocking: extract into a scratch dir, then move to the lab dir (nothing left behind on errors)."""
    dest = service.lab_dir(slug)
    if dest.exists():
        raise ImportError_(f"lab directory {slug} already exists")
    with tempfile.TemporaryDirectory(prefix="avdlab-import-") as tmp:
        scratch = Path(tmp) / slug
        written = extract_archive(data, filename, scratch)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(scratch), dest)
    return written


# ------------------------------------------------------------------------------------------------ export
def export_zip(root: Path) -> bytes:
    """Zip of the lab dir (paths prefixed with the slug) without `.git` and `anta/reports`."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(root.rglob("*")):
            if not f.is_file() or f.is_symlink():
                continue
            rel = f.relative_to(root).as_posix()
            if any(rel == e or rel.startswith(e + "/") for e in EXPORT_EXCLUDES):
                continue
            zf.write(f, f"{root.name}/{rel}")
    return buf.getvalue()
