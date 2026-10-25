"""Phase 4 (backend D) — ACT (Arista Cloud Test) routes. Mounted by `main.FEATURE_MODULES` under /api.

  GET  /labs/{id}/act            {target, prepared, topology_path, tools_server_ip, nodes: [{name, node_type, ip_addr, ip,
                                  version, role}], clients: [{name, ip_addr, links, script_path, script}],
                                  apply_script_path, hints: [str]}
  GET  /labs/{id}/act/topology   the ACT topology YAML (attachment <fabric>-topology.yml); 404 until built with target act
  POST /labs/{id}/act/prepare    re-run the post-processing (e.g. after changing knob act.tools_server_ip) → same as GET

Deploy / destroy / status / validate use the regular lab endpoints; `get_executor` picks `ActExecutor` for target act.
"""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.db import Host, HostKind, Lab, get_session
from ..core.security import current_user
from ..executors import act as act_exec
from ..executors.base import ExecutorError
from . import service

router = APIRouter(prefix="/labs", tags=["act"])


async def _lab(session: AsyncSession, lab_id: str) -> tuple[Lab, Host | None]:
    lab = await session.get(Lab, lab_id)
    if lab is None:
        raise HTTPException(404, "lab not found")
    host = await session.get(Host, lab.host_id) if lab.host_id else None
    return lab, host


def _summary(lab: Lab) -> dict[str, Any]:
    root = service.lab_dir(lab)
    fabric = act_exec.fabric_name_of(lab, root)
    return act_exec.act_summary(lab, root, fabric, act_exec.load_act_topology(root, fabric))


def deploy_precheck(lab: Lab) -> None:
    """Called by POST /labs/{id}/deploy for target act (lab.host loaded)."""
    host = lab.__dict__.get("host")
    if host is not None and host.kind != HostKind.act:
        raise HTTPException(409, f"host {host.name} is kind {host.kind.value}: an ACT lab deploys through a host of kind "
                                 "act (the ACT tools server)")
    if not _summary(lab)["prepared"]:
        raise HTTPException(409, "no ACT topology: build the lab with target act first")


@router.get("/{lab_id}/act")
async def act_info(lab_id: str, session: AsyncSession = Depends(get_session),
                   user: str = Depends(current_user)) -> dict[str, Any]:
    lab, host = await _lab(session, lab_id)
    summary = await asyncio.to_thread(_summary, lab)
    return {"target": lab.target or "clab", **summary, "hints": act_exec.hints(lab, summary, host)}


@router.get("/{lab_id}/act/topology")
async def act_topology(lab_id: str, session: AsyncSession = Depends(get_session),
                       user: str = Depends(current_user)) -> Response:
    lab, _host = await _lab(session, lab_id)
    root = service.lab_dir(lab)
    rel = act_exec.topology_rel(act_exec.fabric_name_of(lab, root))
    path = root / rel
    if not path.is_file():
        raise HTTPException(404, "no ACT topology: set the lab target to act and build the lab")
    text = await asyncio.to_thread(path.read_text, encoding="utf-8")
    return Response(text, media_type="application/x-yaml",
                    headers={"Content-Disposition": f'attachment; filename="{path.name}"'})


@router.post("/{lab_id}/act/prepare")
async def act_prepare(lab_id: str, session: AsyncSession = Depends(get_session),
                      user: str = Depends(current_user)) -> dict[str, Any]:
    lab, host = await _lab(session, lab_id)
    if (lab.target or "clab") != "act":
        raise HTTPException(409, "lab target is not act: set it in the lab settings and build")
    root = service.lab_dir(lab)
    try:
        summary = await asyncio.to_thread(act_exec.prepare_act_topology, lab, root, log=lambda _l: None)
    except ExecutorError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:  # invalid act knob
        raise HTTPException(422, {"message": str(exc), "violations": [{"path": "act", "message": str(exc)}]}) from exc
    await asyncio.to_thread(service.commit_all, root, "act: topology + client scripts")
    return {"target": "act", **summary, "hints": act_exec.hints(lab, summary, host)}
