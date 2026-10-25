"""Phase 4 (backend D): ACT executor — digital-twin build → ACT topology (+ tools server, clients), deploy over a fake
SSH to the tools server (no containerlab), status, validate, destroy, API."""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest
import yaml

from avdlab.core.db import Host, HostKind, LabState, SessionLocal
from avdlab.executors import act as act_exec
from avdlab.executors import terminal
from avdlab.jobs.engine import read_log

from .test_executors import _CREATED, ClabFakeSSH, _cleanup, install, lab_row, run_job, set_state  # noqa: F401

ACT_PW = "Act-Pw-123!"
SWITCHES = [("dc1-spine1", "172.16.1.11"), ("dc1-leaf1a", "172.16.1.101"), ("dc1-leaf1b", "172.16.1.102")]
CLIENT = {"name": "dc1-leaf1-server1", "image": "x", "bond": None, "bonds": [], "vlans": [],
          "links": [{"eth": "eth1", "switch": "dc1-leaf1a", "switch_port": "Ethernet5"},
                    {"eth": "eth2", "switch": "dc1-leaf1b", "switch_port": "Ethernet5"}]}


# ------------------------------------------------------------------ helpers
async def mk_act_host(kind: HostKind = HostKind.act) -> str:
    async with SessionLocal() as s:
        h = Host(name=f"act-{uuid.uuid4().hex[:6]}", kind=kind, address="203.0.113.7", ssh_user="cvpadmin",
                 capabilities={"docker": {"installed": True, "user_can_run": True}})
        s.add(h)
        await s.commit()
        _CREATED["hosts"].append(h.id)
        return h.id


async def mk_act_lab(client, *, host_id: str | None, target: str = "act", knobs: dict[str, Any] | None = None) -> dict[str, Any]:
    r = await client.post("/api/labs", json={"name": f"act-{uuid.uuid4().hex[:6]}", "template": "single-dc-l3ls",
                                             "host_id": host_id, "target": target,
                                             "knobs": {"act": {"password": ACT_PW}, **(knobs or {})}})
    assert r.status_code == 201, r.text
    _CREATED["labs"].append(r.json()["id"])
    return r.json()


def fake_twin_build(lab_dir: Path) -> None:
    """What the twin build leaves behind: raw ACT topology, intended configs/structured configs, client plans."""
    fab = lab_dir / "documentation" / "fabric"
    fab.mkdir(parents=True, exist_ok=True)
    topo = {"nodes": [{n: {"node_type": "veos", "ip_addr": f"{ip}/24", "version": "4.33.1F", "ports": ["Ethernet1"]}}
                      for n, ip in SWITCHES],
            "veos": {"username": "cvpadmin", "password": ACT_PW},
            "links": [{"connection": ["dc1-leaf1a:Ethernet1", "dc1-spine1:Ethernet1"]}]}
    (fab / "FABRIC-topology.yml").write_text(yaml.safe_dump(topo, sort_keys=False))
    for sub in ("configs", "structured_configs"):
        (lab_dir / "intended" / sub).mkdir(parents=True, exist_ok=True)
    for n, _ip in SWITCHES:
        (lab_dir / "intended" / "configs" / f"{n}.cfg").write_text(f"hostname {n}\n")
        (lab_dir / "intended" / "structured_configs" / f"{n}.yml").write_text(f"hostname: {n}\n")
    (lab_dir / "clab").mkdir(exist_ok=True)
    (lab_dir / "clab" / "clients.json").write_text(json.dumps([CLIENT]))


async def prepared_lab(client, host_id: str | None, state: LabState = LabState.built) -> dict[str, Any]:
    lab = await mk_act_lab(client, host_id=host_id)
    fake_twin_build(Path(lab["dir"]))
    act_exec.prepare_act_topology(await lab_row(lab["id"]), Path(lab["dir"]), "FABRIC", log=lambda _l: None)
    await set_state(lab["id"], state)
    return lab


class ActFakeSSH(ClabFakeSSH):
    def __init__(self, *, eapi_fail_first: int = 2, down: tuple[str, ...] = ()) -> None:
        super().__init__("act", eapi_fail_first=eapi_fail_first)
        self.down = down
        self.responses = {"playbooks/deploy.yml": (0, "PLAY RECAP ok=8 failed=0\n"), **self.responses}

    def _curl(self, fake: Any, cmd: str) -> tuple[int, str]:
        if any(f"https://{ip}/" in cmd for n, ip in SWITCHES if n in self.down):
            return 7, ""
        return super()._curl(fake, cmd)


# ------------------------------------------------------------------ pure
def test_act_knob_and_recipe():
    from avdlab.hosts.recipes import act_tools_recipe
    from avdlab.labs.templates import get_template, normalize_act

    assert normalize_act({}) == {"os_version": "4.33.1F", "username": "cvpadmin", "password": "cvp123!",
                                 "internet_access": True, "tools_server_ip": ""}
    assert normalize_act({"tools_server_ip": "172.16.1.200/24", "internet_access": "false"})["tools_server_ip"] == "172.16.1.200"
    for bad in ({"foo": 1}, {"tools_server_ip": "nope"}, {"username": ""}):
        with pytest.raises(ValueError):
            normalize_act(bad)
    for name in ("single-dc-l3ls", "dual-dc-l3ls", "l2ls-fabric", "single-dc-multipod-l3ls", "custom"):
        knob = get_template(name).knob("act")
        assert knob is not None and knob.type == "object" and knob.default["username"] == "cvpadmin", name
    steps = [s.name for s in act_tools_recipe("debian", "ubuntu").steps]
    assert steps == ["base packages", "docker engine", "docker group membership", "remote root", "agent image"]
    assert terminal.act_node_command("cvpadmin", "172.16.1.11", "cli").endswith("cvpadmin@172.16.1.11")
    assert terminal.act_node_command("cvpadmin", "172.16.1.11", "bash").endswith("@172.16.1.11 bash")


def test_postprocess_topology_appends_tools_and_clients():
    raw = {"nodes": [{"s1": {"node_type": "veos", "ip_addr": "10.0.0.2/24", "version": "4.33.1F", "ports": ["Ethernet1"]}}],
           "veos": {"username": "u", "password": "p"}, "links": []}
    act = {"username": "u", "password": "p", "tools_server_ip": ""}
    plan = {"name": "c1", "links": [{"eth": "eth1", "switch": "s1", "switch_port": "Ethernet10"}]}
    out = act_exec.postprocess_topology(raw, [plan], act, gateway="10.0.0.1")
    assert list(out) == ["nodes", "generic", "tools-server", "veos", "links"]
    assert out["nodes"][1] == {"avdlab-tools": {"node_type": "tools-server", "ip_addr": "10.0.0.254/24", "version": "ubuntu-2204-lts"}}
    assert out["nodes"][2] == {"c1": {"node_type": "generic", "ip_addr": "10.0.0.253/24", "version": "ubuntu-2204-lts"}}
    assert out["links"] == [{"connection": ["c1:Ethernet1", "s1:Ethernet10"]}]
    assert out["nodes"][0]["s1"]["ports"] == ["Ethernet1", "Ethernet10"]
    assert out["generic"] == out["tools-server"] == {"username": "u", "password": "p"}
    again = act_exec.postprocess_topology(out, [plan], {**act, "tools_server_ip": "10.0.0.50"})  # idempotent
    assert [next(iter(n)) for n in again["nodes"]] == ["s1", "avdlab-tools", "c1"] and len(again["links"]) == 1
    assert again["nodes"][1]["avdlab-tools"]["ip_addr"] == "10.0.0.50/24"
    with pytest.raises(act_exec.ExecutorError, match="already used"):
        act_exec.postprocess_topology(raw, [], {**act, "tools_server_ip": "10.0.0.2"})
    with pytest.raises(act_exec.ExecutorError, match="outside"):
        act_exec.postprocess_topology(raw, [], {**act, "tools_server_ip": "192.168.9.9"})


# ------------------------------------------------------------------ twin build (real pyavd)
async def test_twin_build_writes_act_topology(client):
    lab = await mk_act_lab(client, host_id=None, knobs={"act": {"password": ACT_PW, "tools_server_ip": "172.16.1.250"}})
    r = await client.post(f"/api/labs/{lab['id']}/build")
    assert r.status_code == 202, r.text
    job = await run_job(client, r.json()["job_id"])
    act_summary = job["result"]["act"]
    assert act_summary["prepared"] and act_summary["topology_path"] == "documentation/fabric/FABRIC-topology.yml"
    root = Path(lab["dir"])
    text = (root / act_summary["topology_path"]).read_text()
    topo = yaml.safe_load(text)
    nodes = {n["name"]: n for n in act_exec.act_nodes(topo)}
    assert nodes["dc1-spine1"]["node_type"] == "veos" and nodes["dc1-spine1"]["ip_addr"] == "172.16.1.11/24"
    assert nodes["dc1-spine1"]["version"] == "4.33.1F"
    assert nodes["avdlab-tools"] == {"name": "avdlab-tools", "node_type": "tools-server", "ip_addr": "172.16.1.250/24",
                                     "ip": "172.16.1.250", "version": "ubuntu-2204-lts", "role": "tools-server"}
    clients = [n for n in nodes.values() if n["role"] == "client"]
    plans = json.loads((root / "clab" / "clients.json").read_text())
    assert plans and sorted(c["name"] for c in clients) == sorted(p["name"] for p in plans)
    p0 = plans[0]
    for i, lk in enumerate(p0["links"], start=1):
        assert {"connection": [f"{p0['name']}:Ethernet{i}", f"{lk['switch']}:{lk['switch_port']}"]} in topo["links"]
    assert topo["veos"]["password"] == ACT_PW and topo["tools-server"]["username"] == "cvpadmin" and "generic" in topo
    # twin mode: Management1, eAPI in the default VRF, ACT user kept across the config replace
    cfg = (root / "intended" / "configs" / "dc1-spine1.cfg").read_text()
    assert "interface Management1" in cfg and "username cvpadmin privilege 15 role network-admin secret sha512" in cfg
    sc = yaml.safe_load((root / "intended" / "structured_configs" / "dc1-spine1.yml").read_text())
    assert sc["metadata"]["digital_twin"]["environment"] == "act"
    assert (root / "act" / "clients" / f"{p0['name']}.sh").is_file() and (root / act_exec.APPLY_SCRIPT).is_file()

    # API: summary + hints, download, re-prepare after a knob change
    info = (await client.get(f"/api/labs/{lab['id']}/act")).json()
    assert info["target"] == "act" and info["tools_server_ip"] == "172.16.1.250" and info["hints"]
    assert any("act.arista.com" in h for h in info["hints"]) and info["clients"][0]["script"].startswith("#!/bin/sh")
    dl = await client.get(f"/api/labs/{lab['id']}/act/topology")
    assert dl.status_code == 200 and "attachment" in dl.headers["content-disposition"] and dl.text == text
    r = await client.patch(f"/api/labs/{lab['id']}", json={"knobs": {"act": {"password": ACT_PW, "tools_server_ip": "172.16.1.240"}}})
    assert r.status_code == 200, r.text
    again = (await client.post(f"/api/labs/{lab['id']}/act/prepare")).json()
    assert again["tools_server_ip"] == "172.16.1.240"


# ------------------------------------------------------------------ executor
async def test_act_deploy_no_containerlab(client, monkeypatch):
    host_id = await mk_act_host()
    lab = await prepared_lab(client, host_id)
    fake = ActFakeSSH(eapi_fail_first=2)
    install(monkeypatch, fake)
    monkeypatch.setattr(act_exec, "EAPI_POLL_INTERVAL", 0)
    r = await client.post(f"/api/labs/{lab['id']}/deploy", json={"wait_timeout": 60})
    assert r.status_code == 202, r.text
    job = await run_job(client, r.json()["job_id"])
    res = job["result"]
    assert sorted(res["ready"]) == sorted(n for n, _ in SWITCHES) and res["pending"] == []
    assert all(n["kind"] == "veos" and n["eapi"] is True for n in res["nodes"])
    assert not any("containerlab" in c for c, _ in fake.calls)
    remote = f"/opt/avdlab/labs/{lab['slug']}"
    assert fake.syncs and fake.syncs[0][1] == remote
    play = [c for c, _ in fake.calls if "ansible-playbook" in c]
    assert len(play) == 1 and play[0].startswith(f"docker run --rm -i --network host -v {remote}:/lab -w /lab ")
    assert "/dev/stdin" not in play[0] and "cat > /tmp/avdlab-extra-vars.yml" in play[0]
    assert "exec ansible-playbook -i inventory.yml playbooks/deploy.yml -e @/tmp/avdlab-extra-vars.yml" in play[0]
    assert fake.curl_calls == 2 + len(SWITCHES)
    # ACT credentials: stdin only
    log = read_log(job["id"])
    assert all(ACT_PW not in c for c, _ in fake.calls) and ACT_PW not in log
    assert any(f"ansible_password: {ACT_PW}" in i for i in fake.inputs) and any(ACT_PW in i and "user =" in i for i in fake.inputs)
    row = await lab_row(lab["id"])
    assert row.state == LabState.deployed and row.settings["deployed_at"]

    # status: inline, one switch down
    fake2 = ActFakeSSH(eapi_fail_first=0, down=("dc1-leaf1b",))
    install(monkeypatch, fake2)
    st = (await client.get(f"/api/labs/{lab['id']}/status")).json()
    assert st["deployed"] is True and st["error"] is None
    by = {n["name"]: n for n in st["nodes"]}
    assert by["dc1-spine1"]["eapi"] is True and by["dc1-leaf1b"] == {
        "name": "dc1-leaf1b", "kind": "veos", "state": "unreachable", "mgmt_ip": "172.16.1.102", "image": "veos 4.33.1F",
        "eapi": False}
    assert not any("containerlab" in c for c, _ in fake2.calls)

    # validate: same ANTA run, host network
    job = await run_job(client, (await client.post(f"/api/labs/{lab['id']}/validate", json={})).json()["job_id"])
    assert job["result"]["totals"]["success"] == 3
    assert any("--network host" in c and "avdlab-validate.yml" in c for c, _ in fake2.calls)

    # traffic / snapshot not supported, target change refused while deployed
    r = await client.post(f"/api/labs/{lab['id']}/traffic/ping", json={})
    assert r.status_code == 409 and "ACT" in r.text
    assert (await client.post(f"/api/labs/{lab['id']}/snapshot", json={})).status_code == 409
    assert (await client.patch(f"/api/labs/{lab['id']}", json={"target": "clab"})).status_code == 409

    # destroy: no SSH, state destroyed, hint in the log
    n_calls = len(fake2.calls)
    job = await run_job(client, (await client.post(f"/api/labs/{lab['id']}/destroy")).json()["job_id"])
    assert "ACT UI" in read_log(job["id"]) and len(fake2.calls) == n_calls
    assert (await lab_row(lab["id"])).state == LabState.destroyed


async def test_act_deploy_refused(client, monkeypatch):
    install(monkeypatch, ActFakeSSH())
    linux = await mk_act_host(HostKind.linux)
    lab = await prepared_lab(client, linux)
    r = await client.post(f"/api/labs/{lab['id']}/deploy")
    assert r.status_code == 409 and "kind act" in r.text
    act_host = await mk_act_host()
    unprepared = await mk_act_lab(client, host_id=act_host)
    await set_state(unprepared["id"], LabState.built)
    r = await client.post(f"/api/labs/{unprepared['id']}/deploy")
    assert r.status_code == 409 and "ACT topology" in r.text
    info = (await client.get(f"/api/labs/{unprepared['id']}/act")).json()
    assert info["prepared"] is False and info["nodes"] == [] and any("Build" in h for h in info["hints"])
    assert (await client.get(f"/api/labs/{unprepared['id']}/act/topology")).status_code == 404
    # the executor re-checks the host kind (job path)
    from avdlab.executors.act import ActExecutor, ExecutorError

    async with SessionLocal() as s:
        host = await s.get(Host, linux)
    with pytest.raises(ExecutorError, match="kind act"):
        await ActExecutor().deploy(act_exec_ctx(), await lab_row(lab["id"]), host)


def act_exec_ctx():
    from avdlab.executors.base import InlineContext

    return InlineContext()


async def test_target_patch_and_validation(client):
    lab = await mk_act_lab(client, host_id=None, target="clab")
    assert lab["target"] == "clab"
    await set_state(lab["id"], LabState.built)
    r = await client.patch(f"/api/labs/{lab['id']}", json={"target": "act"})
    assert r.status_code == 200 and r.json()["target"] == "act" and r.json()["state"] == "draft"
    assert (await client.patch(f"/api/labs/{lab['id']}", json={"target": "gns3"})).status_code == 422
    r = await client.post("/api/labs", json={"name": f"x-{uuid.uuid4().hex[:4]}", "template": "single-dc-l3ls", "target": "vmware"})
    assert r.status_code == 422
    r = await client.post(f"/api/labs/{lab['id']}/act/prepare")
    assert r.status_code == 409  # not built yet
