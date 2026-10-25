"""ACT (Arista Cloud Test) executor — stage 2 target: the same lab on ACT vEOS nodes instead of containerlab.

Model: the lab `Host` (kind `act`) is the ACT lab's **tools server** (generic Ubuntu node with public SSH), bootstrapped
with recipe `act-tools` (Docker + agent image). Every switch mgmt IP is reachable from it. The ACT lab itself is
created by the user in act.arista.com from the topology YAML avdlab generates:

  build (target act)  pyavd with digital_twin=True → documentation/fabric/<fabric>-topology.yml (raw ACT topology),
                      then `prepare_act_topology` appends the tools server node (`avdlab-tools`, knob act.tools_server_ip
                      or the highest free address of the mgmt subnet), one ubuntu `generic` node per client from
                      clab/clients.json (links mirror the client's links: `<client>:Ethernet<i>` ↔ `<switch>:<port>`,
                      ethI on ubuntu), node-type credentials, and writes the client setup scripts to act/clients/.
  deploy              host kind act; sync lab → tools server; NO containerlab; wait for eAPI on every switch mgmt IP
                      (curl from the tools server, ACT credentials); `ansible-playbook playbooks/deploy.yml`
                      (eos_config_deploy_eapi: config replace) in the agent container on the host network; deployed.
  status              eAPI probe of every switch from the tools server.
  validate            same ANTA run as containerlab, agent container on the host network.
  destroy             state destroyed only — the ACT lab is deleted in the ACT UI.

Credentials: ACT boots vEOS with the ACT node user (knob act.username/password, default cvpadmin/cvp123!); the twin
build adds that user to every intended config, so the same credentials keep working after the config replace. They
reach the tools server only on stdin (curl `-K -`, ansible extra-vars file written inside the agent container
from stdin — see common.ansible_stdin_cmd), never on a command line.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import shlex
import time
from pathlib import Path
from typing import Any

import yaml

from ..core.db import Host, HostKind, Lab, LabState, SessionLocal
from ..hosts.ssh import SSHClient
from ..labs import service
from .base import DeployParams, DeployResult, ExecContext, ExecutorError, LabStatus, NodeStatus, ValidateParams, ValidationSummary
from .common import (
    DEPLOYABLE_STATES,
    ansible_stdin_cmd,
    EAPI_PAYLOAD,
    curl_auth,
    docker_needs_sudo,
    eapi_curl,
    eapi_ok,
    remote_lab_dir,
    run_validation,
    sync_lab,
    update_lab,
    wait_eapi,
)

TOOLS_NODE = "avdlab-tools"
UBUNTU_VERSION = "ubuntu-2204-lts"
EOS_NODE_TYPES = ("veos", "cloudeos")
CLIENTS_DIR = "act/clients"
APPLY_SCRIPT = f"{CLIENTS_DIR}/apply-from-tools-server.sh"
DEPLOY_PLAYBOOK = "playbooks/deploy.yml"
GENERATED_DEPLOY_PLAYBOOK = "playbooks/avdlab-act-deploy.yml"
NODE_TYPE_ORDER = ("cloudeos", "cvp", "generic", "third-party", "tools-server", "veos")
EAPI_POLL_INTERVAL = 10.0  # tests set 0
TRAFFIC_UNSUPPORTED = ("traffic tests are not supported on ACT labs in this iteration: clients are ACT generic nodes "
                       f"configured with the scripts in {CLIENTS_DIR}/ (run them from the tools server)")


# ------------------------------------------------------------------------------------------------ knob / paths
def act_settings(lab: Lab) -> dict[str, Any]:
    """The lab's `act` knob, normalised (defaults when the template predates the knob)."""
    from ..labs.templates import normalize_act

    raw = ((lab.settings or {}).get("knobs") or {}).get("act") or {}
    return normalize_act(dict(raw) if isinstance(raw, dict) else {})


def fabric_name_of(lab: Lab, lab_dir: Path) -> str:
    from ..topology.clab import lab_fabric_name

    return lab_fabric_name(lab, lab_dir)


def topology_rel(fabric_name: str) -> str:
    return f"documentation/fabric/{fabric_name}-topology.yml"


def load_act_topology(lab_dir: Path, fabric_name: str) -> dict[str, Any] | None:
    p = Path(lab_dir) / topology_rel(fabric_name)
    if not p.is_file():
        return None
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return None
    return data if isinstance(data, dict) else None


def act_nodes(topo: dict[str, Any] | None) -> list[dict[str, Any]]:
    """[{name, node_type, ip_addr, ip, version, role}] from the ACT topology `nodes` list of single-key mappings."""
    out: list[dict[str, Any]] = []
    for item in (topo or {}).get("nodes") or []:
        if not isinstance(item, dict):
            continue
        for name, d in item.items():
            d = d or {}
            ip_addr = d.get("ip_addr")
            nt = str(d.get("node_type") or "")
            role = "tools-server" if nt == "tools-server" else "client" if nt == "generic" else "switch"
            out.append({"name": str(name), "node_type": nt, "ip_addr": ip_addr,
                        "ip": str(ip_addr).split("/")[0] if ip_addr else None, "version": d.get("version"), "role": role})
    return out


def is_prepared(topo: dict[str, Any] | None) -> bool:
    return any(n["name"] == TOOLS_NODE for n in act_nodes(topo))


# ------------------------------------------------------------------------------------------------ post-processing
def _intf_key(name: str) -> list[int]:
    return [int(x) for x in re.findall(r"\d+", name)]


def _eth_index(eth: str, default: int) -> int:
    m = re.search(r"(\d+)$", eth or "")
    return int(m.group(1)) if m else default


def mgmt_network(topo: dict[str, Any], fallback: str | None = None) -> ipaddress.IPv4Network:
    for n in act_nodes(topo):
        if n["ip_addr"] and "/" in str(n["ip_addr"]) and n["role"] == "switch":
            return ipaddress.ip_interface(str(n["ip_addr"])).network
    if fallback:
        return ipaddress.ip_network(fallback, strict=False)
    raise ExecutorError("cannot derive the management subnet: no switch in the ACT topology has an ip_addr")


def allocate_ips(net: ipaddress.IPv4Network, used: set[str], count: int) -> list[str]:
    """`count` free host addresses of `net`, from the top down (the switches sit at the bottom of the subnet)."""
    out: list[str] = []
    for ip in reversed(list(net.hosts())):
        if len(out) == count:
            break
        if str(ip) not in used:
            out.append(str(ip))
    if len(out) < count:
        raise ExecutorError(f"management subnet {net} has no room for {count} more ACT nodes")
    return out


def postprocess_topology(topo: dict[str, Any], clients: list[dict[str, Any]], act: dict[str, Any], *,
                         mgmt_subnet: str | None = None, gateway: str | None = None) -> dict[str, Any]:
    """Append the tools server + one generic (ubuntu) node per client, their links and node-type credentials.
    Idempotent: nodes/links previously appended (tools server, client names) are replaced."""
    client_names = {c["name"] for c in clients}
    drop = client_names | {TOOLS_NODE}
    nodes = [item for item in topo.get("nodes") or []
             if isinstance(item, dict) and not (set(item) & drop)]
    links = [lk for lk in topo.get("links") or []
             if not any(str(end).split(":", 1)[0] in drop for end in (lk or {}).get("connection") or [])]
    base = {**topo, "nodes": nodes, "links": links}
    net = mgmt_network(base, mgmt_subnet)
    used = {n["ip"] for n in act_nodes(base) if n["ip"]} | {str(net.network_address), str(net.broadcast_address)}
    if gateway:
        used.add(str(gateway))
    tools_ip = act.get("tools_server_ip") or ""
    if tools_ip:
        if ipaddress.ip_address(tools_ip) not in net:
            raise ExecutorError(f"act.tools_server_ip {tools_ip} is outside the management subnet {net}")
        if tools_ip in used:
            raise ExecutorError(f"act.tools_server_ip {tools_ip} is already used by a switch (or is the gateway)")
    else:
        tools_ip = allocate_ips(net, used, 1)[0]
    used.add(tools_ip)
    client_ips = allocate_ips(net, used, len(clients)) if clients else []
    plen = net.prefixlen

    nodes.append({TOOLS_NODE: {"node_type": "tools-server", "ip_addr": f"{tools_ip}/{plen}", "version": UBUNTU_VERSION}})
    switch_ports: dict[str, set[str]] = {}
    for plan, ip in zip(clients, client_ips):
        nodes.append({plan["name"]: {"node_type": "generic", "ip_addr": f"{ip}/{plen}", "version": UBUNTU_VERSION}})
        for i, lk in enumerate(plan.get("links") or [], start=1):
            idx = _eth_index(str(lk.get("eth") or ""), i)
            links.append({"connection": [f"{plan['name']}:Ethernet{idx}", f"{lk['switch']}:{lk['switch_port']}"]})
            switch_ports.setdefault(str(lk["switch"]), set()).add(str(lk["switch_port"]))
    # veos nodes list the ports ACT must create: make sure the client-facing ones are there
    for item in nodes:
        for name, d in item.items():
            if isinstance(d, dict) and d.get("node_type") == "veos" and name in switch_ports:
                d["ports"] = sorted(set(d.get("ports") or []) | switch_ports[name], key=_intf_key)
    creds = {"username": act["username"], "password": act["password"]}
    out: dict[str, Any] = {"nodes": nodes}
    for nt in NODE_TYPE_ORDER:
        if topo.get(nt) or nt in ("tools-server",) or (nt == "generic" and clients):
            out[nt] = topo.get(nt) or dict(creds)
    for k, v in topo.items():  # anything else pyavd may add later, before links
        if k not in out and k != "links":
            out[k] = v
    out["links"] = links
    return out


def _apply_script(clients: list[dict[str, Any]], ips: dict[str, str], user: str, rdir: str) -> str:
    L = ["#!/bin/sh",
         "# Generated by avdlab: run ON THE ACT TOOLS SERVER to configure the ACT generic (ubuntu) client nodes.",
         "# Each client gets its setup script (act/clients/<name>.sh) over ssh; you are prompted for the ACT password",
         f"# ({user}) unless keys are set up. avdlab does not run this script.",
         "set -u",
         f"cd {shlex.quote(rdir + '/' + CLIENTS_DIR)} || exit 1",
         "SSH='ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR'"]
    for c in clients:
        ip = ips.get(c["name"])
        if ip:
            L.append(f"echo '[avdlab] {c['name']} ({ip})'; $SSH {shlex.quote(user)}@{ip} 'sudo sh -s' < {shlex.quote(c['name'] + '.sh')}")
    return "\n".join(L) + "\n"


def prepare_act_topology(lab: Lab, lab_dir: Path, fabric_name: str | None = None, *, log: Any = print) -> dict[str, Any]:
    """Post-process the ACT topology written by the twin build and write the client scripts. Returns the
    `GET /labs/{id}/act` payload (without hints)."""
    from ..topology.clients import load_plans, setup_script

    lab_dir = Path(lab_dir)
    fabric_name = fabric_name or fabric_name_of(lab, lab_dir)
    topo = load_act_topology(lab_dir, fabric_name)
    if topo is None:
        raise ExecutorError(f"{topology_rel(fabric_name)} not found: build the lab with target act first")
    act = act_settings(lab)
    knobs = (lab.settings or {}).get("knobs") or {}
    clients = [c for c in load_plans(lab_dir) or [] if c.get("name")]
    out = postprocess_topology(topo, clients, act, mgmt_subnet=knobs.get("mgmt_subnet"),
                               gateway=knobs.get("mgmt_gateway"))
    text = ("---\n# ACT topology generated by avdlab (pyavd digital twin + tools server + clients). Upload it in "
            "act.arista.com.\n" + yaml.safe_dump(out, sort_keys=False, indent=2, width=130, default_flow_style=False))
    (lab_dir / topology_rel(fabric_name)).write_text(text, encoding="utf-8")
    cdir = lab_dir / CLIENTS_DIR
    cdir.mkdir(parents=True, exist_ok=True)
    for old in cdir.glob("*.sh"):
        old.unlink()
    nodes = act_nodes(out)
    ips = {n["name"]: n["ip"] for n in nodes if n["role"] == "client"}
    for plan in clients:
        (cdir / f"{plan['name']}.sh").write_text(setup_script(plan), encoding="utf-8")  # type: ignore[arg-type]
    (lab_dir / APPLY_SCRIPT).write_text(_apply_script(clients, ips, act["username"], remote_lab_dir(lab)), encoding="utf-8")
    tools = next(n for n in nodes if n["name"] == TOOLS_NODE)
    log(f"ACT topology: {sum(n['role'] == 'switch' for n in nodes)} switches, tools server {TOOLS_NODE} "
        f"({tools['ip_addr']}), {len(clients)} client node(s), {len(out.get('links') or [])} links")
    return act_summary(lab, lab_dir, fabric_name, out)


def act_summary(lab: Lab, lab_dir: Path, fabric_name: str, topo: dict[str, Any] | None) -> dict[str, Any]:
    nodes = act_nodes(topo)
    tools = next((n for n in nodes if n["name"] == TOOLS_NODE), None)
    clients = []
    for n in nodes:
        if n["role"] != "client":
            continue
        rel = f"{CLIENTS_DIR}/{n['name']}.sh"
        p = Path(lab_dir) / rel
        links = [lk["connection"] for lk in (topo or {}).get("links") or []
                 if any(str(e).split(":", 1)[0] == n["name"] for e in lk.get("connection") or [])]
        clients.append({"name": n["name"], "ip_addr": n["ip_addr"], "links": links,
                        "script_path": rel if p.is_file() else None,
                        "script": p.read_text(encoding="utf-8") if p.is_file() else None})
    return {"prepared": is_prepared(topo), "topology_path": topology_rel(fabric_name),
            "tools_server_ip": tools["ip"] if tools else None, "nodes": nodes, "clients": clients,
            "apply_script_path": APPLY_SCRIPT if (Path(lab_dir) / APPLY_SCRIPT).is_file() else None}


def hints(lab: Lab, summary: dict[str, Any], host: Host | None) -> list[str]:
    act = act_settings(lab)
    tools = summary.get("tools_server_ip") or "<tools server mgmt ip>"
    out = []
    if (lab.target or "clab") != "act":
        out.append("Set the lab target to act (lab settings) and build: the build then runs pyavd in digital-twin mode.")
    if not summary.get("prepared"):
        out.append("Build the lab: the ACT topology is generated by the build.")
    out += [
        f"Download the topology ({summary.get('topology_path')}) and create a lab from it in act.arista.com "
        f"(vEOS {act['os_version']}, node user {act['username']}).",
        f"Once the ACT lab is running, add a host of kind act for the tools server ({TOOLS_NODE}, mgmt {tools}; use the "
        "public SSH address ACT shows for it), then Probe and Bootstrap it with recipe act-tools (Docker + agent image).",
        "Set that host as the lab host and Deploy: avdlab syncs the lab to the tools server, waits for eAPI on every switch "
        "and pushes the intended configs with eos_config_deploy_eapi (config replace). Then Validate (ANTA) as usual.",
    ]
    if summary.get("clients"):
        out.append(f"Clients are ubuntu generic nodes (ethN = EthernetN in the topology). Their setup scripts are in "
                   f"{CLIENTS_DIR}/ (synced to the tools server); run {APPLY_SCRIPT} on the tools server to apply them — "
                   "avdlab does not ssh to the clients. Traffic tests are not available on ACT yet.")
    if host is not None and host.kind != HostKind.act:
        out.append(f"The current lab host {host.name} is kind {host.kind.value}: deploy needs a host of kind act.")
    out.append("Destroy only marks the lab destroyed in avdlab: delete the ACT lab in the ACT UI.")
    return out


# ------------------------------------------------------------------------------------------------ executor
def check_act_host(host: Host) -> None:
    if host.kind != HostKind.act:
        raise ExecutorError(f"host {host.name} is kind {host.kind.value}: an ACT lab needs a host of kind act "
                            "(the ACT tools server)")
    caps = host.capabilities or {}
    if caps and not (caps.get("docker") or {}).get("installed"):
        raise ExecutorError(f"host {host.name} lacks docker: run Bootstrap (recipe act-tools) on the Hosts page, then Probe")


def deploy_playbook(fabric_group: str) -> str:
    play = [{"name": "Deploy configurations to ACT devices using eAPI (generated by avdlab)", "hosts": fabric_group,
             "gather_facts": False,
             "tasks": [{"name": "Deploy configurations", "ansible.builtin.import_role": {"name": "arista.avd.eos_config_deploy_eapi"}}]}]
    return "---\n" + yaml.safe_dump(play, sort_keys=False)


def _eos_targets(topo: dict[str, Any] | None) -> dict[str, str | None]:
    return {n["name"]: n["ip"] for n in act_nodes(topo) if n["node_type"] in EOS_NODE_TYPES}


def _node_status(n: dict[str, Any], eapi: bool | None) -> NodeStatus:
    state = ("reachable" if eapi else "unreachable") if eapi is not None else "unknown"
    return NodeStatus(name=n["name"], kind=n["node_type"], state=state, mgmt_ip=n["ip"],
                      image=f"{n['node_type']} {n.get('version') or ''}".strip(), eapi=eapi)


class ActExecutor:
    def _topology(self, lab: Lab) -> tuple[Path, dict[str, Any] | None]:
        root = service.lab_dir(lab)
        return root, load_act_topology(root, fabric_name_of(lab, root))

    # ---- deploy
    async def deploy(self, ctx: ExecContext, lab: Lab, host: Host) -> DeployResult:
        params = DeployParams(**(ctx.params or {}))
        t0 = time.monotonic()
        if lab.state not in DEPLOYABLE_STATES:
            raise ExecutorError(f"lab is {lab.state.value}: build it before deploying")
        check_act_host(host)
        root, topo = self._topology(lab)
        if not is_prepared(topo):
            raise ExecutorError("no ACT topology: set the lab target to act and build the lab first")
        if not any((root / "intended" / "configs").glob("*.cfg")):
            raise ExecutorError("intended/configs is empty: build the lab first")
        act = act_settings(lab)
        fabric_group = str((lab.settings or {}).get("fabric_name") or "FABRIC")
        playbook = DEPLOY_PLAYBOOK
        if not (root / DEPLOY_PLAYBOOK).is_file():
            playbook = GENERATED_DEPLOY_PLAYBOOK
            await asyncio.to_thread(service.write_files, lab, {playbook: deploy_playbook(fabric_group)},
                                    "act: generated deploy playbook")
        targets = _eos_targets(topo)
        by_name = {n["name"]: n for n in act_nodes(topo)}
        rdir = remote_lab_dir(lab)
        dsudo = docker_needs_sudo(host)
        q = shlex.quote
        ready: list[str] = []
        pending = list(targets)
        ctx.log(f"[deploy] ACT lab {lab.name}: tools server {host.name} ({host.address}), {len(targets)} switches — "
                "containerlab is not used")
        async with SessionLocal() as s:
            async with SSHClient.connect(host, s, log=ctx.log, ctx=ctx) as ssh:
                await sync_lab(ctx, ssh, lab, host)
                ctx.check_cancelled()
                if params.wait_eapi and targets:
                    ready, pending = await wait_eapi(ctx, ssh, targets, act["username"], act["password"],
                                                     timeout=params.wait_timeout, interval=EAPI_POLL_INTERVAL)
                elif not params.wait_eapi:
                    ctx.log("[eapi] not waiting for eAPI (wait_eapi=false)")
                cmd = ansible_stdin_cmd(f"--network host -v {q(rdir)}:/lab -w /lab -e ANSIBLE_FORCE_COLOR=0",
                                        f"-i inventory.yml {playbook}")
                ctx.log(f"[deploy] {cmd}   (ACT credentials as extra vars on stdin)")
                creds = yaml.safe_dump({"ansible_user": act["username"], "ansible_password": act["password"]})
                res = await ssh.run(cmd, sudo=dsudo, timeout=3600, log=ctx.log, input=creds)
                if not res.ok:
                    tail = " | ".join((res.stderr or res.stdout).strip().splitlines()[-2:])
                    raise ExecutorError(f"ansible-playbook {playbook} failed (rc={res.returncode}): {tail}")
        nodes = [_node_status(by_name[name], name in ready if params.wait_eapi else None) for name in targets]
        duration = round(time.monotonic() - t0, 1)
        now = time.time()
        await update_lab(lab.id, state=LabState.deployed, deployed_at=now, remote_dir=rdir,
                         last_status={"at": now, "deployed": True, "nodes": nodes})
        ctx.log(f"[deploy] ACT lab {lab.name} configured in {duration}s ({len(ready)} ready, {len(pending)} pending)")
        return DeployResult(nodes=nodes, ready=ready, pending=pending, duration=duration)

    # ---- destroy
    async def destroy(self, ctx: ExecContext, lab: Lab, host: Host) -> None:
        ctx.log("[destroy] avdlab does not manage the ACT lab itself: destroy the ACT lab in the ACT UI (act.arista.com)")
        await update_lab(lab.id, state=LabState.destroyed, destroyed_at=time.time(),
                         last_status={"at": time.time(), "deployed": False, "nodes": []})
        ctx.log(f"[destroy] lab {lab.name} marked destroyed")

    # ---- status
    async def status(self, ctx: ExecContext, lab: Lab, host: Host, *, session: Any = None) -> LabStatus:
        """Single eAPI probe of every switch from the tools server (ACT credentials). Does not write the DB."""
        _root, topo = self._topology(lab)
        targets = _eos_targets(topo)
        if not targets:
            return LabStatus(deployed=False, nodes=[])
        act = act_settings(lab)
        by_name = {n["name"]: n for n in act_nodes(topo)}

        async def _go(s: Any) -> LabStatus:
            async with SSHClient.connect(host, s, log=ctx.log, ctx=ctx) as ssh:
                oks = await asyncio.gather(*(eapi_ok(ssh, ip, act["username"], act["password"]) if ip else _false()
                                             for ip in targets.values()))
            nodes = [_node_status(by_name[name], ok) for name, ok in zip(targets, oks)]
            for n in nodes:
                ctx.log(f"{n['name']:<24} {n['kind']:<10} {n['state']:<12} {n['mgmt_ip'] or '-':<16} eapi={n['eapi']}")
            return LabStatus(deployed=any(oks), nodes=nodes)

        if session is not None:
            return await _go(session)
        async with SessionLocal() as s:
            return await _go(s)

    # ---- validate
    async def validate(self, ctx: ExecContext, lab: Lab, host: Host, *, tags: list[str], skip_tests: list[str],
                       extra_fabric: bool, user_catalogs: bool = False) -> ValidationSummary:
        check_act_host(host)
        params = ValidateParams(extra_fabric_validation=extra_fabric, skip_tests=skip_tests, user_catalogs=user_catalogs,
                                tags=tags)
        return await run_validation(ctx, lab, host, params, network="host")


async def _false() -> bool:
    return False


# ------------------------------------------------------------------------------------------------ node access
def node_mgmt_ip(lab: Lab, node: str) -> str | None:
    root = service.lab_dir(lab)
    topo = load_act_topology(root, fabric_name_of(lab, root))
    return next((n["ip"] for n in act_nodes(topo) if n["name"] == node), None)


async def fetch_running_config(lab: Lab, host: Host, node: str, *, session: Any = None) -> str:
    """`show running-config` over eAPI (text) from the tools server."""
    ip = node_mgmt_ip(lab, node)
    if not ip:
        raise ExecutorError(f"{node} has no mgmt IP in the ACT topology")
    act = act_settings(lab)
    payload = json.loads(EAPI_PAYLOAD)
    payload["params"].update({"cmds": ["enable", "show running-config"], "format": "text"})

    async def _go(s: Any) -> str:
        async with SSHClient.connect(host, s) as ssh:
            res = await ssh.run(eapi_curl(ip, json.dumps(payload), max_time=20), timeout=25,
                                input=curl_auth(act["username"], act["password"]))
        try:
            data = json.loads(res.stdout) if res.ok else {}
        except ValueError:
            data = {}
        if "result" not in data:
            raise ExecutorError(f"show running-config on {node} ({ip}) failed: "
                                f"{(data.get('error') or {}).get('message') or res.stderr.strip() or f'rc={res.returncode}'}")
        return str(data["result"][-1].get("output") or "")

    if session is not None:
        return await _go(session)
    async with SessionLocal() as s:
        return await _go(s)
