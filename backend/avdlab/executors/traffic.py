"""Module 5 — traffic generation between the linux clients: jobs `lab.traffic_ping` and `lab.traffic_iperf`.

All commands run on the lab host over SSH as `docker exec <client> …` (containerlab prefix is empty, so container
name == node name). Plans come from `clab/clients.json` (topology/clients.py). Every run is stored in `traffic_runs`.
"""
from __future__ import annotations

import asyncio
import json
import re
import shlex
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..core.db import Host, Lab, SessionLocal, TrafficRun
from ..hosts.ssh import SSHClient
from ..jobs.engine import JobContext, job
from ..labs import service
from ..topology.clients import ClientPlan, default_ip, load_plans
from .base import ExecutorError
from .clab import DEPLOYED_STATES, docker_needs_sudo

MAX_CONCURRENCY = 8


# ------------------------------------------------------------------------------------------------ params
class PingPair(BaseModel):
    src: str
    dst: str


class PingParams(BaseModel):
    pairs: list[PingPair] | None = None  # None = full mesh over clients that have an IP
    count: int = Field(default=3, ge=1, le=100)
    size: int = Field(default=56, ge=0, le=9000)
    vlan: int | None = Field(default=None, ge=1, le=4094)


class IperfFlow(BaseModel):
    src: str
    dst: str
    proto: Literal["tcp", "udp"] = "tcp"
    bandwidth: str | None = Field(default=None, pattern=r"^\d+(\.\d+)?[KMGkmg]?$")
    duration: int = Field(default=10, ge=1, le=300)
    parallel: int = Field(default=1, ge=1, le=32)
    reverse: bool = False
    vlan: int | None = Field(default=None, ge=1, le=4094)  # target the dst address in this VLAN (default: its default-route VLAN)


class IperfParams(BaseModel):
    flows: list[IperfFlow] = Field(min_length=1, max_length=64)
    sequential: bool = True


# ------------------------------------------------------------------------------------------------ parsing
_TX_RX = re.compile(r"(\d+)\s+packets transmitted,\s*(\d+)\s+(?:packets\s+)?received")
_LOSS = re.compile(r"([\d.]+)%\s+packet loss")
_RTT = re.compile(r"(?:rtt|round-trip)\s+min/avg/max(?:/mdev|/stddev)?\s*=\s*[\d.]+/([\d.]+)/")


def parse_ping(text: str) -> dict[str, Any]:
    """iputils and busybox `ping` summaries → {sent, received, loss_pct, rtt_avg_ms}."""
    m = _TX_RX.search(text or "")
    sent, received = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
    lm = _LOSS.search(text or "")
    loss = float(lm.group(1)) if lm else (100.0 if sent == 0 else round(100.0 * (sent - received) / sent, 1))
    rm = _RTT.search(text or "")
    return {"sent": sent, "received": received, "loss_pct": loss, "rtt_avg_ms": float(rm.group(1)) if rm else None}


def parse_iperf(text: str) -> dict[str, Any]:
    """`iperf3 -J` output → {seconds, bits_per_second_sent/received, retransmits, jitter_ms, lost_percent, error}."""
    out: dict[str, Any] = {"seconds": None, "bits_per_second_sent": None, "bits_per_second_received": None,
                           "retransmits": None, "jitter_ms": None, "lost_percent": None, "error": None}
    text = (text or "").strip()
    start = text.find("{")
    try:
        data = json.loads(text[start:]) if start >= 0 else None
    except ValueError:
        data = None
    if not isinstance(data, dict):
        out["error"] = (text.splitlines()[-1] if text else "no output from iperf3")[:300]
        return out
    if data.get("error"):
        out["error"] = str(data["error"])
    end = data.get("end") or {}
    sent = end.get("sum_sent") or {}
    recv = end.get("sum_received") or {}
    udp = end.get("sum") or {}
    if sent or recv:
        out["seconds"] = sent.get("seconds") or recv.get("seconds")
        out["bits_per_second_sent"] = sent.get("bits_per_second")
        out["bits_per_second_received"] = recv.get("bits_per_second")
        out["retransmits"] = sent.get("retransmits")
    if udp:  # UDP (older iperf3 only has `sum`; newer also sum_sent/sum_received)
        out["seconds"] = out["seconds"] or udp.get("seconds")
        out["bits_per_second_sent"] = out["bits_per_second_sent"] or udp.get("bits_per_second")
        if out["bits_per_second_received"] is None:
            out["bits_per_second_received"] = udp.get("bits_per_second")
        out["jitter_ms"] = udp.get("jitter_ms")
        out["lost_percent"] = udp.get("lost_percent")
    if out["jitter_ms"] is None and recv.get("jitter_ms") is not None:
        out["jitter_ms"], out["lost_percent"] = recv.get("jitter_ms"), recv.get("lost_percent")
    if out["seconds"] is not None:
        out["seconds"] = round(float(out["seconds"]), 3)
    return out


# ------------------------------------------------------------------------------------------------ helpers
def client_index(plans: list[ClientPlan]) -> dict[str, ClientPlan]:
    return {p["name"]: p for p in plans}


def ping_pairs(plans: list[ClientPlan], pairs: list[PingPair] | None, vlan: int | None) -> list[tuple[str, str]]:
    """Explicit pairs, or the full mesh (ordered, src != dst) over clients that have an address (in `vlan`)."""
    if pairs is not None:
        return [(p.src, p.dst) for p in pairs]
    names = [p["name"] for p in plans if default_ip(p, vlan)]
    return [(a, b) for a in names for b in names if a != b]


def check_clients(plans: list[ClientPlan], names: list[str]) -> None:
    known = client_index(plans)
    unknown = sorted({n for n in names if n not in known})
    if unknown:
        raise ExecutorError(f"unknown client(s): {', '.join(unknown)}; clients: {', '.join(sorted(known)) or 'none'}")


def docker_exec(container: str, cmd: str) -> str:
    return f"docker exec {shlex.quote(container)} {cmd}"


def lab_plans(lab: Lab) -> list[ClientPlan]:
    plans = load_plans(service.lab_dir(lab))
    if not plans:
        raise ExecutorError("this lab has no clients: connected endpoints become clients at build time")
    return plans


async def _lab_host(ctx: JobContext) -> tuple[Lab, Host]:
    lab = await ctx.get_lab()
    if lab.state not in DEPLOYED_STATES:
        raise ExecutorError(f"lab is {lab.state.value}: deploy it first")
    if not lab.host_id:
        raise ExecutorError("lab has no host")
    async with SessionLocal() as s:
        host = await s.get(Host, lab.host_id)
    if host is None:
        raise ExecutorError("lab host not found")
    return lab, host


async def _store(lab_id: str, job_id: str, kind: str, params: dict[str, Any], results: dict[str, Any]) -> str:
    async with SessionLocal() as s:
        row = TrafficRun(lab_id=lab_id, job_id=job_id or None, kind=kind, params=params, results=results)
        s.add(row)
        await s.commit()
        return row.id


# ------------------------------------------------------------------------------------------------ ping
async def run_ping(ssh: Any, plans: list[ClientPlan], params: PingParams, *, sudo: bool, log: Any) -> dict[str, Any]:
    by_name = client_index(plans)
    pairs = ping_pairs(plans, params.pairs, params.vlan)
    check_clients(plans, [n for pair in pairs for n in pair])
    sem = asyncio.Semaphore(MAX_CONCURRENCY)

    async def one(src: str, dst: str) -> dict[str, Any]:
        target = default_ip(by_name[dst], params.vlan)
        base = {"src": src, "dst": dst, "target_ip": target}
        if not target:
            log(f"[ping] {src} -> {dst}: {dst} has no address" + (f" in VLAN {params.vlan}" if params.vlan else ""))
            return {**base, "sent": 0, "received": 0, "loss_pct": 100.0, "rtt_avg_ms": None, "ok": False}
        cmd = docker_exec(src, f"ping -c {params.count} -s {params.size} -W 2 -i 0.2 {target}")
        async with sem:
            try:
                res = await ssh.run(cmd, sudo=sudo, timeout=params.count * 3 + 30)
                parsed = parse_ping(res.stdout + "\n" + res.stderr)
            except Exception as exc:  # noqa: BLE001 - a failed pair is a result, not a job failure
                log(f"[ping] {src} -> {dst}: {exc}")
                parsed = {"sent": 0, "received": 0, "loss_pct": 100.0, "rtt_avg_ms": None}
        r = {**base, **parsed, "ok": parsed["received"] > 0}
        log(f"[ping] {src} -> {dst} ({target}): {r['received']}/{r['sent']} received, loss {r['loss_pct']}%"
            + (f", avg {r['rtt_avg_ms']} ms" if r["rtt_avg_ms"] is not None else ""))
        return r

    results = await asyncio.gather(*(one(a, b) for a, b in pairs))
    matrix: dict[str, dict[str, float]] = {}
    for r in results:
        matrix.setdefault(r["src"], {})[r["dst"]] = r["loss_pct"]
    return {"results": list(results), "matrix": matrix}


@job("lab.traffic_ping")
async def lab_traffic_ping(ctx: JobContext) -> dict[str, Any]:
    params = PingParams(**(ctx.params or {}))
    lab, host = await _lab_host(ctx)
    plans = lab_plans(lab)
    ctx.log(f"[ping] {len(plans)} clients, count={params.count} size={params.size}"
            + (f" vlan={params.vlan}" if params.vlan else ""))
    async with SessionLocal() as s:
        async with SSHClient.connect(host, s, log=ctx.log, ctx=ctx) as ssh:
            out = await run_ping(ssh, plans, params, sudo=docker_needs_sudo(host), log=ctx.log)
    ok = sum(1 for r in out["results"] if r["ok"])
    ctx.log(f"[ping] {ok}/{len(out['results'])} pairs reachable")
    out["run_id"] = await _store(lab.id, ctx.job_id, "ping", params.model_dump(), out)
    return out


# ------------------------------------------------------------------------------------------------ iperf
def iperf_client_cmd(flow: IperfFlow, target: str) -> str:
    args = [f"iperf3 -c {target} -J -t {flow.duration}"]
    if flow.proto == "udp":
        args.append(f"-u -b {flow.bandwidth or '1M'}")
    elif flow.bandwidth:
        args.append(f"-b {flow.bandwidth}")
    if flow.parallel > 1:
        args.append(f"-P {flow.parallel}")
    if flow.reverse:
        args.append("-R")
    return " ".join(args)


async def run_iperf(ssh: Any, plans: list[ClientPlan], params: IperfParams, *, sudo: bool, log: Any,
                    check_cancelled: Any = None) -> dict[str, Any]:
    by_name = client_index(plans)
    check_clients(plans, [n for f in params.flows for n in (f.src, f.dst)])

    async def one(i: int, flow: IperfFlow) -> dict[str, Any]:
        base = {"src": flow.src, "dst": flow.dst, "proto": flow.proto}
        target = default_ip(by_name[flow.dst], flow.vlan)
        empty = {"seconds": None, "bits_per_second_sent": None, "bits_per_second_received": None, "retransmits": None,
                 "jitter_ms": None, "lost_percent": None}
        if not target:
            return {**base, **empty, "ok": False, "error": f"{flow.dst} has no address"}
        if check_cancelled:
            check_cancelled()
        srv = docker_exec(flow.dst, "sh -c " + shlex.quote("pgrep iperf3 >/dev/null || iperf3 -s -D"))
        await ssh.run(srv, sudo=sudo, timeout=30)
        cmd = docker_exec(flow.src, iperf_client_cmd(flow, target))
        log(f"[iperf {i + 1}] {flow.src} -> {flow.dst} ({target}) {flow.proto} t={flow.duration}s"
            + (f" b={flow.bandwidth}" if flow.bandwidth else "") + (f" P={flow.parallel}" if flow.parallel > 1 else "")
            + (" reverse" if flow.reverse else ""))
        try:
            res = await ssh.run(cmd, sudo=sudo, timeout=flow.duration + 60)
            parsed = parse_iperf(res.stdout or res.stderr)
            if not res.ok and not parsed["error"]:
                parsed["error"] = f"iperf3 rc={res.returncode}"
        except Exception as exc:  # noqa: BLE001
            parsed = {**empty, "error": str(exc)}
        r = {**base, **parsed, "ok": parsed["error"] is None}
        if r["ok"]:
            mbps = lambda v: f"{v / 1e6:.1f}" if v is not None else "-"  # noqa: E731
            log(f"[iperf {i + 1}] sent {mbps(r['bits_per_second_sent'])} Mbit/s, received "
                f"{mbps(r['bits_per_second_received'])} Mbit/s"
                + (f", retransmits {r['retransmits']}" if r["retransmits"] is not None else "")
                + (f", jitter {r['jitter_ms']} ms, lost {r['lost_percent']}%" if r["jitter_ms"] is not None else ""))
        else:
            log(f"[iperf {i + 1}] failed: {r['error']}")
        return r

    if params.sequential:
        results = [await one(i, f) for i, f in enumerate(params.flows)]
    else:
        results = list(await asyncio.gather(*(one(i, f) for i, f in enumerate(params.flows))))
    return {"results": results}


@job("lab.traffic_iperf")
async def lab_traffic_iperf(ctx: JobContext) -> dict[str, Any]:
    params = IperfParams(**(ctx.params or {}))
    lab, host = await _lab_host(ctx)
    plans = lab_plans(lab)
    ctx.log(f"[iperf] {len(params.flows)} flow(s), {'sequential' if params.sequential else 'parallel'}")
    async with SessionLocal() as s:
        async with SSHClient.connect(host, s, log=ctx.log, ctx=ctx) as ssh:
            out = await run_iperf(ssh, plans, params, sudo=docker_needs_sudo(host), log=ctx.log,
                                  check_cancelled=ctx.check_cancelled)
    out["run_id"] = await _store(lab.id, ctx.job_id, "iperf", params.model_dump(), out)
    return out


def run_out(r: TrafficRun, *, full: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {"id": r.id, "lab_id": r.lab_id, "job_id": r.job_id, "kind": r.kind, "params": r.params,
                           "created_at": r.created_at.isoformat()}
    res = r.results or {}
    rows = res.get("results") or []
    out["summary"] = {"total": len(rows), "ok": sum(1 for x in rows if x.get("ok"))}
    if full:
        out["results"] = rows
        out["matrix"] = res.get("matrix") if r.kind == "ping" else None
    return out
