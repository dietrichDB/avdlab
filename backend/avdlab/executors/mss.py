"""Module 4 — MSS foundation verification: job `lab.mss_verify`.

Each case probes one application between two linux clients (`docker exec` on the lab host, like the traffic jobs) and
compares the observed outcome with the expectation:

* icmp / any (incl. `app-match-all`) → `ping -c 2 -W 2`; allow = at least one reply;
* tcp → a listener `iperf3 -s -p <port> -D` on the destination, then `nc -z -w2 <ip> <port>` on the source; rc 0 = allow,
  rc 1 = deny;
* udp → the same listener, then `iperf3 -u -c <ip> -p <port> -t 1 -b 1M -J`; datagrams received = allow, connect
  error / 100 % loss = deny (note: iperf3 needs its TCP control connection on the same port).

The port is the application's first port (start of a range), 5201 when it has none. `observed: error` = the probe could
not run (exec/SSH failure, destination without an address). Without explicit cases, `derive_cases` builds them from the
knob's policy matrix: every ordered client pair whose traffic is *routed* inside one VRF (the source's default-route
VLAN address and the destination address are in the same VRF, the destination subnet is not local to the source and
the destination has no address in the source's subnet)
and whose two addresses fall into known segments × every application whose policy decision is known. Results are
stored as a `traffic_runs` row of kind `mss`.
"""
from __future__ import annotations

import asyncio
import ipaddress
import shlex
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..core.db import SessionLocal
from ..hosts.ssh import SSHClient
from ..jobs.engine import JobContext, job
from ..labs import mss
from ..topology.clients import ClientPlan, default_ip
from .clab import docker_needs_sudo
from .traffic import MAX_CONCURRENCY, _lab_host, _store, check_clients, client_index, docker_exec, lab_plans, parse_iperf, parse_ping

DEFAULT_PORT = 5201


class MssCase(BaseModel):
    src: str
    dst: str
    application: str
    expect: Literal["allow", "deny"]
    vlan: int | None = Field(default=None, ge=1, le=4094)  # target the dst address in this VLAN (default: its default-route VLAN)


class MssVerifyParams(BaseModel):
    cases: list[MssCase] | None = Field(default=None, max_length=512)


def lab_segmentation(lab: Any) -> dict[str, Any]:
    return mss.normalize_segmentation((((lab.settings or {}).get("knobs") or {}).get("segmentation")) or {})


# ------------------------------------------------------------------------------------------------ derivation
def _addressed(plan: ClientPlan) -> list[dict[str, Any]]:
    return [v for v in plan.get("vlans") or [] if v.get("ip")]


def _source_vlan(plan: ClientPlan) -> dict[str, Any] | None:
    vl = _addressed(plan)
    return next((v for v in vl if v.get("default_route")), vl[0] if vl else None)


def _vlan_of(plan: ClientPlan, vlan: int | None) -> dict[str, Any] | None:
    vl = _addressed(plan)
    if vlan is not None:
        return next((v for v in vl if v["vid"] == vlan), None)
    return _source_vlan(plan)


def derive_cases(seg: dict[str, Any], plans: list[ClientPlan]) -> list[dict[str, Any]]:
    """Auto cases from the policy matrix (see module docstring). Deterministic order: src, dst, vlan, application."""
    seg = mss.normalize_segmentation(seg)
    matrix = mss.policy_matrix(seg)
    apps = [a["name"] for a in seg["applications"]]
    out: list[dict[str, Any]] = []
    for src in plans:
        sv = _source_vlan(src)
        if not sv or not sv.get("vrf"):
            continue
        local = {ipaddress.ip_network(v["ip"], strict=False) for v in _addressed(src)}
        src_seg = mss.segment_of(seg, sv["vrf"], sv["ip"])
        if not src_seg:
            continue
        for dst in plans:
            if dst["name"] == src["name"]:
                continue
            if ipaddress.ip_network(sv["ip"], strict=False) in {ipaddress.ip_network(v["ip"], strict=False) for v in _addressed(dst)}:
                continue  # dst is attached to the source subnet: replies would be bridged (asymmetric path)
            for dv in _addressed(dst):
                if dv.get("vrf") != sv["vrf"] or ipaddress.ip_network(dv["ip"], strict=False) in local:
                    continue  # inter-VRF, or bridged on a subnet the source is attached to
                dst_seg = mss.segment_of(seg, dv["vrf"], dv["ip"])
                if not dst_seg:
                    continue
                policy = matrix[sv["vrf"]][src_seg][dst_seg]
                for app in apps:
                    action = mss.decide(seg, policy, app)
                    if action is None:
                        continue
                    out.append({"src": src["name"], "dst": dst["name"], "application": app,
                                "expect": "allow" if action == "forward" else "deny", "vlan": dv["vid"]})
    return out


def annotate(seg: dict[str, Any], plans: list[ClientPlan], case: dict[str, Any]) -> dict[str, Any]:
    """Add vrf / segments / policy / target_ip to a case (informational)."""
    by = client_index(plans)
    sv = _source_vlan(by[case["src"]]) if case["src"] in by else None
    dv = _vlan_of(by[case["dst"]], case.get("vlan")) if case["dst"] in by else None
    vrf = (dv or {}).get("vrf")
    src_seg = mss.segment_of(seg, vrf, (sv or {}).get("ip")) if sv and sv.get("vrf") == vrf else None
    dst_seg = mss.segment_of(seg, vrf, (dv or {}).get("ip"))
    policy = mss.policy_matrix(seg).get(vrf or "", {}).get(src_seg or "", {}).get(dst_seg or "") if src_seg and dst_seg else None
    return {**case, "vrf": vrf, "src_segment": src_seg, "dst_segment": dst_seg, "policy": policy,
            "target_ip": default_ip(by[case["dst"]], case.get("vlan")) if case["dst"] in by else None}


# ------------------------------------------------------------------------------------------------ probes
def app_port(app: dict[str, Any]) -> int:
    ports = app.get("ports")
    return int(str(ports).split(",")[0].split("-")[0]) if ports else DEFAULT_PORT


def listener_cmd(port: int) -> str:
    return "sh -c " + shlex.quote(f"pgrep -f 'iperf3 -s -p {port}' >/dev/null || iperf3 -s -p {port} -D")


def _exec_failed(rc: int, text: str) -> bool:
    t = (text or "").lower()
    return rc in (125, 126, 127) or "oci runtime" in t or "no such container" in t or "not found" in t


async def probe(ssh: Any, app: dict[str, Any], src: str, dst: str, target: str, *, sudo: bool) -> tuple[str, str]:
    """(observed allow|deny|error, detail)."""
    proto = app["protocol"]
    if proto in ("icmp", "any"):
        res = await ssh.run(docker_exec(src, f"ping -c 2 -W 2 -i 0.2 {target}"), sudo=sudo, timeout=30)
        text = (res.stdout or "") + "\n" + (res.stderr or "")
        if "transmitted" not in text:
            return "error", (text.strip().splitlines() or [f"ping rc={res.returncode}"])[-1][:200]
        p = parse_ping(text)
        return ("allow" if p["received"] > 0 else "deny"), f"ping {p['received']}/{p['sent']}"
    port = app_port(app)
    srv = await ssh.run(docker_exec(dst, listener_cmd(port)), sudo=sudo, timeout=30)
    if not srv.ok:
        return "error", f"listener on {dst}:{port} failed: {(srv.stderr or srv.stdout or '').strip()[:160]}"
    if proto == "tcp":
        res = await ssh.run(docker_exec(src, f"nc -z -w2 {target} {port}"), sudo=sudo, timeout=30)
        if res.returncode == 0:
            return "allow", f"tcp/{port} connected"
        if _exec_failed(res.returncode, (res.stderr or "") + (res.stdout or "")):
            return "error", (res.stderr or res.stdout or f"nc rc={res.returncode}").strip()[:200]
        return "deny", f"tcp/{port} no connection (rc={res.returncode})"
    res = await ssh.run(docker_exec(src, f"iperf3 -c {target} -u -p {port} -t 1 -b 1M -J"), sudo=sudo, timeout=40)
    parsed = parse_iperf(res.stdout or res.stderr)
    if parsed["error"] is None:
        lost = parsed.get("lost_percent")
        if lost is not None and float(lost) >= 100.0:
            return "deny", f"udp/{port} 100% loss"
        return "allow", f"udp/{port} received" + (f", loss {lost}%" if lost is not None else "")
    err = parsed["error"].lower()
    if any(k in err for k in ("unable to connect", "refused", "timed out", "timeout", "no route", "unreachable")):
        return "deny", f"udp/{port}: {parsed['error'][:160]}"
    return "error", parsed["error"][:200]


async def run_cases(ssh: Any, seg: dict[str, Any], plans: list[ClientPlan], cases: list[dict[str, Any]], *,
                    sudo: bool, log: Any) -> dict[str, Any]:
    check_clients(plans, [n for c in cases for n in (c["src"], c["dst"])])
    sem = asyncio.Semaphore(MAX_CONCURRENCY)

    async def one(case: dict[str, Any]) -> dict[str, Any]:
        c = annotate(seg, plans, case)
        app = mss.application(seg, c["application"])
        if app is None:
            observed, detail = "error", f"unknown application {c['application']}"
        elif not c["target_ip"]:
            observed, detail = "error", f"{c['dst']} has no address" + (f" in VLAN {c['vlan']}" if c.get("vlan") else "")
        else:
            async with sem:
                try:
                    observed, detail = await probe(ssh, app, c["src"], c["dst"], c["target_ip"], sudo=sudo)
                except Exception as exc:  # noqa: BLE001 - a failed probe is a result
                    observed, detail = "error", str(exc)[:200]
        r = {**c, "observed": observed, "pass": observed == c["expect"], "detail": detail}
        r["ok"] = r["pass"]  # traffic_runs summary compatibility
        log(f"[mss] {c['src']} -> {c['dst']} ({c['target_ip']}) {c['application']}: expect {c['expect']}, "
            f"observed {observed} — {'PASS' if r['pass'] else 'FAIL'} ({detail})")
        return r

    results = list(await asyncio.gather(*(one(c) for c in cases)))
    passed = sum(1 for r in results if r["pass"])
    return {"cases": results, "passed": passed, "failed": len(results) - passed}


@job("lab.mss_verify")
async def lab_mss_verify(ctx: JobContext) -> dict[str, Any]:
    params = MssVerifyParams(**(ctx.params or {}))
    lab, host = await _lab_host(ctx)
    plans = lab_plans(lab)
    seg = lab_segmentation(lab)
    if params.cases is None:
        cases = derive_cases(seg, plans)
        ctx.log(f"[mss] {len(cases)} case(s) derived from the policy matrix")
    else:
        cases = [c.model_dump() for c in params.cases]
        ctx.log(f"[mss] {len(cases)} explicit case(s)")
    if not seg["enabled"]:
        ctx.log("[mss] warning: segmentation is disabled in the lab knobs — the fabric does not enforce segments")
    async with SessionLocal() as s:
        async with SSHClient.connect(host, s, log=ctx.log, ctx=ctx) as ssh:
            out = await run_cases(ssh, seg, plans, cases, sudo=docker_needs_sudo(host), log=ctx.log)
    ctx.log(f"[mss] {out['passed']} passed, {out['failed']} failed")
    stored = {**out, "results": out["cases"]}
    out["run_id"] = await _store(lab.id, ctx.job_id, "mss", {"cases": params.model_dump()["cases"], "derived": params.cases is None},
                                 stored)
    return out
