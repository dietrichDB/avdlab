"""EOS image catalogue (eos-downloader) + local image store helpers.

* `get_catalog(fmt, token)` – versions from arista.com via `eos_downloader`, cached for `CATALOG_TTL` seconds.
  Raises `NoTokenError` (→ HTTP 409) or `CatalogError` (→ HTTP 502).
* `resolve_token(session)` – plaintext of the secret named `arista_token` (or any secret of kind arista_token).
* file-name / docker-tag conventions shared by the download, upload and push jobs.

`fetch_versions` is the only function that talks to arista.com; tests monkeypatch it.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import settings
from ..core.db import Image, Secret, SecretKind
from ..core.security import secret_store

CATALOG_TTL = 3600
TOKEN_SECRET_NAME = "arista_token"
DOCKER_FORMATS = {"cEOS", "cEOS64", "cEOSarm"}
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+[A-Z]?(?:-[A-Za-z0-9.]+)?$")


class NoTokenError(Exception):
    hint = (f"Create a secret named '{TOKEN_SECRET_NAME}' (kind arista_token) holding your arista.com API token "
            "(arista.com > My Profile > Access Token), then retry.")


class CatalogError(Exception):
    pass


@dataclass
class _CacheEntry:
    at: float
    versions: list[str]


_cache: dict[tuple[str, str], _CacheEntry] = {}


def clear_cache() -> None:
    _cache.clear()


def validate_version(version: str) -> str:
    if not _VERSION_RE.match(version):
        raise ValueError(f"invalid EOS version {version!r} (expected e.g. 4.34.2F)")
    return version


# ---- architectures (phase 3): cEOSarm → arm64 image tagged `<version>-arm64`; cEOS / cEOS64 → x86_64
ARCHES = ("x86_64", "arm64")
FORMAT_ARCH = {"cEOS": "x86_64", "cEOS64": "x86_64", "cEOSarm": "arm64"}
FORMAT_LABELS = {"cEOS": "cEOS-lab (x86_64)", "cEOS64": "cEOS64-lab (x86_64, 64-bit)", "cEOSarm": "cEOSarm-lab (arm64)"}
ARM_SUFFIX = "-arm64"


def format_arch(fmt: str) -> str | None:
    return FORMAT_ARCH.get(fmt)


def normalize_arch(arch: str | None) -> str | None:
    """`uname -m` / docker spellings → x86_64 | arm64 (None if unknown)."""
    a = (arch or "").strip().lower()
    if a in ("x86_64", "amd64", "x64"):
        return "x86_64"
    if a in ("arm64", "aarch64", "armv8", "arm64v8"):
        return "arm64"
    return None


def ceos_image_tag(version: str, arch: str | None = "x86_64") -> str:
    return f"arista/ceos:{version}{ARM_SUFFIX if normalize_arch(arch) == 'arm64' else ''}"


def tag_arch(tag: str) -> str:
    """Arch of a cEOS docker tag by the naming scheme."""
    return "arm64" if tag.endswith(ARM_SUFFIX) else "x86_64"


def formats() -> list[dict[str, str]]:
    return [{"fmt": f, "arch": FORMAT_ARCH[f], "label": FORMAT_LABELS[f]} for f in ("cEOS", "cEOS64", "cEOSarm")]


def arch_mismatch(host_caps: dict[str, Any] | None, image_arch: str | None) -> str | None:
    """Error text when a probed host's CPU arch differs from the image arch; None when compatible/unknown."""
    host_arch = normalize_arch(((host_caps or {}).get("os") or {}).get("arch"))
    image_arch = normalize_arch(image_arch)
    if host_arch and image_arch and host_arch != image_arch:
        return f"host is {host_arch}, image is {image_arch}"
    return None


def docker_tag(fmt: str, version: str) -> str:
    return ceos_image_tag(version, FORMAT_ARCH[fmt]) if fmt in DOCKER_FORMATS else ""


def image_filename(fmt: str, version: str) -> str:
    """Name eos-downloader gives the file (cEOS-lab-4.34.2F.tar.xz, vEOS64-lab-4.34.2F.qcow2, ...)."""
    from eos_downloader.models.data import software_mapping

    entry = software_mapping.EOS.get(fmt) or software_mapping.EOS["default"]
    return f"{entry.prepend}-{version}{entry.extension}"


def supported_formats() -> list[str]:
    from eos_downloader.models.data import software_mapping

    return [f for f in software_mapping.EOS if f not in ("RN", "SOURCE", "default")]


# ------------------------------------------------------------------------------------------------ token
async def resolve_token(session: AsyncSession) -> str:
    sec = await secret_store.by_name(session, TOKEN_SECRET_NAME)
    if sec is None:
        sec = (await session.execute(select(Secret).where(Secret.kind == SecretKind.arista_token))).scalars().first()
    if sec is None:
        raise NoTokenError(TOKEN_SECRET_NAME)
    return secret_store.decrypt(sec.ciphertext)


# ------------------------------------------------------------------------------------------------ catalogue
def fetch_versions(token: str, fmt: str) -> list[str]:
    """Blocking call to arista.com (run in a thread). Returns EOS versions, newest first."""
    from eos_downloader.logics.arista_xml_server import AristaXmlQuerier

    try:
        querier = AristaXmlQuerier(token=token)
        versions = querier.available_public_versions(package="eos")
    except Exception as exc:  # noqa: BLE001 - eos_downloader raises ValueError/AuthenticationError/requests errors
        raise CatalogError(f"arista.com catalogue unavailable: {exc}") from exc
    versions = [v for v in versions if v is not None]
    try:
        versions = sorted(versions, reverse=True)
    except TypeError:
        pass
    seen: list[str] = []
    for v in versions:
        s = str(v)
        if s not in seen:
            seen.append(s)
    return seen


async def get_catalog(fmt: str, token: str, *, refresh: bool = False) -> dict[str, Any]:
    key = (fmt, hashlib.sha256(token.encode()).hexdigest()[:16])
    entry = _cache.get(key)
    now = time.time()
    cached = entry is not None and not refresh and now - entry.at < CATALOG_TTL
    if not cached:
        versions = await asyncio.to_thread(fetch_versions, token, fmt)
        entry = _CacheEntry(at=now, versions=versions)
        _cache[key] = entry
    assert entry is not None
    return {"fmt": fmt, "versions": entry.versions, "cached": cached, "fetched_at": entry.at, "ttl": CATALOG_TTL}


# ------------------------------------------------------------------------------------------------ store
def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def find_downloaded(fmt: str, version: str, directory: Path | None = None) -> Path | None:
    directory = directory or settings.images_dir
    exact = directory / image_filename(fmt, version)
    if exact.exists():
        return exact
    prefix = image_filename(fmt, version).split(version)[0]  # e.g. "cEOS-lab-"
    matches = sorted(p for p in directory.rglob(f"{prefix}{version}*")
                     if p.is_file() and not p.name.endswith((".sha512sum", ".md5sum", ".part")))
    return matches[0] if matches else None


async def upsert_image(session: AsyncSession, fmt: str, version: str, path: Path | None, *,
                       tag: str | None = None) -> Image:
    row = (await session.execute(select(Image).where(Image.fmt == fmt, Image.version == version))).scalar_one_or_none()
    if row is None:
        row = Image(fmt=fmt, version=version, docker_tag=tag if tag is not None else docker_tag(fmt, version),
                    present_on_hosts=[], arch=format_arch(fmt))
        session.add(row)
    if path is not None and path.exists():
        row.local_path = str(path)
        row.size_bytes = path.stat().st_size
        row.sha256 = await asyncio.to_thread(sha256_file, path)
    await session.commit()
    await session.refresh(row)
    return row


async def import_tarball_on_host(ssh: Any, local_path: str | Path, tag: str, *, sudo: bool, log: Any = None,
                                 remote_dir: str | None = None) -> None:
    """Upload a cEOS rootfs tarball over SFTP and `docker import` it as `tag` on the host (shared by image.push and lab.deploy)."""
    import shlex

    remote_dir = remote_dir or f"{settings.remote_root}/images"
    await ssh.run(f"mkdir -p {shlex.quote(remote_dir)}", log=log)
    remote = f"{remote_dir}/{Path(local_path).name}"
    if log:
        log(f"uploading {local_path} -> {remote}")
    await ssh.put(local_path, remote)
    res = await ssh.run(f"docker import {shlex.quote(remote)} {shlex.quote(tag)}", sudo=sudo, log=log, timeout=1800)
    if not res.ok:
        raise RuntimeError(f"docker import of {tag} failed ({res.returncode})")


def image_out(img: Image) -> dict[str, Any]:
    return {
        "id": img.id, "fmt": img.fmt, "version": img.version, "docker_tag": img.docker_tag,
        "local_path": img.local_path, "sha256": img.sha256, "size_bytes": img.size_bytes,
        "present_on_hosts": list(img.present_on_hosts or []), "created_at": img.created_at.isoformat(),
        "arch": img.arch or format_arch(img.fmt) or (tag_arch(img.docker_tag) if img.docker_tag else None),
    }
