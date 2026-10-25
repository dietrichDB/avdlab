"""Images API: arista.com catalogue, download/push jobs, manual upload.

Endpoints (under /api/images):
  GET  /images/catalog?fmt=cEOS[&refresh=true]   versions (cache 1 h); 409 no token, 502 arista.com unreachable
  GET  /images/formats                            [{fmt, arch, label}] for cEOS / cEOS64 / cEOSarm
  GET  /images/token                              {configured, secret_id, name, created_at} (never the value)
  PUT  /images/token {value}                      create or replace the arista.com token (secret `arista_token`), drops the catalogue cache
  DELETE /images/token                            remove the token
  GET  /images                                    image store
  GET  /images/{id}, DELETE /images/{id}
  POST /images/download {fmt, version, force?}    202 job image.download
  POST /images/{id}/push/{host_id} {options: {remote_download?, force?}}   202 job image.push
  POST /images/upload (multipart: file, fmt, version)                       stores + docker import when available
"""
from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import settings
from ..core.db import Host, Image, SecretKind, get_session
from ..core.security import current_user, secret_store
from ..jobs.engine import JobContext, job, runner
from . import docker as docker_mod
from . import service

router = APIRouter(prefix="/images", tags=["images"])


class DownloadIn(BaseModel):
    fmt: str = "cEOS"
    version: str
    force: bool = False


class PushOptions(BaseModel):
    remote_download: bool = False
    force: bool = False
    sudo: bool | None = Field(default=None, description="run docker with sudo on the host; default: from probe")


class PushIn(BaseModel):
    options: PushOptions = PushOptions()


class JobRef(BaseModel):
    job_id: str


class TokenIn(BaseModel):
    value: str = Field(min_length=8, description="arista.com API token (arista.com > My Profile > Access Token)")


def _check_fmt(fmt: str) -> str:
    if fmt not in service.supported_formats():
        raise HTTPException(422, f"unsupported format {fmt!r}; supported: {service.supported_formats()}")
    return fmt


def _check_version(version: str) -> str:
    try:
        return service.validate_version(version)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


# ------------------------------------------------------------------------------------------------ endpoints
@router.get("/formats")
async def formats(user: str = Depends(current_user)) -> list[dict[str, str]]:
    return service.formats()


async def _token_secret(session: AsyncSession):
    sec = await secret_store.by_name(session, service.TOKEN_SECRET_NAME)
    if sec is None:
        from ..core.db import Secret

        sec = (await session.execute(select(Secret).where(Secret.kind == SecretKind.arista_token))).scalars().first()
    return sec


def _token_out(sec) -> dict[str, Any]:
    if sec is None:
        return {"configured": False, "secret_id": None, "name": None, "created_at": None, "hint": service.NoTokenError.hint}
    return {"configured": True, "secret_id": sec.id, "name": sec.name, "created_at": sec.created_at.isoformat(), "hint": None}


@router.get("/token")
async def get_token(session: AsyncSession = Depends(get_session), user: str = Depends(current_user)) -> dict[str, Any]:
    """Whether an arista.com token is configured (the value is never returned)."""
    return _token_out(await _token_secret(session))


@router.put("/token")
async def put_token(body: TokenIn, session: AsyncSession = Depends(get_session), user: str = Depends(current_user)) -> dict[str, Any]:
    """Create or replace the arista.com token in place (same secret row) and forget cached catalogue answers."""
    value = body.value.strip()
    sec = await _token_secret(session)
    if sec is None:
        sec = await secret_store.create(session, service.TOKEN_SECRET_NAME, SecretKind.arista_token, value)
    else:
        sec.ciphertext = secret_store.encrypt(value)
        await session.commit()
        await session.refresh(sec)
    service.clear_cache()
    return _token_out(sec)


@router.delete("/token", status_code=204)
async def delete_token(session: AsyncSession = Depends(get_session), user: str = Depends(current_user)) -> None:
    sec = await _token_secret(session)
    if sec is not None:
        await session.delete(sec)
        await session.commit()
    service.clear_cache()


@router.get("/catalog")
async def catalog(fmt: str = "cEOS", refresh: bool = False, session: AsyncSession = Depends(get_session),
                  user: str = Depends(current_user)) -> dict[str, Any]:
    _check_fmt(fmt)
    try:
        token = await service.resolve_token(session)
    except service.NoTokenError:
        raise HTTPException(409, {"message": "no arista.com token configured", "hint": service.NoTokenError.hint})
    try:
        return await service.get_catalog(fmt, token, refresh=refresh)
    except service.CatalogError as exc:
        raise HTTPException(502, {"message": str(exc), "hint": "check the token and outbound HTTPS to arista.com"})


@router.get("")
async def list_images(session: AsyncSession = Depends(get_session), user: str = Depends(current_user)) -> list[dict[str, Any]]:
    rows = (await session.execute(select(Image).order_by(Image.fmt, Image.version.desc()))).scalars()
    return [service.image_out(i) for i in rows]


async def _get_image(session: AsyncSession, image_id: str) -> Image:
    img = await session.get(Image, image_id)
    if img is None:
        raise HTTPException(404, "image not found")
    return img


@router.get("/{image_id}")
async def get_image(image_id: str, session: AsyncSession = Depends(get_session), user: str = Depends(current_user)) -> dict[str, Any]:
    return service.image_out(await _get_image(session, image_id))


@router.delete("/{image_id}", status_code=204)
async def delete_image(image_id: str, delete_file: bool = True, session: AsyncSession = Depends(get_session),
                       user: str = Depends(current_user)) -> None:
    img = await _get_image(session, image_id)
    if delete_file and img.local_path:
        p = Path(img.local_path)
        if p.is_file() and settings.images_dir.resolve() in p.resolve().parents:
            p.unlink()
    await session.delete(img)
    await session.commit()


@router.post("/download", response_model=JobRef, status_code=202)
async def download(body: DownloadIn, session: AsyncSession = Depends(get_session), user: str = Depends(current_user)) -> JobRef:
    _check_fmt(body.fmt)
    _check_version(body.version)
    try:
        await service.resolve_token(session)
    except service.NoTokenError:
        raise HTTPException(409, {"message": "no arista.com token configured", "hint": service.NoTokenError.hint})
    return JobRef(job_id=await runner.submit("image.download", params=body.model_dump()))


@router.post("/{image_id}/push/{host_id}", response_model=JobRef, status_code=202)
async def push(image_id: str, host_id: str, body: PushIn | None = None, session: AsyncSession = Depends(get_session),
               user: str = Depends(current_user)) -> JobRef:
    img = await _get_image(session, image_id)
    if await session.get(Host, host_id) is None:
        raise HTTPException(404, "host not found")
    opts = (body or PushIn()).options
    if not img.docker_tag:
        raise HTTPException(422, f"{img.fmt} is not a container image; only cEOS formats can be pushed")
    host = await session.get(Host, host_id)
    mismatch = service.arch_mismatch(host.capabilities if host else None,
                                     img.arch or service.format_arch(img.fmt) or service.tag_arch(img.docker_tag))
    if mismatch:
        raise HTTPException(409, mismatch)
    if not opts.remote_download and not img.local_path:
        # without a tarball we fall back to `docker save` on the API host
        if not await docker_mod.get_backend().available():
            raise HTTPException(409, "image has no local file and docker is not available here; use options.remote_download")
    return JobRef(job_id=await runner.submit("image.push", host_id=host_id,
                                             params={"image_id": image_id, "options": opts.model_dump()}))


@router.post("/upload", status_code=201)
async def upload(file: UploadFile = File(...), fmt: str = Form("cEOS"), version: str = Form(...),
                 session: AsyncSession = Depends(get_session), user: str = Depends(current_user)) -> dict[str, Any]:
    _check_fmt(fmt)
    _check_version(version)
    name = Path(file.filename or "").name or service.image_filename(fmt, version)
    if fmt in service.DOCKER_FORMATS and not name.endswith((".tar.xz", ".tar", ".tar.gz")):
        raise HTTPException(422, "cEOS uploads must be .tar.xz (cEOS-lab-<version>.tar.xz)")
    settings.images_dir.mkdir(parents=True, exist_ok=True)
    dest = settings.images_dir / name
    tmp = dest.with_suffix(dest.suffix + ".part")
    with tmp.open("wb") as fh:
        while chunk := await file.read(1 << 20):
            fh.write(chunk)
    tmp.replace(dest)
    img = await service.upsert_image(session, fmt, version, dest)
    out = service.image_out(img)
    out["imported"] = False
    be = docker_mod.get_backend()
    if img.docker_tag and await be.available():
        try:
            await be.import_tarball(dest, img.docker_tag)
            out["imported"] = True
        except docker_mod.DockerError as exc:
            out["import_error"] = str(exc)
    return out


# ------------------------------------------------------------------------------------------------ jobs
@job("image.download")
async def image_download(ctx: JobContext) -> dict[str, Any]:
    fmt = ctx.params.get("fmt", "cEOS")
    version = service.validate_version(ctx.params["version"])
    async with ctx.session() as s:
        token = await service.resolve_token(s)
    be = docker_mod.get_backend()
    dest_dir = settings.images_dir
    dest_dir.mkdir(parents=True, exist_ok=True)
    tag = service.docker_tag(fmt, version)
    existing = service.find_downloaded(fmt, version, dest_dir)
    imported = False
    can_import = bool(tag) and await be.available()
    if existing and not ctx.params.get("force"):
        ctx.log(f"{existing.name} already downloaded — skipping (force=true to re-download)")
        path = existing
    else:
        args = ["get", "eos", "--format", fmt, "--version", version, "--output", str(dest_dir), "--no-progress"]
        if ctx.params.get("force"):
            args.append("--force")
        if can_import:
            args += ["--import-docker", "--docker-name", tag.split(":")[0], "--docker-tag", tag.split(":", 1)[1]]
        elif tag:
            ctx.log("docker is not available on the API host: downloading only (push it to a host to import)")
        ctx.log("$ ardl " + " ".join(shlex.quote(a) for a in args))
        res = await be.run_ardl(args, token=token, log=ctx.log, cwd=dest_dir)
        if not res.ok:
            raise RuntimeError(f"ardl failed with exit code {res.returncode}")
        imported = can_import
        path = service.find_downloaded(fmt, version, dest_dir)
        if path is None:
            raise RuntimeError(f"ardl succeeded but no file for {fmt} {version} found in {dest_dir}")
    if tag and can_import and not imported and not await be.image_exists(tag):
        await be.import_tarball(path, tag, log=ctx.log)
        imported = True
    async with ctx.session() as s:
        img = await service.upsert_image(s, fmt, version, path)
        out = service.image_out(img)
    ctx.artefact(path.name, path)
    ctx.log(f"image {fmt} {version}: {path} ({out['size_bytes']} bytes, sha256 {str(out['sha256'])[:12]}…)")
    return {"image": out, "imported": imported}


@job("image.push")
async def image_push(ctx: JobContext) -> dict[str, Any]:
    from ..hosts import ssh as ssh_mod

    opts = PushOptions(**(ctx.params.get("options") or {}))
    host = await ctx.get_host()
    async with ctx.session() as s:
        img = await s.get(Image, ctx.params["image_id"])
        if img is None:
            raise RuntimeError("image not found")
        fmt, version, tag, local_path = img.fmt, img.version, img.docker_tag, img.local_path
        image_arch = img.arch or service.format_arch(fmt) or service.tag_arch(tag)
    mismatch = service.arch_mismatch(host.capabilities, image_arch)
    if mismatch:
        raise RuntimeError(f"{mismatch}: push an image of the host's architecture")
    sudo = opts.sudo if opts.sudo is not None else not ((host.capabilities or {}).get("docker") or {}).get("user_can_run", True)
    remote_dir = f"{settings.remote_root}/images"
    method = "present"

    async with ctx.session() as s:
        async with ssh_mod.SSHClient.connect(host, s, log=ctx.log, ctx=ctx) as ssh:
            present = (await ssh.run(f"docker image inspect {shlex.quote(tag)} >/dev/null 2>&1", sudo=sudo)).ok
            if present and not opts.force:
                ctx.log(f"{tag} already present on {host.name}")
            else:
                await ssh.run(f"mkdir -p {shlex.quote(remote_dir)}", log=ctx.log)
                if opts.remote_download:
                    method = "remote_download"
                    token = await service.resolve_token(s)
                    cmd = (f"read -r ARISTA_TOKEN && export ARISTA_TOKEN && ardl get eos --format {shlex.quote(fmt)} "
                           f"--version {shlex.quote(version)} --output {shlex.quote(remote_dir)} --no-progress "
                           f"--import-docker --docker-name {shlex.quote(tag.split(':')[0])} "
                           f"--docker-tag {shlex.quote(tag.split(':', 1)[1])}")
                    ctx.log(f"running ardl on {host.name} (token passed on stdin)")
                    res = await ssh.run(cmd, log=ctx.log, timeout=3600, input=token + "\n")
                    if not res.ok:
                        raise RuntimeError(f"remote ardl failed ({res.returncode})")
                elif local_path and Path(local_path).exists():
                    method = "import"
                    await service.import_tarball_on_host(ssh, local_path, tag, sudo=sudo, log=ctx.log, remote_dir=remote_dir)
                else:
                    method = "save_load"
                    be = docker_mod.get_backend()
                    tar = await be.save(tag, ctx.dir / f"{tag.replace('/', '_').replace(':', '_')}.tar", log=ctx.log)
                    remote = f"{remote_dir}/{tar.name}"
                    await ssh.put(tar, remote)
                    res = await ssh.run(f"docker load -i {shlex.quote(remote)} && rm -f {shlex.quote(remote)}", sudo=sudo,
                                        log=ctx.log, timeout=1800)
                    tar.unlink(missing_ok=True)
                    if not res.ok:
                        raise RuntimeError(f"docker load on {host.name} failed ({res.returncode})")
                if not (await ssh.run(f"docker image inspect {shlex.quote(tag)} >/dev/null 2>&1", sudo=sudo)).ok:
                    raise RuntimeError(f"{tag} not present on {host.name} after push")
        row = await s.get(Image, ctx.params["image_id"])
        if row is not None and host.id not in (row.present_on_hosts or []):
            row.present_on_hosts = [*(row.present_on_hosts or []), host.id]
            await s.commit()
    ctx.log(f"{tag} available on {host.name} ({method})")
    return {"image_id": ctx.params["image_id"], "host_id": host.id, "docker_tag": tag, "method": method}
