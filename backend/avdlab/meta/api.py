"""Phase 5: version / build info, changelog and third-party notices.

  GET /meta            {name, version, git_sha, build_date, avd_version, anta_version, python, copyright,
                        copyright_notice, license, changelog_latest: {version, date, sections} | null,
                        app_title, github_url, support_email, registration_enabled, intro_video_url}  (v0.9.0)
  GET /meta/changelog  {versions: [{version, date, sections: {Added: [..], Changed: [..], Fixed: [..]}}], raw}
  GET /meta/notices    {components: [{name, license, url, notes}], markdown, license_text, trademark}

Phase 6 (A), authenticated — see meta/update.py:
  GET  /meta/update          {current, latest, available, url, notes, published_at, image, checked_at, error, source,
                              configured, hint, mode, can_self_update, reason, last_update: {from, to, at, ok, mode, job_id}|null}
  POST /meta/update/check    forces a check → same shape
  POST /meta/update/apply    {force?: bool, to?: str} → 202 {job_id} (job `app.update`); 409 {message, reason} when
                             self-update is impossible, no newer release is known (without force) or any job is running
  GET  /meta/update/log      {log, last_update}

VERSION, CHANGELOG.md, LICENSE and THIRD_PARTY_NOTICES.md live at the repository root (in the image: /app, copied by
deploy/Dockerfile.backend); `settings.meta_dir` overrides the location. These endpoints are public (no session):
the login page footer shows the version and nothing here is sensitive.
"""
from __future__ import annotations

import platform
import re
import subprocess
from functools import lru_cache
from importlib import metadata
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import settings
from ..core.db import Job, JobStatus, get_session
from ..core.security import current_user

router = APIRouter(prefix="/meta", tags=["meta"])
REPO_ROOT = Path(__file__).resolve().parents[3]
LICENSE_SUMMARY = "Proprietary — all rights reserved"
TRADEMARK = ("Arista, EOS and CloudVision are trademarks of Arista Networks, Inc. avdlab is not an Arista product and "
             "is not affiliated with or endorsed by Arista Networks.")
_HEADING = re.compile(r"^##\s+\[?([^\]\s]+)\]?(?:\s*-\s*(\S+))?\s*$")
_SECTION = re.compile(r"^###\s+(.+?)\s*$")


def meta_dir() -> Path:
    return Path(settings.meta_dir) if settings.meta_dir else REPO_ROOT


def _read(name: str) -> str:
    p = meta_dir() / name
    return p.read_text(encoding="utf-8") if p.is_file() else ""


def version() -> str:
    return _read("VERSION").strip() or "0.0.0"


def parse_changelog(text: str) -> list[dict[str, Any]]:
    """Keep-a-Changelog markdown → [{version, date, sections: {name: [items]}}], newest first as written.
    Bullets may wrap onto indented continuation lines; empty `Unreleased` blocks are dropped."""
    versions: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    section: str | None = None
    for line in text.splitlines():
        if m := _HEADING.match(line):
            cur = {"version": m.group(1), "date": m.group(2), "sections": {}}
            versions.append(cur)
            section = None
        elif cur is not None and (m := _SECTION.match(line)):
            section = m.group(1)
            cur["sections"].setdefault(section, [])
        elif cur is not None and section and re.match(r"^\s*[-*]\s+", line):
            cur["sections"][section].append(re.sub(r"^\s*[-*]\s+", "", line).strip())
        elif cur is not None and section and line.startswith((" ", "\t")) and line.strip() and cur["sections"][section]:
            cur["sections"][section][-1] += " " + line.strip()
        elif line.startswith("[") and "]: " in line:
            section = None  # link reference definitions at the bottom
    return [v for v in versions if v["sections"] or v["version"].lower() != "unreleased"]


def parse_notices(text: str) -> list[dict[str, str]]:
    """The first markdown table of THIRD_PARTY_NOTICES.md → [{name, license, url, notes}]."""
    rows: list[dict[str, str]] = []
    for line in text.splitlines():
        if not line.startswith("|") or set(line.replace("|", "").strip()) <= {"-", ":", " "}:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if cells and cells[0].lower() == "component":
            continue
        cells += [""] * (4 - len(cells))
        rows.append({"name": cells[0], "license": cells[1], "url": cells[2], "notes": cells[3]})
    return rows


def _pkg_version(name: str, fallback: str) -> str:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return fallback


@lru_cache(maxsize=1)
def _git_sha_from_checkout() -> str | None:
    if not (REPO_ROOT / ".git").exists():
        return None
    try:
        out = subprocess.run(["git", "-C", str(REPO_ROOT), "rev-parse", "--short=12", "HEAD"], capture_output=True,
                             text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


def git_sha() -> str | None:
    return settings.git_sha.strip() or _git_sha_from_checkout()


def meta_info() -> dict[str, Any]:
    versions = parse_changelog(_read("CHANGELOG.md"))
    return {
        "name": "avdlab",
        "version": version(),
        "git_sha": git_sha(),
        "build_date": settings.build_date.strip() or _read("BUILD_DATE").strip() or None,
        "avd_version": _pkg_version("pyavd", settings.avd_version),
        "anta_version": _pkg_version("anta", settings.anta_version),
        "python": platform.python_version(),
        "copyright": settings.copyright,
        "copyright_notice": f"{settings.copyright.rstrip('.')}. All rights reserved.",
        "license": LICENSE_SUMMARY,
        "changelog_latest": versions[0] if versions else None,
    }


@router.get("")
async def get_meta() -> dict[str, Any]:
    from ..core.app_settings import public_view  # v0.9.0 (A): github_url, support_email, registration_enabled, …

    return {**meta_info(), **await public_view()}


@router.get("/changelog")
async def get_changelog() -> dict[str, Any]:
    raw = _read("CHANGELOG.md")
    return {"versions": parse_changelog(raw), "raw": raw}


@router.get("/notices")
async def get_notices() -> dict[str, Any]:
    raw = _read("THIRD_PARTY_NOTICES.md")
    return {"components": parse_notices(raw), "markdown": raw, "license_text": _read("LICENSE"), "trademark": TRADEMARK}


# ------------------------------------------------------------------------------------------------ phase 6: updates
from . import update as _update  # noqa: E402 - registers job app.update


class UpdateApplyIn(BaseModel):
    force: bool = False
    to: str | None = None


def _conflict(message: str, reason: str | None = None) -> HTTPException:
    return HTTPException(409, detail={"message": message, "reason": reason})


@router.get("/update")
async def get_update(user: str = Depends(current_user)) -> dict[str, Any]:
    return _update.status()


@router.post("/update/check")
async def post_update_check(user: str = Depends(current_user)) -> dict[str, Any]:
    await _update.check_now()
    return _update.status()


@router.post("/update/apply", status_code=202)
async def post_update_apply(body: UpdateApplyIn | None = None, session: AsyncSession = Depends(get_session),
                            user: str = Depends(current_user)) -> dict[str, str]:
    from ..jobs.engine import runner

    body = body or UpdateApplyIn()
    st = _update.status()
    if not st["can_self_update"]:
        raise _conflict("self-update is not possible", st["reason"])
    if not body.force and not st["available"]:
        raise _conflict(f"no newer release known (current {st['current']}, latest {st['latest']}): check first or "
                        "pass force: true")
    if settings.update_mode == "image" and not st.get("image"):
        raise _conflict("image mode needs a release image: the update source did not provide one")
    busy = (await session.execute(select(Job.id, Job.type).where(
        Job.status.in_([JobStatus.queued, JobStatus.running])).limit(1))).first()
    if busy:
        raise _conflict(f"job {busy.id} ({busy.type}) is running: wait for it to finish before updating")
    job_id = await runner.submit("app.update", params={"to": body.to or st["latest"], "image": st.get("image")})
    return {"job_id": job_id}


@router.get("/update/log")
async def get_update_log(user: str = Depends(current_user)) -> dict[str, Any]:
    p = _update.update_log_path()
    return {"log": p.read_text(encoding="utf-8", errors="replace") if p.is_file() else "", "last_update": _update.last_update()}
