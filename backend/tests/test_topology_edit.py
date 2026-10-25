"""Phase 5: editable topology (ops → AVD inputs → validated → rebuilt graph), free ports, layout."""
from __future__ import annotations

import uuid

import pytest
import yaml

from avdlab.jobs.engine import read_log, runner
from avdlab.labs import service


def _links(graph: dict) -> set[frozenset]:
    return {frozenset({(link["a"], link["a_if"]), (link["b"], link["b_if"])}) for link in graph["links"]}


async def _lab(client, template: str = "single-dc-l3ls", **knobs) -> dict:
    r = await client.post("/api/labs", json={"name": f"edit-{uuid.uuid4().hex[:6]}", "template": template, "knobs": knobs})
    assert r.status_code == 201, r.text
    return r.json()


def _gv(lab: dict, group: str) -> dict:
    return yaml.safe_load((service.lab_dir(lab["slug"]) / "group_vars" / f"{group}.yml").read_text())


async def test_ports(client):
    lab = await _lab(client)
    r = await client.get(f"/api/topology/{lab['id']}/ports")
    assert r.status_code == 200, r.text
    ports = r.json()
    spine = ports["dc1-spine1"]
    assert spine["used"] == ["Ethernet1", "Ethernet2", "Ethernet3", "Ethernet4"]
    assert spine["free"] == ["Ethernet5", "Ethernet6", "Ethernet7", "Ethernet8"]
    assert spine["peers"]["Ethernet1"] == "dc1-leaf1a:Ethernet1"
    leaf = ports["dc1-leaf1a"]
    assert {"Ethernet1", "Ethernet2", "Ethernet3", "Ethernet4", "Ethernet5", "Ethernet8"} <= set(leaf["used"])
    assert leaf["peers"]["Ethernet5"] == "dc1-leaf1-server1:PCI1" and "Ethernet6" in leaf["free"]
    assert ports["dc1-leaf1-server1"]["type"] == "servers"


async def test_set_uplink_dry_run_apply_build_and_knob_patch(client):
    lab = await _lab(client)
    op = {"op": "set_uplink", "node": "dc1-leaf1a", "index": 0, "switch": "dc1-spine1", "switch_port": "Ethernet5"}
    moved = frozenset({("dc1-leaf1a", "Ethernet1"), ("dc1-spine1", "Ethernet5")})

    r = await client.post(f"/api/topology/{lab['id']}/edit", json={"ops": [op], "dry_run": True})
    assert r.status_code == 200, r.text
    dry = r.json()
    assert dry["dry_run"] is True and dry["commit"] is None
    assert dry["changed_files"] == ["group_vars/DC1_L3_LEAVES.yml"]
    assert "uplink_switch_interfaces" in dry["diff"]
    assert moved in _links(dry["graph"])
    assert "uplink_switch_interfaces" not in (service.lab_dir(lab["slug"]) / "group_vars/DC1_L3_LEAVES.yml").read_text()

    r = await client.post(f"/api/topology/{lab['id']}/edit", json={"ops": [op]})
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["commit"] and moved in _links(res["graph"])
    assert frozenset({("dc1-leaf1a", "Ethernet1"), ("dc1-spine1", "Ethernet1")}) not in _links(res["graph"])
    node = next(n for g in _gv(lab, "DC1_L3_LEAVES")["l3leaf"]["node_groups"] for n in g["nodes"] if n["name"] == "dc1-leaf1a")
    assert node["uplink_switches"] == ["dc1-spine1", "dc1-spine2"]
    assert node["uplink_switch_interfaces"] == ["Ethernet5", "Ethernet1"]
    detail = (await client.get(f"/api/labs/{lab['id']}")).json()
    assert detail["settings"]["topology_overrides"]["nodes"]["dc1-leaf1a"]["uplink_switch_interfaces"][0] == "Ethernet5"
    topo = (await client.get(f"/api/topology/{lab['id']}")).json()
    assert moved in _links(topo)
    ports = (await client.get(f"/api/topology/{lab['id']}/ports")).json()
    assert "Ethernet5" in ports["dc1-spine1"]["used"] and "Ethernet1" in ports["dc1-spine1"]["free"]

    # the real build agrees
    job_id = (await client.post(f"/api/labs/{lab['id']}/build")).json()["job_id"]
    await runner.wait(job_id)
    job = (await client.get(f"/api/jobs/{job_id}")).json()
    assert job["status"] == "succeeded", read_log(job_id)[-2000:]
    assert moved in _links((await client.get(f"/api/topology/{lab['id']}")).json())

    # override survives a knob change (re-render)
    r = await client.patch(f"/api/labs/{lab['id']}", json={"knobs": {"spine_asn": 65200}})
    assert r.status_code == 200, r.text
    node = next(n for g in _gv(lab, "DC1_L3_LEAVES")["l3leaf"]["node_groups"] for n in g["nodes"] if n["name"] == "dc1-leaf1a")
    assert node["uplink_switch_interfaces"] == ["Ethernet5", "Ethernet1"]
    assert _gv(lab, "DC1_SPINES")["spine"]["defaults"]["bgp_as"] == 65200


async def test_invalid_ports_422(client):
    lab = await _lab(client)
    url = f"/api/topology/{lab['id']}/edit"
    base = {"op": "set_uplink", "node": "dc1-leaf1a", "index": 0, "switch": "dc1-spine1"}
    ok = {"op": "set_uplink", "node": "dc1-leaf2a", "index": 1, "switch": "dc1-spine2", "switch_port": "Ethernet6"}
    r = await client.post(url, json={"ops": [ok, {**base, "switch_port": "Ethernet2"}]})  # used by dc1-leaf1b
    assert r.status_code == 422
    d = r.json()["detail"]
    assert d["op_index"] == 1 and "in use" in d["message"] and d["violations"]
    for bad, idx in (({**base, "switch_port": "Port-Channel1"}, 0), ({**base, "switch_port": "Ethernet5", "index": 7}, 0),
                     ({**base, "switch": "nope", "switch_port": "Ethernet5"}, 0)):
        r = await client.post(url, json={"ops": [bad]})
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["op_index"] == idx
    r = await client.post(url, json={"ops": [{"op": "add_endpoint", "name": "srv9", "adapters": [
        {"switches": ["dc1-leaf1a"], "switch_ports": ["Ethernet5"], "mode": "access", "vlans": "11"}]}]})
    assert r.status_code == 422 and "in use" in str(r.json()["detail"]["violations"])
    r = await client.post(url, json={"ops": [{"op": "frobnicate"}]})
    assert r.status_code == 422 and r.json()["detail"]["op_index"] == 0
    r = await client.post(url, json={"ops": [{"op": "rename_node", "node": "dc1-leaf1a", "new_name": "x"}]})
    assert r.status_code == 409
    r = await client.post(url, json={"ops": [{"op": "set_spine_count", "n": 9}]})
    assert r.status_code == 422 and r.json()["detail"]["violations"][0]["path"] == "spine_count"
    # nothing was written
    assert "uplink_switch_interfaces" not in (service.lab_dir(lab["slug"]) / "group_vars/DC1_L3_LEAVES.yml").read_text()


async def test_endpoint_add_update_remove(client):
    lab = await _lab(client)
    url = f"/api/topology/{lab['id']}/edit"
    add = {"op": "add_endpoint", "name": "dc1-leaf1-server2", "type": "servers", "adapters": [
        {"switches": ["dc1-leaf1a", "dc1-leaf1b"], "switch_ports": ["Ethernet6", "Ethernet6"],
         "endpoint_ports": ["eth1", "eth2"], "mode": "trunk", "vlans": "11-12", "port_channel": {"mode": "active"}}]}
    r = await client.post(url, json={"ops": [add]})
    assert r.status_code == 200, r.text
    graph = r.json()["graph"]
    node = next(n for n in graph["nodes"] if n["name"] == "dc1-leaf1-server2")
    assert node["kind"] == "linux"
    links = _links(graph)
    assert frozenset({("dc1-leaf1a", "Ethernet6"), ("dc1-leaf1-server2", "eth1")}) in links
    assert frozenset({("dc1-leaf1b", "Ethernet6"), ("dc1-leaf1-server2", "eth2")}) in links
    detail = (await client.get(f"/api/labs/{lab['id']}")).json()
    assert [s["name"] for s in detail["knobs"]["servers"]][-1] == "dc1-leaf1-server2"
    assert "dc1-leaf1-server2" in (service.lab_dir(lab["slug"]) / "group_vars/CONNECTED_ENDPOINTS.yml").read_text()

    r = await client.post(url, json={"ops": [{"op": "add_endpoint", "name": "dc1-leaf1-server2", "adapters": add["adapters"]}]})
    assert r.status_code == 422  # duplicate name

    upd = {"op": "update_endpoint", "name": "dc1-leaf1-server2", "adapters": [
        {"switches": ["dc1-leaf1a"], "switch_ports": ["Ethernet7"], "mode": "access", "vlans": "11"}]}
    r = await client.post(url, json={"ops": [upd]})
    assert r.status_code == 200, r.text
    links = _links(r.json()["graph"])
    assert frozenset({("dc1-leaf1a", "Ethernet7"), ("dc1-leaf1-server2", "eth1")}) in links
    assert not any(("dc1-leaf1b", "Ethernet6") in link for link in links)

    # firewall (non-knob endpoint type) goes to the overrides and survives a knob PATCH
    fw = {"op": "add_endpoint", "name": "fw1", "type": "firewalls", "adapters": [
        {"switches": ["dc1-leaf2a"], "switch_ports": ["Ethernet6"], "mode": "access", "vlans": "11"}]}
    r = await client.post(url, json={"ops": [fw, {"op": "remove_endpoint", "name": "dc1-leaf1-server2"}]})
    assert r.status_code == 200, r.text
    names = {n["name"] for n in r.json()["graph"]["nodes"]}
    assert "fw1" in names and "dc1-leaf1-server2" not in names
    assert (await client.patch(f"/api/labs/{lab['id']}", json={"knobs": {"spine_asn": 65111}})).status_code == 200
    assert _gv(lab, "CONNECTED_ENDPOINTS")["firewalls"][0]["name"] == "fw1"
    r = await client.post(url, json={"ops": [{"op": "remove_endpoint", "name": "nope"}]})
    assert r.status_code == 422


async def test_knob_ops_leaf_pairs(client):
    lab = await _lab(client)
    url = f"/api/topology/{lab['id']}/edit"
    r = await client.post(url, json={"ops": [{"op": "add_leaf_pair"}]})
    assert r.status_code == 200, r.text
    names = {n["name"] for n in r.json()["graph"]["nodes"]}
    assert {"dc1-leaf3a", "dc1-leaf3b", "dc1-leaf3c"} <= names
    groups = _gv(lab, "DC1_L3_LEAVES")["l3leaf"]["node_groups"]
    assert len(groups) == 3
    assert (await client.get(f"/api/labs/{lab['id']}")).json()["knobs"]["leaf_pairs"] == 3
    assert frozenset({("dc1-leaf3a", "Ethernet1"), ("dc1-spine1", "Ethernet5")}) in _links(r.json()["graph"])

    r = await client.post(url, json={"ops": [{"op": "set_l2leaf_per_pair", "n": 0}, {"op": "remove_last_leaf_pair"}]})
    assert r.status_code == 200, r.text
    names = {n["name"] for n in r.json()["graph"]["nodes"]}
    assert "dc1-leaf3a" not in names and "dc1-leaf1c" not in names
    assert "group_vars/DC1_L2_LEAVES.yml" in r.json()["changed_files"]
    assert not (service.lab_dir(lab["slug"]) / "group_vars/DC1_L2_LEAVES.yml").exists()
    assert any("dc1-leaf1c" in w for w in r.json()["warnings"])  # the iLO adapters point at the removed L2 leafs


async def test_knob_ops_rejected_for_static_templates(client):
    lab = await _lab(client, "l2ls-fabric")
    r = await client.post(f"/api/topology/{lab['id']}/edit", json={"ops": [{"op": "add_leaf_pair"}]})
    assert r.status_code == 409
    assert r.json()["detail"]["hint"]


async def test_layout_round_trip(client):
    lab = await _lab(client)
    url = f"/api/topology/{lab['id']}/layout"
    assert (await client.get(url)).json() == {"positions": {}, "locked": False}
    body = {"positions": {"dc1-spine1": {"x": 10, "y": 20.5}, "dc1-leaf1a": {"x": -3, "y": 400}}, "locked": True}
    r = await client.put(url, json=body)
    assert r.status_code == 200, r.text
    got = (await client.get(url)).json()
    assert got["locked"] is True and got["positions"]["dc1-spine1"] == {"x": 10.0, "y": 20.5}
    assert "clab/layout.json" in service.history(lab["slug"], 1)[0]["files"]
    assert (await client.put(url, json={"positions": {"a": {"x": "nope"}}})).status_code == 422
    # GET /topology carries the layout once a graph exists (an edit writes clab/graph.json)
    r = await client.post(f"/api/topology/{lab['id']}/edit", json={"ops": [{"op": "set_uplink", "node": "dc1-leaf2b",
                          "index": 1, "switch": "dc1-spine2", "switch_port": "Ethernet8"}]})
    assert r.status_code == 200, r.text
    assert r.json()["graph"]["layout"]["positions"]["dc1-leaf1a"] == {"x": -3.0, "y": 400.0}
    topo = (await client.get(f"/api/topology/{lab['id']}")).json()
    assert topo["layout"]["locked"] is True


async def test_custom_lab_edits_group_vars_in_place(client):
    from tests.test_import import lab_files, make_zip, upload

    r = await upload(client, make_zip(lab_files()), f"custom-{uuid.uuid4().hex[:6]}")
    assert r.status_code == 201, r.text
    lab = r.json().get("lab") or r.json()
    url = f"/api/topology/{lab['id']}/edit"
    ops = [{"op": "set_uplink", "node": "dc1-leaf2b", "index": 0, "switch": "dc1-spine1", "switch_port": "Ethernet7"},
           {"op": "add_endpoint", "name": "fw9", "type": "firewalls", "adapters": [
               {"switches": ["dc1-leaf2a"], "switch_ports": ["Ethernet9"], "mode": "access", "vlans": "11"}]}]
    r = await client.post(url, json={"ops": ops})
    assert r.status_code == 200, r.text
    res = r.json()
    assert sorted(res["changed_files"]) == ["group_vars/CONNECTED_ENDPOINTS.yml", "group_vars/DC1_L3_LEAVES.yml"]
    links = _links(res["graph"])
    assert frozenset({("dc1-leaf2b", "Ethernet1"), ("dc1-spine1", "Ethernet7")}) in links
    assert frozenset({("dc1-leaf2a", "Ethernet9"), ("fw9", "eth1")}) in links
    assert _gv(lab, "CONNECTED_ENDPOINTS")["firewalls"][0]["name"] == "fw9"
    detail = (await client.get(f"/api/labs/{lab['id']}")).json()
    assert "topology_overrides" not in detail["settings"]
    assert "group_vars/DC1_L3_LEAVES.yml" in detail["custom_files"]
    r = await client.post(url, json={"ops": [{"op": "add_leaf_pair"}]})
    assert r.status_code == 409
