"""Module 5: connected endpoints → client plans + setup scripts (topology/clients.py), GET /labs/{id}/clients."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from avdlab.core.db import Lab, LabState, SessionLocal
from avdlab.labs.build import build_lab_dir
from avdlab.labs.templates import get_template, render_lab
from avdlab.topology import clients as cl
from avdlab.topology.clab import build_clab_topology, client_image_for, generate_for_lab

from .test_executors import _cleanup, mk_host, mk_lab  # noqa: F401 - autouse cleanup fixture

MT = "ghcr.io/srl-labs/network-multitool:latest"


def _server_plan(name: str, octet: int, leaf: int) -> dict:
    def v(vid: int, vrf: str, default: bool = False) -> dict:
        return {"vid": vid, "iface": f"bond0.{vid}", "ip": f"10.10.{vid}.{octet}/24", "gateway": f"10.10.{vid}.1",
                "default_route": default, "tenant": "TENANT1", "vrf": vrf}

    bond = {"name": "bond0", "members": ["eth1", "eth2"]}
    return {
        "name": name, "image": MT,
        "links": [{"eth": "eth1", "switch": f"dc1-leaf{leaf}a", "switch_port": "Ethernet5"},
                  {"eth": "eth2", "switch": f"dc1-leaf{leaf}b", "switch_port": "Ethernet5"},
                  {"eth": "eth3", "switch": f"dc1-leaf{leaf}c", "switch_port": "Ethernet5"}],
        "bond": bond, "bonds": [bond],
        "vlans": [
            {"vid": 4092, "iface": "bond0", "ip": None, "gateway": None, "default_route": False, "tenant": None, "vrf": None},
            v(11, "VRF10", True), v(12, "VRF10"), v(21, "VRF11"), v(22, "VRF11"),
            # iLO: access VLAN 11 again → L2 only (a VLAN is addressed once per client)
            {"vid": 11, "iface": "eth3", "ip": None, "gateway": None, "default_route": False, "tenant": "TENANT1",
             "vrf": "VRF10"},
        ],
    }


@pytest.fixture(scope="module")
def built_lab(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("clients") / "lab"
    for rel, content in render_lab(get_template("single-dc-l3ls"), {}, slug="cli").files().items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    build_lab_dir(root, log=lambda _l: None)
    return root


def test_single_dc_plan_snapshot(built_lab):
    build = build_clab_topology(built_lab, "FABRIC", "arista/ceos:4.34.2F", host_image=MT)
    assert build.clients == [_server_plan("dc1-leaf1-server1", 100, 1), _server_plan("dc1-leaf2-server1", 101, 2)]
    # deterministic: a second generation is byte-identical
    again = build_clab_topology(built_lab, "FABRIC", "arista/ceos:4.34.2F", host_image=MT)
    assert again.host_scripts == build.host_scripts
    node = build.topology.topology.nodes["dc1-leaf1-server1"]
    assert node.kind == "linux" and node.image == MT


def test_setup_script(built_lab):
    build = build_clab_topology(built_lab, "FABRIC", "arista/ceos:4.34.2F", host_image=MT)
    s = build.host_scripts["dc1-leaf2-server1"]
    assert s.startswith("#!/bin/sh\n") and "hostname dc1-leaf2-server1" in s
    assert "accept_ra=0" in s
    lines = s.splitlines()
    order = [lines.index(x) for x in (
        "ip link add bond0 type bond mode 802.3ad miimon 100 2>/dev/null || true",
        "ip link set eth1 down; ip link set eth1 master bond0 || true; ip link set eth1 up || true",
        "ip link set bond0 up || true",
        "ip link add link bond0 name bond0.11 type vlan id 11 2>/dev/null || true",
        "ip addr replace 10.10.11.101/24 dev bond0.11",
        "ip route replace default via 10.10.11.1 dev bond0.11",
        "ip addr replace 10.10.22.101/24 dev bond0.22",
    )]
    assert order == sorted(order)
    assert "lacp_rate" not in s  # lacp_rate fast off
    assert s.count("ip route replace default") == 1
    assert "ip link set eth3 up || true" in lines and "bond0.4092" not in s
    assert "iperf3 -s -D" in s
    plan_json = re.search(r"<<'AVDLAB_EOF'\n(.*)\nAVDLAB_EOF", s, re.S).group(1)
    assert json.loads(plan_json) == _server_plan("dc1-leaf2-server1", 101, 2)


def test_clients_json_written(built_lab):
    build = build_clab_topology(built_lab, "FABRIC", "arista/ceos:4.34.2F", host_image=MT)
    build.write(built_lab / "clab")
    assert cl.load_plans(built_lab) == build.clients
    assert (built_lab / "clab/hosts/dc1-leaf1-server1.sh").read_text() == build.host_scripts["dc1-leaf1-server1"]


def test_client_image_knob(built_lab):
    lab = Lab(id="x", slug="cli", name="cli", template="single-dc-l3ls", settings={"knobs": {"client_image": "my/img:1"}})
    assert client_image_for(lab) == "my/img:1"
    assert client_image_for(Lab(settings={"knobs": {"client_image": None}})) == MT
    build = generate_for_lab(lab, built_lab, log=lambda _l: None)
    assert {c["image"] for c in build.clients} == {"my/img:1"}
    assert build.topology.topology.nodes["dc1-leaf1-server1"].image == "my/img:1"
    for name in ("single-dc-l3ls", "dual-dc-l3ls", "l2ls-fabric", "single-dc-multipod-l3ls"):
        assert get_template(name).knob("client_image") is not None, name


# ------------------------------------------------------------------------------------------------ pure helpers
def test_parse_vlans_and_ips():
    assert cl.parse_vlans("11-12,21-22") == [11, 12, 21, 22]
    assert cl.parse_vlans("10, 20,30") == [10, 20, 30]
    assert cl.parse_vlans("all", [30, 10]) == [10, 30]
    assert cl.parse_vlans(None) == []
    assert cl.client_ip("10.1.1.0/24", 0, "10.1.1.1") == "10.1.1.100/24"
    assert cl.client_ip("10.1.1.0/24", 3, "10.1.1.1") == "10.1.1.103/24"
    assert cl.client_ip("10.1.1.0/24", 0, "10.1.1.100") == "10.1.1.101/24"  # skip the gateway
    small = cl.client_ip("10.1.1.0/29", 0, "10.1.1.1")  # .100 is outside a /29: wrap inside the usable hosts
    assert small.endswith("/29") and small not in ("10.1.1.1/29", "10.1.1.0/29", "10.1.1.7/29")


def test_profiles_varp_access_and_generic_endpoints():
    hv = {"leaf1": {
        "port_profiles": [{"profile": "BASE", "mode": "trunk", "vlans": "10,20"},
                          {"profile": "ACC", "parent_profile": "BASE", "mode": "access", "vlans": "20"}],
        "tenants": [{"name": "T", "vrfs": [{"name": "default", "svis": [
            {"id": 10, "ip_virtual_router_addresses": ["10.0.10.1"], "nodes": [{"node": "s1", "ip_address": "10.0.10.2/24"}]},
            {"id": 20, "ip_virtual_router_addresses": ["10.0.20.1/24"]}]}],
            "l2vlans": [{"id": 30, "name": "L2"}]}],
        "firewalls": [{"name": "fw", "adapters": [{"switches": ["s1", "s2"], "switch_ports": ["Ethernet5", "Ethernet5"],
                                                  "profile": "BASE", "port_channel": {"mode": "active"}}]}],
        "connected_endpoints": [{"name": "pc", "type": "workstation", "adapters": [
            {"switches": ["s1"], "switch_ports": ["Ethernet6"], "profile": "ACC"}]},
            {"name": "l2box", "adapters": [{"switches": ["s1"], "switch_ports": ["Ethernet7"], "mode": "access", "vlans": "30"}]}],
    }}
    links = {
        "fw": [{"eth": "eth1", "switch": "s1", "switch_port": "Ethernet5"}, {"eth": "eth2", "switch": "s2", "switch_port": "Ethernet5"}],
        "pc": [{"eth": "eth1", "switch": "s1", "switch_port": "Ethernet6"}],
        "l2box": [{"eth": "eth1", "switch": "s1", "switch_port": "Ethernet7"}],
        "stranger": [{"eth": "eth1", "switch": "s9", "switch_port": "Ethernet1"}],
    }
    plans = {p["name"]: p for p in cl.plan_clients(links, hv, "img")}
    fw = plans["fw"]
    assert fw["bond"] == {"name": "bond0", "members": ["eth1", "eth2"]}
    assert [(v["iface"], v["ip"], v["gateway"], v["default_route"]) for v in fw["vlans"]] == [
        ("bond0.10", "10.0.10.100/24", "10.0.10.1", True), ("bond0.20", "10.0.20.100/24", "10.0.20.1", False)]
    pc = plans["pc"]  # index 1 (firewalls come before connected_endpoints), access via parent profile chain
    assert pc["bond"] is None and pc["vlans"] == [{"vid": 20, "iface": "eth1", "ip": "10.0.20.101/24", "gateway": "10.0.20.1",
                                                   "default_route": True, "tenant": "T", "vrf": "default"}]
    assert plans["l2box"]["vlans"][0]["ip"] is None and plans["l2box"]["vlans"][0]["tenant"] == "T"
    assert plans["stranger"]["vlans"] == [] and plans["stranger"]["links"][0]["switch"] == "s9"
    assert "ip addr" not in cl.setup_script(plans["l2box"])
    assert cl.default_ip(fw) == "10.0.10.100" and cl.default_ip(fw, 20) == "10.0.20.100" and cl.default_ip(fw, 99) is None


def test_l2ls_template_endpoints():
    hv = render_lab(get_template("l2ls-fabric"), {}, slug="l2").hostvars()
    eps = [e["name"] for e in cl.collect_endpoints(hv)]
    assert {"HostA", "FIREWALL"} <= set(eps)
    host_a = next(e for e in cl.collect_endpoints(hv) if e["name"] == "HostA")
    assert host_a["adapters"][0]["mode"] == "access" and host_a["adapters"][0]["vlans"] == "10"


# ------------------------------------------------------------------------------------------------ API
async def test_clients_api(client):
    host_id = await mk_host()
    lab = await mk_lab(client, host_id=host_id)
    assert (await client.get(f"/api/labs/{lab['id']}/clients")).json() == []
    plans = [_server_plan("dc1-leaf1-server1", 100, 1), _server_plan("dc1-leaf2-server1", 101, 2)]
    cl.write_plans(Path(lab["dir"]) / "clab", plans)
    r = await client.get(f"/api/labs/{lab['id']}/clients")
    assert r.status_code == 200
    assert [c["name"] for c in r.json()] == ["dc1-leaf1-server1", "dc1-leaf2-server1"]
    assert r.json()[0]["container_state"] is None and r.json()[0]["mgmt_ip"] is None
    async with SessionLocal() as s:
        row = await s.get(Lab, lab["id"])
        row.state = LabState.deployed
        row.settings = {**row.settings, "last_inspect": {"at": 10, "nodes": [
            {"name": "dc1-leaf1-server1", "kind": "linux", "state": "running", "mgmt_ip": "172.16.1.3"}]}}
        await s.commit()
    got = (await client.get(f"/api/labs/{lab['id']}/clients")).json()
    assert got[0]["container_state"] == "running" and got[0]["mgmt_ip"] == "172.16.1.3"
    assert got[0]["vlans"] == plans[0]["vlans"] and got[1]["container_state"] is None
    assert (await client.get("/api/labs/nope/clients")).status_code == 404
