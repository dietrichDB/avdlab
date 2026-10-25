"""Job `lab.netbox_sync`: build the plan from the lab's build outputs and reconcile it into the lab's NetBox target.

Runs entirely in the API process (pynetbox in a worker thread); no SSH, no lab host needed. The result
`{created, updated, deleted, unchanged, skipped, netbox_url, dry_run, at, target}` is the job result and is stored in
`lab.settings["netbox_last_sync"]`.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel

from ..core.db import Lab, LabState, NetBoxTarget, SessionLocal
from ..jobs.engine import JobCancelled, JobContext, job
from ..labs import service as lab_service
from . import service as nbs
from .sync import Reconciler, build_plan, lab_tag

SYNC_STATES = (LabState.built, LabState.deployed, LabState.validated, LabState.destroyed)


class NetBoxSyncParams(BaseModel):
    delete_stale: bool = False
    dry_run: bool = False
    include_clients: bool = True


def devices_url(url: str, slug: str) -> str:
    return f"{url.rstrip('/')}/dcim/devices/?tag={lab_tag(slug)['slug']}"


@job("lab.netbox_sync")
async def lab_netbox_sync(ctx: JobContext) -> dict[str, Any]:
    from ..topology.clab import lab_fabric_name

    params = NetBoxSyncParams(**(ctx.params or {}))
    lab = await ctx.get_lab()
    if lab.state not in SYNC_STATES:
        raise RuntimeError(f"lab is {lab.state.value}: build it before syncing to NetBox")
    async with SessionLocal() as s:
        target = await s.get(NetBoxTarget, lab.netbox_target_id) if lab.netbox_target_id else None
        if target is None:
            raise RuntimeError("lab has no NetBox target: pick one in the lab's NetBox tab")
        token = await nbs.target_token(s, target)
    if not token:
        raise RuntimeError(f"NetBox target {target.name} has no API token")

    loop = asyncio.get_running_loop()
    mlog = nbs.masking_log(ctx.log, [token])

    def tlog(line: str) -> None:  # called from the worker thread
        loop.call_soon_threadsafe(mlog, line)

    def check() -> None:
        if ctx.cancelled:
            raise JobCancelled()

    root = lab_service.lab_dir(lab)
    fabric = lab_fabric_name(lab, root)
    knobs = (lab.settings or {}).get("knobs") or {}
    mlog(f"[netbox] target {target.name} ({target.url}), lab {lab.slug}, fabric {fabric}, "
         f"delete_stale={params.delete_stale} dry_run={params.dry_run} include_clients={params.include_clients}")
    try:
        plan = await asyncio.to_thread(build_plan, root, fabric_name=fabric, knobs=knobs,
                                       include_clients=params.include_clients)
    except FileNotFoundError as exc:
        raise RuntimeError(str(exc)) from exc
    mlog(f"[netbox] plan: {plan.summary()}")

    def run() -> dict[str, Any]:
        nb = nbs.api_client(target.url, token, verify_certs=bool(target.verify_certs))
        rec = Reconciler(nb, slug=lab.slug, dry_run=params.dry_run, log=tlog, check_cancelled=check)
        return rec.run(plan, delete_stale=params.delete_stale)

    try:
        result = await asyncio.to_thread(run)
    except JobCancelled:
        raise
    except Exception as exc:  # noqa: BLE001 - pynetbox RequestError carries NetBox's validation message
        detail = getattr(exc, "error", None) or str(exc)
        raise RuntimeError(f"NetBox sync failed: {str(detail).replace(token, nbs.MASK)}") from exc
    await asyncio.sleep(0)  # flush thread log lines
    result["netbox_url"] = devices_url(target.url, lab.slug)
    result["at"] = datetime.now(timezone.utc).isoformat()
    result["target"] = {"id": target.id, "name": target.name}
    result["job_id"] = ctx.job_id
    for key in ("created", "updated", "deleted"):
        mlog(f"[netbox] {'would be ' if params.dry_run else ''}{key}: "
             f"{', '.join(f'{k} {v}' for k, v in result[key].items()) or 'nothing'}")
    for line in result["skipped"]:
        mlog(f"[netbox] skipped: {line}")
    async with SessionLocal() as s:
        row = await s.get(Lab, lab.id)
        if row is not None:
            row.settings = {**(row.settings or {}), "netbox_last_sync": result}
            await s.commit()
    return result
