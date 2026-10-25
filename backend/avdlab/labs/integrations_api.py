"""Phase 3 (backend B) lab endpoints: git coupling, custom-lab import/export, VS Code links + sync.

  GET    /labs/{id}/git            → {configured, url, branch, auth_kind, has_secret, ahead, behind, last_sync}
  PUT    /labs/{id}/git            {url, branch, auth_kind, secret_id|null, token|null} → {url, branch, auth_kind, has_secret}
  DELETE /labs/{id}/git            → 204
  POST   /labs/{id}/git/push       {force} → 202 {job_id} (lab.git_push)
  POST   /labs/{id}/git/pull       → 202 {job_id} (lab.git_pull)
  POST   /labs/import              multipart file (zip|tar.gz), name, host_id? → 201 lab (+ import_warnings)
  POST   /labs/import/git          {name, url, branch, auth_kind, secret_id|null, token|null, host_id?} → 202 {job_id}
  GET    /labs/{id}/export         → application/zip (lab dir without .git, anta/reports)
  GET    /labs/{id}/open-links     → {remote_ssh, clab_topology_path, remote_ssh_topology, devcontainer, ssh_command, hints}
  POST   /labs/{id}/sync           → 202 {job_id} (lab.sync: copy the lab to the host, no deploy)

Kept in its own router (registered in main.FEATURE_MODULES after avdlab.labs.api) so it does not touch labs/api.py.
"""
from __future__ import annotations

import asyncio
from typing import Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.db import Host, Job, JobStatus, Lab, Secret, SecretKind, get_session
from ..core.security import current_user, secret_store
from ..jobs.engine import runner
from . import archive, service
from . import git as labgit
from .api import _get_lab, _lab_out, _unique_slug

router = APIRouter(prefix="/labs", tags=["labs"])
GIT_JOBS = ("lab.git_push", "lab.git_pull", "lab.build")
MAX_UPLOAD = 100 * 1024 * 1024


class GitConfigIn(BaseModel):
    url: str
    branch: str = Field(default="main", min_length=1, max_length=200)
    auth_kind: Literal["none", "token", "ssh_key"] = "none"
    secret_id: str | None = None
    token: str | None = Field(default=None, description="write-only; stored as a generic Secret")


class GitPushIn(BaseModel):
    force: bool = False


class GitImportIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    url: str
    branch: str | None = None  # None → the remote's default branch
    auth_kind: Literal["none", "token", "ssh_key"] = "none"
    secret_id: str | None = None
    token: str | None = None
    host_id: str | None = None


# ------------------------------------------------------------------------------------------------ helpers
async def _busy(session: AsyncSession, lab: Lab, types: tuple[str, ...] = GIT_JOBS) -> None:
    running = (await session.execute(select(Job.id, Job.type).where(
        Job.lab_id == lab.id, Job.type.in_(types), Job.status.in_([JobStatus.queued, JobStatus.running])))).first()
    if running:
        raise HTTPException(409, {"message": f"{running[1]} is already running for this lab", "job_id": running[0]})


def _check_url(url: str) -> str:
    try:
        return labgit.check_url(url)
    except ValueError as exc:
        raise HTTPException(422, {"message": str(exc), "violations": [{"path": "url", "message": str(exc)}]}) from exc


async def _resolve_auth(session: AsyncSession, *, slug: str, auth_kind: str, secret_id: str | None, token: str | None,
                        current: dict[str, Any] | None = None) -> dict[str, Any]:
    """→ {kind, secret_id}. A token creates/replaces the Secret `git-token-<slug>` (kind generic)."""
    if auth_kind == "none":
        return {"kind": "none", "secret_id": None}
    if auth_kind == "token" and token:
        name = f"git-token-{slug}"
        sec = await secret_store.by_name(session, name)
        if sec is None:
            sec = await secret_store.create(session, name, SecretKind.generic, token, {"purpose": "git", "lab": slug})
        else:
            sec.ciphertext = secret_store.encrypt(token)
            await session.commit()
        return {"kind": "token", "secret_id": sec.id}
    if secret_id:
        if await session.get(Secret, secret_id) is None:
            raise HTTPException(422, {"message": "secret not found", "violations": [{"path": "secret_id", "message": "unknown secret"}]})
        return {"kind": auth_kind, "secret_id": secret_id}
    cur = (current or {}).get("auth") or {}
    if cur.get("kind") == auth_kind and cur.get("secret_id"):
        return {"kind": auth_kind, "secret_id": cur["secret_id"]}  # write-only token kept
    field = "token" if auth_kind == "token" else "secret_id"
    raise HTTPException(422, {"message": f"auth_kind {auth_kind} needs a {field}",
                              "violations": [{"path": field, "message": "required"}]})


# ------------------------------------------------------------------------------------------------ git
@router.get("/{lab_id}/git")
async def get_git(lab_id: str, session: AsyncSession = Depends(get_session), user: str = Depends(current_user)) -> dict[str, Any]:
    lab = await _get_lab(session, lab_id)
    return await asyncio.to_thread(labgit.git_status_out, lab)


@router.put("/{lab_id}/git")
async def put_git(lab_id: str, body: GitConfigIn, session: AsyncSession = Depends(get_session),
                  user: str = Depends(current_user)) -> dict[str, Any]:
    lab = await _get_lab(session, lab_id)
    url = _check_url(body.url)
    current = labgit.lab_git(lab)
    auth = await _resolve_auth(session, slug=lab.slug, auth_kind=body.auth_kind, secret_id=body.secret_id,
                               token=body.token, current=current)
    root = service.lab_dir(lab)
    await asyncio.to_thread(labgit.configure_remote, root, url)
    changed = await asyncio.to_thread(service.ensure_gitignore, root)
    if changed:
        await asyncio.to_thread(service.commit_all, root, "avdlab: .gitignore")
    cfg = {"url": url, "branch": body.branch, "auth": auth,
           "last_sync": (current or {}).get("last_sync") if current and current.get("url") == url else None}
    lab.settings = {**(lab.settings or {}), "git": cfg}
    await session.commit()
    return {"url": url, "branch": body.branch, "auth_kind": auth["kind"], "has_secret": bool(auth["secret_id"])}


@router.delete("/{lab_id}/git", status_code=204)
async def delete_git(lab_id: str, session: AsyncSession = Depends(get_session), user: str = Depends(current_user)) -> None:
    lab = await _get_lab(session, lab_id)
    await asyncio.to_thread(labgit.remove_remote, service.lab_dir(lab))
    st = dict(lab.settings or {})
    cfg = st.pop("git", None) or {}
    lab.settings = st
    sec_id = (cfg.get("auth") or {}).get("secret_id")
    if sec_id:  # only the token secret we created for this lab is removed; user-picked secrets stay
        sec = await session.get(Secret, sec_id)
        if sec is not None and sec.name == f"git-token-{lab.slug}":
            await session.delete(sec)
    await session.commit()


def _require_git(lab: Lab) -> None:
    if labgit.lab_git(lab) is None:
        raise HTTPException(409, "no git remote configured: PUT /labs/{id}/git first")


@router.post("/{lab_id}/git/push", status_code=202)
async def git_push(lab_id: str, body: GitPushIn | None = None, session: AsyncSession = Depends(get_session),
                   user: str = Depends(current_user)) -> dict[str, str]:
    lab = await _get_lab(session, lab_id)
    _require_git(lab)
    await _busy(session, lab)
    return {"job_id": await runner.submit("lab.git_push", lab_id=lab.id, params=(body or GitPushIn()).model_dump())}


@router.post("/{lab_id}/git/pull", status_code=202)
async def git_pull(lab_id: str, session: AsyncSession = Depends(get_session), user: str = Depends(current_user)) -> dict[str, str]:
    lab = await _get_lab(session, lab_id)
    _require_git(lab)
    await _busy(session, lab)
    return {"job_id": await runner.submit("lab.git_pull", lab_id=lab.id, params={})}


# ------------------------------------------------------------------------------------------------ import / export
async def _check_new_lab(session: AsyncSession, name: str, host_id: str | None) -> None:
    if (await session.execute(select(Lab.id).where(Lab.name == name))).first():
        raise HTTPException(409, "a lab with this name exists")
    if host_id and await session.get(Host, host_id) is None:
        raise HTTPException(422, {"message": "host not found", "violations": [{"path": "host_id", "message": "unknown host"}]})


@router.post("/import", status_code=201)
async def import_archive(file: UploadFile = File(...), name: str = Form(...), host_id: str | None = Form(None),
                         session: AsyncSession = Depends(get_session), user: str = Depends(current_user)) -> dict[str, Any]:
    """Create a custom lab from an archive. Invalid AVD inputs still create the lab (draft) with `import_warnings`."""
    name = name.strip()
    if not name:
        raise HTTPException(422, {"message": "name is required", "violations": [{"path": "name", "message": "required"}]})
    host_id = host_id or None
    await _check_new_lab(session, name, host_id)
    data = await file.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "archive too large (max 100 MB)")
    slug = await _unique_slug(session, name)
    try:
        await asyncio.to_thread(archive.import_archive_files, data, file.filename or "", slug)
    except archive.ImportError_ as exc:
        raise HTTPException(422, {"message": str(exc), "violations": [{"path": "file", "message": str(exc)}]}) from exc
    try:
        lab, warnings = await archive.create_custom_lab(name=name, slug=slug, host_id=host_id, source="archive",
                                                        log=lambda _l: None)
    except Exception:
        await asyncio.to_thread(service.delete_lab_dir, slug)
        raise
    out = _lab_out(await _get_lab(session, lab.id))
    out["import_warnings"] = warnings
    return out


@router.post("/import/git", status_code=202)
async def import_git(body: GitImportIn, session: AsyncSession = Depends(get_session),
                     user: str = Depends(current_user)) -> dict[str, str]:
    url = _check_url(body.url)
    await _check_new_lab(session, body.name, body.host_id)
    slug = await _unique_slug(session, body.name)
    auth = await _resolve_auth(session, slug=slug, auth_kind=body.auth_kind, secret_id=body.secret_id, token=body.token)
    params = {"name": body.name, "slug": slug, "url": url, "branch": body.branch, "auth": auth, "host_id": body.host_id}
    return {"job_id": await runner.submit("lab.import_git", params=params, host_id=body.host_id)}


@router.get("/{lab_id}/export")
async def export(lab_id: str, session: AsyncSession = Depends(get_session), user: str = Depends(current_user)) -> Response:
    lab = await _get_lab(session, lab_id)
    data = await asyncio.to_thread(archive.export_zip, service.lab_dir(lab))
    return Response(data, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{lab.slug}.zip"'})


# ------------------------------------------------------------------------------------------------ VS Code
VSCODE_HINTS = [
    "Install the VS Code extensions ms-vscode-remote.remote-ssh and srl-labs.vscode-containerlab.",
    "The containerlab extension discovers *.clab.yml in the opened workspace: opening the remote lab folder is enough "
    "(clab/topology.clab.yml).",
    "Use \"Sync files to host\" first so the remote folder shows the current files (deploy syncs too).",
    "Your ~/.ssh/config must let VS Code reach the host as shown in ssh_command (same key as avdlab).",
]


def open_links(lab: Lab, host: Host) -> dict[str, Any]:
    from ..executors.clab import TOPO_REL, remote_lab_dir

    rdir = remote_lab_dir(lab)
    target = f"{host.ssh_user}@{host.address}" + (f":{host.ssh_port}" if host.ssh_port and host.ssh_port != 22 else "")
    remote_ssh = f"vscode://vscode-remote/ssh-remote+{target}{rdir}"
    cfg = labgit.lab_git(lab)
    devcontainer = None
    if cfg and cfg["url"].startswith(("https://", "http://")):
        devcontainer = f"vscode://ms-vscode-remote.remote-containers/cloneInVolume?url={quote(cfg['url'], safe='')}"
    ssh_command = f"ssh {host.ssh_user}@{host.address}" + (f" -p {host.ssh_port}" if host.ssh_port and host.ssh_port != 22 else "")
    return {"remote_ssh": remote_ssh, "clab_topology_path": f"{rdir}/{TOPO_REL}",
            "remote_ssh_topology": f"{remote_ssh}/{TOPO_REL}", "devcontainer": devcontainer,
            "ssh_command": ssh_command, "hints": list(VSCODE_HINTS)}


@router.get("/{lab_id}/open-links")
async def get_open_links(lab_id: str, session: AsyncSession = Depends(get_session),
                         user: str = Depends(current_user)) -> dict[str, Any]:
    lab = await _get_lab(session, lab_id)
    if not lab.host_id or lab.host is None:
        raise HTTPException(409, "lab has no host: set one in the lab settings")
    return open_links(lab, lab.host)


@router.post("/{lab_id}/sync", status_code=202)
async def sync(lab_id: str, session: AsyncSession = Depends(get_session), user: str = Depends(current_user)) -> dict[str, str]:
    lab = await _get_lab(session, lab_id)
    if not lab.host_id:
        raise HTTPException(409, "lab has no host: set one in the lab settings")
    await _busy(session, lab, ("lab.sync", "lab.deploy", "lab.destroy"))
    return {"job_id": await runner.submit("lab.sync", lab_id=lab.id, host_id=lab.host_id, params={})}
