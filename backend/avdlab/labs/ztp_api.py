"""Module 3 — ZTP status (phase 4, backend A).

  GET /labs/{id}/ztp → {mode, source, server_ip, nodes: [{name, mac, ip, lease, bootfile_fetched, eapi, bootfile}],
                        at, fresh, error}

Inline: the last deploy's poll (`lab.settings["ztp_last"]`) refreshed with a fresh read of the dnsmasq leases and the
http.server access log from the lab host (15 s budget; eAPI state comes from the deploy poll). `mode` is the knob's
mode; the node list is empty until the lab is built in ztp mode.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.db import Host, Lab, LabState, get_session
from ..core.security import current_user
from ..topology import ztp as ztp_mod
from . import service

router = APIRouter(prefix="/labs", tags=["ztp"])
FRESH_BUDGET = 15.0


async def _fresh_read(session: AsyncSession, lab: Lab) -> tuple[dict[str, str], set[tuple[str, str]]]:
    from ..executors.clab import ClabExecutor, docker_needs_sudo
    from ..hosts.ssh import SSHClient

    host = await session.get(Host, lab.host_id)
    if host is None:
        raise RuntimeError("lab host not found")
    async with SSHClient.connect(host, session) as ssh:
        return await ClabExecutor()._ztp_read(ssh, docker_needs_sudo(host))


@router.get("/{lab_id}/ztp")
async def ztp_status(lab_id: str, session: AsyncSession = Depends(get_session),
                     user: str = Depends(current_user)) -> dict[str, Any]:
    lab = await session.get(Lab, lab_id)
    if lab is None:
        raise HTTPException(404, "lab not found")
    st = lab.settings or {}
    prov = ztp_mod.provisioning_of(st.get("knobs"))
    meta = ztp_mod.load_meta(service.lab_dir(lab))
    last = st.get("ztp_last") or {}
    out: dict[str, Any] = {"mode": prov["mode"], "source": (meta or {}).get("source") or prov["ztp_source"],
                           "server_ip": (meta or {}).get("server_ip"), "nodes": [], "at": last.get("at"),
                           "fresh": False, "error": None}
    if meta is None:
        return out
    prev = {n["name"]: n for n in last.get("nodes") or []}
    macs = {k: v["mac"] for k, v in prev.items() if v.get("mac")}
    eapi = {k: bool(v.get("eapi")) for k, v in prev.items()}
    leases = {v["mac"]: v["ip"] for v in prev.values() if v.get("lease") and v.get("mac")}
    fetched = {(v["ip"], v["bootfile"]) for v in prev.values() if v.get("bootfile_fetched")}
    if lab.host_id and lab.state in (LabState.deployed, LabState.validated) and last:
        try:
            fl, ff = await asyncio.wait_for(_fresh_read(session, lab), FRESH_BUDGET)
            leases.update(fl)
            fetched |= ff
            out["fresh"] = True
            out["at"] = time.time()
        except TimeoutError:
            out["error"] = f"lab host did not answer within {int(FRESH_BUDGET)} s"
        except Exception as exc:  # noqa: BLE001 - status stays available from the last poll
            out["error"] = str(exc)[:300]
    out["nodes"] = ztp_mod.node_progress(meta, macs, leases, fetched, eapi)
    return out
