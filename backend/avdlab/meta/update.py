"""Phase 6 (A): update check + self-update.

Check: `settings.update_source` is `github:<owner>/<repo>` (GitHub Releases API, latest release) or a manifest URL
returning `{"latest", "url", "notes", "published_at", "image"}`. A background task checks every
`update_check_interval` hours (first check a few seconds after startup, never blocking it); the result is cached in
memory and in `<data_dir>/update-check.json`. HTTP goes through `http_client()` so tests inject an httpx MockTransport.

Self-update (job `app.update`): runs a *detached* helper container through the docker wrapper
(`avdlab.images.docker.backend.run_cli`) so the update survives the API being recreated:

    docker run -d --rm --name avdlab-update-<job> -v /var/run/docker.sock:/var/run/docker.sock
               -v <repo>:<repo> -w <deploy dir> docker:27-cli sh -c '<cmd>'

`<repo>` is the parent of `AVDLAB_DEPLOY_DIR` (the host path of `deploy/`), mounted at the *same* path so the compose
file's relative build context (`..`) and any relative bind mounts resolve to host paths. Commands:
  source: git -C .. pull --ff-only && docker compose up -d --build        (git is apk-added to the helper)
  image:  AVDLAB_IMAGE=<release image> written to deploy/.env, docker compose pull api && docker compose up -d api
The job follows `docker logs -f` into `<data_dir>/update.log` until the helper exits or the API is recreated.
`<data_dir>/update.json` records {from, to, mode, at, job_id, helper}; the next API process (`finalize_previous_update`,
called from the app lifespan) compares its version with `to`, stores `ok`, closes the job row and exposes
`last_update: {from, to, at, ok}` in `GET /meta/update`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shlex
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from ..core.config import settings
from ..jobs.engine import JobContext, job

log = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
HELPER_EXIT_RE = re.compile(r"\[avdlab-update\] exit=(\d+)")
_state: dict[str, Any] = {}
_task: asyncio.Task[None] | None = None


# ------------------------------------------------------------------------------------------------ versions
def parse_version(raw: str | None) -> tuple[tuple[int, ...], tuple[Any, ...]] | None:
    """'v1.2.3', '1.2', '1.2.3-rc.1' → ((1, 2, 3), prerelease key); None when not a version.
    A release sorts after its pre-releases (semver precedence, numeric identifiers compare numerically)."""
    if not raw:
        return None
    m = re.match(r"^\s*v?(\d+(?:\.\d+){0,3})(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?\s*$", str(raw))
    if not m:
        return None
    nums = tuple(int(x) for x in m.group(1).split("."))
    nums = nums + (0,) * (3 - len(nums)) if len(nums) < 3 else nums
    if m.group(2) is None:
        return nums, (1,)  # release > any pre-release
    ids = tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in m.group(2).split("."))
    return nums, (0, ids)


def is_newer(latest: str | None, current: str | None) -> bool:
    a, b = parse_version(latest), parse_version(current)
    if a is None or b is None:
        return False
    return a > b


def _clean_version(raw: str | None) -> str | None:
    return str(raw).strip().lstrip("vV") if raw else None


# ------------------------------------------------------------------------------------------------ check
def http_client() -> httpx.AsyncClient:
    """Overridable in tests (monkeypatch to return an AsyncClient with a MockTransport)."""
    return httpx.AsyncClient(timeout=15, follow_redirects=True, headers={"User-Agent": "avdlab-update-check"})


def parse_github_release(data: dict[str, Any], owner: str, repo: str) -> dict[str, Any]:
    tag = str(data.get("tag_name") or data.get("name") or "")
    version = _clean_version(tag)
    return {
        "latest": version,
        "url": data.get("html_url") or f"https://github.com/{owner}/{repo}/releases/tag/{tag}",
        "notes": data.get("body") or "",
        "published_at": data.get("published_at"),
        "image": f"ghcr.io/{owner.lower()}/{repo.lower()}:{version}" if version else None,
    }


def parse_manifest(data: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(data, dict) or not data.get("latest"):
        raise ValueError("manifest must be a JSON object with a 'latest' version")
    return {
        "latest": _clean_version(data["latest"]),
        "url": data.get("url"),
        "notes": data.get("notes") or "",
        "published_at": data.get("published_at"),
        "image": data.get("image"),
    }


async def fetch_latest(source: str) -> dict[str, Any]:
    """Resolve the latest release for `source` (see module doc). Raises on HTTP/parse errors."""
    source = source.strip()
    async with http_client() as client:
        if source.startswith("github:"):
            slug = source.split(":", 1)[1].strip().strip("/")
            if slug.count("/") != 1:
                raise ValueError(f"update source {source!r}: expected github:<owner>/<repo>")
            owner, repo = slug.split("/")
            r = await client.get(f"{GITHUB_API}/repos/{owner}/{repo}/releases/latest",
                                 headers={"Accept": "application/vnd.github+json"})
            r.raise_for_status()
            return parse_github_release(r.json(), owner, repo)
        if source.startswith(("http://", "https://")):
            r = await client.get(source)
            r.raise_for_status()
            return parse_manifest(r.json())
    raise ValueError(f"update source {source!r}: use github:<owner>/<repo> or an http(s) manifest URL")


def _check_file() -> Path:
    return settings.data_dir / "update-check.json"


def _load_cached() -> dict[str, Any]:
    if not _state:
        try:
            _state.update(json.loads(_check_file().read_text()))
        except (OSError, ValueError):
            pass
    return _state


async def check_now() -> dict[str, Any]:
    """Run a check and cache it. Never raises: failures land in `error`."""
    from .api import version

    current = version()
    result: dict[str, Any] = {"checked_at": datetime.now(timezone.utc).isoformat(), "source": settings.update_source,
                              "latest": None, "url": None, "notes": "", "published_at": None, "image": None,
                              "error": None}
    if settings.update_source.strip():
        try:
            result.update(await fetch_latest(settings.update_source))
        except Exception as exc:  # noqa: BLE001 - network/HTTP/parse: report, never crash
            result["error"] = f"{type(exc).__name__}: {exc}"
    result["checked_version"] = current
    _state.clear()
    _state.update(result)
    try:
        _check_file().parent.mkdir(parents=True, exist_ok=True)
        _check_file().write_text(json.dumps(result, indent=2))
    except OSError:
        pass
    return result


# ------------------------------------------------------------------------------------------------ self-update
def can_self_update() -> tuple[bool, str | None]:
    """(ok, reason). Needs mode source|image, the docker socket mounted, AVDLAB_DEPLOY_DIR and a docker CLI."""
    if settings.update_mode == "manual":
        return False, "AVDLAB_UPDATE_MODE=manual: update the deployment yourself (see README → Updating)"
    if not Path(settings.update_docker_socket).exists():
        return False, (f"docker socket {settings.update_docker_socket} is not mounted into the API container "
                       "(uncomment the docker.sock volume in deploy/docker-compose.yml)")
    if not settings.deploy_dir.strip():
        return False, "AVDLAB_DEPLOY_DIR is not set (host path of the deploy/ directory, e.g. AVDLAB_DEPLOY_DIR=${PWD})"
    if not Path(settings.deploy_dir).is_absolute():
        return False, f"AVDLAB_DEPLOY_DIR must be an absolute host path (got {settings.deploy_dir!r})"
    from ..images import docker

    if not shutil.which(getattr(docker.get_backend(), "binary", "docker")):
        return False, "docker CLI not found in the API container"
    return True, None


def helper_script(mode: str, *, image: str | None = None) -> str:
    """Shell run inside the helper container (cwd = deploy dir). Ends with a sentinel carrying the exit code."""
    if mode == "source":
        steps = [
            "apk add --no-cache git >/dev/null",
            "git config --global --add safe.directory '*'",
            "git -C .. pull --ff-only",
            'AVDLAB_GIT_SHA=$(git -C .. rev-parse --short=12 HEAD) AVDLAB_BUILD_DATE=$(date -u +%Y-%m-%dT%H:%M:%SZ) '
            "docker compose up -d --build",
        ]
    elif mode == "image":
        if not image:
            raise ValueError("image mode needs the release image (manifest 'image' / GitHub release)")
        q = shlex.quote(f"AVDLAB_IMAGE={image}")
        steps = [
            "touch .env",
            f"(grep -q '^AVDLAB_IMAGE=' .env && sed -i 's#^AVDLAB_IMAGE=.*#'{q}'#' .env || echo {q} >> .env)",
            "docker compose pull api",
            "docker compose up -d api",
        ]
    else:
        raise ValueError(f"mode {mode!r} cannot self-update")
    return "set -x; (" + " && ".join(steps) + '); rc=$?; echo "[avdlab-update] exit=$rc"; exit $rc'


def helper_argv(mode: str, *, name: str, image: str | None = None) -> list[str]:
    """`docker run` arguments (without the binary) for the detached helper."""
    deploy = str(Path(settings.deploy_dir))
    repo = str(Path(deploy).parent)
    sock = str(settings.update_docker_socket)
    return ["run", "-d", "--rm", "--name", name, "--label", "avdlab.role=updater",
            "-v", f"{sock}:/var/run/docker.sock", "-v", f"{repo}:{repo}", "-w", deploy,
            "-e", f"AVDLAB_DEPLOY_DIR={deploy}",
            settings.update_helper_image, "sh", "-c", helper_script(mode, image=image)]


def update_log_path() -> Path:
    return settings.data_dir / "update.log"


def update_json_path() -> Path:
    return settings.data_dir / "update.json"


def _read_update_json() -> dict[str, Any] | None:
    try:
        data = json.loads(update_json_path().read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_update_json(data: dict[str, Any]) -> None:
    update_json_path().parent.mkdir(parents=True, exist_ok=True)
    update_json_path().write_text(json.dumps(data, indent=2))


def last_update() -> dict[str, Any] | None:
    data = _read_update_json()
    if not data:
        return None
    return {"from": data.get("from"), "to": data.get("to"), "at": data.get("at"), "ok": data.get("ok"),
            "mode": data.get("mode"), "job_id": data.get("job_id")}


async def finalize_previous_update() -> dict[str, Any] | None:
    """On boot: close the bookkeeping of an update whose API process was replaced mid-job."""
    from ..core.db import Job, JobStatus, SessionLocal
    from .api import version

    data = _read_update_json()
    if not data or data.get("ok") is not None:
        return data
    current = version()
    log_text = update_log_path().read_text(errors="replace") if update_log_path().exists() else ""
    m = HELPER_EXIT_RE.findall(log_text)
    helper_rc = int(m[-1]) if m else None
    to = data.get("to")
    if helper_rc not in (None, 0):
        ok = False
    elif to and to != data.get("from"):
        ok = current == to
    else:  # same version (source mode rebuild) — trust the helper's exit sentinel when we saw it
        ok = helper_rc == 0 or current == to
    data.update(ok=ok, finished_at=datetime.now(timezone.utc).isoformat(), running_version=current)
    _write_update_json(data)
    if data.get("job_id"):
        async with SessionLocal() as s:
            row = await s.get(Job, data["job_id"])
            if row and row.status in (JobStatus.queued, JobStatus.running):
                row.status = JobStatus.succeeded if ok else JobStatus.failed
                row.result = {**(row.result or {}), "from": data.get("from"), "to": to, "ok": ok, "restarted": True,
                              "running_version": current}
                if not ok:
                    row.error = f"update to {to} not confirmed: running {current} (see update.log)"
                row.ended_at = datetime.now(timezone.utc)
                await s.commit()
    return data


# ------------------------------------------------------------------------------------------------ status
def status() -> dict[str, Any]:
    from .api import version

    cached = _load_cached()
    current = version()
    ok, reason = can_self_update()
    latest = cached.get("latest")
    configured = bool(settings.update_source.strip())
    return {
        "current": current,
        "latest": latest,
        "available": is_newer(latest, current),
        "url": cached.get("url"),
        "notes": cached.get("notes") or "",
        "published_at": cached.get("published_at"),
        "image": cached.get("image"),
        "checked_at": cached.get("checked_at"),
        "error": cached.get("error"),
        "source": settings.update_source or None,
        "configured": configured,
        "hint": None if configured else ("update checks are disabled: set AVDLAB_UPDATE_SOURCE=github:<owner>/<repo> "
                                         "or a manifest URL"),
        "mode": settings.update_mode,
        "can_self_update": ok,
        "reason": reason,
        "last_update": last_update(),
    }


# ------------------------------------------------------------------------------------------------ background
async def _loop() -> None:
    await asyncio.sleep(5)
    while True:
        if settings.update_source.strip():
            await check_now()
        hours = settings.update_check_interval
        if hours <= 0:
            return
        await asyncio.sleep(hours * 3600)


def start_checker() -> None:
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(_loop(), name="update-checker")


async def stop_checker() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        _task = None


# ------------------------------------------------------------------------------------------------ job
class UpdateError(RuntimeError):
    pass


def _append_log(line: str) -> None:
    with update_log_path().open("a", encoding="utf-8") as fh:
        fh.write(line.rstrip("\n") + "\n")


@job("app.update")
async def app_update(ctx: JobContext) -> dict[str, Any]:
    """Launch the detached helper and follow its output until it exits (or this process is replaced)."""
    from ..images import docker
    from .api import version

    ok, reason = can_self_update()
    if not ok:
        raise UpdateError(reason or "self-update not possible")
    mode = settings.update_mode
    cached = _load_cached()
    current = version()
    target = _clean_version(ctx.params.get("to")) or cached.get("latest") or current
    image = ctx.params.get("image") or cached.get("image")
    name = f"avdlab-update-{ctx.job_id[:12]}"
    argv = helper_argv(mode, name=name, image=image)
    at = datetime.now(timezone.utc).isoformat()
    record = {"from": current, "to": target, "mode": mode, "at": at, "job_id": ctx.job_id, "helper": name,
              "image": image if mode == "image" else None, "ok": None}
    _write_update_json(record)
    update_log_path().write_text(f"# avdlab update {current} -> {target} (mode {mode}) {at}\n", encoding="utf-8")

    def tee(line: str) -> None:
        ctx.log(line)
        _append_log(line)

    def fail(message: str) -> UpdateError:
        record.update(ok=False, finished_at=datetime.now(timezone.utc).isoformat(), error=message)
        _write_update_json(record)
        tee(f"[update] FAILED: {message}")
        return UpdateError(message)

    backend = docker.get_backend()
    tee(f"[update] {current} -> {target}, mode {mode}: starting helper {name} ({settings.update_helper_image})")
    tee("[update] docker " + " ".join(shlex.quote(a) for a in argv))
    started = await backend.run_cli(argv, timeout=300)
    if not started.ok:
        raise fail(f"docker run failed ({started.returncode}): {started.stdout[-500:]}")
    cid = (started.stdout.strip().splitlines() or [name])[-1].strip()
    tee(f"[update] helper container {cid[:12]} running; following its output (the API restarts when compose "
        "recreates it — the new process finishes this job)")
    followed = await backend.run_cli(["logs", "-f", cid], log=tee, timeout=3600)
    codes = HELPER_EXIT_RE.findall(followed.stdout)
    if not codes:
        raise fail("helper output ended without an exit status")
    rc = int(codes[-1])
    if rc != 0:
        raise fail(f"helper exited with {rc}")
    record.update(ok=True, finished_at=datetime.now(timezone.utc).isoformat())
    _write_update_json(record)
    tee("[update] helper finished and this API process was not replaced (nothing to restart?)")
    return {"from": current, "to": target, "mode": mode, "helper": name, "ok": True, "restarted": False}
