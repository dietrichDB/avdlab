"""Phase 6 (C) routes: interactive test hosts (hot-added linux clients), mounted at /api/labs.

  POST   /labs/{id}/clients                 {name, switch, switch_port, vlans, mode, ip, image?, deploy}
                                            → 202 {job_id, client, switch, switch_port, rebuilt, push_hosts, commit}
                                              (deploy=false: 200, job_id null — inputs/topology only)
  DELETE /labs/{id}/clients/{name}          → 202 {job_id, client, push_hosts} (lab.client_remove);
                                              lab not deployed: 200 {job_id: null} (inputs only)
  POST   /labs/{id}/clients/{name}/exec     {cmd}                    → {client, cmd, rc, stdout, stderr}   (inline, 20 s)
  POST   /labs/{id}/clients/{name}/ping     {dst, count}             → {src, dst, target_ip, sent, received, loss_pct,
                                                                         rtt_avg_ms, ok, output}          (inline)
  POST   /labs/{id}/clients/{name}/iperf    {dst, seconds, proto, reverse} → {src, dst, target_ip, proto, ok, error,
                                                                         seconds, bits_per_second_sent/received, …}
`GET /labs/{id}/clients` (executors/lab_routes.py) carries `hot_added` / `deployed`.
"""
from __future__ import annotations

import asyncio
import ipaddress
import re
import time
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.db import Host, Lab, LabState, get_session
from ..core.security import current_user
from ..hosts.ssh import SSHClient
from ..jobs.engine import runner
from ..labs import service
from ..topology import edit as topo_edit
from ..topology.clients import ClientPlan, default_ip, load_plans
from . import hot_clients as hc
from . import traffic
from .clab import DEPLOYED_STATES, docker_needs_sudo
from .lab_routes import BLOCKING_JOBS, _busy, _lab, _require_deployed

router = APIRouter(prefix="/labs", tags=["clients"])
CLIENT_JOBS = ("lab.client_deploy", "lab.client_remove")
EXEC_BUDGET = 20.0
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")


# ------------------------------------------------------------------------------------------------ bodies
class ClientIn(BaseModel):
    name: str
    switch: str
    switch_port: str = Field(pattern=r"^Ethernet\d+(/\d+)?$")
    vlans: str = Field(pattern=r"^\d{1,4}(-\d{1,4})?(,\d{1,4}(-\d{1,4})?)*$")
    mode: Literal["access", "trunk"] = "access"
    ip: str = "auto"
    image: str | None = None
    deploy: bool = True
    type: str = "servers"

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not NAME_RE.match(v):
            raise ValueError("letters, digits, '.', '_' and '-' only (max 63)")
        return v

    @field_validator("ip")
    @classmethod
    def _ip(cls, v: str) -> str:
        if v.lower() == "auto":
            return "auto"
        if "/" not in v:
            raise ValueError("ip must be 'auto' or a.b.c.d/nn")
        return str(ipaddress.IPv4Interface(v))


class ExecIn(BaseModel):
    cmd: str = Field(min_length=1, max_length=2000)


class PingIn(BaseModel):
    dst: str
    count: int = Field(default=3, ge=1, le=10)


class IperfIn(BaseModel):
    dst: str
    seconds: int = Field(default=5, ge=1, le=30)
    proto: Literal["tcp", "udp"] = "tcp"
    reverse: bool = False
    bandwidth: str | None = Field(default=None, pattern=r"^\d+(\.\d+)?[KMGkmg]?$")


# ------------------------------------------------------------------------------------------------ helpers
def _stored_knobs(lab: Lab) -> dict[str, Any]:
    from ..labs.api import _stored_knobs as stored

    return stored(lab)


def _check_clab(lab: Lab) -> None:
    if (lab.target or "clab") != "clab":
        raise HTTPException(409, "test hosts are only supported on containerlab labs")


async def _apply_ops(session: AsyncSession, lab: Lab, ops: list[dict[str, Any]]) -> str:
    """Same persistence as `POST /topology/{id}/edit` (inputs + overrides + graph), without touching the lab state."""
    root = service.lab_dir(lab)
    try:
        res = await asyncio.to_thread(topo_edit.run_edit, lab, root, ops, knobs=_stored_knobs(lab))
    except topo_edit.EditError as exc:
        raise HTTPException(exc.status, exc.detail()) from exc
    sess = res["session"]
    files = dict(res["files"])
    files[topo_edit.GRAPH_REL] = res["graph"].model_copy(update={"layout": None}).model_dump_json(indent=2) + "\n"
    sha = await asyncio.to_thread(service.write_files, lab, files, "test host: " + "; ".join(res["summary"]),
                                  prune_group_vars=sess.generated)
    st = dict(lab.settings or {})
    if sess.generated:
        from ..labs.api import _split_secrets

        public, secrets = _split_secrets(sess.tpl, sess.knobs)
        st.update({"knobs": public, "secret_knobs": secrets})
        st["topology_overrides"] = sess.overrides
    elif res["changed_files"]:
        st["custom_files"] = sorted(set(st.get("custom_files", [])) | set(res["changed_files"]))
    lab.settings = st
    await session.commit()
    return sha


async def _partial_build(lab: Lab, switches: list[str]) -> dict[str, Any]:
    from ..labs.build import BuildError, build_devices

    log: list[str] = []
    try:
        res = await asyncio.to_thread(build_devices, lab, switches, log=log.append)
    except BuildError as exc:
        raise HTTPException(422, {"message": f"partial build failed: {exc}", "violations": exc.violations}) from exc
    await asyncio.to_thread(service.commit_all, service.lab_dir(lab), f"partial build: {', '.join(res['hosts'])}")
    res["log"] = log
    return res


def _plans(lab: Lab) -> list[ClientPlan]:
    return load_plans(service.lab_dir(lab)) or []


def _plan(lab: Lab, name: str) -> ClientPlan:
    plan = next((p for p in _plans(lab) if p["name"] == name), None)
    if plan is None:
        raise HTTPException(404, f"{name} is not a client of this lab")
    return plan


def _target(lab: Lab, dst: str) -> tuple[str, str | None]:
    """dst = client name or IPv4 address → (ip, client name or None)."""
    plan = next((p for p in _plans(lab) if p["name"] == dst), None)
    if plan is not None:
        ip = default_ip(plan)
        if not ip:
            raise HTTPException(409, f"{dst} has no IP address")
        return ip, dst
    try:
        return str(ipaddress.IPv4Address(dst)), None
    except ValueError:
        raise HTTPException(422, f"dst must be a client name or an IPv4 address: {dst!r}") from None


async def _run_inline(session: AsyncSession, lab: Lab, cmds: list[str], budget: float) -> list[Any]:
    host = await session.get(Host, lab.host_id)
    if host is None:
        raise HTTPException(409, "lab host not found")
    dsudo = docker_needs_sudo(host)

    async def go() -> list[Any]:
        out = []
        async with SSHClient.connect(host, session) as ssh:
            for c in cmds:
                out.append(await ssh.run(c, sudo=dsudo, timeout=budget))
        return out

    try:
        return await asyncio.wait_for(go(), budget + 5)
    except TimeoutError as exc:
        raise HTTPException(504, f"command timed out after {int(budget)} s") from exc
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - ssh / docker errors
        raise HTTPException(502, str(exc)) from exc


async def _deployed_client(session: AsyncSession, lab_id: str, name: str) -> tuple[Lab, ClientPlan]:
    lab = await _lab(session, lab_id)
    _require_deployed(lab)
    _check_clab(lab)
    return lab, _plan(lab, name)


# ------------------------------------------------------------------------------------------------ add / remove
@router.post("/{lab_id}/clients", status_code=202)
async def add_client(lab_id: str, body: ClientIn, session: AsyncSession = Depends(get_session),
                     user: str = Depends(current_user)) -> Any:
    lab = await _lab(session, lab_id)
    _check_clab(lab)
    if body.deploy:
        _require_deployed(lab)
    elif lab.state not in (LabState.built, *DEPLOYED_STATES):
        raise HTTPException(409, f"lab is {lab.state.value}: build it first")
    await _busy(session, lab, BLOCKING_JOBS + CLIENT_JOBS)
    adapter: dict[str, Any] = {"switches": [body.switch], "switch_ports": [body.switch_port],
                               "endpoint_ports": [hc.CLIENT_ETH], "mode": body.mode, "vlans": body.vlans,
                               "description": "avdlab test host"}
    if body.mode == "access" and ("-" in body.vlans or "," in body.vlans):
        raise HTTPException(422, {"message": "access mode takes a single VLAN", "violations": [
            {"path": "vlans", "message": "access mode takes a single VLAN"}]})
    sha = await _apply_ops(session, lab, [{"op": "add_endpoint", "name": body.name, "type": body.type,
                                           "adapters": [adapter]}])
    hot = dict((lab.settings or {}).get("hot_clients") or {})
    hot[body.name] = {"switch": body.switch, "port": body.switch_port, "vlans": body.vlans, "mode": body.mode,
                      "ip": None if body.ip == "auto" else body.ip, "image": body.image, "added_at": time.time(),
                      "deployed": False}
    lab.settings = {**(lab.settings or {}), "hot_clients": hot}
    await session.commit()
    try:
        built = await _partial_build(lab, [body.switch])
    except HTTPException:  # roll the inputs back so the lab stays buildable
        await _apply_ops(session, lab, [{"op": "remove_endpoint", "name": body.name}])
        hot.pop(body.name, None)
        lab.settings = {**(lab.settings or {}), "hot_clients": hot}
        await session.commit()
        raise
    new_plan = next((p for p in _plans(lab) if p["name"] == body.name), None)
    ip, gw = hc.gateway_of(new_plan) if new_plan else (None, None)
    out: dict[str, Any] = {"client": body.name, "switch": body.switch, "switch_port": body.switch_port, "ip": ip,
                           "gateway": gw, "rebuilt": built["hosts"], "push_hosts": built["changed_hosts"],
                           "commit": sha or None, "job_id": None}
    if not body.deploy:
        return JSONResponse(out, status_code=200)
    out["job_id"] = await runner.submit("lab.client_deploy", lab_id=lab.id, host_id=lab.host_id, params={
        "name": body.name, "switch": body.switch, "switch_port": body.switch_port,
        "push_hosts": built["changed_hosts"]})
    return out


@router.delete("/{lab_id}/clients/{name}", status_code=202)
async def remove_client(lab_id: str, name: str, session: AsyncSession = Depends(get_session),
                        user: str = Depends(current_user)) -> Any:
    lab = await _lab(session, lab_id)
    _check_clab(lab)
    plan = _plan(lab, name)
    await _busy(session, lab, BLOCKING_JOBS + CLIENT_JOBS)
    switches = sorted({lk["switch"] for lk in plan.get("links") or []})
    sess = topo_edit.EditSession(lab, service.lab_dir(lab), knobs=_stored_knobs(lab))
    in_inputs = await asyncio.to_thread(sess.find_endpoint, name) is not None
    if in_inputs:
        await _apply_ops(session, lab, [{"op": "remove_endpoint", "name": name}])
    push: list[str] = []
    if switches and lab.state in (LabState.built, *DEPLOYED_STATES):
        push = (await _partial_build(lab, switches))["changed_hosts"]
    deployed = lab.state in DEPLOYED_STATES
    if not deployed:
        hot = dict((lab.settings or {}).get("hot_clients") or {})
        hot.pop(name, None)
        lab.settings = {**(lab.settings or {}), "hot_clients": hot}
        await session.commit()
        return JSONResponse({"job_id": None, "client": name, "push_hosts": [], "removed_from_inputs": in_inputs},
                            status_code=200)
    if not lab.host_id:
        raise HTTPException(409, "lab has no host")
    job_id = await runner.submit("lab.client_remove", lab_id=lab.id, host_id=lab.host_id,
                                 params={"name": name, "push_hosts": push})
    return {"job_id": job_id, "client": name, "push_hosts": push, "removed_from_inputs": in_inputs}


# ------------------------------------------------------------------------------------------------ quick actions
@router.post("/{lab_id}/clients/{name}/exec")
async def client_exec(lab_id: str, name: str, body: ExecIn, session: AsyncSession = Depends(get_session),
                      user: str = Depends(current_user)) -> dict[str, Any]:
    lab, _plan_ = await _deployed_client(session, lab_id, name)
    cmd = traffic.docker_exec(name, "sh -c " + hc.q(body.cmd))
    (res,) = await _run_inline(session, lab, [cmd], EXEC_BUDGET)
    return {"client": name, "cmd": body.cmd, "rc": res.returncode, "stdout": res.stdout, "stderr": res.stderr}


@router.post("/{lab_id}/clients/{name}/ping")
async def client_ping(lab_id: str, name: str, body: PingIn, session: AsyncSession = Depends(get_session),
                      user: str = Depends(current_user)) -> dict[str, Any]:
    lab, _plan_ = await _deployed_client(session, lab_id, name)
    target, _dst_client = _target(lab, body.dst)
    cmd = traffic.docker_exec(name, f"ping -c {body.count} -W 2 -i 0.2 {target}")
    (res,) = await _run_inline(session, lab, [cmd], EXEC_BUDGET)
    text = res.stdout + "\n" + res.stderr
    parsed = traffic.parse_ping(text)
    return {"src": name, "dst": body.dst, "target_ip": target, **parsed, "ok": parsed["received"] > 0,
            "output": text.strip()}


@router.post("/{lab_id}/clients/{name}/iperf")
async def client_iperf(lab_id: str, name: str, body: IperfIn, session: AsyncSession = Depends(get_session),
                       user: str = Depends(current_user)) -> dict[str, Any]:
    lab, _plan_ = await _deployed_client(session, lab_id, name)
    target, dst_client = _target(lab, body.dst)
    flow = traffic.IperfFlow(src=name, dst=body.dst, proto=body.proto, duration=body.seconds, reverse=body.reverse,
                             bandwidth=body.bandwidth)
    cmds = []
    if dst_client:
        cmds.append(traffic.docker_exec(dst_client, "sh -c " + hc.q("pgrep iperf3 >/dev/null || iperf3 -s -D")))
    cmds.append(traffic.docker_exec(name, traffic.iperf_client_cmd(flow, target)))
    results = await _run_inline(session, lab, cmds, body.seconds + 20.0)
    res = results[-1]
    parsed = traffic.parse_iperf(res.stdout or res.stderr)
    if not res.ok and not parsed["error"]:
        parsed["error"] = f"iperf3 rc={res.returncode}"
    return {"src": name, "dst": body.dst, "target_ip": target, "proto": body.proto, **parsed,
            "ok": parsed["error"] is None}
