"""containerlab executor: deploy / destroy / status / validate a lab on a Linux host over SSH.

Remote layout: `<settings.remote_root>/labs/<slug>/` is a tar-pipe copy of the local lab dir; containerlab runs with
sudo from that directory (`clab/topology.clab.yml`, startup configs `../intended/configs/<host>.cfg`).
Validation runs `arista.avd.anta_runner` inside `settings.agent_image` attached to the clab mgmt network.

Everything remote goes through `SSHClient` (tests fake `SSHClient.connect`). Secrets (device password) are passed on
stdin, never on a command line or in the job log.
"""
from __future__ import annotations

import asyncio
import json
import shlex
import time
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select

from ..core.db import Host, Image, Lab, LabState, SessionLocal
from ..hosts.ssh import SSHClient
from ..jobs.engine import JobContext, job
from ..labs import service
from ..topology import ztp as ztp_mod
from .base import (
    DeployParams,
    DeployResult,
    ExecContext,
    ExecutorError,
    LabStatus,
    NodeStatus,
    ValidateParams,
    ValidationSummary,
    get_executor,
)
from .common import (  # noqa: F401 - re-exported: other modules import these from .clab
    DEPLOYABLE_STATES,
    DEPLOYED_STATES,
    EAPI_PAYLOAD as _EAPI_PAYLOAD,
    MIN_ANTA,
    REPORT_FORMATS,
    RSYNC_EXCLUDES,
    VALIDATE_PLAYBOOK,
    count_files,
    curl_config_quote as _curl_config_quote,
    docker_needs_sudo,
    eapi_credentials,
    eapi_ok,
    remote_lab_dir,
    run_validation,
    sync_lab,
    update_lab,
    validate_playbook,
    version_tuple as _version_tuple,
    wait_eapi,
)

TOPO_REL = "clab/topology.clab.yml"
EOS_KINDS = {"arista_ceos", "ceos"}
EAPI_POLL_INTERVAL = 10.0  # seconds between eAPI readiness polls (tests set 0)


# ------------------------------------------------------------------------------------------------ helpers
def load_topology(lab_dir: Path) -> dict[str, Any]:
    topo = lab_dir / TOPO_REL
    if not topo.exists():
        raise ExecutorError(f"{TOPO_REL} not found in the lab: build the lab first")
    return yaml.safe_load(topo.read_text()) or {}


def topology_image(topo: dict[str, Any]) -> str:
    kinds = (topo.get("topology") or {}).get("kinds") or {}
    image = (kinds.get("arista_ceos") or kinds.get("ceos") or {}).get("image")
    if not image:
        raise ExecutorError("topology has no topology.kinds.arista_ceos.image")
    return str(image)


def mgmt_network(topo: dict[str, Any]) -> str:
    net = (topo.get("mgmt") or {}).get("network")
    return str(net) if net else "clab"


def mgmt_subnet(topo: dict[str, Any]) -> str | None:
    net = (topo.get("mgmt") or {}).get("ipv4-subnet")
    return str(net) if net else None


# docker network ls/inspect in one go: "<name>|<subnets>|<container count>" per line, mgmt networks of containerlab labs only
STALE_NET_LIST = ("docker network ls --filter name=clab- --format '{{.Name}}' | xargs -r docker network inspect "
                  "-f '{{.Name}}|{{range .IPAM.Config}}{{.Subnet}} {{end}}|{{len .Containers}}'")


def parse_networks(output: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for line in (output or "").splitlines():
        parts = line.strip().split("|")
        if len(parts) != 3:
            continue
        try:
            containers = int(parts[2])
        except ValueError:
            containers = -1
        out.append({"name": parts[0], "subnets": parts[1].split(), "containers": containers})
    return out


async def clear_subnet_conflicts(ssh: Any, *, network: str, subnet: str | None, dsudo: bool, log: Any) -> None:
    """0.10.0: after a host crash (or a destroy that never ran) a lab's docker network survives without its containers,
    and containerlab refuses to create ours on the same subnet. Remove such empty leftovers; refuse clearly when the
    other network still has containers (another lab is running on that subnet)."""
    if not subnet:
        return
    res = await ssh.run(STALE_NET_LIST, sudo=dsudo, timeout=60)
    if not res.ok:
        return
    for net in parse_networks(res.stdout):
        if net["name"] == network or subnet not in net["subnets"]:
            continue
        if net["containers"] == 0:
            log(f"[deploy] docker network {net['name']} holds {subnet} but has no containers (stale after a crash/destroy): removing it")
            rm = await ssh.run(f"docker network rm {shlex.quote(net['name'])}", sudo=dsudo, timeout=60)
            if not rm.ok:
                raise ExecutorError(f"could not remove stale docker network {net['name']} on {subnet}: {(rm.stderr or rm.stdout).strip()[:200]}")
        else:
            other = net["name"].removeprefix("clab-mgmt-").removeprefix("clab-")
            raise ExecutorError(f"management subnet {subnet} is in use by the running lab '{other}' ({net['containers']} containers) on this host: "
                                f"destroy that lab first, or give this lab another subnet (Settings → mgmt_subnet knob, e.g. 172.16.2.0/24) and rebuild")


def check_host_capabilities(host: Host) -> None:
    caps = host.capabilities or {}
    if not caps:
        raise ExecutorError(f"host {host.name} was never probed: run Probe, then Bootstrap (recipe clab-host)")
    missing = [name for name in ("docker", "containerlab") if not (caps.get(name) or {}).get("installed")]
    if missing:
        raise ExecutorError(f"host {host.name} lacks {' and '.join(missing)}: run Bootstrap (recipe clab-host) "
                            "on the Hosts page, then Probe again")


def check_image_arch(host: Host, image: str, image_arch: str | None = None) -> None:
    """Phase 3: refuse to deploy an arm64 image on an x86_64 host (and vice versa); unknown host arch passes."""
    from ..images.service import arch_mismatch, tag_arch

    mismatch = arch_mismatch(host.capabilities, image_arch or tag_arch(image))
    if mismatch:
        raise ExecutorError(f"{mismatch} ({image}): set the lab knob ceos_arch to the host's architecture and rebuild")


def lab_arch_mismatch(lab: Lab) -> str | None:
    """API pre-check for deploy: 'host is x86_64, image is arm64' (None when compatible, unknown or not built)."""
    from ..images.service import arch_mismatch, tag_arch

    host = lab.__dict__.get("host")
    try:
        image = topology_image(load_topology(service.lab_dir(lab)))
    except ExecutorError:
        return None
    return arch_mismatch(host.capabilities if host is not None else None, tag_arch(image))


def parse_inspect(text: str, lab_name: str | None = None) -> list[NodeStatus]:
    """`containerlab inspect --format json` (both the `{"containers": [...]}` and the `{"<lab>": [...]}` shapes)."""
    text = (text or "").strip()
    start = min([i for i in (text.find("{"), text.find("[")) if i >= 0], default=-1)
    if start < 0:
        return []
    try:
        data = json.loads(text[start:])
    except ValueError:
        return []
    if isinstance(data, dict):
        containers = data.get("containers")
        if containers is None:
            containers = [c for v in data.values() if isinstance(v, list) for c in v]
    elif isinstance(data, list):
        containers = data
    else:
        containers = []
    nodes: list[NodeStatus] = []
    for c in containers or []:
        if not isinstance(c, dict):
            continue
        name = str((c.get("labels") or {}).get("clab-node-name") or c.get("name") or "")
        lab = c.get("lab_name") or lab_name
        if lab and name.startswith(f"clab-{lab}-"):
            name = name[len(f"clab-{lab}-"):]
        raw_ip = str(c.get("ipv4_address") or c.get("ipv4") or "").strip()
        ip = "" if raw_ip.upper() in ("N/A", "") else raw_ip.split("/")[0]
        kind = str(c.get("kind") or "")
        nodes.append(NodeStatus(name=name, kind=kind, state=str(c.get("state") or "unknown"),
                                mgmt_ip=ip or None, image=str(c.get("image") or ""),
                                eapi=False if kind in EOS_KINDS else None))
    return sorted(nodes, key=lambda n: n["name"])


# ------------------------------------------------------------------------------------------------ executor
class ClabExecutor:
    async def _run_clab(self, ssh: Any, lab: Lab, args: str, *, log: Any, timeout: float = 900) -> Any:
        cmd = f"cd {shlex.quote(remote_lab_dir(lab))} && containerlab {args}"
        return await ssh.run(cmd, sudo=True, timeout=timeout, log=log)

    async def _inspect(self, ssh: Any, lab: Lab, topo: dict[str, Any], log: Any) -> list[NodeStatus]:
        res = await self._run_clab(ssh, lab, f"inspect -t {TOPO_REL} --format json", log=log, timeout=120)
        if not res.ok:
            return []
        return parse_inspect(res.stdout, str(topo.get("name") or lab.slug))

    async def _eapi_ok(self, ssh: Any, ip: str, user: str, password: str) -> bool:
        return await eapi_ok(ssh, ip, user, password)

    async def _probe_eapi(self, ssh: Any, lab: Lab, nodes: list[NodeStatus]) -> None:
        user, password = eapi_credentials(lab)
        targets = [n for n in nodes if n["kind"] in EOS_KINDS and n["mgmt_ip"] and n["state"] == "running"]
        oks = await asyncio.gather(*(self._eapi_ok(ssh, n["mgmt_ip"] or "", user, password) for n in targets))
        for n, ok in zip(targets, oks):
            n["eapi"] = ok

    # ---- deploy
    async def deploy(self, ctx: ExecContext, lab: Lab, host: Host) -> DeployResult:
        params = DeployParams(**(ctx.params or {}))
        t0 = time.monotonic()
        if lab.state not in DEPLOYABLE_STATES:
            raise ExecutorError(f"lab is {lab.state.value}: build it before deploying")
        check_host_capabilities(host)
        root = service.lab_dir(lab)
        await ztp_align_topology(lab, root, ctx.log)
        topo = load_topology(root)
        image = topology_image(topo)
        rdir = remote_lab_dir(lab)
        ztp_meta = ztp_mod.load_meta(root) if is_ztp_topology(topo) else None
        dsudo = docker_needs_sudo(host)
        async with SessionLocal() as s:
            img_row = (await s.execute(select(Image).where(Image.docker_tag == image))).scalars().first()
            check_image_arch(host, image, img_row.arch if img_row is not None else None)
            async with SSHClient.connect(host, s, log=ctx.log, ctx=ctx) as ssh:
                # 1. image
                ctx.log(f"[deploy] checking image {image} on {host.name}")
                present = (await ssh.run(f"docker image inspect {shlex.quote(image)} >/dev/null", sudo=dsudo,
                                         log=ctx.log)).ok
                if not present:
                    if img_row is not None and img_row.local_path and Path(img_row.local_path).exists():
                        from ..images.service import import_tarball_on_host

                        ctx.log(f"[deploy] {image} missing on {host.name}: importing {Path(img_row.local_path).name}")
                        await import_tarball_on_host(ssh, img_row.local_path, image, sudo=dsudo, log=ctx.log)
                        if host.id not in (img_row.present_on_hosts or []):
                            img_row.present_on_hosts = [*(img_row.present_on_hosts or []), host.id]
                            await s.commit()
                    else:
                        raise ExecutorError(f"image {image} not on host {host.name} — push it from Images")
                ctx.log(f"[deploy] image {image} present")
                ctx.check_cancelled()
                # 2. sync
                await sync_lab(ctx, ssh, lab, host)
                ctx.check_cancelled()
                # 3. containerlab deploy (after clearing a stale network left on our subnet by a crashed/undestroyed lab)
                await clear_subnet_conflicts(ssh, network=mgmt_network(topo), subnet=mgmt_subnet(topo), dsudo=dsudo, log=ctx.log)
                args = f"deploy -t {TOPO_REL}" + (" --reconfigure" if params.reconfigure else "")
                ctx.log(f"[deploy] sudo containerlab {args}")
                res = await self._run_clab(ssh, lab, args, log=ctx.log, timeout=900)
                if not res.ok:
                    tail = " | ".join((res.stderr or res.stdout).strip().splitlines()[-2:])
                    raise ExecutorError(f"containerlab deploy failed (rc={res.returncode}): {tail}")
                # 4. inspect
                nodes = await self._inspect(ssh, lab, topo, ctx.log)
                ctx.log(f"[deploy] {len(nodes)} containers: " + ", ".join(f"{n['name']}={n['state']}" for n in nodes))
                await update_lab(lab.id, last_inspect={"at": time.time(), "nodes": nodes}, remote_dir=rdir)
                # 5. eAPI
                eos = [n for n in nodes if n["kind"] in EOS_KINDS]
                ready: list[str] = []
                pending = [n["name"] for n in eos]
                if ztp_meta is not None:  # phase 4 (A): DHCP lease → bootfile → eAPI
                    ready, pending = await self._ztp_provision(ctx, ssh, s, lab, topo, nodes, ztp_meta, params, rdir,
                                                               dsudo)
                elif params.wait_eapi and eos:
                    user, password = eapi_credentials(lab)
                    by_name = {n["name"]: n for n in eos}

                    def _mark(name: str) -> None:
                        by_name[name]["eapi"] = True

                    ready, pending = await wait_eapi(ctx, ssh, {n["name"]: n["mgmt_ip"] for n in eos}, user, password,
                                                     timeout=params.wait_timeout, interval=EAPI_POLL_INTERVAL,
                                                     on_ready=_mark)
                elif not params.wait_eapi:
                    ctx.log("[eapi] not waiting for eAPI (wait_eapi=false)")
        duration = round(time.monotonic() - t0, 1)
        # 0.10.0: remember which intended config every switch booted from, so "Push config changes" can be incremental
        from .push import config_shas, record_pushed

        await record_pushed(lab.id, None, config_shas(lab), job_id=ctx.job_id)
        await update_lab(lab.id, state=LabState.deployed, deployed_at=time.time(), remote_dir=rdir,
                         last_inspect={"at": time.time(), "nodes": nodes}, last_status={"at": time.time(), "deployed": True, "nodes": nodes})
        ctx.log(f"[deploy] lab {lab.name} deployed in {duration}s ({len(ready)} ready, {len(pending)} pending)")
        return DeployResult(nodes=nodes, ready=ready, pending=pending, duration=duration)

    # ---- phase 4 (A): ZTP provisioning
    async def _ztp_read(self, ssh: Any, dsudo: bool) -> tuple[dict[str, str], set[tuple[str, str]]]:
        """(leases {mac: ip}, fetched {(ip, path)}) read inside the ztp container."""
        inner = f"cat /ztp/leases 2>/dev/null; echo {ZTP_SPLIT}; tail -n 5000 /ztp/http.log 2>/dev/null"
        try:
            res = await ssh.run(f"docker exec {ztp_mod.ZTP_NODE} sh -c {shlex.quote(inner)}", sudo=dsudo, timeout=30)
        except Exception:  # noqa: BLE001 - not readable yet = nothing yet
            return {}, set()
        leases_txt, _, http_txt = (res.stdout or "").partition(ZTP_SPLIT)
        return ztp_mod.parse_leases(leases_txt), ztp_mod.parse_http_log(http_txt)

    async def _ztp_macs(self, ssh: Any, topo: dict[str, Any], names: list[str], dsudo: bool, log: Any) -> dict[str, str]:
        fmt = '{{.Name}} {{(index .NetworkSettings.Networks "%s").MacAddress}}' % mgmt_network(topo)
        res = await ssh.run(f"docker inspect -f {shlex.quote(fmt)} " + " ".join(shlex.quote(n) for n in names),
                            sudo=dsudo, timeout=60, log=log)
        return ztp_mod.parse_macs(res.stdout)

    async def _ztp_publish_cv(self, ctx: Any, ssh: Any, session: Any, lab: Lab, rdir: str) -> None:
        """ztp_source cloudvision: the target's ZTP bootstrap script (preferred) or onboarding token → remote www/
        (0600, via stdin; never in the lab repo or the log). Stale copies are removed."""
        from ..cloudvision import service as cv
        from ..core.db import CVTarget

        www = f"{rdir}/clab/{ztp_mod.ZTP_DIR}/www"
        q = shlex.quote
        await ssh.run(f"rm -f {q(www + '/' + ztp_mod.CV_BOOTSTRAP_FILE)} {q(www + '/' + ztp_mod.CV_TOKEN_FILE)}")
        target = await session.get(CVTarget, lab.cv_target_id) if lab.cv_target_id else None
        if target is None:
            ctx.log("[ztp] ztp_source cloudvision but the lab has no CloudVision target: bootstrap applies the intended "
                    "config only")
            return
        script = await cv.target_ztp_script(session, target)
        if script:
            await ssh.run(f"umask 077 && cat > {q(www + '/' + ztp_mod.CV_BOOTSTRAP_FILE)}", input=script, timeout=60)
            ctx.log(f"[ztp] published the ZTP bootstrap script of CloudVision target {target.name}")
            return
        _, onboarding = await cv.target_tokens(session, target)
        if onboarding:
            await ssh.run(f"umask 077 && cat > {q(www + '/' + ztp_mod.CV_TOKEN_FILE)}", input=onboarding + "\n",
                          timeout=60)
            ctx.log(f"[ztp] published the onboarding token of CloudVision target {target.name}")
        else:
            ctx.log(f"[ztp] CloudVision target {target.name} has neither a ZTP script nor an onboarding token: "
                    "bootstrap applies the intended config only")

    async def _ztp_provision(self, ctx: Any, ssh: Any, session: Any, lab: Lab, topo: dict[str, Any],
                             nodes: list[NodeStatus], meta: dict[str, Any], params: DeployParams, rdir: str,
                             dsudo: bool) -> tuple[list[str], list[str]]:
        """MACs → dhcp-host/option-67 lines → dnsmasq restart → poll leases / http log / eAPI until `wait_timeout`."""
        q = shlex.quote
        plan = meta.get("nodes") or {}
        eos = {n["name"]: n for n in nodes if n["kind"] in EOS_KINDS}
        names = sorted(n for n in plan if n in eos)
        server_ip = str(meta.get("server_ip"))
        macs = await self._ztp_macs(ssh, topo, names, dsudo, ctx.log)
        for n in names:
            ctx.log(f"[ztp] {n}: mac {macs.get(n, '?')} → {plan[n]['ip']} bootfile {plan[n]['bootfile']}")
        missing = [n for n in names if n not in macs]
        if missing:
            ctx.log(f"[ztp] no management MAC for: {', '.join(missing)} (container not on {mgmt_network(topo)}?)")
        if meta.get("source") == "cloudvision":
            await self._ztp_publish_cv(ctx, ssh, session, lab, rdir)
        skeleton_file = service.lab_dir(lab) / "clab" / ztp_mod.ZTP_DIR / "dnsmasq.conf"
        skeleton = skeleton_file.read_text().split(ztp_mod.NODES_MARKER)[0] + ztp_mod.NODES_MARKER + "\n"
        rows = [{"name": n, "mac": macs.get(n), "ip": plan[n]["ip"], "bootfile": plan[n]["bootfile"]} for n in names]
        conf = skeleton + ztp_mod.dnsmasq_node_lines(server_ip, rows)
        await ssh.run(f"cat > {q(rdir + '/clab/' + ztp_mod.ZTP_DIR + '/dnsmasq.conf')}", input=conf, timeout=60)
        ctx.log(f"[ztp] dnsmasq.conf: {sum(1 for r in rows if r['mac'])} dhcp-host entries, bootfiles from "
                f"http://{server_ip}/")
        res = await ssh.run(f"docker exec {ztp_mod.ZTP_NODE} sh /ztp/run.sh restart", sudo=dsudo, timeout=60, log=ctx.log)
        if not res.ok:
            raise ExecutorError(f"could not restart dnsmasq in the {ztp_mod.ZTP_NODE} container (rc={res.returncode})")

        user, password = eapi_credentials(lab)
        eapi: dict[str, bool] = {}
        seen: dict[str, set[str]] = {n: set() for n in names}
        meta_view = {**meta, "nodes": {n: plan[n] for n in names}}

        last: list[Any] = [None]

        async def snapshot(progress: list[dict[str, Any]]) -> None:
            if progress == last[0]:
                return
            last[0] = progress
            await update_lab(lab.id, ztp_last={"at": time.time(), "mode": "ztp", "source": meta.get("source"),
                                               "server_ip": server_ip, "nodes": progress})

        deadline = time.monotonic() + params.wait_timeout
        ctx.log(f"[ztp] waiting for {len(names)} cEOS nodes: DHCP → bootfile → eAPI (timeout {params.wait_timeout}s)")
        while True:
            ctx.check_cancelled()
            leases, fetched = await self._ztp_read(ssh, dsudo)
            for n in names:
                if n in eapi:
                    continue
                node = eos[n]
                if node["mgmt_ip"] and await self._eapi_ok(ssh, node["mgmt_ip"], user, password):
                    eapi[n] = True
                    node["eapi"] = True
            progress = ztp_mod.node_progress(meta_view, macs, leases, fetched, eapi)
            for p in progress:
                for stage, label in (("lease", f"DHCP lease {p['ip']}"), ("bootfile_fetched", f"fetched {p['bootfile']}"),
                                     ("eapi", "eAPI ready")):
                    if p[stage] and stage not in seen[p["name"]]:
                        seen[p["name"]].add(stage)
                        ctx.log(f"[ztp] {p['name']}: {label}")
            await snapshot(progress)
            pending = [n for n in names if n not in eapi]
            if not pending or not params.wait_eapi:
                if not params.wait_eapi:
                    ctx.log("[ztp] not waiting for ZTP to finish (wait_eapi=false)")
                break
            if time.monotonic() >= deadline:
                stuck = {p["name"]: ("no DHCP lease" if not p["lease"] else "bootfile not fetched"
                                     if not p["bootfile_fetched"] else "eAPI down") for p in progress if not p["eapi"]}
                raise ExecutorError(f"ZTP not finished after {params.wait_timeout}s: "
                                    + ", ".join(f"{k} ({v})" for k, v in stuck.items()))
            ctx.log(f"[ztp] pending: {', '.join(pending)}")
            await asyncio.sleep(EAPI_POLL_INTERVAL)
        ready = [n for n in names if n in eapi]
        pending = [n for n in eos if n not in eapi]
        return ready, pending

    # ---- destroy
    async def destroy(self, ctx: ExecContext, lab: Lab, host: Host) -> None:
        rdir = remote_lab_dir(lab)
        async with SessionLocal() as s:
            async with SSHClient.connect(host, s, log=ctx.log, ctx=ctx) as ssh:
                exists = (await ssh.run(f"test -f {shlex.quote(rdir + '/' + TOPO_REL)}")).ok
                if not exists:
                    ctx.log(f"[destroy] {rdir}/{TOPO_REL} not on {host.name}: nothing deployed")
                else:
                    ctx.log(f"[destroy] sudo containerlab destroy -t {TOPO_REL} --cleanup")
                    res = await self._run_clab(ssh, lab, f"destroy -t {TOPO_REL} --cleanup", log=ctx.log, timeout=600)
                    out = (res.stdout + res.stderr).lower()
                    if not res.ok and not any(t in out for t in ("not found", "no containers", "no such")):
                        raise ExecutorError(f"containerlab destroy failed (rc={res.returncode})")
        await update_lab(lab.id, state=LabState.destroyed, destroyed_at=time.time(),
                         last_status={"at": time.time(), "deployed": False, "nodes": []})
        ctx.log(f"[destroy] lab {lab.name} destroyed")

    # ---- status
    async def status(self, ctx: ExecContext, lab: Lab, host: Host, *, session: Any = None) -> LabStatus:
        """Single pass inspect + eAPI probe. Does not write the DB (callers persist `last_status`)."""
        topo = load_topology(service.lab_dir(lab))

        async def _go(s: Any) -> LabStatus:
            async with SSHClient.connect(host, s, log=ctx.log, ctx=ctx) as ssh:
                if not (await ssh.run(f"test -f {shlex.quote(remote_lab_dir(lab) + '/' + TOPO_REL)}")).ok:
                    return LabStatus(deployed=False, nodes=[])
                nodes = await self._inspect(ssh, lab, topo, ctx.log)
                await self._probe_eapi(ssh, lab, nodes)
            for n in nodes:
                ctx.log(f"{n['name']:<24} {n['kind']:<12} {n['state']:<10} {n['mgmt_ip'] or '-':<16} eapi={n['eapi']}")
            return LabStatus(deployed=any(n["state"] == "running" for n in nodes), nodes=nodes)

        if session is not None:
            return await _go(session)
        async with SessionLocal() as s:
            return await _go(s)

    # ---- validate
    async def validate(self, ctx: ExecContext, lab: Lab, host: Host, *, tags: list[str], skip_tests: list[str],
                       extra_fabric: bool, user_catalogs: bool = False) -> ValidationSummary:
        if lab.state not in DEPLOYED_STATES:
            raise ExecutorError(f"lab is {lab.state.value}: deploy it before validating")
        topo = load_topology(service.lab_dir(lab))
        params = ValidateParams(extra_fabric_validation=extra_fabric, skip_tests=skip_tests, user_catalogs=user_catalogs,
                                tags=tags)
        return await run_validation(ctx, lab, host, params, network=mgmt_network(topo))


# ------------------------------------------------------------------------------------------------ phase 4 (A): ZTP
ZTP_SPLIT = "@@AVDLAB-HTTP-LOG@@"


def is_ztp_topology(topo: dict[str, Any]) -> bool:
    node = ((topo.get("topology") or {}).get("nodes") or {}).get(ztp_mod.ZTP_NODE) or {}
    return (node.get("labels") or {}).get("avdlab.role") == "ztp"


async def ztp_align_topology(lab: Lab, root: Path, log: Any) -> None:
    """The provisioning knob does not re-render group_vars, so a knob change leaves the topology as built: regenerate
    clab/ when the knob's mode and the built topology disagree (no-op otherwise, and when nothing is built)."""
    topo_file = root / TOPO_REL
    if not topo_file.exists() or not any((root / "documentation" / "fabric").glob("*-topology.csv")):
        return
    want_ztp = ztp_mod.provisioning_of((lab.settings or {}).get("knobs"))["mode"] == "ztp"
    have_ztp = is_ztp_topology(yaml.safe_load(topo_file.read_text()) or {})
    if want_ztp == have_ztp:
        return
    from ..topology.clab import generate_for_lab

    log(f"[deploy] provisioning knob is {'ztp' if want_ztp else 'startup-config'}: regenerating the topology")
    await asyncio.to_thread(generate_for_lab, lab, root, log=log)
    await asyncio.to_thread(service.commit_all, root, "topology: provisioning mode")


# ------------------------------------------------------------------------------------------------ jobs
async def _lab_and_host(ctx: JobContext) -> tuple[Lab, Host]:
    lab = await ctx.get_lab()
    if not lab.host_id:
        raise ExecutorError("lab has no host: set one in the lab settings")
    async with SessionLocal() as s:
        host = await s.get(Host, lab.host_id)
    if host is None:
        raise ExecutorError("lab host not found")
    return lab, host


@job("lab.deploy")
async def lab_deploy(ctx: JobContext) -> dict[str, Any]:
    lab, host = await _lab_and_host(ctx)
    return dict(await get_executor(lab).deploy(ctx, lab, host))


@job("lab.destroy")
async def lab_destroy(ctx: JobContext) -> dict[str, Any]:
    lab, host = await _lab_and_host(ctx)
    await get_executor(lab).destroy(ctx, lab, host)
    return {"destroyed": True}


@job("lab.status")
async def lab_status(ctx: JobContext) -> dict[str, Any]:
    lab, host = await _lab_and_host(ctx)
    st = await get_executor(lab).status(ctx, lab, host)
    await update_lab(lab.id, last_status={"at": time.time(), **st})
    return dict(st)


@job("lab.validate")
async def lab_validate(ctx: JobContext) -> dict[str, Any]:
    lab, host = await _lab_and_host(ctx)
    p = ValidateParams(**(ctx.params or {}))
    return dict(await get_executor(lab).validate(ctx, lab, host, tags=p.tags, skip_tests=p.skip_tests,
                                                 extra_fabric=p.extra_fabric_validation, user_catalogs=p.user_catalogs))


# ------------------------------------------------------------------------------------------------ phase 3: lab.sync
SYNC_EXCLUDES = [".git", "anta/reports", "anta/runs", "**/__pycache__"]  # documentation included (VS Code browsing)


@job("lab.sync")
async def lab_sync(ctx: JobContext) -> dict[str, Any]:
    """Copy the lab dir to `<remote_root>/labs/<slug>` without deploying (keeps "Open in VS Code" current)."""
    lab, host = await _lab_and_host(ctx)
    root = service.lab_dir(lab)
    rdir = remote_lab_dir(lab)
    n_files = await asyncio.to_thread(count_files, root, SYNC_EXCLUDES)
    async with SessionLocal() as s:
        async with SSHClient.connect(host, s, log=ctx.log, ctx=ctx) as ssh:
            ctx.log(f"[sync] {n_files} files -> {host.name}:{rdir}")
            await ssh.rsync_dir(root, rdir, exclude=SYNC_EXCLUDES, log=ctx.log)
    await update_lab(lab.id, remote_dir=rdir, synced_at=time.time())
    return {"remote_dir": rdir, "files": n_files, "host_id": host.id}
