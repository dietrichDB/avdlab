"""NetBox API (Module 6): targets (write-only tokens), connection test, lab sync job, lab NetBox status.

  GET/POST /netbox/targets             GET/PATCH/DELETE /netbox/targets/{id}      POST /netbox/targets/{id}/test
  POST /labs/{id}/netbox/sync {delete_stale, dry_run, include_clients} → 202 {job_id} (job lab.netbox_sync)
  GET  /labs/{id}/netbox/status → {target, last_sync}

The lab ↔ target binding is `PATCH /labs/{id} {netbox_target_id}` (labs/api.py).
"""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.db import Job, JobStatus, Lab, NetBoxTarget, Secret, get_session
from ..core.security import current_user
from ..jobs.engine import runner
from ..labs import service as lab_service
from . import jobs as nb_jobs  # registers lab.netbox_sync
from . import service as nbs

targets = APIRouter(prefix="/netbox", tags=["netbox"])
labs_nb = APIRouter(prefix="/labs", tags=["netbox"])
router = APIRouter()


class TargetIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    url: str = Field(min_length=1, max_length=255)
    verify_certs: bool = True
    token: str | None = None  # write-only


class TargetPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    url: str | None = Field(default=None, min_length=1, max_length=255)
    verify_certs: bool | None = None
    token: str | None = None  # omitted → keep; null or '' → remove


async def target_out(session: AsyncSession, t: NetBoxTarget) -> dict[str, Any]:
    token_meta = None
    if t.token_secret_id:
        sec = await session.get(Secret, t.token_secret_id)
        if sec is not None:
            token_meta = {"updated_at": (sec.meta or {}).get("updated_at")}
    labs = (await session.execute(select(Lab.id, Lab.name).where(Lab.netbox_target_id == t.id)
                                  .order_by(Lab.name))).all()
    return {"id": t.id, "name": t.name, "url": t.url, "verify_certs": bool(t.verify_certs),
            "has_token": bool(t.token_secret_id), "token_meta": token_meta,
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "labs": [{"id": i, "name": n} for i, n in labs]}


async def _get_target(session: AsyncSession, target_id: str) -> NetBoxTarget:
    t = await session.get(NetBoxTarget, target_id)
    if t is None:
        raise HTTPException(404, "NetBox target not found")
    return t


def _url(raw: str) -> str:
    try:
        return nbs.clean_url(raw)
    except nbs.NetBoxError as exc:
        raise HTTPException(422, str(exc)) from exc


async def _apply_token(session: AsyncSession, t: NetBoxTarget, body: TargetIn | TargetPatch) -> None:
    token = "" if ("token" in body.model_fields_set and body.token is None) else body.token
    try:
        await nbs.set_target_token(session, t, token)
    except nbs.NetBoxError as exc:
        raise HTTPException(422, str(exc)) from exc


# ------------------------------------------------------------------------------------------------ targets
@targets.get("/targets")
async def list_targets(session: AsyncSession = Depends(get_session), user: str = Depends(current_user)) -> list[dict[str, Any]]:
    rows = (await session.execute(select(NetBoxTarget).order_by(NetBoxTarget.name))).scalars().all()
    return [await target_out(session, t) for t in rows]


@targets.post("/targets", status_code=201)
async def create_target(body: TargetIn, session: AsyncSession = Depends(get_session),
                        user: str = Depends(current_user)) -> dict[str, Any]:
    if (await session.execute(select(NetBoxTarget.id).where(NetBoxTarget.name == body.name))).first():
        raise HTTPException(409, "a NetBox target with this name exists")
    t = NetBoxTarget(name=body.name, url=_url(body.url), verify_certs=body.verify_certs)
    session.add(t)
    await session.flush()
    await _apply_token(session, t, body)
    await session.commit()
    return await target_out(session, t)


@targets.get("/targets/{target_id}")
async def get_target(target_id: str, session: AsyncSession = Depends(get_session),
                     user: str = Depends(current_user)) -> dict[str, Any]:
    return await target_out(session, await _get_target(session, target_id))


@targets.patch("/targets/{target_id}")
async def patch_target(target_id: str, body: TargetPatch, session: AsyncSession = Depends(get_session),
                       user: str = Depends(current_user)) -> dict[str, Any]:
    t = await _get_target(session, target_id)
    if body.name and body.name != t.name:
        if (await session.execute(select(NetBoxTarget.id).where(NetBoxTarget.name == body.name,
                                                                NetBoxTarget.id != t.id))).first():
            raise HTTPException(409, "a NetBox target with this name exists")
        t.name = body.name
    if body.url:
        t.url = _url(body.url)
    if body.verify_certs is not None:
        t.verify_certs = body.verify_certs
    await _apply_token(session, t, body)
    await session.commit()
    return await target_out(session, t)


@targets.delete("/targets/{target_id}", status_code=204)
async def delete_target(target_id: str, session: AsyncSession = Depends(get_session),
                        user: str = Depends(current_user)) -> None:
    t = await _get_target(session, target_id)
    bound = (await session.execute(select(Lab.name).where(Lab.netbox_target_id == t.id))).scalars().all()
    if bound:
        raise HTTPException(409, {"message": f"target is used by {len(bound)} lab(s): unbind them first", "labs": bound})
    sid = t.token_secret_id
    await session.delete(t)
    await session.flush()
    if sid:
        sec = await session.get(Secret, sid)
        if sec is not None:
            await session.delete(sec)
    await session.commit()


@targets.post("/targets/{target_id}/test")
async def test_target(target_id: str, session: AsyncSession = Depends(get_session),
                      user: str = Depends(current_user)) -> dict[str, Any]:
    """Inline pynetbox `status()` (15 s budget) → `{ok, version, error}`."""
    t = await _get_target(session, target_id)
    token = await nbs.target_token(session, t)
    try:
        return await asyncio.wait_for(asyncio.to_thread(nbs.check_target, t.url, token,
                                                        verify_certs=bool(t.verify_certs)), nbs.TEST_BUDGET + 1)
    except TimeoutError:
        return {"ok": False, "version": None, "error": f"no answer within {int(nbs.TEST_BUDGET)} s"}


# ------------------------------------------------------------------------------------------------ labs
async def _lab(session: AsyncSession, lab_id: str) -> Lab:
    lab = await session.get(Lab, lab_id)
    if lab is None:
        raise HTTPException(404, "lab not found")
    return lab


@labs_nb.post("/{lab_id}/netbox/sync", status_code=202)
async def sync(lab_id: str, body: nb_jobs.NetBoxSyncParams | None = None, session: AsyncSession = Depends(get_session),
               user: str = Depends(current_user)) -> dict[str, str]:
    lab = await _lab(session, lab_id)
    if lab.state not in nb_jobs.SYNC_STATES:
        raise HTTPException(409, f"lab is {lab.state.value}: build it before syncing to NetBox")
    sc_dir = lab_service.lab_dir(lab) / "intended" / "structured_configs"
    if not sc_dir.is_dir() or not any(sc_dir.glob("*.yml")):
        raise HTTPException(409, "no structured configs: build the lab first")
    if not lab.netbox_target_id:
        raise HTTPException(409, "lab has no NetBox target: pick one in the lab's NetBox tab")
    t = await session.get(NetBoxTarget, lab.netbox_target_id)
    if t is None:
        raise HTTPException(409, "the lab's NetBox target no longer exists")
    if not t.token_secret_id:
        raise HTTPException(409, f"NetBox target {t.name} has no API token")
    running = (await session.execute(select(Job.id, Job.type).where(
        Job.lab_id == lab.id, Job.type.in_(("lab.build", "lab.netbox_sync")),
        Job.status.in_([JobStatus.queued, JobStatus.running])))).first()
    if running:
        raise HTTPException(409, {"message": f"{running[1]} is already running for this lab", "job_id": running[0]})
    params = (body or nb_jobs.NetBoxSyncParams()).model_dump()
    return {"job_id": await runner.submit("lab.netbox_sync", lab_id=lab.id, host_id=lab.host_id, params=params)}


@labs_nb.get("/{lab_id}/netbox/status")
async def status(lab_id: str, session: AsyncSession = Depends(get_session),
                 user: str = Depends(current_user)) -> dict[str, Any]:
    lab = await _lab(session, lab_id)
    t = await session.get(NetBoxTarget, lab.netbox_target_id) if lab.netbox_target_id else None
    return {"target": await target_out(session, t) if t is not None else None,
            "last_sync": (lab.settings or {}).get("netbox_last_sync")}


router.include_router(targets)
router.include_router(labs_nb)
