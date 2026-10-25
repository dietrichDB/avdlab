"""containerlab topology generation: link-for-link equality with the shipped AVD single-dc-l3ls clab topology."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from avdlab.labs.build import build_lab_dir
from avdlab.labs.templates import get_template, render_lab
from avdlab.topology.clab import build_clab_topology, clab_port, read_links, read_node_types
from avdlab.topology.models import ClabTopology

EXAMPLE = Path("/home/claude/arista/avd/ansible_collections/arista/avd/examples/single-dc-l3ls")
pytestmark = pytest.mark.skipif(not EXAMPLE.exists(), reason="AVD examples not available")


@pytest.fixture(scope="module")
def built_lab(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("topo") / "lab"
    for rel, content in render_lab(get_template("single-dc-l3ls"), {}, slug="topo").files().items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    build_lab_dir(root, log=lambda _l: None)
    return root


def test_clab_port():
    assert clab_port("Ethernet3") == "eth3"
    assert clab_port("Ethernet1/1") == "eth1_1"
    with pytest.raises(ValueError):
        clab_port("Port-Channel1")


def test_links_equal_shipped_example(built_lab):
    build = build_clab_topology(built_lab, "FABRIC", "arista/ceos:4.34.2F")
    shipped = yaml.safe_load((EXAMPLE / "clab/topology.clab.yml").read_text())["topology"]
    want = {frozenset(link["endpoints"]) for link in shipped["links"]}
    got = {frozenset(link.endpoints) for link in build.topology.topology.links}
    assert got == want
    assert len(build.topology.topology.links) == len(want) == 22  # no duplicates
    assert set(build.topology.topology.nodes) == set(shipped["nodes"])
    for name, node in shipped["nodes"].items():  # same mgmt IPs for the switches
        if name in build.mappings:
            assert build.topology.topology.nodes[name].mgmt_ipv4 == node["mgmt-ipv4"]


def test_node_files_and_roles(built_lab):
    build = build_clab_topology(built_lab, "FABRIC", "arista/ceos:4.34.2F", serials={"dc1-spine1": "ABC123"})
    out = build.write(built_lab / "clab")
    topo = yaml.safe_load(out.read_text())
    ClabTopology.from_yaml_dict(topo)  # round-trips through the model
    assert topo["topology"]["kinds"]["arista_ceos"] == {"image": "arista/ceos:4.34.2F", "enforce-startup-config": True}
    leaf = topo["topology"]["nodes"]["dc1-leaf1a"]
    assert leaf["startup-config"] == "../intended/configs/dc1-leaf1a.cfg"
    assert "ceos-config/dc1-leaf1a:/mnt/flash/ceos-config:ro" in leaf["binds"]
    assert leaf["labels"]["avdlab.role"] == "l3leaf"
    assert topo["topology"]["nodes"]["dc1-leaf1b"]["labels"]["avdlab.role"] == "l3leaf"  # not the peer type mlag_peer
    assert "exec" not in leaf and "env" not in leaf
    server = topo["topology"]["nodes"]["dc1-leaf1-server1"]
    assert server["kind"] == "linux" and server["image"] and server["exec"] == ["sh /root/setup.sh"]
    assert (built_lab / "clab/ceos-config/dc1-spine1").read_text() == "SERIALNUMBER=ABC123\n"
    assert (built_lab / "clab/ceos-config/dc1-leaf1a").read_text() == "SERIALNUMBER=TOPO0003\n"
    mapping = json.loads((built_lab / "clab/dc1-leaf1a.EosIntfMapping.json").read_text())
    assert mapping["ManagementIntf"] == {"eth0": "Management1"}
    assert mapping["EthernetIntf"]["eth1"] == "Ethernet1" and "eth5" in mapping["EthernetIntf"]
    assert "ip link set eth2 up" in (built_lab / "clab/hosts/dc1-leaf1-server1.sh").read_text()
    graph = build.graph
    assert {n.name for n in graph.nodes} == set(topo["topology"]["nodes"])
    assert graph.mgmt_subnet == "172.16.1.0/24"


def test_csv_helpers(built_lab):
    csv_path = built_lab / "documentation/fabric/FABRIC-topology.csv"
    links = read_links(csv_path)
    assert len(links) == 22
    types = read_node_types(csv_path)
    assert types["dc1-spine1"] == "spine" and types["dc1-leaf2b"] == "l3leaf" and types["dc1-leaf2c"] == "l2leaf"


async def test_topology_api_unknown_lab(client):
    assert (await client.get("/api/topology/doesnotexist")).status_code == 404
    assert (await client.post("/api/topology/doesnotexist/regenerate")).status_code == 404
