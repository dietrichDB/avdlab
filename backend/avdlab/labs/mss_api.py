"""Module 4 — MSS foundation routes (phase 4, backend C).

  GET  /labs/{id}/mss          → {enabled, applications, policies, vrfs, matrix: {vrf: {src_segment: {dst_segment: policy|null}}},
                                  rendered_on: [hostnames], last_verify: {run_id, job_id, passed, failed, created_at}|null}
  POST /labs/{id}/mss/verify   {cases: [{src, dst, application, expect: allow|deny, vlan?}] | null} → 202 {job_id} (lab.mss_verify)

The knob itself is edited through `PATCH /labs/{id}` (`knobs.segmentation`); results are `traffic_runs` rows of kind
`mss` (also listed by `GET /labs/{id}/traffic/runs`).
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.db import Lab, TrafficRun, get_session
from ..core.security import current_user
from ..executors import mss as mss_exec  # registers lab.mss_verify
from ..executors.lab_routes import BLOCKING_JOBS, TRAFFIC_JOBS, _busy, _require_deployed, _unknown
from ..jobs.engine import runner
from ..topology.clients import load_plans
from . import mss, service

router = APIRouter(prefix="/labs", tags=["mss"])


async def _lab(session: AsyncSession, lab_id: str) -> Lab:
    lab = await session.get(Lab, lab_id)
    if lab is None:
        raise HTTPException(404, "lab not found")
    return lab


def _structured(lab_dir: Path) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for f in sorted((lab_dir / "intended" / "structured_configs").glob("*.yml")):
        try:
            out[f.stem] = yaml.safe_load(f.read_text()) or {}
        except yaml.YAMLError:
            continue
    return out


@router.get("/{lab_id}/mss")
async def get_mss(lab_id: str, session: AsyncSession = Depends(get_session),
                  user: str = Depends(current_user)) -> dict[str, Any]:
    lab = await _lab(session, lab_id)
    seg = mss_exec.lab_segmentation(lab)
    structured = await asyncio.to_thread(_structured, service.lab_dir(lab))
    out = mss.mss_summary(seg, structured)
    row = (await session.execute(select(TrafficRun).where(TrafficRun.lab_id == lab.id, TrafficRun.kind == "mss")
                                 .order_by(TrafficRun.created_at.desc()).limit(1))).scalar_one_or_none()
    out["last_verify"] = None if row is None else {
        "run_id": row.id, "job_id": row.job_id, "passed": (row.results or {}).get("passed"),
        "failed": (row.results or {}).get("failed"), "created_at": row.created_at.isoformat()}
    return out


@router.post("/{lab_id}/mss/verify", status_code=202)
async def verify_mss(lab_id: str, body: mss_exec.MssVerifyParams | None = None, session: AsyncSession = Depends(get_session),
                     user: str = Depends(current_user)) -> dict[str, Any]:
    lab = await _lab(session, lab_id)
    body = body or mss_exec.MssVerifyParams()
    _require_deployed(lab)
    plans = await asyncio.to_thread(load_plans, service.lab_dir(lab))
    if not plans:
        raise HTTPException(409, "this lab has no clients (connected endpoints become clients at build time)")
    seg = mss_exec.lab_segmentation(lab)
    if body.cases is None:
        if not seg["vrfs"]:
            raise HTTPException(409, "no segments defined: set knobs.segmentation first (or pass explicit cases)")
        if not mss_exec.derive_cases(seg, plans):
            raise HTTPException(409, "no case can be derived: no client pair is routed between known segments "
                                     "(clients must sit in VLANs whose prefixes are segment prefixes, in different subnets)")
    else:
        if not body.cases:
            raise HTTPException(422, {"message": "cases is empty", "violations": [{"path": "cases", "message": "empty"}]})
        _unknown(plans, [n for c in body.cases for n in (c.src, c.dst)])
        bad = sorted({c.application for c in body.cases if mss.application(seg, c.application) is None})
        if bad:
            raise HTTPException(422, {"message": f"unknown application(s): {', '.join(bad)}",
                                      "violations": [{"path": "cases.application", "message": f"unknown application {a}"}
                                                     for a in bad]})
    await _busy(session, lab, BLOCKING_JOBS + TRAFFIC_JOBS + ("lab.mss_verify",))
    return {"job_id": await runner.submit("lab.mss_verify", lab_id=lab.id, host_id=lab.host_id,
                                          params=body.model_dump())}
