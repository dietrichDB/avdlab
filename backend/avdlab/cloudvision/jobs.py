"""CloudVision jobs, run over SSH on the lab host:

* `lab.cv_onboard` — copy the onboarding token into every cEOS container at the `token_file` the build rendered,
  restart TerminAttr, then poll TerminAttr + (with a service-account token) CV inventory streaming status.
* `lab.cv_deploy`  — `arista.avd.cv_deploy` inside the agent container, server/token passed through a 0600 env-file
  that is removed in a `finally`; the role's detailed results are parsed from the AVDLAB_CV_RESULT markers.
"""
from __future__ import annotations

import asyncio
import re
import shlex
import time
from typing import Any

from pydantic import BaseModel, Field

from ..core.config import settings
from ..core.db import CVTarget, LabState, SessionLocal
from ..executors.base import ExecutorError
from ..executors.clab import (
    EOS_KINDS,
    RSYNC_EXCLUDES,
    _lab_and_host,
    docker_needs_sudo,
    load_topology,
    remote_lab_dir,
    update_lab,
)
from ..hosts.ssh import SSHClient
from ..jobs.engine import JobContext, job
from ..labs import service as lab_service
from ..labs.templates import cv_token_file, load_group_vars_dir, serials_from_group_vars
from . import service as cv

ONBOARD_TIMEOUT = 120.0  # seconds to wait for TerminAttr / streaming
ONBOARD_POLL_INTERVAL = 5.0  # tests set 0
ONBOARD_STATES = (LabState.deployed, LabState.validated)
CV_DEPLOY_STATES = (LabState.built, LabState.deployed, LabState.validated, LabState.destroyed)
TERMINATTR_STOP = "configure\ndaemon TerminAttr\nshutdown\nend"
TERMINATTR_START = "configure\ndaemon TerminAttr\nno shutdown\nend"
TERMINATTR_RESTART = TERMINATTR_STOP  # kept for older tests/imports
RESTART_PAUSE = 2.0  # seconds between shutdown and no shutdown so ProcMgr really restarts the agent (tests set 0)


class CVDeployParams(BaseModel):
    submit_workspace: bool = True
    run_change_control: bool = False
    skip_missing_devices: bool = True
    strict_tags: bool = False
    workspace_name: str | None = Field(default=None, max_length=200)


# ------------------------------------------------------------------------------------------------ helpers
async def load_target(lab_id: str) -> CVTarget:
    from ..core.db import Lab

    async with SessionLocal() as s:
        lab = await s.get(Lab, lab_id)
        if lab is None or not lab.cv_target_id:
            raise ExecutorError("lab has no CloudVision target: pick one in the lab settings")
        target = await s.get(CVTarget, lab.cv_target_id)
        if target is None:
            raise ExecutorError("CloudVision target not found")
        return target


async def _tokens(target: CVTarget) -> tuple[str | None, str | None]:
    async with SessionLocal() as s:
        return await cv.target_tokens(s, target)


def container_name(topo: dict[str, Any], node: str) -> str:
    """containerlab naming: prefix '' → bare node name; missing → clab-<lab>-<node>; else <prefix>-<lab>-<node>."""
    if "prefix" in topo:
        prefix = topo.get("prefix")
        if prefix in ("", "__lab-name"):
            return node if prefix == "" else f"{topo.get('name')}-{node}"
        return f"{prefix}-{topo.get('name')}-{node}"
    return f"clab-{topo.get('name')}-{node}"


_RUNNING_RE = re.compile(r"\(running(?: with pid \d+)?\)", re.I)


def terminattr_running(text: str) -> bool:
    """`show daemon TerminAttr` prints `Agent: TerminAttr (running with PID n)` or `(not running)`."""
    t = text or ""
    return bool(_RUNNING_RE.search(t)) and "not running" not in t.lower()


# newest TerminAttr log, only the lines that matter (auth, streaming, redirector, token, cert) — Sysdb sync spam is dropped
TERMINATTR_LOG_TAIL = ("bash -c 'f=$(ls -t /var/log/agents/TerminAttr-* 2>/dev/null | head -1); "
                       "[ -n \"$f\" ] && { echo \"== $f\"; grep -iE \"libmain|terminattr_auth|stream|redirect|token|enroll|cert|error|dial|dns|resolve\" \"$f\" | tail -n 15; } "
                       "|| ls /var/log/agents'")
_PID_RE = re.compile(r"running with PID (\d+)", re.I)


def terminattr_pid(text: str) -> int | None:
    m = _PID_RE.search(text or "")
    return int(m.group(1)) if m else None


# ------------------------------------------------------------------------------------------------ onboarding
@job("lab.cv_onboard")
async def lab_cv_onboard(ctx: JobContext) -> dict[str, Any]:
    lab, host = await _lab_and_host(ctx)
    if lab.state not in ONBOARD_STATES:
        raise ExecutorError(f"lab is {lab.state.value}: deploy it before onboarding to CloudVision")
    target = await load_target(lab.id)
    token, onboarding = await _tokens(target)
    if not onboarding:
        raise ExecutorError(f"CloudVision target {target.name} has no onboarding token: add one on the CloudVision page")
    log = cv.masking_log(ctx.log, [token, onboarding])
    root = lab_service.lab_dir(lab)
    gv = await asyncio.to_thread(load_group_vars_dir, root)
    kind, token_file = cv_token_file(gv)
    if not token_file:
        raise ExecutorError("the lab has no cv_settings: set the cloudvision knob (mode cvaas/onprem) and rebuild/redeploy")
    serials = serials_from_group_vars(gv)
    topo = load_topology(root)
    nodes = [n for n in ((lab.settings or {}).get("last_inspect") or {}).get("nodes", []) if n.get("kind") in EOS_KINDS]
    if not nodes:
        raise ExecutorError("no cEOS nodes known for this lab: deploy it (or refresh its status) first")
    dsudo = docker_needs_sudo(host)
    q = shlex.quote
    log(f"[cv] target {target.name} ({target.kind}, {cv.server_host(target.server)}), cluster {kind}, "
        f"token_file {token_file}, {len(nodes)} cEOS nodes")
    results: dict[str, dict[str, Any]] = {
        n["name"]: {"name": n["name"], "serial": serials.get(n["name"]), "terminattr_running": False,
                    "streaming": None if not token else False} for n in nodes}
    async with SessionLocal() as s:
        async with SSHClient.connect(host, s, log=log, ctx=ctx) as ssh:
            for n in nodes:
                ctx.check_cancelled()
                cname = container_name(topo, n["name"])
                # Write the token from INSIDE the container's mount namespace: cEOS mounts a tmpfs on /tmp at boot, so a
                # `docker cp` lands on the hidden overlay path and TerminAttr never sees it. Content goes over stdin
                # (`docker exec -i`), never on a command line; a persistent copy on flash survives TerminAttr restarts.
                inner = (f"umask 077; d=$(dirname {q(token_file)}); mkdir -p \"$d\"; cat > {q(token_file)} && "
                         f"cp {q(token_file)} /mnt/flash/avdlab-cv-onboarding-token 2>/dev/null; test -s {q(token_file)}")
                res = await ssh.run(f"docker exec -i {q(cname)} sh -c {q(inner)}", sudo=dsudo, timeout=60, log=log,
                                    input=onboarding.strip() + "\n")
                if not res.ok:
                    raise ExecutorError(f"could not write the onboarding token into {cname}:{token_file} (rc={res.returncode})")
                # verify the token really landed (non-empty file inside the switch container)
                chk = await ssh.run(f"docker exec {q(cname)} bash -c {q(f'test -s {token_file} && wc -c < {token_file}')}", sudo=dsudo, timeout=30)
                if not chk.ok:
                    raise ExecutorError(f"{n['name']}: onboarding token file {token_file} is missing or empty inside the container")
                before = await ssh.run(f"docker exec {q(cname)} Cli -p 15 -c 'show daemon TerminAttr'", sudo=dsudo, timeout=30)
                old_pid = terminattr_pid(before.stdout)
                exec_line = next((ln.strip() for ln in (before.stdout or "").splitlines() if "/usr/bin/TerminAttr" in ln), "")
                log(f"[cv] {n['name']}: token written to {token_file} ({chk.stdout.strip()} bytes); TerminAttr pid={old_pid}"
                    + (f"; {exec_line[:160]}" if exec_line else "; WARNING: no TerminAttr exec line configured (cv_settings missing?)"))
                # two-step restart: a shutdown/no shutdown in one batch is coalesced by ProcMgr and the agent keeps its stale auth state
                res = await ssh.run(f"docker exec {q(cname)} Cli -p 15 -c {q(TERMINATTR_STOP)}", sudo=dsudo, timeout=60, log=log)
                await asyncio.sleep(RESTART_PAUSE)
                res2 = await ssh.run(f"docker exec {q(cname)} Cli -p 15 -c {q(TERMINATTR_START)}", sudo=dsudo, timeout=60, log=log)
                if not (res.ok and res2.ok):
                    log(f"[cv] {n['name']}: TerminAttr restart returned rc={res.returncode}/{res2.returncode}")
                r0 = results[n["name"]]
                r0["old_pid"] = old_pid
            deadline = time.monotonic() + ONBOARD_TIMEOUT
            while True:
                ctx.check_cancelled()
                for name, r in results.items():
                    if r["terminattr_running"]:
                        continue
                    # `show daemon` needs privilege 15 on EOS — without -p 15 it is "% Invalid input" and looks down
                    res = await ssh.run(f"docker exec {q(container_name(topo, name))} Cli -p 15 -c 'show daemon TerminAttr'",
                                        sudo=dsudo, timeout=30)
                    new_pid = terminattr_pid(res.stdout)
                    r["terminattr_running"] = res.ok and terminattr_running(res.stdout)
                    if r["terminattr_running"] and r.get("old_pid") and new_pid == r["old_pid"] and not r.get("_pid_warned"):
                        r["_pid_warned"] = True
                        log(f"[cv] {name}: WARNING TerminAttr pid unchanged ({new_pid}) — the agent did not restart; it may still hold stale auth state")
                    if not r["terminattr_running"] and not r.get("_logged_status"):
                        r["_logged_status"] = True
                        head = "\n".join((res.stdout or res.stderr or "").strip().splitlines()[:3])
                        log(f"[cv] {name}: show daemon TerminAttr (rc={res.returncode}): {head or '<no output>'}")
                if token and not all(r["streaming"] for r in results.values()):
                    try:
                        inv = await cv.cv_inventory(target.server, token, verify_certs=target.verify_certs)
                        active = {d["serial"] for d in inv if d["streaming"]}
                        for r in results.values():
                            r["streaming"] = bool(r["serial"]) and r["serial"] in active
                    except cv.CVError as exc:
                        log(f"[cv] inventory check failed: {exc}")
                done = all(r["terminattr_running"] and (r["streaming"] is None or r["streaming"])
                           for r in results.values())
                summary = ", ".join(f"{k}={'up' if r['terminattr_running'] else 'down'}"
                                    + ("" if r["streaming"] is None else f"/{'streaming' if r['streaming'] else 'not streaming'}")
                                    for k, r in results.items())
                log(f"[cv] {summary}")
                if done or time.monotonic() >= deadline:
                    break
                await asyncio.sleep(ONBOARD_POLL_INTERVAL)
            # diagnostics for anything still down / not streaming: TerminAttr's own log tells you DNS, auth or connectivity
            for name, r in results.items():
                if r["terminattr_running"] and r["streaming"] is not False:
                    continue
                res = await ssh.run(f"docker exec {q(container_name(topo, name))} {TERMINATTR_LOG_TAIL}", sudo=dsudo, timeout=30)
                tail = "\n".join((res.stdout or "").strip().splitlines()[-12:])
                log(f"[cv] {name}: TerminAttr log tail:\n{tail or '<empty>'}")
    for r in results.values():
        for k in ("_logged_status", "_pid_warned", "old_pid"):
            r.pop(k, None)
    out_nodes = sorted(results.values(), key=lambda r: r["name"])
    complete = all(r["terminattr_running"] and r["streaming"] is not False for r in out_nodes)
    if not complete:
        log("[cv] not every node is running TerminAttr/streaming yet (CloudVision may still be registering them)")
    onboarding_state = {"at": time.time(), "target_id": target.id, "cluster": kind, "token_file": token_file,
                        "complete": complete, "nodes": out_nodes}
    await update_lab(lab.id, cv_onboarding=onboarding_state)
    return {"nodes": out_nodes, "complete": complete}


# ------------------------------------------------------------------------------------------------ cv_deploy
@job("lab.cv_deploy")
async def lab_cv_deploy(ctx: JobContext) -> dict[str, Any]:
    lab, host = await _lab_and_host(ctx)
    params = CVDeployParams(**(ctx.params or {}))
    if lab.state not in CV_DEPLOY_STATES:
        raise ExecutorError(f"lab is {lab.state.value}: build it before deploying to CloudVision")
    root = lab_service.lab_dir(lab)
    sc_dir = root / "intended" / "structured_configs"
    if not sc_dir.is_dir() or not any(sc_dir.iterdir()):
        raise ExecutorError("intended/structured_configs is empty: build the lab first")
    target = await load_target(lab.id)
    token, onboarding = await _tokens(target)
    if not token:
        raise ExecutorError(f"CloudVision target {target.name} has no service-account token")
    log = cv.masking_log(ctx.log, [token, onboarding])
    fabric_group = str((lab.settings or {}).get("fabric_name") or "FABRIC")
    playbook = cv.cv_deploy_playbook(fabric_group, verify_certs=target.verify_certs,
                                     submit_workspace=params.submit_workspace,
                                     run_change_control=params.run_change_control,
                                     skip_missing_devices=params.skip_missing_devices, strict_tags=params.strict_tags,
                                     workspace_name=params.workspace_name)
    sha = await asyncio.to_thread(lab_service.write_files, lab, {cv.CV_DEPLOY_PLAYBOOK: playbook},
                                  "cv_deploy: generated playbook")
    server = cv.server_host(target.server)
    # 0.8.0 pre-flight: a 15 s REST call from the orchestrator turns "ansible failed" into a precise cause
    # (expired token, unreachable server, bad certificate) before a 20-minute playbook run.
    pre = await cv.check_target(target.server, token, verify_certs=bool(target.verify_certs))
    if not pre.get("ok"):
        err = str(pre.get("error") or "unknown error")
        hint = ("service-account token rejected: generate a new one in CloudVision (Settings > Access Control > Service Accounts) "
                "and save it on the target" if re.search(r"401|403|unauth|invalid token|expired", err, re.I)
                else "the orchestrator could not reach the CloudVision API on 443; check DNS/firewall/proxy and verify_certs")
        await update_lab(lab.id, cv_last_deploy={"at": time.time(), "job_id": ctx.job_id, "target_id": target.id, "ok": False,
                                                 "params": params.model_dump(), "workspace": None, "change_control": None,
                                                 "devices": [], "errors": [f"pre-flight: {err}"], "warnings": []})
        raise ExecutorError(f"CloudVision pre-flight failed for {target.name} ({server}): {err} — {hint}")
    log(f"[cv] pre-flight ok: {target.name} answers, {pre.get('devices_seen', 0)} device(s) in its inventory")
    log("[cv] change control: " + ("AVD will approve and execute it (run_change_control=true)" if params.run_change_control
                                    else "created in 'pending approval' — approve and run it in CloudVision (run_change_control=false)")
        if params.submit_workspace else "none (workspace is only built, not submitted)")
    log(f"[cv] {cv.CV_DEPLOY_PLAYBOOK} {'updated' if sha else 'unchanged'}; target {target.name} ({server}), "
        f"submit_workspace={params.submit_workspace} run_change_control={params.run_change_control} "
        f"skip_missing_devices={params.skip_missing_devices} strict_tags={params.strict_tags}")
    rdir = remote_lab_dir(lab)
    q = shlex.quote
    dsudo = docker_needs_sudo(host)
    agent = settings.agent_image
    env_content = f"AVDLAB_CV_SERVER={server}\nAVDLAB_CV_TOKEN={token}\nANSIBLE_FORCE_COLOR=0\n"
    async with SessionLocal() as s:
        async with SSHClient.connect(host, s, log=log, ctx=ctx) as ssh:
            log(f"[cv] syncing lab to {host.name}:{rdir}")
            await ssh.rsync_dir(root, rdir, exclude=RSYNC_EXCLUDES, log=log)
            ctx.check_cancelled()
            mk = await ssh.run("umask 077 && mkdir -p \"$HOME/.avdlab\" && mktemp \"$HOME/.avdlab/cv-env.XXXXXX\"",
                               timeout=30)
            env_file = mk.stdout.strip().splitlines()[-1] if mk.ok and mk.stdout.strip() else ""
            if not env_file:
                raise ExecutorError(f"could not create the env-file on {host.name}")
            try:
                wr = await ssh.run(f"umask 077 && cat > {q(env_file)} && chmod 600 {q(env_file)}", timeout=30,
                                   input=env_content)
                if not wr.ok:
                    raise ExecutorError(f"could not write the env-file on {host.name}")
                cmd = (f"docker run --rm --env-file {q(env_file)} -v {q(rdir)}:/lab -w /lab {q(agent)} "
                       f"ansible-playbook -i inventory.yml {cv.CV_DEPLOY_PLAYBOOK} -v")
                log(f"[cv] {cmd}")
                run = await ssh.run(cmd, sudo=dsudo, timeout=3600, log=log)
            finally:
                await ssh.run(f"rm -f {q(env_file)}", timeout=30)
    log(f"[cv] ansible-playbook rc={run.returncode}")
    raw = cv.extract_result_json(run.stdout)
    summary = cv.summarize_deploy(raw, server=server, output=run.stdout, rc=run.returncode)
    last = {"at": time.time(), "job_id": ctx.job_id, "target_id": target.id, "params": params.model_dump(), **summary}
    await update_lab(lab.id, cv_last_deploy=last)
    ws = summary.get("workspace") or {}
    cc = summary.get("change_control") or {}
    log(f"[cv] workspace {ws.get('id')} state={ws.get('state')}; change control {cc.get('id')} state={cc.get('state')}")
    if ws and not cc and not summary["errors"] and str(ws.get("state") or "").lower() in {"submitted", "completed", "success"}:
        log("[cv] no change control was created: CloudVision found no config difference between the designed config "
            "(AVD configlets) and what the devices run — this is the expected outcome right after a deploy with the same "
            "intended configs. Change a knob, build and deploy again to see a change control.")
    for e in summary["errors"]:
        log(f"[cv] ERROR {e}")
    for w in summary["warnings"]:
        log(f"[cv] warning {w}")
    if raw is None and run.returncode != 0:
        raise ExecutorError(f"cv_deploy failed (rc={run.returncode}): {'; '.join(summary['errors'])[:500]}")
    if summary["errors"]:
        raise ExecutorError(f"cv_deploy reported {len(summary['errors'])} error(s): {summary['errors'][0][:400]}")
    return last
