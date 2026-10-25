"""CloudVision API: targets (write-only tokens), connection test, lab onboarding / cv_deploy jobs, lab CV status.

  GET/POST /cv/targets                 GET/PATCH/DELETE /cv/targets/{id}          POST /cv/targets/{id}/test
  POST /labs/{id}/cv/onboard → 202 job lab.cv_onboard
  POST /labs/{id}/cv/deploy {submit_workspace, run_change_control, skip_missing_devices, strict_tags, workspace_name?}
       → 202 job lab.cv_deploy
  GET  /labs/{id}/cv/status → {mode, target, onboarding, last_deploy}

The lab ↔ target binding is `PATCH /labs/{id} {cv_target_id}` (labs/api.py).
"""
from __future__ import annotations

import asyncio
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.db import CVTarget, Lab, Secret, get_session
from ..core.security import current_user
from ..jobs.engine import runner
from ..labs import service as lab_service
from . import jobs as cv_jobs  # noqa: F401 - registers lab.cv_onboard / lab.cv_deploy
from . import service as cv

targets = APIRouter(prefix="/cv", tags=["cloudvision"])
labs_cv = APIRouter(prefix="/labs", tags=["cloudvision"])
router = APIRouter()


# ------------------------------------------------------------------------------------------------ schemas
class TargetIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    kind: Literal["cvaas", "onprem"]
    server: str = Field(min_length=1, max_length=255)
    verify_certs: bool = True
    token: str | None = None  # write-only; '' on PATCH removes it
    onboarding_token: str | None = None  # write-only; '' on PATCH removes it
    onboarding_token_expires_at: str | None = None  # stored in the onboarding secret's meta
    ztp_script: str | None = None  # phase 4 (A): CV ZTP bootstrap script, write-only; '' / null on PATCH removes it


class TargetPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    kind: Literal["cvaas", "onprem"] | None = None
    server: str | None = Field(default=None, min_length=1, max_length=255)
    verify_certs: bool | None = None
    token: str | None = None
    onboarding_token: str | None = None
    onboarding_token_expires_at: str | None = None
    ztp_script: str | None = None


async def _target_out(session: AsyncSession, t: CVTarget) -> dict[str, Any]:
    meta = None
    if t.onboarding_token_secret_id:
        sec = await session.get(Secret, t.onboarding_token_secret_id)
        if sec is not None:
            meta = {"expires_at": (sec.meta or {}).get("expires_at"), "updated_at": (sec.meta or {}).get("updated_at")}
    token_meta = None
    if t.token_secret_id:
        sec = await session.get(Secret, t.token_secret_id)
        if sec is not None:
            token_meta = {"updated_at": (sec.meta or {}).get("updated_at")}
    labs = (await session.execute(select(Lab.id, Lab.name).where(Lab.cv_target_id == t.id).order_by(Lab.name))).all()
    ztp_sec = await cv.target_ztp_script_secret(session, t.id)
    return {"id": t.id, "name": t.name, "kind": t.kind, "server": t.server, "verify_certs": bool(t.verify_certs),
            "has_token": bool(t.token_secret_id), "token_meta": token_meta,
            "has_onboarding_token": bool(t.onboarding_token_secret_id), "onboarding_token_meta": meta,
            "has_ztp_script": ztp_sec is not None,
            "ztp_script_meta": {"updated_at": (ztp_sec.meta or {}).get("updated_at")} if ztp_sec is not None else None,
            "labs": [{"id": i, "name": n} for i, n in labs]}


async def _get_target(session: AsyncSession, target_id: str) -> CVTarget:
    t = await session.get(CVTarget, target_id)
    if t is None:
        raise HTTPException(404, "CloudVision target not found")
    return t


def _clean_server(server: str) -> str:
    s = server.strip().rstrip("/")
    if not cv.server_host(s):
        raise HTTPException(422, "server must be a hostname or IP")
    return s


async def _apply_tokens(session: AsyncSession, t: CVTarget, body: TargetIn | TargetPatch) -> None:
    # a field explicitly sent as null means "remove" (the UI sends null); omitted means "keep"
    token = "" if ("token" in body.model_fields_set and body.token is None) else body.token
    onboarding = "" if ("onboarding_token" in body.model_fields_set and body.onboarding_token is None) else body.onboarding_token
    try:
        await cv.set_target_secret(session, t, "token", token)
        meta = {"expires_at": body.onboarding_token_expires_at} if "onboarding_token_expires_at" in body.model_fields_set else None
        await cv.set_target_secret(session, t, "onboarding", onboarding, meta)
        ztp_script = "" if ("ztp_script" in body.model_fields_set and body.ztp_script is None) else body.ztp_script
        await cv.set_target_ztp_script(session, t, ztp_script)
    except cv.CVError as exc:
        raise HTTPException(422, str(exc)) from exc


# ------------------------------------------------------------------------------------------------ targets
@targets.get("/targets")
async def list_targets(session: AsyncSession = Depends(get_session), user: str = Depends(current_user)) -> list[dict[str, Any]]:
    rows = (await session.execute(select(CVTarget).order_by(CVTarget.name))).scalars().all()
    return [await _target_out(session, t) for t in rows]


@targets.post("/targets", status_code=201)
async def create_target(body: TargetIn, session: AsyncSession = Depends(get_session),
                        user: str = Depends(current_user)) -> dict[str, Any]:
    if (await session.execute(select(CVTarget.id).where(CVTarget.name == body.name))).first():
        raise HTTPException(409, "a CloudVision target with this name exists")
    t = CVTarget(name=body.name, kind=body.kind, server=_clean_server(body.server), verify_certs=body.verify_certs)
    session.add(t)
    await session.flush()
    await _apply_tokens(session, t, body)
    await session.commit()
    return await _target_out(session, t)


@targets.get("/targets/{target_id}")
async def get_target(target_id: str, session: AsyncSession = Depends(get_session),
                     user: str = Depends(current_user)) -> dict[str, Any]:
    return await _target_out(session, await _get_target(session, target_id))


@targets.patch("/targets/{target_id}")
async def patch_target(target_id: str, body: TargetPatch, session: AsyncSession = Depends(get_session),
                       user: str = Depends(current_user)) -> dict[str, Any]:
    t = await _get_target(session, target_id)
    if body.name and body.name != t.name:
        if (await session.execute(select(CVTarget.id).where(CVTarget.name == body.name, CVTarget.id != t.id))).first():
            raise HTTPException(409, "a CloudVision target with this name exists")
        t.name = body.name
    if body.kind:
        t.kind = body.kind
    if body.server:
        t.server = _clean_server(body.server)
    if body.verify_certs is not None:
        t.verify_certs = body.verify_certs
    await _apply_tokens(session, t, body)
    await session.commit()
    return await _target_out(session, t)


@targets.delete("/targets/{target_id}", status_code=204)
async def delete_target(target_id: str, session: AsyncSession = Depends(get_session),
                        user: str = Depends(current_user)) -> None:
    t = await _get_target(session, target_id)
    bound = (await session.execute(select(Lab.name).where(Lab.cv_target_id == t.id))).scalars().all()
    if bound:
        raise HTTPException(409, {"message": f"target is used by {len(bound)} lab(s): unbind them first", "labs": bound})
    secret_ids = [i for i in (t.token_secret_id, t.onboarding_token_secret_id) if i]
    ztp_sec = await cv.target_ztp_script_secret(session, t.id)
    if ztp_sec is not None:
        secret_ids.append(ztp_sec.id)
    await session.delete(t)
    await session.flush()
    for sid in secret_ids:
        sec = await session.get(Secret, sid)
        if sec is not None:
            await session.delete(sec)
    await session.commit()


@targets.post("/targets/{target_id}/test")
async def test_target(target_id: str, session: AsyncSession = Depends(get_session),
                      user: str = Depends(current_user)) -> dict[str, Any]:
    """Inline (15 s budget) REST inventory call → `{ok, devices_seen, error}`."""
    t = await _get_target(session, target_id)
    token, _ = await cv.target_tokens(session, t)
    try:
        return await asyncio.wait_for(cv.check_target(t.server, token, verify_certs=bool(t.verify_certs)),
                                      cv.TEST_BUDGET + 1)
    except TimeoutError:
        return {"ok": False, "devices_seen": 0, "error": f"no answer within {int(cv.TEST_BUDGET)} s"}


# ------------------------------------------------------------------------------------------------ labs
def cloudvision_mode(lab: Lab) -> str:
    return str((((lab.settings or {}).get("knobs") or {}).get("cloudvision") or {}).get("mode") or "none")


async def _lab(session: AsyncSession, lab_id: str) -> Lab:
    lab = await session.get(Lab, lab_id)
    if lab is None:
        raise HTTPException(404, "lab not found")
    return lab


async def _lab_target(session: AsyncSession, lab: Lab) -> CVTarget:
    if not lab.cv_target_id:
        raise HTTPException(409, "lab has no CloudVision target: pick one in the lab settings")
    t = await session.get(CVTarget, lab.cv_target_id)
    if t is None:
        raise HTTPException(409, "the lab's CloudVision target no longer exists")
    return t


@labs_cv.post("/{lab_id}/cv/onboard", status_code=202)
async def onboard(lab_id: str, session: AsyncSession = Depends(get_session), user: str = Depends(current_user)) -> dict[str, str]:
    from ..labs.api import _busy, _require_host, _require_target

    lab = await _lab(session, lab_id)
    _require_host(lab)
    _require_target(lab)
    if cloudvision_mode(lab) == "none":
        raise HTTPException(409, "the lab's cloudvision knob is mode none: set cvaas or onprem, rebuild and redeploy")
    if lab.state not in cv_jobs.ONBOARD_STATES:
        raise HTTPException(409, f"lab is {lab.state.value}: deploy it before onboarding to CloudVision")
    t = await _lab_target(session, lab)
    if not t.onboarding_token_secret_id:
        raise HTTPException(409, f"CloudVision target {t.name} has no onboarding token")
    await _busy(session, lab)
    return {"job_id": await runner.submit("lab.cv_onboard", lab_id=lab.id, host_id=lab.host_id)}


@labs_cv.post("/{lab_id}/cv/deploy", status_code=202)
async def cv_deploy(lab_id: str, body: cv_jobs.CVDeployParams | None = None, session: AsyncSession = Depends(get_session),
                    user: str = Depends(current_user)) -> dict[str, str]:
    from ..labs.api import _busy, _require_host

    lab = await _lab(session, lab_id)
    _require_host(lab)
    if lab.state not in cv_jobs.CV_DEPLOY_STATES:
        raise HTTPException(409, f"lab is {lab.state.value}: build it before deploying to CloudVision")
    sc_dir = lab_service.lab_dir(lab) / "intended" / "structured_configs"
    if not sc_dir.is_dir() or not any(sc_dir.iterdir()):
        raise HTTPException(409, "no structured configs: build the lab first")
    t = await _lab_target(session, lab)
    if not t.token_secret_id:
        raise HTTPException(409, f"CloudVision target {t.name} has no service-account token")
    await _busy(session, lab)
    params = (body or cv_jobs.CVDeployParams()).model_dump()
    return {"job_id": await runner.submit("lab.cv_deploy", lab_id=lab.id, host_id=lab.host_id, params=params)}


@labs_cv.get("/{lab_id}/cv/status")
async def cv_status(lab_id: str, session: AsyncSession = Depends(get_session), user: str = Depends(current_user)) -> dict[str, Any]:
    lab = await _lab(session, lab_id)
    t = await session.get(CVTarget, lab.cv_target_id) if lab.cv_target_id else None
    st = lab.settings or {}
    return {"mode": cloudvision_mode(lab), "target": await _target_out(session, t) if t is not None else None,
            "onboarding": st.get("cv_onboarding"), "last_deploy": st.get("cv_last_deploy")}


router.include_router(targets)
router.include_router(labs_cv)
