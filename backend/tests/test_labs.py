"""Labs module: template rendering, pyavd build equality with the AVD example, knob changes, API flow."""
from __future__ import annotations

import shutil
from pathlib import Path

import re
import pytest
import yaml

from avdlab.jobs.engine import read_log, runner
from avdlab.labs.build import build_lab_dir
from avdlab.labs.templates import coerce_knobs, get_template, load_templates, render_lab, validate_hostvars

EXAMPLES = Path("/home/claude/arista/avd/ansible_collections/arista/avd/examples")
EXAMPLE = EXAMPLES / "single-dc-l3ls"
needs_example = pytest.mark.skipif(not EXAMPLE.exists(), reason="AVD examples not available")


def write_rendered(rendered, root: Path) -> Path:
    for rel, content in rendered.files().items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return root


async def wait_job(client, job_id: str) -> dict:
    await runner.wait(job_id)
    job = (await client.get(f"/api/jobs/{job_id}")).json()
    assert job["status"] == "succeeded", (job["error"], read_log(job_id)[-3000:])
    return job


# ------------------------------------------------------------------ templates (pure)
def test_templates_load_and_defaults():
    tpls = load_templates()
    assert {"single-dc-l3ls", "dual-dc-l3ls", "l2ls-fabric", "single-dc-multipod-l3ls"} <= set(tpls)
    tpl = tpls["single-dc-l3ls"]
    pub = tpl.public()
    knobs = {k["key"]: k for k in pub["knobs"]}
    for key in ("mgmt_subnet", "mgmt_gateway", "ceos_version", "device_password", "spine_count", "leaf_pairs",
                "l2leaf_per_pair", "pools", "spine_asn", "leaf_asn_base", "serial_scheme", "tenants", "servers"):
        assert key in knobs, key
    assert knobs["tenants"]["default"][0]["name"] == "TENANT1"  # resolved from the example's NETWORK_SERVICES
    assert knobs["leaf_pairs"]["default"] == 2
    values = coerce_knobs(tpl, {"leaf_pairs": "3", "pools": {"loopback": "10.0.0.0/27"}})
    assert values["leaf_pairs"] == 3 and values["pools"]["loopback"] == "10.0.0.0/27" and values["pools"]["vtep"]


def test_render_generates_nodes_and_serials():
    tpl = get_template("single-dc-l3ls")
    r = render_lab(tpl, {"leaf_pairs": 3, "l2leaf_per_pair": 2}, slug="demo-lab")
    l3 = r.group_vars["DC1_L3_LEAVES"]["l3leaf"]
    assert [g["bgp_as"] for g in l3["node_groups"]] == [65101, 65102, 65103]
    names = [n["name"] for g in l3["node_groups"] for n in g["nodes"]]
    assert names == ["dc1-leaf1a", "dc1-leaf1b", "dc1-leaf2a", "dc1-leaf2b", "dc1-leaf3a", "dc1-leaf3b"]
    l2 = r.group_vars["DC1_L2_LEAVES"]["l2leaf"]["node_groups"]
    assert [n["name"] for n in l2[0]["nodes"]] == ["dc1-leaf1c", "dc1-leaf1d"]
    serials = [n["serial_number"] for g in l3["node_groups"] for n in g["nodes"]]
    assert all(s.startswith("DEMO") for s in serials) and len(set(serials)) == len(serials)
    assert r.group_vars["DC1"]["management_eapi"] == {"enabled": True}
    assert r.group_vars["FABRIC"]["eos_designs_documentation"]["topology_csv"] is True
    hv = r.hostvars()
    assert hv["dc1-spine1"]["type"] == "spine" and hv["dc1-leaf3b"]["tenants"]


@pytest.mark.parametrize("name", ["dual-dc-l3ls", "l2ls-fabric", "single-dc-multipod-l3ls"])
def test_static_templates_render_and_validate(name):
    tpl = get_template(name)
    r = render_lab(tpl, {"mgmt_subnet": "10.99.0.0/24", "device_password": "s3cret"}, slug=name)
    _validated, violations = validate_hostvars(r.hostvars())
    assert violations == []
    text = "\n".join(r.files().values())
    assert "10.99.0." in text and "serial_number" in text


def test_invalid_knobs_rejected():
    from avdlab.labs.templates import KnobError

    with pytest.raises(KnobError):
        coerce_knobs(get_template("single-dc-l3ls"), {"leaf_pairs": 99, "bogus": 1})


# ------------------------------------------------------------------ builds (pyavd)
@needs_example
def test_leaf_pairs_3_builds(tmp_path):
    tpl = get_template("single-dc-l3ls")
    root = write_rendered(render_lab(tpl, {"leaf_pairs": 3}, slug="three"), tmp_path / "lab")
    res = build_lab_dir(root, log=lambda _l: None)
    assert len(res.hosts) == 2 + 6 + 3
    spine1 = (root / "intended/configs/dc1-spine1.cfg").read_text()
    assert "interface Ethernet5" in spine1 and "interface Ethernet6" in spine1  # links to dc1-leaf3a/b
    assert "neighbor 10.255.0.7 peer group EVPN-OVERLAY-PEERS" in spine1 or "10.255.0.7" in spine1
    leaf3a = yaml.safe_load((root / "intended/structured_configs/dc1-leaf3a.yml").read_text())
    assert leaf3a["router_bgp"]["as"] == "65103"
    assert leaf3a["metadata"]["serial_number"].startswith("THRE")
    assert (root / "documentation/fabric/FABRIC-topology.csv").exists()
    assert list((root / "anta/avd_catalogs").glob("*.json"))


# ------------------------------------------------------------------ API
async def test_templates_endpoint(client):
    r = await client.get("/api/labs/templates")
    assert r.status_code == 200
    names = [t["name"] for t in r.json()]
    assert "single-dc-l3ls" in names


async def test_create_validation_errors(client):
    r = await client.post("/api/labs", json={"name": "bad", "template": "single-dc-l3ls", "knobs": {"leaf_pairs": 42}})
    assert r.status_code == 422 and r.json()["detail"]["violations"]
    # pyavd schema violation: svi id must be an int
    bad_tenants = [{"name": "T", "mac_vrf_vni_base": 10000,
                    "vrfs": [{"name": "V", "vrf_vni": 1, "svis": [{"id": "not-an-int", "name": "X", "enabled": True}]}]}]
    r = await client.post("/api/labs", json={"name": "bad2", "template": "single-dc-l3ls", "knobs": {"tenants": bad_tenants}})
    assert r.status_code == 422, r.text
    v = r.json()["detail"]["violations"]
    assert any("tenants" in (x.get("path") or "") for x in v), v
    r = await client.post("/api/labs", json={"name": "bad3", "template": "nope", "knobs": {}})
    assert r.status_code == 422


@needs_example
async def test_api_flow_default_build_matches_example(client):
    """create → build (job via runner) → configs == AVD example → devices → topology → files → history/diff → patch → delete."""
    r = await client.post("/api/labs", json={"name": "Flow Lab", "template": "single-dc-l3ls", "knobs": {}})
    assert r.status_code == 201, r.text
    lab = r.json()
    assert lab["slug"] == "flow-lab" and lab["state"] == "draft"
    assert lab["knobs"]["device_password"] == "********"  # secret knobs are masked
    assert lab["ceos_version"] == "4.34.2F"
    lab_id = lab["id"]
    assert (await client.post("/api/labs", json={"name": "Flow Lab", "template": "single-dc-l3ls"})).status_code == 409

    # topology not there before the build
    assert (await client.get(f"/api/topology/{lab_id}")).status_code == 404

    r = await client.post(f"/api/labs/{lab_id}/build")
    assert r.status_code == 202
    job = await wait_job(client, r.json()["job_id"])
    assert len(job["result"]["hosts"]) == 8 and job["result"]["topology"]["links"] == 22

    root = Path(lab["dir"])
    # the example streams to a placeholder CVP; with cloudvision.mode=none the lab renders no TerminAttr block
    strip = lambda text: re.sub(r"daemon TerminAttr\n(?:   .*\n)+!\n", "", text)  # noqa: E731
    for f in sorted((EXAMPLE / "intended/configs").glob("*.cfg")):
        assert (root / "intended/configs" / f.name).read_text() == strip(f.read_text()), f.name
        assert "TerminAttr" not in (root / "intended/configs" / f.name).read_text()
    sc = yaml.safe_load((root / "intended/structured_configs/dc1-spine1.yml").read_text())
    assert sc["metadata"]["serial_number"] == "FLOW0001"

    lab = (await client.get(f"/api/labs/{lab_id}")).json()
    assert lab["state"] == "built" and lab["last_build_job"]["status"] == "succeeded"

    devs = {d["hostname"]: d for d in (await client.get(f"/api/labs/{lab_id}/devices")).json()}
    assert devs["dc1-leaf1a"] == {**devs["dc1-leaf1a"], "type": "l3leaf", "platform": "cEOSLab",
                                   "mgmt_ip": "172.16.1.101/24", "loopback0": "10.255.0.3/32", "vtep": "10.255.1.3/32",
                                   "bgp_as": "65101"}
    assert devs["dc1-spine1"]["type"] == "spine" and devs["dc1-leaf1c"]["type"] == "l2leaf"

    topo = (await client.get(f"/api/topology/{lab_id}")).json()
    assert len(topo["links"]) == 22 and len(topo["nodes"]) == 10
    roles = {n["name"]: n["role"] for n in topo["nodes"]}
    assert roles["dc1-leaf1b"] == "l3leaf" and roles["dc1-leaf1-server1"] == "server"
    clab = (await client.get(f"/api/topology/{lab_id}/clab")).text
    assert "arista/ceos:4.34.2F" in clab
    r = await client.post(f"/api/topology/{lab_id}/regenerate")
    assert r.status_code == 202
    await wait_job(client, r.json()["job_id"])

    # files
    tree = {e["path"]: e for e in (await client.get(f"/api/labs/{lab_id}/files")).json()}
    assert tree["group_vars/NETWORK_SERVICES.yml"]["editable"] and not tree["intended/configs/dc1-spine1.cfg"]["editable"]
    f = (await client.get(f"/api/labs/{lab_id}/files/group_vars/NETWORK_SERVICES.yml")).json()
    assert "TENANT1" in f["content"]
    assert (await client.get(f"/api/labs/{lab_id}/files/../../etc/passwd")).status_code in (400, 404)
    assert (await client.put(f"/api/labs/{lab_id}/files/intended/configs/dc1-spine1.cfg",
                             json={"content": "x"})).status_code == 403
    bad = f["content"].replace("vrf_vni: 10", "vrf_vni: ten", 1)
    r = await client.put(f"/api/labs/{lab_id}/files/group_vars/NETWORK_SERVICES.yml", json={"content": bad})
    assert r.status_code == 422, r.text
    good = f["content"].replace("name: VRF10_VLAN11", "name: VRF10_VLAN11_EDITED", 1)
    r = await client.put(f"/api/labs/{lab_id}/files/group_vars/NETWORK_SERVICES.yml", json={"content": good})
    assert r.status_code == 200 and r.json()["changed"], r.text
    assert r.json()["custom_knobs"] == ["tenants"]
    lab = (await client.get(f"/api/labs/{lab_id}")).json()
    assert lab["custom_knobs"] == ["tenants"] and lab["state"] == "draft"

    # history / diff
    hist = (await client.get(f"/api/labs/{lab_id}/history")).json()
    msgs = [h["message"] for h in hist]
    assert msgs[0] == "edit group_vars/NETWORK_SERVICES.yml" and msgs[-1].startswith("create lab")
    assert any(m.startswith("build:") for m in msgs)
    d = (await client.get(f"/api/labs/{lab_id}/diff", params={"a": hist[1]["sha"], "b": hist[0]["sha"]})).json()
    assert "VRF10_VLAN11_EDITED" in d["diff"] and d["files"][0]["path"] == "group_vars/NETWORK_SERVICES.yml"
    assert (await client.get(f"/api/labs/{lab_id}/diff", params={"a": "nope"})).status_code == 400

    # patch knobs → re-render (+ validation), custom flags cleared
    r = await client.patch(f"/api/labs/{lab_id}", json={"knobs": {"leaf_pairs": 3, "device_password": "********"}})
    assert r.status_code == 200, r.text
    lab = r.json()
    assert lab["knobs"]["leaf_pairs"] == 3 and lab["custom_knobs"] == []
    inv = (await client.get(f"/api/labs/{lab_id}/files/inventory.yml")).json()["content"]
    assert "dc1-leaf3b" in inv
    assert (await client.patch(f"/api/labs/{lab_id}", json={"knobs": {"spine_count": 9}})).status_code == 422

    assert (await client.get("/api/labs")).json()[0]["id"] == lab_id
    assert (await client.delete(f"/api/labs/{lab_id}")).status_code == 204
    assert (await client.get(f"/api/labs/{lab_id}")).status_code == 404
    assert not root.exists()


def test_pool_knob_validation():
    from avdlab.labs.templates import Knob, _coerce
    k = Knob(key="pools", type="object", value_type="pool", default={"loopback": "10.255.0.0/27"})
    assert _coerce(k, {"uplink": "10.1.0.0/26, 10.2.0.1-10.2.0.9"})["uplink"] == "10.1.0.0/26, 10.2.0.1-10.2.0.9"
    import pytest
    with pytest.raises(ValueError):
        _coerce(k, {"loopback": "not-a-cidr"})
