"""ANTA security advisories (`anta psirt`, preview in ANTA 1.10) against a running lab — demo of the PSIRT tooling.

  POST /labs/{id}/psirt {tests?: [SA117, …]}   202 job lab.psirt
  GET  /labs/{id}/psirt                        last run: summary + rows (+ which tests exist in the agent image)
  GET  /labs/{id}/psirt/report.md              the Markdown report of the last run

The job builds an ANTA inventory from the lab's running cEOS nodes (management IPs from the last inspect), runs
`anta psirt csv` and `anta psirt md-report` in the agent container on the lab's management network (credentials
through an env-file, like validate/cv_deploy), parses the CSV into per-device/per-advisory rows and keeps both
reports under anta/psirt/. cEOS-lab reports its EOS version like hardware does, so advisories resolve on the lab
exactly as they would on a real fabric — that is the point of the demo.
"""
from __future__ import annotations

import asyncio
import csv
import io
import shlex
import time
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import settings
from ..core.db import Host, SessionLocal, get_session
from ..core.security import current_user
from ..hosts.ssh import SSHClient
from ..jobs.engine import JobContext, job, runner
from ..labs import service as lab_service
from .base import ExecutorError
from .clab import EOS_KINDS
from .common import DEPLOYED_STATES, agent_uid_gid, chown_back, docker_needs_sudo, eapi_credentials, remote_lab_dir, sync_lab, update_lab
from .hot_clients import mgmt_network_of
from .lab_routes import _busy, _lab, _require_deployed, _runtime_nodes

router = APIRouter(prefix="/labs", tags=["psirt"])
q = shlex.quote
PSIRT_DIR = "anta/psirt"
PSIRT_JOBS = ("lab.psirt", "lab.validate", "lab.deploy", "lab.destroy")
RESULT_ORDER = ("affected", "inconclusive", "mitigated", "not affected", "error", "skipped")
SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "none": 4, "unknown": 5}


# ------------------------------------------------------------------------------------------------ pure helpers
def anta_inventory(nodes: dict[str, dict[str, Any]]) -> tuple[str, list[str]]:
    """ANTA inventory YAML for the running EOS nodes → (yaml, hostnames)."""
    hosts = [{"name": n, "host": v["mgmt_ip"]} for n, v in sorted(nodes.items())
             if v.get("kind") in EOS_KINDS and v.get("mgmt_ip") and v.get("state") == "running"]
    return yaml.safe_dump({"anta_inventory": {"hosts": hosts}}, sort_keys=False), [h["name"] for h in hosts]


def parse_psirt_csv(text: str) -> dict[str, Any]:
    """`anta psirt csv` → {rows, by_device, by_advisory, summary}. One CSV row = one vulnerability assessment; the
    whole-advisory result is repeated on every row and is the authoritative one."""
    rows: list[dict[str, Any]] = []
    for r in csv.DictReader(io.StringIO(text)):
        rows.append({
            "device": r.get("Device", ""), "test": r.get("Test Name", ""),
            "advisory_id": r.get("Advisory ID", ""), "advisory_title": r.get("Advisory Title", ""),
            "advisory_severity": (r.get("Advisory Severity") or "unknown").lower(), "advisory_url": r.get("Advisory URL", ""),
            "advisory_result": (r.get("Advisory Result") or "").lower(),
            "advisory_messages": (r.get("Advisory Result Messages") or "").replace("\\n", "\n"),
            "vulnerability_id": r.get("Vulnerability ID", ""), "vulnerability_severity": (r.get("Vulnerability Severity") or "").lower(),
            "vulnerability_result": (r.get("Vulnerability Result") or "").lower(),
            "vulnerability_messages": (r.get("Vulnerability Result Messages") or "").replace("\\n", "\n"),
            "remediation": r.get("Vulnerability Remediation") or r.get("Advisory Remediation") or "",
        })
    # authoritative per (device, advisory)
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        key = (r["device"], r["advisory_id"] or r["test"])
        seen.setdefault(key, {"device": r["device"], "advisory_id": r["advisory_id"], "test": r["test"], "title": r["advisory_title"],
                              "severity": r["advisory_severity"], "url": r["advisory_url"], "result": r["advisory_result"]})
    summary = {k: 0 for k in RESULT_ORDER}
    for v in seen.values():
        summary[v["result"] if v["result"] in summary else "error"] += 1
    by_device: dict[str, dict[str, int]] = {}
    for v in seen.values():
        d = by_device.setdefault(v["device"], {k: 0 for k in RESULT_ORDER})
        d[v["result"] if v["result"] in d else "error"] += 1
    advisories = sorted({(v["advisory_id"], v["title"], v["severity"], v["url"]) for v in seen.values()},
                        key=lambda a: (SEVERITY_ORDER.get(a[2], 9), a[0]))
    return {"rows": rows, "advisories": [{"id": a, "title": t, "severity": s, "url": u} for a, t, s, u in advisories],
            "assessments": sorted(seen.values(), key=lambda v: (RESULT_ORDER.index(v["result"]) if v["result"] in RESULT_ORDER else 9,
                                                                 SEVERITY_ORDER.get(v["severity"], 9), v["device"])),
            "summary": summary, "by_device": by_device, "devices": sorted(by_device)}


def psirt_cmd(rdir: str, network: str, env_file: str, image: str, sub: str, tests: list[str], *, entrypoint_is_anta: bool = True) -> str:
    """The ANTA image's entrypoint is `anta` (command = `psirt …`); on the AVD universal image we call `anta psirt …`.
    `--enable`: many advisory checks run `show running-config …`, which needs privileged mode even for a privilege-15 user
    over eAPI — without it every such check ends in "requires privileged mode"."""
    sel = "".join(f" --test {q(t)}" for t in tests)
    prog = "" if entrypoint_is_anta else "anta "
    return (f"docker run --rm --network {q(network)} --env-file {q(env_file)} -v {q(rdir)}:/lab -w /lab {q(image)} "
            f"{prog}psirt -i {PSIRT_DIR}/inventory.yml --insecure --enable{sel} {sub}")


async def pick_image(ssh: Any, dsudo: bool, log: Any) -> tuple[str, bool]:
    """(image, entrypoint_is_anta): the dedicated ANTA image (pulled on first use), else the agent image if its ANTA has psirt."""
    anta_img, agent = settings.anta_image, settings.agent_image
    present = (await ssh.run(f"docker image inspect {q(anta_img)} >/dev/null 2>&1", sudo=dsudo, timeout=60)).ok
    if not present:
        log(f"[psirt] pulling {anta_img} (first use)")
        pull = await ssh.run(f"docker pull {q(anta_img)}", sudo=dsudo, timeout=900, log=log)
        present = pull.ok
        if not present:
            log(f"[psirt] could not pull {anta_img}: trying the agent image {agent}")
    if present:
        res = await ssh.run(f"docker run --rm {q(anta_img)} psirt --help", sudo=dsudo, timeout=300)
        if res.ok and "security advisor" in (res.stdout + res.stderr).lower():
            return anta_img, True
        log(f"[psirt] {anta_img} has no `anta psirt`?! trying the agent image {agent}")
    res = await ssh.run(f"docker run --rm --entrypoint anta {q(agent)} psirt --help", sudo=dsudo, timeout=300)
    if res.ok and "security advisor" in (res.stdout + res.stderr).lower():
        return agent, False
    raise ExecutorError(f"no image with `anta psirt` (ANTA >= 1.10): could not pull {anta_img} and the agent image {agent} "
                        "bundles an older ANTA — check the host's access to ghcr.io or set AVDLAB_ANTA_IMAGE")


# ------------------------------------------------------------------------------------------------ job
class PsirtParams(BaseModel):
    tests: list[str] = Field(default_factory=list, description="advisory test class names, e.g. SA117; empty = all built-in")


@job("lab.psirt")
async def lab_psirt(ctx: JobContext) -> dict[str, Any]:
    lab = await ctx.get_lab()
    params = PsirtParams(**(ctx.params or {}))
    if lab.state not in DEPLOYED_STATES:
        raise ExecutorError(f"lab is {lab.state.value}: deploy it first")
    if not lab.host_id:
        raise ExecutorError("lab has no host")
    async with SessionLocal() as s:
        host = await s.get(Host, lab.host_id)
    if host is None:
        raise ExecutorError("lab host not found")
    nodes = _runtime_nodes(lab)
    inv, names = anta_inventory(nodes)
    if not names:
        raise ExecutorError("no running cEOS nodes with a management IP known: refresh the lab status (Runtime tab) first")
    root = lab_service.lab_dir(lab)
    local_dir = root / PSIRT_DIR
    local_dir.mkdir(parents=True, exist_ok=True)
    (local_dir / "inventory.yml").write_text(inv)
    for f in ("psirt.csv", "psirt.md"):
        (local_dir / f).unlink(missing_ok=True)
    await asyncio.to_thread(lab_service.commit_all, root, "psirt: inventory")
    user, password = eapi_credentials(lab)
    rdir = remote_lab_dir(lab)
    dsudo = docker_needs_sudo(host)
    network = mgmt_network_of(lab)
    ctx.log(f"[psirt] {len(names)} device(s): {', '.join(names)}" + (f"; tests: {', '.join(params.tests)}" if params.tests else "; all built-in advisories"))
    ctx.log("[psirt] anta psirt is a preview feature of ANTA 1.10: reports may change between releases")
    t0 = time.monotonic()
    async with SessionLocal() as s:
        async with SSHClient.connect(host, s, log=ctx.log, ctx=ctx) as ssh:
            image, ep_anta = await pick_image(ssh, dsudo, ctx.log)
            ctx.log(f"[psirt] using {image}")
            await sync_lab(ctx, ssh, lab, host, tag="psirt")
            uid_gid = await agent_uid_gid(ssh)
            await ssh.run(f"cd {q(rdir)} && chmod -R a+rwX {PSIRT_DIR}", log=ctx.log)
            mk = await ssh.run("umask 077 && mktemp -p \"${HOME:-/tmp}\" .avdlab-psirt-env.XXXXXX", timeout=30)
            env_file = (mk.stdout or "").strip().splitlines()[-1] if mk.ok and mk.stdout.strip() else ""
            if not env_file:
                raise ExecutorError(f"could not create the env-file on {host.name}")
            try:
                wr = await ssh.run(f"cat > {q(env_file)} && chmod 600 {q(env_file)}", timeout=30,
                                   input=f"ANTA_USERNAME={user}\nANTA_PASSWORD={password}\n")
                if not wr.ok:
                    raise ExecutorError(f"could not write the env-file on {host.name}")
                for sub in (f"csv --csv-output {PSIRT_DIR}/psirt.csv", f"md-report --md-output {PSIRT_DIR}/psirt.md"):
                    cmd = psirt_cmd(rdir, network, env_file, image, sub, params.tests, entrypoint_is_anta=ep_anta)
                    ctx.log(f"[psirt] {cmd}")
                    run = await ssh.run(cmd, sudo=dsudo, timeout=1800, log=ctx.log)
                    ctx.log(f"[psirt] rc={run.returncode} (non-zero when a device is affected — the report decides)")
            finally:
                await ssh.run(f"rm -f {q(env_file)}", timeout=30)
            await chown_back(ssh, rdir, PSIRT_DIR, uid_gid, sudo=dsudo, log=ctx.log)
            got: dict[str, Path] = {}
            for f in ("psirt.csv", "psirt.md"):
                try:
                    await ssh.get(f"{rdir}/{PSIRT_DIR}/{f}", local_dir / f)
                    got[f] = local_dir / f
                except Exception as exc:  # noqa: BLE001
                    ctx.log(f"[psirt] could not fetch {f}: {exc}")
    if "psirt.csv" not in got:
        raise ExecutorError("anta psirt produced no CSV report: see the log above")
    parsed = parse_psirt_csv(got["psirt.csv"].read_text())
    await asyncio.to_thread(lab_service.commit_all, root, f"psirt: {parsed['summary']['affected']} affected")
    last = {"at": time.time(), "job_id": ctx.job_id, "tests": params.tests, "devices": names, "duration": round(time.monotonic() - t0, 1),
            "summary": parsed["summary"], "by_device": parsed["by_device"], "advisories": parsed["advisories"],
            "assessments": parsed["assessments"], "rows": parsed["rows"][:2000], "has_md": "psirt.md" in got}
    await update_lab(lab.id, last_psirt=last)
    s_ = parsed["summary"]
    ctx.log(f"[psirt] done in {last['duration']}s: affected {s_['affected']}, inconclusive {s_['inconclusive']}, mitigated {s_['mitigated']}, "
            f"not affected {s_['not affected']}, error {s_['error']}, skipped {s_['skipped']} (device × advisory)")
    return {k: v for k, v in last.items() if k != "rows"}


# ------------------------------------------------------------------------------------------------ API
class PsirtIn(BaseModel):
    tests: list[str] = Field(default_factory=list)


@router.post("/{lab_id}/psirt", status_code=202)
async def start_psirt(lab_id: str, body: PsirtIn | None = None, session: AsyncSession = Depends(get_session),
                      user: str = Depends(current_user)) -> dict[str, str]:
    lab = await _lab(session, lab_id)
    _require_deployed(lab)
    await _busy(session, lab, PSIRT_JOBS)
    params = (body or PsirtIn()).model_dump()
    return {"job_id": await runner.submit("lab.psirt", lab_id=lab.id, host_id=lab.host_id, params=params)}


@router.get("/{lab_id}/psirt")
async def get_psirt(lab_id: str, session: AsyncSession = Depends(get_session), user: str = Depends(current_user)) -> dict[str, Any]:
    lab = await _lab(session, lab_id)
    last = (lab.settings or {}).get("last_psirt")
    return {"available": lab.state in DEPLOYED_STATES and bool(lab.host_id), "last": last}


@router.get("/{lab_id}/psirt/report.md", response_class=PlainTextResponse)
async def psirt_report(lab_id: str, session: AsyncSession = Depends(get_session), user: str = Depends(current_user)) -> str:
    lab = await _lab(session, lab_id)
    p = lab_service.lab_dir(lab) / PSIRT_DIR / "psirt.md"
    if not p.exists():
        raise HTTPException(404, "no PSIRT report yet")
    return p.read_text()
