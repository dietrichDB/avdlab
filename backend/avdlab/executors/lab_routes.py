"""Phase 3 (backend A) lab routes, mounted under /labs by labs/api.py:

  GET  /labs/{id}/clients                                   [ClientPlan + {container_state, mgmt_ip, hot_added, deployed}]
  POST /labs/{id}/traffic/ping   {pairs|null, count, size, vlan?}          → 202 {job_id} (lab.traffic_ping)
  POST /labs/{id}/traffic/iperf  {flows: [...], sequential}                → 202 {job_id} (lab.traffic_iperf)
  GET  /labs/{id}/traffic/runs                              newest first   GET /labs/{id}/traffic/runs/{run_id}
  GET  /labs/{id}/nodes/{node}/running-config               {node, config, fetched_at}   (inline, 25 s)
  GET  /labs/{id}/nodes/{node}/config-diff                  {node, intended, running, diff, identical}
  POST /labs/{id}/snapshot       {commands}                                → 202 {job_id} (lab.snapshot)
  GET  /labs/{id}/snapshots                                 [{id, created_at, nodes, commands, node_names, command_slugs}]
  GET  /labs/{id}/snapshots/{snap}/{node}/{cmd_slug}        text/plain
  WS   /labs/{id}/nodes/{node}/terminal?shell=cli|bash&cols=&rows=
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, WebSocket
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.db import Host, Job, JobStatus, Lab, TrafficRun, get_session
from ..core.security import current_user
from ..jobs.engine import runner
from ..labs import service
from ..topology.clients import default_ip, load_plans
from . import configs, terminal, traffic
from .base import ExecutorError
from .clab import DEPLOYED_STATES, TOPO_REL, load_topology

router = APIRouter()
BLOCKING_JOBS = ("lab.build", "lab.deploy", "lab.destroy")
TRAFFIC_JOBS = ("lab.traffic_ping", "lab.traffic_iperf")


async def _lab(session: AsyncSession, lab_id: str) -> Lab:
    lab = await session.get(Lab, lab_id)
    if lab is None:
        raise HTTPException(404, "lab not found")
    return lab


def _require_deployed(lab: Lab) -> None:
    if not lab.host_id:
        raise HTTPException(409, "lab has no host: set one in the lab settings")
    if lab.state not in DEPLOYED_STATES:
        raise HTTPException(409, f"lab is {lab.state.value}: deploy it first")


async def _busy(session: AsyncSession, lab: Lab, types: tuple[str, ...]) -> None:
    running = (await session.execute(select(Job.id, Job.type).where(
        Job.lab_id == lab.id, Job.type.in_(types), Job.status.in_([JobStatus.queued, JobStatus.running])))).first()
    if running:
        raise HTTPException(409, {"message": f"{running[1]} is already running for this lab", "job_id": running[0]})


def _runtime_nodes(lab: Lab) -> dict[str, dict[str, Any]]:
    """Most recent of last_inspect / last_status → {name: node status}."""
    st = lab.settings or {}
    snaps = [x for x in (st.get("last_inspect"), st.get("last_status")) if isinstance(x, dict) and x.get("nodes") is not None]
    if not snaps:
        return {}
    latest = max(snaps, key=lambda x: x.get("at") or 0)
    return {n.get("name"): n for n in latest.get("nodes") or [] if isinstance(n, dict)}


# ------------------------------------------------------------------------------------------------ clients
@router.get("/{lab_id}/clients")
async def clients(lab_id: str, session: AsyncSession = Depends(get_session),
                  user: str = Depends(current_user)) -> list[dict[str, Any]]:
    lab = await _lab(session, lab_id)
    plans = await asyncio.to_thread(load_plans, service.lab_dir(lab)) or []
    deployed_lab = lab.state in DEPLOYED_STATES
    runtime = _runtime_nodes(lab) if deployed_lab else {}
    hot = (lab.settings or {}).get("hot_clients") or {}  # phase 6 (C): hot-added test hosts
    out = []
    for p in plans:
        rt = runtime.get(p["name"]) or {}
        h = hot.get(p["name"])
        deployed = deployed_lab and (rt.get("state") == "running" if h is None else bool(h.get("deployed")))
        out.append({**p, "container_state": rt.get("state"), "mgmt_ip": rt.get("mgmt_ip"),
                    "hot_added": h is not None, "deployed": deployed})
    return out


# ------------------------------------------------------------------------------------------------ traffic
async def _traffic_precheck(session: AsyncSession, lab: Lab) -> list[Any]:
    _require_deployed(lab)
    if (lab.target or "clab") == "act":  # phase 4 (backend D)
        from .act import TRAFFIC_UNSUPPORTED

        raise HTTPException(409, TRAFFIC_UNSUPPORTED)
    plans = await asyncio.to_thread(load_plans, service.lab_dir(lab))
    if not plans:
        raise HTTPException(409, "this lab has no clients (connected endpoints become clients at build time)")
    await _busy(session, lab, BLOCKING_JOBS + TRAFFIC_JOBS)
    return plans


def _unknown(plans: list[Any], names: list[str]) -> None:
    known = {p["name"] for p in plans}
    bad = sorted({n for n in names if n not in known})
    if bad:
        raise HTTPException(422, {"message": f"unknown client(s): {', '.join(bad)}",
                                  "violations": [{"path": "src/dst", "message": f"unknown client {n}"} for n in bad]})


@router.post("/{lab_id}/traffic/ping", status_code=202)
async def traffic_ping(lab_id: str, body: traffic.PingParams | None = None, session: AsyncSession = Depends(get_session),
                       user: str = Depends(current_user)) -> dict[str, str]:
    lab = await _lab(session, lab_id)
    body = body or traffic.PingParams()
    plans = await _traffic_precheck(session, lab)
    if body.pairs is not None:
        _unknown(plans, [n for p in body.pairs for n in (p.src, p.dst)])
    elif sum(1 for p in plans if default_ip(p, body.vlan)) < 2:
        raise HTTPException(409, "fewer than two clients have an IP address" + (f" in VLAN {body.vlan}" if body.vlan else ""))
    return {"job_id": await runner.submit("lab.traffic_ping", lab_id=lab.id, host_id=lab.host_id,
                                          params=body.model_dump())}


@router.post("/{lab_id}/traffic/iperf", status_code=202)
async def traffic_iperf(lab_id: str, body: traffic.IperfParams, session: AsyncSession = Depends(get_session),
                        user: str = Depends(current_user)) -> dict[str, str]:
    lab = await _lab(session, lab_id)
    plans = await _traffic_precheck(session, lab)
    _unknown(plans, [n for f in body.flows for n in (f.src, f.dst)])
    return {"job_id": await runner.submit("lab.traffic_iperf", lab_id=lab.id, host_id=lab.host_id,
                                          params=body.model_dump())}


@router.get("/{lab_id}/traffic/runs")
async def traffic_runs(lab_id: str, limit: int = 50, session: AsyncSession = Depends(get_session),
                       user: str = Depends(current_user)) -> list[dict[str, Any]]:
    lab = await _lab(session, lab_id)
    rows = (await session.execute(select(TrafficRun).where(TrafficRun.lab_id == lab.id)
                                  .order_by(TrafficRun.created_at.desc()).limit(limit))).scalars().all()
    return [traffic.run_out(r) for r in rows]


@router.get("/{lab_id}/traffic/runs/{run_id}")
async def traffic_run(lab_id: str, run_id: str, session: AsyncSession = Depends(get_session),
                      user: str = Depends(current_user)) -> dict[str, Any]:
    lab = await _lab(session, lab_id)
    row = await session.get(TrafficRun, run_id)
    if row is None or row.lab_id != lab.id:
        raise HTTPException(404, "traffic run not found")
    return traffic.run_out(row, full=True)


# ------------------------------------------------------------------------------------------------ running configs
async def _eos_node(lab: Lab, node: str) -> None:
    try:
        nodes = await asyncio.to_thread(configs.eos_nodes, service.lab_dir(lab))
    except ExecutorError as exc:
        raise HTTPException(409, str(exc)) from exc
    if node not in nodes:
        raise HTTPException(404, f"{node} is not a cEOS node of this lab")


async def _running(session: AsyncSession, lab: Lab, node: str) -> str:
    _require_deployed(lab)
    await _eos_node(lab, node)
    host = await session.get(Host, lab.host_id)
    if host is None:
        raise HTTPException(409, "lab host not found")
    fetch = configs.fetch_running_config
    if (lab.target or "clab") == "act":  # phase 4 (backend D): eAPI from the tools server
        from .act import fetch_running_config as fetch
    try:
        return await asyncio.wait_for(fetch(lab, host, node, session=session), configs.RUNNING_CONFIG_BUDGET)
    except TimeoutError as exc:
        raise HTTPException(504, f"show running-config on {node} timed out after 25 s") from exc
    except Exception as exc:  # noqa: BLE001 - ssh / docker errors
        raise HTTPException(502, str(exc)) from exc


@router.get("/{lab_id}/nodes/{node}/running-config")
async def running_config(lab_id: str, node: str, session: AsyncSession = Depends(get_session),
                         user: str = Depends(current_user)) -> dict[str, Any]:
    lab = await _lab(session, lab_id)
    text = await _running(session, lab, node)
    return {"node": node, "config": text, "fetched_at": datetime.now(timezone.utc).isoformat()}


@router.get("/{lab_id}/nodes/{node}/config-diff")
async def config_diff(lab_id: str, node: str, session: AsyncSession = Depends(get_session),
                      user: str = Depends(current_user)) -> dict[str, Any]:
    lab = await _lab(session, lab_id)
    intended_path = service.lab_dir(lab) / "intended" / "configs" / f"{node}.cfg"
    _require_deployed(lab)
    await _eos_node(lab, node)
    if not intended_path.is_file():
        raise HTTPException(404, f"intended/configs/{node}.cfg not found: build the lab")
    running = await _running(session, lab, node)
    intended = await asyncio.to_thread(intended_path.read_text, encoding="utf-8")
    return configs.config_diff(node, intended, running)


# ------------------------------------------------------------------------------------------------ snapshots
@router.post("/{lab_id}/snapshot", status_code=202)
async def snapshot(lab_id: str, body: configs.SnapshotParams | None = None, session: AsyncSession = Depends(get_session),
                   user: str = Depends(current_user)) -> dict[str, str]:
    lab = await _lab(session, lab_id)
    _require_deployed(lab)
    if (lab.target or "clab") == "act":  # phase 4 (backend D)
        raise HTTPException(409, "snapshots are not supported on ACT labs in this iteration")
    if not (service.lab_dir(lab) / TOPO_REL).exists():
        raise HTTPException(409, "no containerlab topology: build the lab first")
    await _busy(session, lab, BLOCKING_JOBS + ("lab.snapshot",))
    params = (body or configs.SnapshotParams()).model_dump()
    return {"job_id": await runner.submit("lab.snapshot", lab_id=lab.id, host_id=lab.host_id, params=params)}


@router.get("/{lab_id}/snapshots")
async def snapshots(lab_id: str, session: AsyncSession = Depends(get_session),
                    user: str = Depends(current_user)) -> list[dict[str, Any]]:
    lab = await _lab(session, lab_id)
    return await asyncio.to_thread(configs.list_snapshots, service.lab_dir(lab))


@router.get("/{lab_id}/snapshots/{snap}/{node}/{cmd_slug}", response_class=PlainTextResponse)
async def snapshot_output(lab_id: str, snap: str, node: str, cmd_slug: str, session: AsyncSession = Depends(get_session),
                          user: str = Depends(current_user)) -> PlainTextResponse:
    lab = await _lab(session, lab_id)
    slug = cmd_slug[:-4] if cmd_slug.endswith(".txt") else cmd_slug
    try:
        path = configs.snapshot_file(service.lab_dir(lab), snap, node, slug)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not path.is_file():
        raise HTTPException(404, "snapshot output not found")
    return PlainTextResponse(await asyncio.to_thread(path.read_text, encoding="utf-8"))


# ------------------------------------------------------------------------------------------------ terminal
@router.websocket("/{lab_id}/nodes/{node}/terminal")
async def node_terminal(ws: WebSocket, lab_id: str, node: str, shell: str = "cli", cols: int = 80, rows: int = 24) -> None:
    if not ws.session.get("user"):
        await ws.close(code=terminal.CLOSE_UNAUTHORIZED)
        return
    from ..core.db import SessionLocal

    async with SessionLocal() as s:
        lab = await s.get(Lab, lab_id)
        host = await s.get(Host, lab.host_id) if lab is not None and lab.host_id else None
    await ws.accept()
    code, reason = 0, ""
    kind = ""
    if shell not in ("cli", "bash"):
        code, reason = terminal.CLOSE_BAD_REQUEST, "shell must be cli or bash"
    elif lab is None:
        code, reason = terminal.CLOSE_NOT_FOUND, "lab not found"
    elif lab.state not in DEPLOYED_STATES or host is None:
        code, reason = terminal.CLOSE_CONFLICT, f"lab is {lab.state.value}: deploy it first"
    elif (lab.target or "clab") == "act":  # phase 4 (backend D): ssh from the tools server to the node mgmt IP
        from .act import act_settings, node_mgmt_ip

        ip = await asyncio.to_thread(node_mgmt_ip, lab, node)
        if not ip or node == "avdlab-tools":
            code, reason = terminal.CLOSE_NOT_FOUND, f"{node} is not a node of this ACT lab"
        else:
            kind = f"act:{act_settings(lab)['username']}@{ip}"
    else:
        try:
            nodes = ((await asyncio.to_thread(load_topology, service.lab_dir(lab))).get("topology") or {}).get("nodes") or {}
        except ExecutorError:
            nodes = {}
        if node not in nodes:
            code, reason = terminal.CLOSE_NOT_FOUND, f"{node} is not a node of this lab"
        else:
            kind = str((nodes[node] or {}).get("kind") or "")
    if code:
        await ws.send_bytes(f"[avdlab] {reason}\r\n".encode())
        await ws.close(code=code, reason=reason)
        return
    c, r = terminal.clamp_size(cols, rows)
    command = (terminal.act_node_command(*kind[4:].rsplit("@", 1), shell) if kind.startswith("act:")  # phase 4 (D)
               else terminal.node_command(node, shell, kind))
    await terminal.serve(ws, host, command, cols=c, rows=r,
                         label=f"lab={lab.slug} node={node} shell={shell}")
