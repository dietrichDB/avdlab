"""Hosts API: CRUD, SSH key generation, probe/bootstrap/verify jobs, capabilities, devcontainer bundle."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Literal

import asyncssh
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.db import Host, HostKind, Lab, Secret, SecretKind, get_session
from ..core.security import current_user, secret_store
from ..jobs.engine import runner
from . import devcontainer
from . import probe as _probe  # noqa: F401  (registers host.probe)
from . import recipes as _recipes  # registers host.bootstrap / host.verify

router = APIRouter(prefix="/hosts", tags=["hosts"])


# ------------------------------------------------------------------ schemas
class HostIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    kind: HostKind = HostKind.linux
    address: str = Field(min_length=1, max_length=255)
    ssh_port: int = Field(default=22, ge=1, le=65535)
    ssh_user: str = Field(default="ubuntu", min_length=1, max_length=64)
    ssh_key_secret_id: str | None = None
    password_secret_id: str | None = None
    sudo_password_secret_id: str | None = None
    jump_host: str | None = Field(default=None, max_length=255, description="1.0.1: '[user@]host[:port]' SSH jump host, same credentials")
    platform: Literal["linux", "mac", "windows"] | None = Field(default=None, description="1.0.2: what the user says the host is; mac/windows = Linux VM behind the machine (beta)")


class HostPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    kind: HostKind | None = None
    address: str | None = Field(default=None, min_length=1, max_length=255)
    ssh_port: int | None = Field(default=None, ge=1, le=65535)
    ssh_user: str | None = Field(default=None, min_length=1, max_length=64)
    ssh_key_secret_id: str | None = None
    password_secret_id: str | None = None
    sudo_password_secret_id: str | None = None
    jump_host: str | None = Field(default=None, max_length=255, description="1.0.1: '[user@]host[:port]' SSH jump host, same credentials")
    platform: Literal["linux", "mac", "windows"] | None = Field(default=None, description="1.0.2: what the user says the host is; mac/windows = Linux VM behind the machine (beta)")


class HostOut(BaseModel):
    id: str
    name: str
    kind: HostKind
    address: str
    ssh_port: int
    ssh_user: str
    ssh_key_secret_id: str | None
    password_secret_id: str | None
    sudo_password_secret_id: str | None
    jump_host: str | None = None
    platform: str = "linux"
    last_probe_at: str | None
    created_at: str
    summary: dict[str, Any]

    @classmethod
    def from_row(cls, h: Host) -> "HostOut":
        caps = h.capabilities or {}
        return cls(
            id=h.id, name=h.name, kind=h.kind, address=h.address, ssh_port=h.ssh_port, ssh_user=h.ssh_user,
            ssh_key_secret_id=h.ssh_key_secret_id, password_secret_id=h.password_secret_id,
            sudo_password_secret_id=h.sudo_password_secret_id, jump_host=getattr(h, 'jump_host', None), platform=getattr(h, 'platform', None) or 'linux',
            last_probe_at=h.last_probe_at.isoformat() if h.last_probe_at else None, created_at=h.created_at.isoformat(),
            summary={
                "os": (caps.get("os") or {}).get("name"),
                "docker": (caps.get("docker") or {}).get("server_version"),
                "containerlab": (caps.get("containerlab") or {}).get("version"),
                "recommendations": len(caps.get("recommendations") or []),
                # 1.0: arch + platform for list badges / image defaults (full detail in /capabilities)
                "arch": (caps.get("os") or {}).get("arch"),
                "platform": (caps.get("os") or {}).get("platform"),
                "platform_label": (caps.get("os") or {}).get("platform_label"),
                "beta": bool((caps.get("os") or {}).get("beta")),
            },
        )


class KeyGenIn(BaseModel):
    name: str | None = Field(default=None, description="secret name; default ssh-key-<timestamp>")
    comment: str = "avdlab"


class KeyGenOut(BaseModel):
    secret_id: str
    name: str
    public_key: str


class BootstrapOptions(BaseModel):
    native_toolchain: bool = False
    clab_mode: Literal["native", "container"] = "native"


class BootstrapIn(BaseModel):
    recipe: str = "clab-host"
    options: BootstrapOptions = BootstrapOptions()


class DevcontainerIn(BaseModel):
    mode: Literal["local-clab", "remote-host"]
    avd_version: str | None = None
    include_vscode_extensions: bool = True


class JobRef(BaseModel):
    job_id: str


# ------------------------------------------------------------------ helpers
async def _get_host(session: AsyncSession, host_id: str) -> Host:
    h = await session.get(Host, host_id)
    if h is None:
        raise HTTPException(404, "host not found")
    return h


async def _validate_secret(session: AsyncSession, secret_id: str | None, kind: SecretKind) -> None:
    if secret_id is None:
        return
    s = await session.get(Secret, secret_id)
    if s is None:
        raise HTTPException(422, f"secret {secret_id} not found")
    if s.kind != kind:
        raise HTTPException(422, f"secret {secret_id} is {s.kind.value}, expected {kind.value}")


def _generate_keypair(comment: str) -> tuple[str, str]:
    key = asyncssh.generate_private_key("ssh-ed25519", comment=comment)
    return key.export_private_key("openssh").decode(), key.export_public_key("openssh").decode().strip()


# ------------------------------------------------------------------ static routes (before /{host_id})
@router.post("/keys/generate", response_model=KeyGenOut, status_code=201)
async def generate_key(body: KeyGenIn | None = None, session: AsyncSession = Depends(get_session),
                       user: str = Depends(current_user)) -> KeyGenOut:
    body = body or KeyGenIn()
    name = body.name or "ssh-key-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    if await secret_store.by_name(session, name):
        raise HTTPException(409, "secret name exists")
    private_pem, public = await asyncio.to_thread(_generate_keypair, body.comment)
    sec = await secret_store.create(session, name, SecretKind.ssh_key, private_pem, {"public_key": public, "type": "ed25519"})
    return KeyGenOut(secret_id=sec.id, name=sec.name, public_key=public)


@router.post("/devcontainer")
async def devcontainer_bundle(body: DevcontainerIn, user: str = Depends(current_user)) -> Response:
    files = devcontainer.generate_files(body.mode, avd_version=body.avd_version, include_vscode_extensions=body.include_vscode_extensions)
    data = await asyncio.to_thread(devcontainer.build_zip, files)
    fname = f"avdlab-devcontainer-{body.mode}.zip"
    return Response(content=data, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@router.get("/recipes")
async def list_recipes(user: str = Depends(current_user)) -> list[dict[str, Any]]:
    return [{"name": name, "options": BootstrapOptions.model_json_schema()} for name in sorted(_recipes.RECIPES)]


# ------------------------------------------------------------------ CRUD
@router.get("", response_model=list[HostOut])
async def list_hosts(session: AsyncSession = Depends(get_session), user: str = Depends(current_user)) -> list[HostOut]:
    rows = (await session.execute(select(Host).order_by(Host.name))).scalars()
    return [HostOut.from_row(h) for h in rows]


@router.post("", response_model=HostOut, status_code=201)
async def create_host(body: HostIn, session: AsyncSession = Depends(get_session), user: str = Depends(current_user)) -> HostOut:
    if (await session.execute(select(Host).where(Host.name == body.name))).scalar_one_or_none():
        raise HTTPException(409, "host name exists")
    await _validate_secret(session, body.ssh_key_secret_id, SecretKind.ssh_key)
    await _validate_secret(session, body.password_secret_id, SecretKind.password)
    await _validate_secret(session, body.sudo_password_secret_id, SecretKind.password)
    data = body.model_dump()
    data["platform"] = data.get("platform") or "linux"
    h = Host(**data)
    session.add(h)
    await session.commit()
    await session.refresh(h)
    return HostOut.from_row(h)


@router.get("/{host_id}", response_model=HostOut)
async def get_host(host_id: str, session: AsyncSession = Depends(get_session), user: str = Depends(current_user)) -> HostOut:
    return HostOut.from_row(await _get_host(session, host_id))


@router.patch("/{host_id}", response_model=HostOut)
async def patch_host(host_id: str, body: HostPatch, session: AsyncSession = Depends(get_session),
                     user: str = Depends(current_user)) -> HostOut:
    h = await _get_host(session, host_id)
    changes = body.model_dump(exclude_unset=True)
    if "name" in changes and changes["name"] != h.name:
        if (await session.execute(select(Host).where(Host.name == changes["name"]))).scalar_one_or_none():
            raise HTTPException(409, "host name exists")
    if "ssh_key_secret_id" in changes:
        await _validate_secret(session, changes["ssh_key_secret_id"], SecretKind.ssh_key)
    if "password_secret_id" in changes:
        await _validate_secret(session, changes["password_secret_id"], SecretKind.password)
    if "sudo_password_secret_id" in changes:
        await _validate_secret(session, changes["sudo_password_secret_id"], SecretKind.password)
    for k, v in changes.items():
        setattr(h, k, v)
    await session.commit()
    await session.refresh(h)
    return HostOut.from_row(h)


@router.delete("/{host_id}", status_code=204)
async def delete_host(host_id: str, session: AsyncSession = Depends(get_session), user: str = Depends(current_user)) -> None:
    h = await _get_host(session, host_id)
    attached = (await session.execute(select(Lab.id).where(Lab.host_id == h.id).limit(1))).first()
    if attached:
        raise HTTPException(409, "host has labs attached")
    await session.delete(h)
    await session.commit()


@router.get("/{host_id}/capabilities")
async def capabilities(host_id: str, session: AsyncSession = Depends(get_session), user: str = Depends(current_user)) -> dict[str, Any]:
    h = await _get_host(session, host_id)
    return {"host_id": h.id, "last_probe_at": h.last_probe_at.isoformat() if h.last_probe_at else None,
            "capabilities": h.capabilities or {}}


# ------------------------------------------------------------------ jobs
@router.post("/{host_id}/probe", response_model=JobRef, status_code=202)
async def probe(host_id: str, session: AsyncSession = Depends(get_session), user: str = Depends(current_user)) -> JobRef:
    h = await _get_host(session, host_id)
    return JobRef(job_id=await runner.submit("host.probe", host_id=h.id, params={}))


@router.post("/{host_id}/bootstrap", response_model=JobRef, status_code=202)
async def bootstrap(host_id: str, body: BootstrapIn | None = None, session: AsyncSession = Depends(get_session),
                    user: str = Depends(current_user)) -> JobRef:
    body = body or BootstrapIn()
    if body.recipe not in _recipes.RECIPES:
        raise HTTPException(422, f"unknown recipe {body.recipe!r}")
    h = await _get_host(session, host_id)
    params = {"recipe": body.recipe, "options": body.options.model_dump()}
    return JobRef(job_id=await runner.submit("host.bootstrap", host_id=h.id, params=params))


@router.post("/{host_id}/verify", response_model=JobRef, status_code=202)
async def verify(host_id: str, session: AsyncSession = Depends(get_session), user: str = Depends(current_user)) -> JobRef:
    h = await _get_host(session, host_id)
    return JobRef(job_id=await runner.submit("host.verify", host_id=h.id, params={}))



# ================================================================ phase 3 — backend A: host shell terminal
from fastapi import WebSocket  # noqa: E402


@router.websocket("/{host_id}/terminal")
async def host_terminal(ws: WebSocket, host_id: str, cols: int = 80, rows: int = 24) -> None:
    """WS /hosts/{id}/terminal?cols=&rows= → `$SHELL -l` on a PTY (binary frames; JSON {"resize": {cols, rows}})."""
    from ..core.db import SessionLocal
    from ..executors import terminal

    if not ws.session.get("user"):
        await ws.close(code=terminal.CLOSE_UNAUTHORIZED)
        return
    async with SessionLocal() as s:
        host = await s.get(Host, host_id)
    await ws.accept()
    if host is None:
        await ws.send_bytes(b"[avdlab] host not found\r\n")
        await ws.close(code=terminal.CLOSE_NOT_FOUND, reason="host not found")
        return
    c, r = terminal.clamp_size(cols, rows)
    await terminal.serve(ws, host, terminal.HOST_COMMAND, cols=c, rows=r, label=f"host={host.name}")
# ================================================================ end phase 3 — backend A
