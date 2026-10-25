"""Phase 6 (A): every template renders, validates, builds through the job runner and yields a containerlab topology.
Plus the catalogue metadata (tags / node_count / learn) and the topology specifics of the new AVD examples."""
from __future__ import annotations

import io
import zipfile

import pytest
import yaml

from avdlab.jobs.engine import read_log, runner
from avdlab.labs.templates import get_template, load_templates, render_lab, validate_hostvars

TEMPLATES = sorted(load_templates())
NEW = ("campus-fabric", "cv-pathfinder", "isis-ldp-ipvpn", "single-dc-l3ls-ipv6")
KNOBS = {"single-dc-l3ls-ipv6": {"mgmt_ipv6_subnet": "2001:db8:100::/64"}}


def test_new_templates_present():
    assert set(NEW) <= set(TEMPLATES)


@pytest.mark.parametrize("name", [t for t in TEMPLATES if t != "custom"])
def test_render_and_validate(name):
    tpl = get_template(name)
    r = render_lab(tpl, {"mgmt_subnet": "10.99.0.0/24", "device_password": "s3cret", **KNOBS.get(name, {})}, slug=name)
    _validated, violations = validate_hostvars(r.hostvars())
    assert violations == []
    text = "\n".join(r.files().values())
    assert "10.99.0." in text and "serial_number" in text and "{{" not in text


def test_catalogue_metadata():
    for name in TEMPLATES:
        pub = get_template(name).public()
        assert pub["tags"], name
        assert pub["learn"] and "https://avd.arista.com/6.4/" in pub["learn"], name
        if name == "custom":
            assert pub["node_count"] is None
        else:
            assert pub["node_count"] and pub["node_count"] >= 6, (name, pub["node_count"])
    assert get_template("cv-pathfinder").public()["notes"].startswith("WAN path-selection needs CloudVision Pathfinder")
    assert get_template("cv-pathfinder").public()["node_count"] == 17
    assert "ipv6" in get_template("single-dc-l3ls-ipv6").public()["tags"]
    assert {"mpls", "isis"} <= set(get_template("isis-ldp-ipvpn").public()["tags"])
    assert "campus" in get_template("campus-fabric").public()["tags"]


async def test_templates_endpoint_fields(client):
    tpls = {t["name"]: t for t in (await client.get("/api/labs/templates")).json()}
    assert set(NEW) <= set(tpls)
    for t in tpls.values():
        assert {"tags", "node_count", "learn"} <= set(t), t["name"]
    assert tpls["isis-ldp-ipvpn"]["node_count"] == 9


def _zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for rel, content in files.items():
            zf.writestr(f"imported/{rel}", content)
    return buf.getvalue()


async def _create(client, name: str) -> dict:
    if name == "custom":  # a custom lab is an imported inventory: use a rendered AVD example as the archive
        files = render_lab(get_template("isis-ldp-ipvpn"), {}, slug="custom-all").files()
        r = await client.post("/api/labs/import", files={"file": ("lab.zip", _zip(files), "application/zip")},
                              data={"name": "all-custom"})
    else:
        r = await client.post("/api/labs", json={"name": f"all-{name}", "template": name, "knobs": KNOBS.get(name, {})})
    assert r.status_code == 201, r.text
    return r.json()


@pytest.mark.parametrize("name", TEMPLATES)
async def test_build_via_runner_and_topology(client, name):
    lab = await _create(client, name)
    lab_id = lab["id"]
    try:
        r = await client.post(f"/api/labs/{lab_id}/build")
        assert r.status_code == 202, r.text
        job_id = r.json()["job_id"]
        await runner.wait(job_id)
        job = (await client.get(f"/api/jobs/{job_id}")).json()
        assert job["status"] == "succeeded", (job["error"], read_log(job_id)[-3000:])
        assert job["result"]["topology"]["links"] >= 1, job["result"]
        topo = (await client.get(f"/api/topology/{lab_id}")).json()
        assert len(topo["links"]) >= 1 and len(topo["nodes"]) >= 2
        clab = yaml.safe_load((await client.get(f"/api/topology/{lab_id}/clab")).text)
        nodes = clab["topology"]["nodes"]
        endpoints = {e.split(":")[0] for link in clab["topology"]["links"] for e in link["endpoints"]}
        assert endpoints <= set(nodes)  # every link end is a node of the topology
        for link in clab["topology"]["links"]:
            for ep in link["endpoints"]:
                assert "." not in ep.split(":")[1] and " " not in ep  # sub-interfaces collapse to the parent port

        if name == "single-dc-l3ls-ipv6":
            assert clab["mgmt"]["ipv6-subnet"] == "2001:db8:100::/64"
            assert nodes["dc1-leaf1a"]["mgmt-ipv6"] == "2001:db8:100::65"
            assert nodes["dc1-leaf1a"]["mgmt-ipv4"] == "172.16.1.101"
        if name == "isis-ldp-ipvpn":  # core_interfaces.p2p_links → 13 cables between P/PE/RR
            assert len(clab["topology"]["links"]) == 13
            assert {"endpoints": ["pe1:eth1", "p1:eth1"]} in clab["topology"]["links"] or \
                   {"endpoints": ["p1:eth1", "pe1:eth1"]} in clab["topology"]["links"]
        if name == "campus-fabric":  # L2 uplinks with explicit uplink_switch_interfaces + the external WAN router
            assert nodes["WAN"]["kind"] == "linux"
            assert any(set(link["endpoints"]) == {"LEAF3C:eth97_1", "LEAF3A:eth97_3"} for link in clab["topology"]["links"])
        if name == "cv-pathfinder":
            assert {"pf1", "site3-wan1", "inet-cloud"} <= set(nodes)
            assert any(set(link["endpoints"]) == {"site3-wan1:eth1", "site3-leaf1:eth1"} for link in clab["topology"]["links"])
    finally:
        assert (await client.delete(f"/api/labs/{lab_id}")).status_code == 204


def test_shift_endpoint_ports_with_more_spines():
    """0.10.0: 4 spines → uplinks Ethernet1-4, peer-link 5-6: default server ports on Ethernet5 move to Ethernet7."""
    from avdlab.labs.templates import shift_endpoint_ports

    servers = [{"name": "s1", "adapters": [{"switch_ports": ["Ethernet5", "Ethernet5"], "switches": ["a", "b"]}]},
               {"name": "s2", "adapters": [{"switch_ports": ["Ethernet2", "Ethernet9"]}]}]
    assert shift_endpoint_ports(servers, 2) == servers
    out = shift_endpoint_ports(servers, 4)
    assert out[0]["adapters"][0]["switch_ports"] == ["Ethernet7", "Ethernet7"]
    assert out[1]["adapters"][0]["switch_ports"] == ["Ethernet2", "Ethernet9"]  # untouched
    assert servers[0]["adapters"][0]["switch_ports"] == ["Ethernet5", "Ethernet5"]  # input not mutated
    out3 = shift_endpoint_ports(servers, 3)
    assert out3[0]["adapters"][0]["switch_ports"] == ["Ethernet6", "Ethernet6"]
