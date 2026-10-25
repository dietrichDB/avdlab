"""Module 3 (phase 4, backend A): provisioning knob, ZTP topology + clab/ztp files, deploy ZTP branch with a fake SSH,
GET /labs/{id}/ztp, CloudVision target `ztp_script` (write-only)."""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest
import yaml

from avdlab.core.db import CVTarget, Lab, LabState, Secret, SessionLocal
from avdlab.jobs.engine import read_log
from avdlab.labs.build import build_lab_dir
from avdlab.labs.templates import KnobError, coerce_knobs, get_template, render_lab
from avdlab.topology import ztp as ztp_mod
from avdlab.topology.clab import build_clab_topology

from .test_executors import (  # noqa: F401 - _cleanup is an autouse fixture
    _CREATED,
    NODES,
    ClabFakeSSH,
    _cleanup,
    fake_build,
    install,
    lab_row,
    mk_host,
    ran,
    run_job,
    set_state,
)

ZTP_SCRIPT = "#!/usr/bin/env python3\n# CVaaS ZTP bootstrap\nTOKEN = 'enroll-SECRET-ztp-9f8e'\n"
ONB_TOKEN = "onboard-TOKEN-ztp.also-secret"
MACS = {n: f"aa:c1:ab:00:00:{i + 1:02x}" for i, (n, _ip) in enumerate(NODES)}


# ------------------------------------------------------------------ knob
def test_provisioning_knob():
    tpl = get_template("single-dc-l3ls")
    assert coerce_knobs(tpl, {})["provisioning"] == {"mode": "startup-config", "ztp_source": "intended",
                                                     "ztp_server_ip": None}
    v = coerce_knobs(tpl, {"provisioning": {"mode": "ztp", "ztp_server_ip": "172.16.1.5"}})["provisioning"]
    assert v == {"mode": "ztp", "ztp_source": "intended", "ztp_server_ip": "172.16.1.5"}
    for bad in ({"mode": "pxe"}, {"ztp_source": "netbox"}, {"ztp_server_ip": "nope"}, {"foo": 1}):
        with pytest.raises(KnobError):
            coerce_knobs(tpl, {"provisioning": bad})
    for name in ("custom", "dual-dc-l3ls", "l2ls-fabric", "single-dc-multipod-l3ls"):
        assert get_template(name).knob("provisioning") is not None


def test_server_ip_and_parsers():
    assert ztp_mod.pick_server_ip("172.16.1.0/24", {"172.16.1.254"}, "172.16.1.1") == "172.16.1.253"
    assert ztp_mod.pick_server_ip("172.16.1.0/24", set(), "172.16.1.1", "172.16.1.9") == "172.16.1.9"
    with pytest.raises(ValueError):
        ztp_mod.pick_server_ip("172.16.1.0/24", {"172.16.1.11"}, "172.16.1.1", "172.16.1.11")
    with pytest.raises(ValueError):
        ztp_mod.pick_server_ip("172.16.1.0/24", set(), "172.16.1.1", "10.0.0.1")
    assert ztp_mod.parse_macs("/dc1-spine1 AA:C1:AB:00:00:01\n/x <no value>\n") == {"dc1-spine1": "aa:c1:ab:00:00:01"}
    assert ztp_mod.parse_leases("1700000000 aa:c1:ab:00:00:01 172.16.1.11 dc1-spine1 *\n") == {
        "aa:c1:ab:00:00:01": "172.16.1.11"}
    log = ('172.16.1.11 - - [22/Sep/2026 10:00:00] "GET /dc1-spine1.cfg HTTP/1.1" 200 -\n'
           '172.16.1.12 - - [22/Sep/2026 10:00:00] "GET /nope.cfg HTTP/1.1" 404 -\n')
    assert ztp_mod.parse_http_log(log) == {("172.16.1.11", "dc1-spine1.cfg")}
    cfg = ztp_mod.with_token_handler("hostname a\n!\nend\n", "/tmp/cv-onboarding-token")
    assert cfg.endswith("action bash mkdir -p /tmp && cp /mnt/flash/avdlab-cv-token /tmp/cv-onboarding-token\n!\nend\n")


# ------------------------------------------------------------------ topology
@pytest.fixture(scope="module")
def built_lab(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("ztp") / "lab"
    for rel, content in render_lab(get_template("single-dc-l3ls"), {}, slug="ztp").files().items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    build_lab_dir(root, log=lambda _l: None)
    return root


def test_startup_config_mode_unchanged(built_lab, tmp_path):
    default = build_clab_topology(built_lab, "FABRIC", "arista/ceos:4.34.2F")
    explicit = build_clab_topology(built_lab, "FABRIC", "arista/ceos:4.34.2F",
                                   provisioning={"mode": "startup-config"})
    a, b = default.topology.to_yaml_dict(), explicit.topology.to_yaml_dict()
    assert a == b and "ztp" not in a["topology"]["nodes"]
    assert a["topology"]["kinds"]["arista_ceos"]["enforce-startup-config"] is True
    spine = a["topology"]["nodes"]["dc1-spine1"]
    assert spine["startup-config"] == "../intended/configs/dc1-spine1.cfg" and "suppress-startup-config" not in spine
    (tmp_path / "clab" / "ztp").mkdir(parents=True)  # a previous ztp build is cleaned up
    default.write(tmp_path / "clab")
    assert not (tmp_path / "clab" / "ztp").exists()


@pytest.mark.parametrize("source", ["intended", "cloudvision"])
def test_ztp_topology_and_files(built_lab, tmp_path, source):
    lab_dir = tmp_path / "lab"
    for sub in ("intended", "documentation"):
        import shutil

        shutil.copytree(built_lab / sub, lab_dir / sub)
    build = build_clab_topology(lab_dir, "FABRIC", "arista/ceos:4.34.2F", provisioning={"mode": "ztp", "ztp_source": source},
                                mgmt_gateway="172.16.1.1", dns_servers=["1.1.1.1", "8.8.8.8"],
                                token_file="/tmp/cv-onboarding-token")
    topo = build.topology.to_yaml_dict()
    nodes = topo["topology"]["nodes"]
    assert "enforce-startup-config" not in topo["topology"]["kinds"]["arista_ceos"]
    for name in build.mappings:
        assert "startup-config" not in nodes[name] and nodes[name]["suppress-startup-config"] is True
    z = nodes["ztp"]
    assert z["kind"] == "linux" and z["mgmt-ipv4"] == "172.16.1.254" and z["binds"] == ["ztp/:/ztp"]
    assert z["exec"] == ["sh /ztp/run.sh"] and z["labels"] == {"avdlab.role": "ztp"}
    from avdlab.core.config import settings

    assert z["image"] == settings.agent_image
    build.write(lab_dir / "clab")
    zdir = lab_dir / "clab" / "ztp"
    conf = (zdir / "dnsmasq.conf").read_text()
    for line in ("interface=eth0", "dhcp-range=172.16.1.0,static,255.255.255.0", "dhcp-option=3,172.16.1.1",
                 "dhcp-option=6,1.1.1.1,8.8.8.8", "log-dhcp", "dhcp-leasefile=/ztp/leases", ztp_mod.NODES_MARKER):
        assert line in conf.splitlines(), line
    run = (zdir / "run.sh").read_text()
    assert "dnsmasq --conf-file=/ztp/dnsmasq.conf --no-daemon" in run and "http.server 80 --directory /ztp/www" in run
    meta = json.loads((zdir / "ztp.json").read_text())
    assert meta["server_ip"] == "172.16.1.254" and meta["source"] == source
    assert meta["nodes"]["dc1-spine1"]["ip"] == "172.16.1.11"
    intended = (lab_dir / "intended" / "configs" / "dc1-spine1.cfg").read_text()
    served = (zdir / "www" / "dc1-spine1.cfg").read_text()
    if source == "intended":
        assert meta["nodes"]["dc1-spine1"]["bootfile"] == "dc1-spine1.cfg" and served == intended
        assert not list((zdir / "www").glob("bootstrap-*"))
    else:
        assert meta["nodes"]["dc1-spine1"]["bootfile"] == "bootstrap-dc1-spine1.sh"
        boot = (zdir / "www" / "bootstrap-dc1-spine1.sh").read_text()
        assert boot.startswith("#!/bin/bash") and "http://172.16.1.254/cv-bootstrap.sh" in boot.replace("$ZTP", "http://172.16.1.254")
        assert "cp /mnt/flash/avdlab-cv-token /tmp/cv-onboarding-token" in boot
        assert "event-handler avdlab-cv-token" in served and served.rstrip().endswith("end")
    assert yaml.safe_load((lab_dir / "clab" / "topology.clab.yml").read_text())["topology"]["nodes"]["ztp"]


# ------------------------------------------------------------------ deploy (fake SSH)
class ZtpFakeSSH(ClabFakeSSH):
    """Progression per `docker exec ztp sh -c` read: 1 → nothing, 2 → leases, >=3 → leases + bootfiles fetched."""

    def __init__(self, lab_name: str, meta: dict[str, Any], **kw: Any) -> None:
        super().__init__(lab_name, **kw)
        self.reads = 0
        self.meta = meta
        new = {
            "docker inspect -f": (0, "".join(f"/{n} {m}\n" for n, m in MACS.items())),
            "run.sh restart": (0, "dnsmasq restarted\n"),
            "docker exec ztp sh -c": self._read,
        }
        self.responses = {**new, **self.responses}

    def _read(self, fake: Any, cmd: str) -> tuple[int, str]:
        self.reads += 1
        leases = "".join(f"1700000000 {MACS[n]} {ip} {n} *\n" for n, ip in NODES) if self.reads >= 2 else ""
        http = "".join(f'{ip} - - [22/Sep/2026 10:00:0{i}] "GET /{self.meta["nodes"][n]["bootfile"]} HTTP/1.1" 200 -\n'
                       for i, (n, ip) in enumerate(NODES)) if self.reads >= 3 else ""
        return 0, leases + "@@AVDLAB-HTTP-LOG@@\n" + http


def ztp_build(lab_dir: Path, slug: str, source: str = "intended") -> dict[str, Any]:
    """fake_build + a ztp node and clab/ztp/ files as the real build writes them."""
    fake_build(lab_dir, slug)
    topo_file = lab_dir / "clab" / "topology.clab.yml"
    topo = yaml.safe_load(topo_file.read_text())
    for n, _ip in NODES:
        topo["topology"]["nodes"][n].pop("startup-config")
        topo["topology"]["nodes"][n]["suppress-startup-config"] = True
    topo["topology"]["nodes"]["ztp"] = {"kind": "linux", "image": "agent", "mgmt-ipv4": "172.16.1.254",
                                        "binds": ["ztp/:/ztp"], "exec": ["sh /ztp/run.sh"], "labels": {"avdlab.role": "ztp"}}
    topo_file.write_text(yaml.safe_dump(topo))
    (lab_dir / "intended" / "configs").mkdir(parents=True, exist_ok=True)
    for n, _ip in NODES:
        (lab_dir / "intended" / "configs" / f"{n}.cfg").write_text(f"hostname {n}\n!\nend\n")
    fmt = "{}.cfg" if source == "intended" else "bootstrap-{}.sh"
    plan = ztp_mod.ZtpPlan(source=source, server_ip="172.16.1.254", subnet="172.16.1.0/24", gateway="172.16.1.1",
                           dns=["1.1.1.1"], nodes={n: {"ip": ip, "bootfile": fmt.format(n)} for n, ip in NODES},
                           token_file="/tmp/cv-onboarding-token")
    plan.write(lab_dir / "clab", lab_dir)
    return plan.meta()


async def mk_ztp_lab(client, host_id: str, source: str = "intended") -> tuple[dict[str, Any], dict[str, Any]]:
    r = await client.post("/api/labs", json={"name": f"ztp-{uuid.uuid4().hex[:6]}", "template": "single-dc-l3ls",
                                             "host_id": host_id,
                                             "knobs": {"provisioning": {"mode": "ztp", "ztp_source": source}}})
    assert r.status_code == 201, r.text
    lab = r.json()
    _CREATED["labs"].append(lab["id"])
    meta = ztp_build(Path(lab["dir"]), lab["slug"], source)
    await set_state(lab["id"], LabState.built)
    return lab, meta


async def test_deploy_ztp_flow_and_status(client, monkeypatch):
    host_id = await mk_host()
    lab, meta = await mk_ztp_lab(client, host_id)
    # before deploying: plan only
    st = (await client.get(f"/api/labs/{lab['id']}/ztp")).json()
    assert st["mode"] == "ztp" and st["server_ip"] == "172.16.1.254" and len(st["nodes"]) == 4
    assert not any(n["lease"] or n["eapi"] or n["mac"] for n in st["nodes"]) and st["fresh"] is False

    fake = ZtpFakeSSH(lab["slug"], meta, eapi_fail_first=8)  # eAPI answers from the 3rd poll on
    install(monkeypatch, fake)
    r = await client.post(f"/api/labs/{lab['id']}/deploy", json={"wait_timeout": 60})
    job = await run_job(client, r.json()["job_id"])
    assert sorted(job["result"]["ready"]) == sorted(n for n, _ in NODES) and job["result"]["pending"] == []

    # MACs read on the clab mgmt network, then dhcp-host / option 67 lines written on stdin, then dnsmasq restarted
    inspect = ran(fake, "docker inspect -f")[0][0]
    assert f'(index .NetworkSettings.Networks "clab-mgmt-{lab["slug"]}").MacAddress' in inspect
    conf = next(i for i in fake.inputs if "dhcp-host=" in i)
    assert "interface=eth0" in conf
    assert f"dhcp-host={MACS['dc1-spine1']},172.16.1.11,set:dc1-spine1" in conf
    assert 'dhcp-option=tag:dc1-spine1,67,"http://172.16.1.254/dc1-spine1.cfg"' in conf
    remote = f"/opt/avdlab/labs/{lab['slug']}"
    order = [c for c, _ in fake.calls]
    i_cat = next(i for i, c in enumerate(order) if c.startswith(f"cat > {remote}/clab/ztp/dnsmasq.conf"))
    i_restart = next(i for i, c in enumerate(order) if c == "docker exec ztp sh /ztp/run.sh restart")
    i_deploy = next(i for i, c in enumerate(order) if "containerlab deploy" in c)
    assert i_deploy < i_cat < i_restart
    assert fake.reads == 3
    log = read_log(job["id"])
    for needle in ("[ztp] dc1-spine1: DHCP lease 172.16.1.11", "[ztp] dc1-spine1: fetched dc1-spine1.cfg",
                   "[ztp] dc1-spine1: eAPI ready", "[ztp] pending:"):
        assert needle in log, needle
    assert log.index("DHCP lease 172.16.1.11") < log.index("fetched dc1-spine1.cfg") < log.index("dc1-spine1: eAPI ready")
    row = await lab_row(lab["id"])
    assert row.state == LabState.deployed
    last = {n["name"]: n for n in row.settings["ztp_last"]["nodes"]}
    assert last["dc1-leaf1b"] == {"name": "dc1-leaf1b", "mac": MACS["dc1-leaf1b"], "ip": "172.16.1.102", "lease": True,
                                  "bootfile_fetched": True, "eapi": True, "bootfile": "dc1-leaf1b.cfg"}

    # GET /ztp: last poll + a fresh read of leases/log
    reads = fake.reads
    st = (await client.get(f"/api/labs/{lab['id']}/ztp")).json()
    assert fake.reads == reads + 1 and st["fresh"] is True and st["error"] is None
    assert st["mode"] == "ztp" and st["source"] == "intended" and st["server_ip"] == "172.16.1.254"
    assert all(n["lease"] and n["bootfile_fetched"] and n["eapi"] for n in st["nodes"])
    assert set(st["nodes"][0]) == {"name", "mac", "ip", "lease", "bootfile_fetched", "eapi", "bootfile"}
    assert (await client.get("/api/labs/nope/ztp")).status_code == 404


async def test_startup_config_lab_ztp_endpoint_and_deploy_untouched(client, monkeypatch):
    host_id = await mk_host()
    r = await client.post("/api/labs", json={"name": f"ztp-{uuid.uuid4().hex[:6]}", "template": "single-dc-l3ls",
                                             "host_id": host_id})
    lab = r.json()
    _CREATED["labs"].append(lab["id"])
    fake_build(Path(lab["dir"]), lab["slug"])
    await set_state(lab["id"], LabState.built)
    assert (await client.get(f"/api/labs/{lab['id']}/ztp")).json() == {
        "mode": "startup-config", "source": "intended", "server_ip": None, "nodes": [], "at": None, "fresh": False,
        "error": None}
    fake = ClabFakeSSH(lab["slug"], eapi_fail_first=0)
    install(monkeypatch, fake)
    await run_job(client, (await client.post(f"/api/labs/{lab['id']}/deploy")).json()["job_id"])
    assert not ran(fake, "docker inspect -f") and not ran(fake, "run.sh") and not ran(fake, "docker exec")


# ------------------------------------------------------------------ CloudVision target ztp_script
async def test_cv_target_ztp_script_write_only(client):
    r = await client.post("/api/cv/targets", json={"name": f"cvt-ztp-{uuid.uuid4().hex[:6]}", "kind": "cvaas",
                                                   "server": "www.arista.io", "ztp_script": ZTP_SCRIPT})
    assert r.status_code == 201, r.text
    t = r.json()
    assert t["has_ztp_script"] is True and t["ztp_script_meta"]["updated_at"] and "ztp_script" not in t
    for resp in (await client.get("/api/cv/targets"), await client.get(f"/api/cv/targets/{t['id']}"),
                 await client.get("/api/secrets")):
        assert "enroll-SECRET" not in resp.text
    async with SessionLocal() as s:
        sec = (await s.execute(__import__("sqlalchemy").select(Secret).where(
            Secret.name == f"cv-target-{t['id']}-ztp"))).scalar_one()
        assert sec.kind.value == "generic" and "enroll-SECRET" not in sec.ciphertext
        from avdlab.cloudvision import service as cv

        assert await cv.target_ztp_script(s, await s.get(CVTarget, t["id"])) == ZTP_SCRIPT
    # PATCH without the field keeps it, "" / null removes it
    assert (await client.patch(f"/api/cv/targets/{t['id']}", json={"server": "10.1.1.1"})).json()["has_ztp_script"] is True
    assert (await client.patch(f"/api/cv/targets/{t['id']}", json={"ztp_script": None})).json()["has_ztp_script"] is False
    assert (await client.patch(f"/api/cv/targets/{t['id']}", json={"ztp_script": "#!/bin/sh\n"})).json()["has_ztp_script"]
    assert (await client.delete(f"/api/cv/targets/{t['id']}")).status_code == 204
    async with SessionLocal() as s:
        from avdlab.core.security import secret_store

        assert await secret_store.by_name(s, f"cv-target-{t['id']}-ztp") is None


@pytest.mark.parametrize("with_script", [True, False])
async def test_deploy_ztp_cloudvision_publishes_secrets(client, monkeypatch, with_script):
    host_id = await mk_host()
    lab, meta = await mk_ztp_lab(client, host_id, "cloudvision")
    body = {"name": f"cvt-ztp-{uuid.uuid4().hex[:6]}", "kind": "cvaas", "server": "www.arista.io",
            "onboarding_token": ONB_TOKEN}
    if with_script:
        body["ztp_script"] = ZTP_SCRIPT
    t = (await client.post("/api/cv/targets", json=body)).json()
    async with SessionLocal() as s:  # PATCH /labs refuses a target while the cloudvision knob is mode none
        row = await s.get(Lab, lab["id"])
        row.cv_target_id = t["id"]
        await s.commit()
    fake = ZtpFakeSSH(lab["slug"], meta, eapi_fail_first=0)
    install(monkeypatch, fake)
    job = await run_job(client, (await client.post(f"/api/labs/{lab['id']}/deploy", json={"wait_timeout": 60})).json()["job_id"])
    www = f"/opt/avdlab/labs/{lab['slug']}/clab/ztp/www"
    conf = next(i for i in fake.inputs if "dhcp-host=" in i)
    assert 'dhcp-option=tag:dc1-spine1,67,"http://172.16.1.254/bootstrap-dc1-spine1.sh"' in conf
    assert ran(fake, f"rm -f {www}/cv-bootstrap.sh {www}/cv-token")
    log = read_log(job["id"])
    if with_script:
        assert ran(fake, f"umask 077 && cat > {www}/cv-bootstrap.sh") and ZTP_SCRIPT in fake.inputs
        assert not ran(fake, f"cat > {www}/cv-token")
    else:
        assert ran(fake, f"umask 077 && cat > {www}/cv-token") and ONB_TOKEN + "\n" in fake.inputs
    assert all("enroll-SECRET" not in c and ONB_TOKEN not in c for c, _ in fake.calls)
    assert "enroll-SECRET" not in log and ONB_TOKEN not in log
    async with SessionLocal() as s:
        row = await s.get(Lab, lab["id"])
        row.cv_target_id = None
        await s.commit()


def test_ztp_network_facts():
    from avdlab.topology.clab import ztp_network_facts

    gv = {"FABRIC": {"mgmt_gateway": "172.16.1.1", "dns_settings": {"servers": [{"ip_address": "9.9.9.9"}]},
                     "cv_settings": {"cvaas": {"clusters": [{"name": "cvaas"}]}}}}
    assert ztp_network_facts({}, gv) == {"mgmt_gateway": "172.16.1.1", "dns_servers": ["9.9.9.9"],
                                         "token_file": "/tmp/cv-onboarding-token"}
    assert ztp_network_facts({"mgmt_gateway": "10.0.0.1", "dns_servers": ["1.1.1.1"]}, {}) == {
        "mgmt_gateway": "10.0.0.1", "dns_servers": ["1.1.1.1"], "token_file": None}
