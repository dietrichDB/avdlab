"""Phase 6 (C): hot-added test hosts — add_endpoint op + partial build + scripted fake SSH for the client jobs."""
from __future__ import annotations

import json
import uuid
from pathlib import Path

import yaml

from avdlab.core.db import Lab, LabState, SessionLocal
from avdlab.executors import hot_clients as hc
from avdlab.labs import service
from avdlab.labs.build import build_devices_dir
from avdlab.topology.clients import ClientPlan, apply_client_overrides, load_plans, setup_script, with_ip, write_plans

from .test_executors import _CREATED, ClabFakeSSH, _cleanup, install, mk_host, ran, run_job, set_state  # noqa: F401
from .test_traffic import IPERF_TCP, IPUTILS_OK

SHOW_INTF = """Port       Name   Status       Vlan     Duplex Speed  Type            Flags Encapsulation
Et1        P2P    connected    routed   full   1G     EbraTestPhyPort
Et6               connected    11       full   1G     EbraTestPhyPort
Ma0               connected    routed   a-full a-1G   10/100/1000
"""


class HotFakeSSH(ClabFakeSSH):
    def __init__(self, lab_name: str, *, intf_visible: bool = True, existing: str | None = None) -> None:
        super().__init__(lab_name, eapi_fail_first=0)
        self.existing = existing  # label of an existing container with the client's name
        self.responses.update({
            "ansible-playbook": (0, "PLAY RECAP\ndc1-leaf1a : ok=1 changed=1 failed=0\n"),
        })
        self.down: list[str] = []  # switches reported as not running by the 0.10.0 pre-check
        self.responses = {
            "{{.State.Status}}": lambda f, c: (0, "".join(f"/{h} {'exited' if h in f.down else 'running'}\n"
                                                         for h in c.split("' ")[-1].replace("2>/dev/null", "").split())),
            "docker inspect -f": lambda f, c: (1, "") if f.existing is None else (0, f.existing + "\n"),
            "docker rm -f": self._rm,
            "docker run -d": (0, "4f1c0ffee\n"),
            "ip link show": (1, ""),
            "tools veth create": (0, "INFO veth created\n"),
            "show interfaces status": (0, SHOW_INTF if intf_visible else SHOW_INTF.replace("Et6 ", "Et9 ")),
            "sh -s": (0, "[avdlab] ready\n"),
            "ping -c": (0, IPUTILS_OK),
            "pgrep iperf3": (0, ""),
            "iperf3 -c": (0, json.dumps(IPERF_TCP)),
            "sh -c 'ip -br addr'": (0, "eth1 UP 10.1.11.50/24\n"),
            **self.responses,
        }

    def _rm(self, fake, cmd: str) -> tuple[int, str]:
        self.existing = None
        return 0, ""


def _plan(name: str = "t1") -> ClientPlan:
    return ClientPlan(name=name, image="alpine:3", links=[{"eth": "eth1", "switch": "sw1", "switch_port": "Ethernet6"}],
                      bond=None, bonds=[], vlans=[{"vid": 11, "iface": "eth1", "ip": "10.1.11.102/24", "gateway": "10.1.11.1",
                                                   "default_route": True, "tenant": "T", "vrf": "V"}])


# ------------------------------------------------------------------ pure helpers
def test_helpers():
    assert hc.intf_listed(SHOW_INTF, "Ethernet6") and not hc.intf_listed(SHOW_INTF, "Ethernet60")
    assert not hc.intf_listed(SHOW_INTF, "Ethernet5")
    assert hc.docker_run_cmd("t1", "alpine:3", "lab-a") == (
        "docker run -d --name t1 --hostname t1 --network none --label avdlab.lab=lab-a --label avdlab.role=client "
        "--cap-add NET_ADMIN --cap-add NET_RAW alpine:3 sleep infinity")
    assert hc.veth_cmd("dc1-leaf1a", "eth6", "t1") == "containerlab tools veth create -a dc1-leaf1a:eth6 -b t1:eth1"
    play = yaml.safe_load(hc.push_playbook("FABRIC"))[0]
    assert play["hosts"] == "FABRIC" and play["tasks"][0]["arista.eos.eos_config"]["replace"] == "config"
    cmd = hc.push_cmd("/r/labs/x", "clab-mgmt-x", ["dc1-leaf1a", "dc1-leaf1b"])
    assert "--network clab-mgmt-x" in cmd and "--limit dc1-leaf1a,dc1-leaf1b" in cmd
    # 0.7.2: never `-e @/dev/stdin` (Ansible cannot read the pipe path); stdin is copied to a 0600 file in the container
    assert "/dev/stdin" not in cmd and " sh -c " in cmd and "umask 077; cat > /tmp/avdlab-extra-vars.yml" in cmd
    assert "exec ansible-playbook -i inventory.yml" in cmd and cmd.rstrip("'").endswith("-e @/tmp/avdlab-extra-vars.yml")
    assert hc.gateway_of(_plan()) == ("10.1.11.102", "10.1.11.1")
    assert with_ip(_plan(), "auto") == _plan()
    assert with_ip(_plan(), "10.1.11.50/24")["vlans"][0]["ip"] == "10.1.11.50/24"


def test_apply_client_overrides(tmp_path: Path):
    clab = tmp_path / "clab"
    (clab / "hosts").mkdir(parents=True)
    write_plans(clab, [_plan("t1"), _plan("t2")])
    (clab / "hosts" / "t1.sh").write_text(setup_script(_plan("t1")))
    (clab / "topology.clab.yml").write_text(yaml.safe_dump({"topology": {"nodes": {"t1": {"kind": "linux", "image": "alpine:3"},
                                                                                   "t2": {"kind": "linux", "image": "alpine:3"}}}}))
    assert apply_client_overrides(tmp_path, {"t1": {"ip": "10.1.11.50/24", "image": "nicolaka/netshoot"}}) == ["t1"]
    plans = {p["name"]: p for p in load_plans(tmp_path)}
    assert plans["t1"]["vlans"][0]["ip"] == "10.1.11.50/24" and plans["t1"]["image"] == "nicolaka/netshoot"
    assert plans["t2"]["vlans"][0]["ip"] == "10.1.11.102/24"
    assert "ip addr replace 10.1.11.50/24 dev eth1" in (clab / "hosts" / "t1.sh").read_text()
    topo = yaml.safe_load((clab / "topology.clab.yml").read_text())
    assert topo["topology"]["nodes"]["t1"]["image"] == "nicolaka/netshoot"
    assert topo["topology"]["nodes"]["t2"]["image"] == "alpine:3"
    assert apply_client_overrides(tmp_path, {"t1": {"ip": "10.1.11.50/24", "image": "nicolaka/netshoot"}}) == []


# ------------------------------------------------------------------ API + jobs
async def _built_lab(client, *, host_id: str | None) -> dict:
    r = await client.post("/api/labs", json={"name": f"hot-{uuid.uuid4().hex[:6]}", "template": "single-dc-l3ls",
                                             "host_id": host_id, "knobs": {"device_password": "arista123"}})
    assert r.status_code == 201, r.text
    lab = r.json()
    _CREATED["labs"].append(lab["id"])
    job_id = (await client.post(f"/api/labs/{lab['id']}/build")).json()["job_id"]
    await run_job(client, job_id)
    return lab


def _configs(lab: dict) -> dict[str, str]:
    d = service.lab_dir(lab["slug"]) / "intended" / "configs"
    return {f.stem: f.read_text() for f in sorted(d.glob("*.cfg"))}


async def test_409_when_not_deployed(client):
    host_id = await mk_host()
    r = await client.post("/api/labs", json={"name": f"hot-{uuid.uuid4().hex[:6]}", "template": "single-dc-l3ls",
                                             "host_id": host_id})
    lab = r.json()
    _CREATED["labs"].append(lab["id"])
    body = {"name": "t1", "switch": "dc1-leaf1a", "switch_port": "Ethernet6", "vlans": "11", "mode": "access"}
    r = await client.post(f"/api/labs/{lab['id']}/clients", json=body)
    assert r.status_code == 409 and "deploy it first" in r.text
    for sub, payload in (("exec", {"cmd": "ip a"}), ("ping", {"dst": "10.0.0.1"}), ("iperf", {"dst": "10.0.0.1"})):
        r = await client.post(f"/api/labs/{lab['id']}/clients/dc1-leaf1-server1/{sub}", json=payload)
        assert r.status_code == 409, (sub, r.text)
    r = await client.post(f"/api/labs/{lab['id']}/clients", json={**body, "switch_port": "eth6"})
    assert r.status_code == 422


async def test_hot_add_exec_ping_iperf_remove(client, monkeypatch):
    host_id = await mk_host()
    lab = await _built_lab(client, host_id=host_id)
    await set_state(lab["id"], LabState.deployed)
    fake = HotFakeSSH(lab["slug"])
    install(monkeypatch, fake)
    monkeypatch.setattr(hc, "INTF_CHECK_INTERVAL", 0)
    monkeypatch.setattr(hc, "GATEWAY_INTERVAL", 0)
    root = service.lab_dir(lab["slug"])
    before = _configs(lab)

    # port already in use → 422 from the add_endpoint op, nothing written
    bad = {"name": "t1", "switch": "dc1-leaf1a", "switch_port": "Ethernet5", "vlans": "11", "mode": "access"}
    r = await client.post(f"/api/labs/{lab['id']}/clients", json=bad)
    assert r.status_code == 422 and "in use" in r.text
    r = await client.post(f"/api/labs/{lab['id']}/clients", json={**bad, "switch_port": "Ethernet6", "vlans": "11-12"})
    assert r.status_code == 422  # access takes one VLAN

    body = {"name": "t1", "switch": "dc1-leaf1a", "switch_port": "Ethernet6", "vlans": "11", "mode": "access",
            "ip": "auto", "deploy": True}
    r = await client.post(f"/api/labs/{lab['id']}/clients", json=body)
    assert r.status_code == 202, r.text
    add = r.json()
    # op applied: the endpoint is in the AVD inputs and the topology
    detail = (await client.get(f"/api/labs/{lab['id']}")).json()
    srv = next(s for s in detail["knobs"]["servers"] if s["name"] == "t1")
    assert srv["adapters"][0]["switches"] == ["dc1-leaf1a"] and srv["adapters"][0]["switch_ports"] == ["Ethernet6"]
    topo = yaml.safe_load((root / "clab" / "topology.clab.yml").read_text())
    assert "t1" in topo["topology"]["nodes"]
    assert any(set(lk["endpoints"]) == {"dc1-leaf1a:eth6", "t1:eth1"} for lk in topo["topology"]["links"])
    # partial build: only the affected switch's config changed, related devices rebuilt, others untouched
    after = _configs(lab)
    assert add["push_hosts"] == ["dc1-leaf1a"]
    assert "dc1-leaf1a" in add["rebuilt"] and "dc1-leaf1b" in add["rebuilt"] and "dc1-leaf2a" not in add["rebuilt"]
    assert "interface Ethernet6" in after["dc1-leaf1a"] and "interface Ethernet6" not in before["dc1-leaf1a"]
    assert {h for h in after if after[h] != before[h]} == {"dc1-leaf1a"}
    plan = next(p for p in load_plans(root) if p["name"] == "t1")
    assert plan["links"] == [{"eth": "eth1", "switch": "dc1-leaf1a", "switch_port": "Ethernet6"}]
    assert add["ip"] == plan["vlans"][0]["ip"].split("/")[0] and add["gateway"] == plan["vlans"][0]["gateway"]

    job = await run_job(client, add["job_id"])
    res = job["result"]
    assert res["client"] == "t1" and res["switch"] == "dc1-leaf1a" and res["port"] == "Ethernet6"
    assert res["ip"] == add["ip"] and res["gateway"] == add["gateway"] and res["reachable"] is True
    assert res["interface_visible"] is True and res["pushed"] == ["dc1-leaf1a"]
    push = ran(fake, "ansible-playbook")
    assert len(push) == 1 and "--limit dc1-leaf1a " in push[0][0] and f"clab-mgmt-{lab['slug']}" in push[0][0]
    assert any("ansible_password: arista123" in i for i in fake.inputs)  # secret on stdin only
    assert "arista123" not in push[0][0]
    order = [c for c, _ in fake.calls]
    steps = [next(i for i, c in enumerate(order) if needle in c)
             for needle in ("ansible-playbook", "docker run -d", "tools veth create", "sh -s", "ping -c")]
    assert steps == sorted(steps)
    assert ran(fake, "docker run -d")[0][0].startswith("docker run -d --name t1 --hostname t1 --network none "
                                                      f"--label avdlab.lab={lab['slug']} --label avdlab.role=client")
    veth = ran(fake, "tools veth create")[0]
    assert veth == ("containerlab tools veth create -a dc1-leaf1a:eth6 -b t1:eth1", True)
    assert f"ip addr replace {plan['vlans'][0]['ip']} dev eth1" in next(i for i in fake.inputs if "#!/bin/sh" in i)
    assert f"ping -c 3 -W 2 -i 0.2 {add['gateway']}" in ran(fake, "ping -c")[0][0]
    assert (root / hc.PUSH_PLAYBOOK).is_file()

    clients = {c["name"]: c for c in (await client.get(f"/api/labs/{lab['id']}/clients")).json()}
    assert clients["t1"]["hot_added"] is True and clients["t1"]["deployed"] is True
    assert clients["t1"]["container_state"] is None  # no containerlab inspect snapshot in this test
    assert clients["dc1-leaf1-server1"]["hot_added"] is False and clients["dc1-leaf1-server1"]["deployed"] is False

    # quick actions
    r = await client.post(f"/api/labs/{lab['id']}/clients/t1/exec", json={"cmd": "ip -br addr"})
    assert r.status_code == 200, r.text
    assert r.json() == {"client": "t1", "cmd": "ip -br addr", "rc": 0, "stdout": "eth1 UP 10.1.11.50/24\n", "stderr": ""}
    assert ran(fake, "sh -c 'ip -br addr'")[-1][0] == "docker exec t1 sh -c 'ip -br addr'"
    r = await client.post(f"/api/labs/{lab['id']}/clients/t1/ping", json={"dst": "dc1-leaf2-server1", "count": 2})
    assert r.status_code == 200, r.text
    ping = r.json()
    assert ping["ok"] and ping["received"] == 3 and ping["rtt_avg_ms"] == 1.204 and ping["target_ip"]
    assert ran(fake, "ping -c 2")[-1][0] == f"docker exec t1 ping -c 2 -W 2 -i 0.2 {ping['target_ip']}"
    r = await client.post(f"/api/labs/{lab['id']}/clients/t1/ping", json={"dst": "not-an-ip"})
    assert r.status_code == 422
    r = await client.post(f"/api/labs/{lab['id']}/clients/nope/ping", json={"dst": "10.0.0.1"})
    assert r.status_code == 404
    r = await client.post(f"/api/labs/{lab['id']}/clients/t1/iperf", json={"dst": "dc1-leaf2-server1", "seconds": 3})
    assert r.status_code == 200, r.text
    ip = r.json()
    assert ip["ok"] and ip["bits_per_second_sent"] == 9.41e8 and ip["proto"] == "tcp"
    assert ran(fake, "pgrep iperf3")[-1][0].startswith("docker exec dc1-leaf2-server1 sh -c")
    assert ran(fake, "iperf3 -c")[-1][0] == f"docker exec t1 iperf3 -c {ip['target_ip']} -J -t 3"

    # remove
    fake.existing = lab["slug"]
    r = await client.delete(f"/api/labs/{lab['id']}/clients/t1")
    assert r.status_code == 202, r.text
    rm = r.json()
    assert rm["push_hosts"] == ["dc1-leaf1a"] and rm["removed_from_inputs"] is True
    job = await run_job(client, rm["job_id"])
    assert job["result"] == {"client": "t1", "removed": True, "pushed": ["dc1-leaf1a"], "warnings": []}
    assert ran(fake, "docker rm -f t1")
    assert "--limit dc1-leaf1a " in ran(fake, "ansible-playbook")[-1][0]
    assert _configs(lab) == before  # back to the original intended configs
    assert "t1" not in {p["name"] for p in load_plans(root)}
    detail = (await client.get(f"/api/labs/{lab['id']}")).json()
    assert "t1" not in {s["name"] for s in detail["knobs"]["servers"]}
    assert "t1" not in (detail["settings"].get("hot_clients") or {})
    r = await client.delete(f"/api/labs/{lab['id']}/clients/t1")
    assert r.status_code == 404


async def test_hot_add_explicit_ip_survives_rebuild_and_warns(client, monkeypatch):
    host_id = await mk_host()
    lab = await _built_lab(client, host_id=host_id)
    await set_state(lab["id"], LabState.deployed)
    fake = HotFakeSSH(lab["slug"], intf_visible=False, existing="other-lab")
    install(monkeypatch, fake)
    monkeypatch.setattr(hc, "INTF_CHECK_INTERVAL", 0)
    monkeypatch.setattr(hc, "GATEWAY_INTERVAL", 0)
    root = service.lab_dir(lab["slug"])
    body = {"name": "t2", "switch": "dc1-leaf2a", "switch_port": "Ethernet7", "vlans": "11-12", "mode": "trunk",
            "ip": "10.1.11.50/24", "image": "nicolaka/netshoot:latest"}
    r = await client.post(f"/api/labs/{lab['id']}/clients", json=body)
    assert r.status_code == 202, r.text
    assert r.json()["ip"] == "10.1.11.50"
    # a foreign container with the same name → the job refuses
    await run_job(client, r.json()["job_id"], status="failed")
    assert not ran(fake, "docker run -d")
    fake.existing = None
    r = await client.post(f"/api/labs/{lab['id']}/clients", json={**body, "name": "t2"})
    assert r.status_code == 422  # still in the inputs
    # busy check + direct re-run of the job
    from avdlab.jobs.engine import runner

    job_id = await runner.submit("lab.client_deploy", lab_id=lab["id"], host_id=host_id,
                                 params={"name": "t2", "switch": "dc1-leaf2a", "switch_port": "Ethernet7", "push_hosts": []})
    job = await run_job(client, job_id)
    assert job["result"]["interface_visible"] is False and job["result"]["warnings"]
    assert len(ran(fake, "show interfaces status")) == hc.INTF_CHECK_TRIES
    assert "nicolaka/netshoot:latest sleep infinity" in ran(fake, "docker run -d")[0][0]
    script = next(i for i in fake.inputs if "#!/bin/sh" in i)
    assert "ip addr replace 10.1.11.50/24 dev eth1.11" in script
    # full rebuild keeps the explicit address and image
    job_id = (await client.post(f"/api/labs/{lab['id']}/build")).json()["job_id"]
    await run_job(client, job_id)
    plan = next(p for p in load_plans(root) if p["name"] == "t2")
    assert any(v["ip"] == "10.1.11.50/24" for v in plan["vlans"]) and plan["image"] == "nicolaka/netshoot:latest"
    topo = yaml.safe_load((root / "clab" / "topology.clab.yml").read_text())
    assert topo["topology"]["nodes"]["t2"]["image"] == "nicolaka/netshoot:latest"
    async with SessionLocal() as s:
        row = await s.get(Lab, lab["id"])
        assert row.settings["hot_clients"]["t2"]["deployed"] is True


def test_build_devices_dir_only_touches_targets(tmp_path: Path):
    """build_devices_dir without expansion writes only the requested host's intended files."""
    from avdlab.labs.build import build_lab_dir
    from avdlab.labs.templates import get_template, render_lab

    for rel, content in render_lab(get_template("single-dc-l3ls"), {}, slug="bd").files().items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    build_lab_dir(tmp_path, log=lambda m: None)
    cfg = tmp_path / "intended" / "configs"
    stamps = {f.name: f.stat().st_mtime_ns for f in cfg.glob("*.cfg")}
    (cfg / "dc1-spine1.cfg").write_text("stale\n")
    (cfg / "dc1-spine2.cfg").write_text("stale too\n")
    res = build_devices_dir(tmp_path, ["dc1-spine1"], log=lambda m: None, expand=False)
    assert res["hosts"] == ["dc1-spine1"] and res["changed_hosts"] == ["dc1-spine1"]
    assert (cfg / "dc1-spine1.cfg").read_text().startswith("!")
    assert (cfg / "dc1-spine2.cfg").read_text() == "stale too\n"
    assert all((cfg / n).stat().st_mtime_ns == t for n, t in stamps.items() if n not in ("dc1-spine1.cfg", "dc1-spine2.cfg"))
    assert "intended/configs/dc1-spine1.cfg" in res["changed_files"]
    assert not any("dc1-spine2" in f for f in res["changed_files"])


async def test_push_precheck_and_tolerant_remove(client, monkeypatch):
    """0.10.0: a switch that is not running gives an actionable error on add; remove still completes with a warning;
    ansible failures are reported from the fatal line, not from deprecation warnings."""
    from avdlab.executors.hot_clients import ansible_failure

    host_id = await mk_host()
    lab = await _built_lab(client, host_id=host_id)
    await set_state(lab["id"], LabState.deployed)
    fake = HotFakeSSH(lab["slug"])
    install(monkeypatch, fake)
    monkeypatch.setattr(hc, "INTF_CHECK_INTERVAL", 0)
    monkeypatch.setattr(hc, "GATEWAY_INTERVAL", 0)
    fake.down = ["dc1-leaf1a"]
    body = {"name": "t9", "switch": "dc1-leaf1a", "switch_port": "Ethernet6", "vlans": "11", "mode": "access"}
    r = await client.post(f"/api/labs/{lab['id']}/clients", json=body)
    assert r.status_code == 202, r.text
    job = await run_job(client, r.json()["job_id"], status="failed")
    assert "dc1-leaf1a not running" in job["error"] and "Redeploy" in job["error"]
    assert not ran(fake, "ansible-playbook")

    # remove while the switch is down: client gone, inputs updated, warning instead of failure
    fake.existing = lab["slug"]
    r = await client.delete(f"/api/labs/{lab['id']}/clients/t9")
    assert r.status_code == 202, r.text
    job = await run_job(client, r.json()["job_id"])
    assert job["result"]["removed"] is True and job["result"]["warnings"] and "not running" in job["result"]["warnings"][0]

    out = "PLAY\n[DEPRECATION WARNING]: Direct access to the `environment` attribute is deprecated.\n" \
          'fatal: [x]: FAILED! => {"changed": false, "msg": "Task failed: Could not connect to https://172.16.1.152:443/command-api: [Errno 113] No route to host"}\n'
    assert ansible_failure(out, "") == "Task failed: Could not connect to https://172.16.1.152:443/command-api: [Errno 113] No route to host"
    assert ansible_failure("[DEPRECATION WARNING]: x\nsomething else\n", "") == "something else"

