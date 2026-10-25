"""Incremental config push (0.10.0): after a Build, send only the devices whose intended config changed to the running
lab over eAPI (`eos_config replace`), without recreating containers.

  GET  /labs/{id}/push/plan          {changed: [hostnames], unchanged: [...], unknown: [...], since: ts|null}
  POST /labs/{id}/push {hosts?}      202 job lab.push_configs — hosts omitted = every changed (and unknown) device

How "changed" is known: `settings["pushed_configs"]` maps hostname → sha256 of `intended/configs/<host>.cfg` as it was
when the device last got it (recorded by lab.deploy for every device and by lab.push_configs for the pushed ones).
Devices with no record (deployed before 0.10.0, or new in the topology) are "unknown" and included by default.
The lab git history still has the full story (Files tab, History).
"""
from __future__ import annotations

import hashlib
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.db import Host, Lab, SessionLocal, get_session
from ..core.security import current_user
from ..hosts.ssh import SSHClient
from ..jobs.engine import JobContext, job, runner
from ..labs import service as lab_service
from .base import ExecutorError
from .common import DEPLOYED_STATES, update_lab
from .hot_clients import push_configs
from .lab_routes import _busy, _lab, _require_deployed

router = APIRouter(prefix="/labs", tags=["push"])
PUSH_JOBS = ("lab.deploy", "lab.destroy", "lab.push_configs", "lab.client_deploy", "lab.validate")


# ------------------------------------------------------------------------------------------------ pure helpers
def config_shas(lab: Lab) -> dict[str, str]:
    """hostname → sha256 of intended/configs/<host>.cfg (the files the switches boot from / are pushed)."""
    d = lab_service.lab_dir(lab) / "intended" / "configs"
    if not d.is_dir():
        return {}
    return {p.stem: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(d.glob("*.cfg"))}


def push_plan(lab: Lab, shas: dict[str, str] | None = None) -> dict[str, Any]:
    shas = config_shas(lab) if shas is None else shas
    rec = (lab.settings or {}).get("pushed_configs") or {}
    recorded = rec.get("shas") if isinstance(rec, dict) else {}
    recorded = recorded if isinstance(recorded, dict) else {}
    changed = sorted(h for h, s in shas.items() if h in recorded and recorded[h] != s)
    unchanged = sorted(h for h, s in shas.items() if h in recorded and recorded[h] == s)
    unknown = sorted(h for h in shas if h not in recorded)
    return {"changed": changed, "unchanged": unchanged, "unknown": unknown, "total": len(shas),
            "since": rec.get("at") if isinstance(rec, dict) else None, "last_job_id": rec.get("job_id") if isinstance(rec, dict) else None}


async def record_pushed(lab_id: str, hosts: list[str] | None, shas: dict[str, str], *, job_id: str | None) -> None:
    """Merge the shas of `hosts` (None = all) into settings.pushed_configs."""
    async with SessionLocal() as s:
        row = await s.get(Lab, lab_id)
        if row is None:
            return
        st = dict(row.settings or {})
        rec = st.get("pushed_configs") if isinstance(st.get("pushed_configs"), dict) else {}
        merged = dict(rec.get("shas") or {}) if isinstance(rec.get("shas"), dict) else {}
        for h in (hosts if hosts is not None else list(shas)):
            if h in shas:
                merged[h] = shas[h]
        st["pushed_configs"] = {"at": time.time(), "job_id": job_id, "shas": merged}
        row.settings = st
        await s.commit()


# ------------------------------------------------------------------------------------------------ job
class PushParams(BaseModel):
    hosts: list[str] | None = None  # None → changed + unknown


@job("lab.push_configs")
async def lab_push_configs(ctx: JobContext) -> dict[str, Any]:
    lab = await ctx.get_lab()
    params = PushParams(**(ctx.params or {}))
    if lab.state not in DEPLOYED_STATES:
        raise ExecutorError(f"lab is {lab.state.value}: deploy it first")
    if (lab.target or "clab") != "clab":
        raise ExecutorError("incremental push is only available for containerlab labs")
    if not lab.host_id:
        raise ExecutorError("lab has no host")
    async with SessionLocal() as s:
        host = await s.get(Host, lab.host_id)
    if host is None:
        raise ExecutorError("lab host not found")
    shas = config_shas(lab)
    if not shas:
        raise ExecutorError("no intended configs: build the lab first")
    plan = push_plan(lab, shas)
    hosts = params.hosts if params.hosts else [*plan["changed"], *plan["unknown"]]
    missing = [h for h in hosts if h not in shas]
    if missing:
        raise ExecutorError(f"no intended config for {', '.join(missing)}")
    ctx.log(f"[push] plan: {len(plan['changed'])} changed, {len(plan['unknown'])} unknown, {len(plan['unchanged'])} unchanged"
            + (f" (since {time.strftime('%Y-%m-%d %H:%M', time.localtime(plan['since']))})" if plan["since"] else ""))
    if not hosts:
        ctx.log("[push] nothing to push: every device already runs its intended config")
        return {"pushed": [], "skipped": plan["unchanged"]}
    ctx.log(f"[push] pushing to {len(hosts)} device(s): {', '.join(hosts)} — config replace over eAPI, no reboot")
    async with SessionLocal() as s:
        async with SSHClient.connect(host, s, log=ctx.log, ctx=ctx) as ssh:
            await push_configs(ctx, ssh, lab, host, hosts)
    await record_pushed(lab.id, hosts, shas, job_id=ctx.job_id)
    await update_lab(lab.id, last_push={"at": time.time(), "job_id": ctx.job_id, "hosts": hosts})
    ctx.log(f"[push] done: {', '.join(hosts)} now run their intended config. Validate to confirm.")
    return {"pushed": hosts, "skipped": plan["unchanged"]}


# ------------------------------------------------------------------------------------------------ API
@router.get("/{lab_id}/push/plan")
async def get_push_plan(lab_id: str, session: AsyncSession = Depends(get_session), user: str = Depends(current_user)) -> dict[str, Any]:
    lab = await _lab(session, lab_id)
    plan = push_plan(lab)
    plan["available"] = lab.state in DEPLOYED_STATES and (lab.target or "clab") == "clab" and bool(lab.host_id)
    return plan


class PushIn(BaseModel):
    hosts: list[str] | None = None


@router.post("/{lab_id}/push", status_code=202)
async def start_push(lab_id: str, body: PushIn | None = None, session: AsyncSession = Depends(get_session),
                     user: str = Depends(current_user)) -> dict[str, str]:
    lab = await _lab(session, lab_id)
    _require_deployed(lab)
    if (lab.target or "clab") != "clab":
        raise HTTPException(409, "incremental push is only available for containerlab labs")
    if not config_shas(lab):
        raise HTTPException(409, "no intended configs: build the lab first")
    await _busy(session, lab, PUSH_JOBS)
    params = (body or PushIn()).model_dump()
    return {"job_id": await runner.submit("lab.push_configs", lab_id=lab.id, host_id=lab.host_id, params=params)}
