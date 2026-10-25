"""Topology API: graph JSON for the UI + job `lab.topology` (regenerate clab/ from the last build).

  GET  /topology/{lab_id}             nodes/links graph (404 until the lab has been built)
  GET  /topology/{lab_id}/clab        raw clab/topology.clab.yml
  POST /topology/{lab_id}/regenerate  202 job lab.topology
  GET/PUT /topology/{lab_id}/layout   {positions: {node: {x, y}}, locked}   (clab/layout.json, committed)
  POST /topology/{lab_id}/edit        {ops: [{op, ...}], dry_run} → 200 {graph, changed_files, warnings, diff, ...}
                                      | 422/409 {detail: {message, violations, op_index, hint?}}  (see topology/edit.py)
  GET  /topology/{lab_id}/ports       {node: {type, used: [if], free: [if], peers: {if: "peer:if"}}}
"""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.db import Lab, get_session
from ..core.security import current_user
from ..jobs.engine import JobContext, job, runner
from ..labs import service as lab_service
from .clab import generate_for_lab, load_graph
from . import edit as topo_edit
from .models import TopologyGraph

router = APIRouter(prefix="/topology", tags=["topology"])


async def _lab(session: AsyncSession, lab_id: str) -> Lab:
    lab = await session.get(Lab, lab_id)
    if lab is None:
        raise HTTPException(404, "lab not found")
    return lab


@router.get("/{lab_id}", response_model=TopologyGraph)
async def get_topology(lab_id: str, session: AsyncSession = Depends(get_session),
                       user: str = Depends(current_user)) -> TopologyGraph:
    lab = await _lab(session, lab_id)
    root = lab_service.lab_dir(lab)
    graph = load_graph(root)
    if graph is None:
        csv_present = any((root / "documentation" / "fabric").glob("*-topology.csv"))
        if not csv_present:
            raise HTTPException(404, "no topology yet — build the lab first")
        graph = (await asyncio.to_thread(generate_for_lab, lab, root, log=lambda _l: None)).graph
    graph.lab_id = lab.id
    graph.layout = await asyncio.to_thread(topo_edit.read_layout, root)  # phase 5
    return graph


@router.get("/{lab_id}/clab", response_class=PlainTextResponse)
async def get_clab_yaml(lab_id: str, session: AsyncSession = Depends(get_session), user: str = Depends(current_user)) -> str:
    lab = await _lab(session, lab_id)
    p = lab_service.lab_dir(lab) / "clab" / "topology.clab.yml"
    if not p.exists():
        raise HTTPException(404, "no topology yet — build the lab first")
    return p.read_text()


@router.post("/{lab_id}/regenerate", status_code=202)
async def regenerate(lab_id: str, session: AsyncSession = Depends(get_session), user: str = Depends(current_user)) -> dict[str, str]:
    lab = await _lab(session, lab_id)
    if not any((lab_service.lab_dir(lab) / "documentation" / "fabric").glob("*-topology.csv")):
        raise HTTPException(409, "build the lab first (topology CSV missing)")
    return {"job_id": await runner.submit("lab.topology", lab_id=lab.id)}


@job("lab.topology")
async def lab_topology(ctx: JobContext) -> dict[str, Any]:
    lab = await ctx.get_lab()
    root = lab_service.lab_dir(lab)
    build = await asyncio.to_thread(generate_for_lab, lab, root, log=ctx.log)
    sha = await asyncio.to_thread(lab_service.commit_all, root, "topology: containerlab")
    ctx.log(f"committed ({sha[:8] if sha else 'unchanged'})")
    ctx.artefact("topology", root / "clab" / "topology.clab.yml")
    return {"nodes": len(build.topology.topology.nodes), "links": len(build.topology.topology.links), "commit": sha}


# ================================================================ phase 5: editable topology
from pydantic import BaseModel, Field  # noqa: E402
from sqlalchemy import select  # noqa: E402

from ..core.db import Job, JobStatus, LabState  # noqa: E402


class Position(BaseModel):
    x: float
    y: float


class LayoutIn(BaseModel):
    positions: dict[str, Position] = {}
    locked: bool = False


class EditIn(BaseModel):
    ops: list[dict[str, Any]] = Field(min_length=1)
    dry_run: bool = False


def _stored_knobs(lab: Lab) -> dict[str, Any]:
    from ..labs.api import _stored_knobs as stored

    return stored(lab)


@router.get("/{lab_id}/layout")
async def get_layout(lab_id: str, session: AsyncSession = Depends(get_session),
                     user: str = Depends(current_user)) -> dict[str, Any]:
    lab = await _lab(session, lab_id)
    return await asyncio.to_thread(topo_edit.read_layout, lab_service.lab_dir(lab)) or {"positions": {}, "locked": False}


@router.put("/{lab_id}/layout")
async def put_layout(lab_id: str, body: LayoutIn, session: AsyncSession = Depends(get_session),
                     user: str = Depends(current_user)) -> dict[str, Any]:
    lab = await _lab(session, lab_id)
    positions = {n: {"x": p.x, "y": p.y} for n, p in body.positions.items()}
    await asyncio.to_thread(lab_service.write_files, lab, {topo_edit.LAYOUT_REL: topo_edit.layout_json(positions, body.locked)},
                            "topology: layout")
    return {"positions": positions, "locked": body.locked}


@router.get("/{lab_id}/ports")
async def get_ports(lab_id: str, session: AsyncSession = Depends(get_session),
                    user: str = Depends(current_user)) -> dict[str, Any]:
    lab = await _lab(session, lab_id)
    try:
        return await asyncio.to_thread(topo_edit.lab_ports, lab, lab_service.lab_dir(lab), knobs=_stored_knobs(lab))
    except topo_edit.EditError as exc:
        raise HTTPException(exc.status, exc.detail()) from exc


@router.post("/{lab_id}/edit")
async def edit_topology(lab_id: str, body: EditIn, session: AsyncSession = Depends(get_session),
                        user: str = Depends(current_user)) -> dict[str, Any]:
    lab = await _lab(session, lab_id)
    running = (await session.execute(select(Job.id, Job.type).where(
        Job.lab_id == lab.id, Job.type.in_(("lab.build", "lab.deploy", "lab.destroy")),
        Job.status.in_([JobStatus.queued, JobStatus.running])))).first()
    if running and not body.dry_run:
        raise HTTPException(409, {"message": f"{running[1]} is running for this lab", "violations": [], "op_index": None})
    root = lab_service.lab_dir(lab)
    try:
        res = await asyncio.to_thread(topo_edit.run_edit, lab, root, body.ops, knobs=_stored_knobs(lab))
    except topo_edit.EditError as exc:
        raise HTTPException(exc.status, exc.detail()) from exc
    sess = res["session"]
    graph: TopologyGraph = res["graph"]
    graph.layout = await asyncio.to_thread(topo_edit.read_layout, root)
    warnings = list(sess.warnings)
    if lab.state in (LabState.deployed, LabState.validated):
        warnings.append(f"lab is {lab.state.value}: build and redeploy to apply the change")
    out: dict[str, Any] = {"dry_run": body.dry_run, "graph": graph.model_dump(), "changed_files": res["changed_files"],
                           "warnings": warnings, "diff": res["diff"], "ops": res["summary"], "commit": None}
    if body.dry_run:
        return out
    files = dict(res["files"])
    files[topo_edit.GRAPH_REL] = graph.model_copy(update={"layout": None}).model_dump_json(indent=2) + "\n"
    message = "topology edit: " + "; ".join(res["summary"])
    sha = await asyncio.to_thread(lab_service.write_files, lab, files, message, prune_group_vars=sess.generated)
    st = dict(lab.settings or {})
    if sess.generated:
        from ..labs.api import _split_secrets

        public, secrets = _split_secrets(sess.tpl, sess.knobs)
        st.update({"knobs": public, "secret_knobs": secrets, "custom_knobs": [], "custom_files": []})
        st["topology_overrides"] = sess.overrides
    elif res["changed_files"]:
        st["custom_files"] = sorted(set(st.get("custom_files", [])) | set(res["changed_files"]))
    lab.settings = st
    if sha and lab.state == LabState.built:
        lab.state = LabState.draft  # intended configs are stale until the next build
    await session.commit()
    out["commit"] = sha or None
    out["state"] = lab.state.value
    return out
