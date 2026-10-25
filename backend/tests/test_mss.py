"""Phase 4 (C) — MSS foundation: knob `segmentation` → router segment-security (real pyavd build), policy matrix and
case derivation, `lab.mss_verify` with a scripted fake SSH, API."""
from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path
from typing import Any

import pytest
import yaml

from avdlab.core.db import LabState
from avdlab.executors import mss as mss_exec
from avdlab.labs import mss
from avdlab.labs.build import build_lab_dir
from avdlab.labs.templates import KnobError, coerce_knobs, get_template, load_base, render_lab
from avdlab.topology.clients import write_plans

from .test_executors import _CREATED, _cleanup, fake_build, install, mk_host, ran, run_job, set_state  # noqa: F401
from .test_traffic import IPERF_ERR, IPERF_UDP, IPUTILS_OK, UNREACHABLE, TrafficFakeSSH

SEG: dict[str, Any] = {
    "enabled": True,
    "applications": [{"name": "WEB", "protocol": "tcp", "ports": "80,443"},
                     {"name": "PING", "protocol": "icmp"},
                     {"name": "DNS", "protocol": "udp", "ports": "53"}],
    "policies": [{"name": "WEB-ONLY", "rules": [{"application": "WEB", "action": "forward", "log": True},
                                                {"application": "PING", "action": "drop"}]}],
    "vrfs": [{"name": "VRF10", "segments": [
        {"name": "APP", "prefixes": ["10.10.11.0/24"], "policies": [{"from": "DB", "policy": "WEB-ONLY"}], "fallback": "drop"},
        {"name": "DB", "prefixes": ["10.10.12.0/24"], "policies": [{"from": "forwarding-segments", "policy": "policy-forward-all"}]},
    ]}],
}
TEMPLATES = ("single-dc-l3ls", "dual-dc-l3ls", "l2ls-fabric", "single-dc-multipod-l3ls", "custom")


def write_rendered(rendered, root: Path) -> Path:
    for rel, content in rendered.files().items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return root


# ------------------------------------------------------------------ knob
def test_knob_on_all_templates_and_normalization():
    for name in TEMPLATES:
        k = get_template(name).knob("segmentation")
        assert k is not None and k.type == "yaml" and k.default == {}, name
    tpl = get_template("single-dc-l3ls")
    assert coerce_knobs(tpl, {})["segmentation"] == mss.EMPTY
    v = coerce_knobs(tpl, {"segmentation": yaml.safe_dump(SEG)})["segmentation"]
    assert v["applications"][1] == {"name": "PING", "protocol": "icmp", "ports": None}
    assert v["vrfs"][0]["segments"][1] == {"name": "DB", "prefixes": ["10.10.12.0/24"], "interfaces": [],
                                           "policies": [{"from": "forwarding-segments", "policy": "policy-forward-all"}],
                                           "fallback": None}
    bad = [
        {"applications": [{"name": "X", "protocol": "sctp"}]},
        {"applications": [{"name": "X", "protocol": "icmp", "ports": "80"}]},
        {"applications": [{"name": "X", "protocol": "tcp", "ports": "70000"}]},
        {"policies": [{"name": "P", "rules": [{"application": "nope", "action": "forward"}]}]},
        {"vrfs": [{"name": "VRF10", "segments": [{"name": "A", "prefixes": ["10.0.0.0/8"],
                                                  "policies": [{"from": "B", "policy": "policy-drop-all"}]}]}]},
        {"vrfs": [{"name": "VRF10", "segments": [{"name": "A"}]}]},
        {"vrfs": [{"name": "VRF10", "segments": [{"name": "A", "prefixes": ["10.0.0.0/8"], "fallback": "maybe"}]}]},
        {"enabled": True},
        {"bogus": 1},
    ]
    for b in bad:
        with pytest.raises(KnobError):
            coerce_knobs(tpl, {"segmentation": b})
    with pytest.raises(KnobError) as ei:  # VRF carried by no node
        render_lab(tpl, {"segmentation": {**SEG, "vrfs": [{**SEG["vrfs"][0], "name": "VRF99"}]}})
    assert "VRF99" in str(ei.value)
    with pytest.raises(KnobError):  # l2ls: L2 spines, no L3 network services
        render_lab(get_template("l2ls-fabric"), {"segmentation": {**SEG, "vrfs": [{**SEG["vrfs"][0], "name": "default"}]}})


def test_structured_config_rendering():
    sc = mss.structured_config_for(SEG, ["VRF10"])
    rss = sc["router_segment_security"]
    assert rss["enabled"] is True
    assert rss["policies"] == [{"name": "WEB-ONLY", "sequence_numbers": [
        {"sequence": 10, "application": "WEB", "action": "forward", "stateless": True, "log": True},
        {"sequence": 20, "application": "PING", "action": "drop", "stateless": True}]}]
    assert rss["vrfs"][0]["segments"][0] == {
        "name": "APP", "definition": {"match_lists": [{"address_family": "ipv4", "prefix": "MSS-VRF10-APP"}]},
        "policies": [{"from": "DB", "policy": "WEB-ONLY"}], "fallback_policy": "policy-drop-all"}
    assert sc["match_list_input"]["prefix_ipv4"][1] == {"name": "MSS-VRF10-DB", "prefixes": ["10.10.12.0/24"]}
    atr = sc["application_traffic_recognition"]
    assert atr["field_sets"]["l4_ports"] == [{"name": "MSS-WEB", "port_values": ["80", "443"]}]
    assert atr["applications"]["l4_applications"] == [
        {"name": "WEB", "protocols": ["tcp"], "tcp_dest_port_set_name": "MSS-WEB"}, {"name": "PING", "protocols": ["icmp"]}]
    assert mss.structured_config_for(SEG, ["VRF11"]) == {}
    assert mss.structured_config_for({**SEG, "enabled": False}, ["VRF10"]) == {}
    # protocol any → built-in app-match-all, no ATR entry
    anyseg = copy.deepcopy(SEG)
    anyseg["applications"].append({"name": "ALL", "protocol": "any"})
    anyseg["policies"][0]["rules"].append({"application": "ALL", "action": "drop"})
    seqs = mss.structured_config_for(anyseg, ["VRF10"])["router_segment_security"]["policies"][0]["sequence_numbers"]
    assert seqs[-1] == {"sequence": 30, "application": "app-match-all", "action": "drop", "stateless": True}


def _tenants_vrf11_on_leaf1() -> list[dict[str, Any]]:
    base, _ = load_base(get_template("single-dc-l3ls"))
    tenants = copy.deepcopy(base["NETWORK_SERVICES"]["tenants"])
    for vrf in tenants[0]["vrfs"]:
        if vrf["name"] == "VRF11":  # no SVIs: VRF11 only exists where its routed interface is
            vrf.pop("svis")
            vrf["l3_interfaces"] = [{"interfaces": ["Ethernet10"], "ip_addresses": ["10.10.99.0/31"], "nodes": ["dc1-leaf1a"]}]
    return tenants


@pytest.fixture(scope="module")
def mss_build(tmp_path_factory) -> tuple[Path, Any]:
    seg = copy.deepcopy(SEG)
    seg["vrfs"].append({"name": "VRF11", "segments": [
        {"name": "RED", "prefixes": ["10.10.21.0/24"], "interfaces": ["Ethernet10"],
         "policies": [{"from": "BLUE", "policy": "policy-drop-all"}]},
        {"name": "BLUE", "prefixes": ["10.10.22.0/24"]}]})
    rendered = render_lab(get_template("single-dc-l3ls"), {"segmentation": seg, "tenants": _tenants_vrf11_on_leaf1()},
                          slug="mss")
    root = write_rendered(rendered, tmp_path_factory.mktemp("mss") / "lab")
    return root, build_lab_dir(root, log=lambda _l: None)


def test_real_pyavd_build_renders_only_on_vrf_carriers(mss_build):
    root, res = mss_build
    sc = res.structured_configs
    carriers = {h: [v["name"] for v in s.get("vrfs") or [] if v["name"] in ("VRF10", "VRF11")] for h, s in sc.items()}
    carriers = {h: v for h, v in carriers.items() if v}
    assert carriers == {"dc1-leaf1a": ["VRF10", "VRF11"], "dc1-leaf1b": ["VRF10"],
                        "dc1-leaf2a": ["VRF10"], "dc1-leaf2b": ["VRF10"]}  # VRF11 only has an L3 interface on leaf1a
    assert mss.rendered_on(sc) == sorted(carriers)
    for host in res.hosts:
        cfg = (root / "intended" / "configs" / f"{host}.cfg").read_text()
        assert ("router segment-security" in cfg) == (host in carriers), host
        rss_vrfs = [v["name"] for v in (sc[host].get("router_segment_security") or {}).get("vrfs") or []]
        assert rss_vrfs == carriers.get(host, []), host  # only the VRFs the node carries
    cfg = (root / "intended" / "configs" / "dc1-leaf1a.cfg").read_text()
    for line in ("router segment-security\n   no shutdown", "   policy WEB-ONLY\n      10 application WEB action forward stateless log",
                 "      20 application PING action drop stateless", "   vrf VRF10\n      segment APP\n         definition\n"
                 "            match prefix-ipv4 MSS-VRF10-APP", "            from DB policy WEB-ONLY\n            fallback policy policy-drop-all",
                 "            from forwarding-segments policy policy-forward-all", "            match interface Ethernet10",
                 "            from BLUE policy policy-drop-all", "match-list input prefix-ipv4 MSS-VRF10-APP\n   match prefix-ipv4 10.10.11.0/24",
                 "application traffic recognition", "   application l4 WEB\n      protocol tcp destination port field-set MSS-WEB",
                 "   field-set l4-port MSS-WEB\n      80, 443"):
        assert line in cfg, line
    assert "application l4 DNS" not in cfg  # not referenced by any rendered policy
    leaf2 = (root / "intended" / "configs" / "dc1-leaf1b.cfg").read_text()
    assert "vrf VRF11" not in leaf2.split("router segment-security")[1].split("\n!\n")[0]
    # the pure carrier computation agrees with pyavd
    hv = render_lab(get_template("single-dc-l3ls"), {"tenants": _tenants_vrf11_on_leaf1()}, slug="mss").hostvars()
    assert mss.vrf_carriers(hv, ["VRF10", "VRF11"]) == carriers
    assert mss.carriers_from_structured(sc, ["VRF10", "VRF11"]) == carriers


def test_post_build_injection_for_custom_labs(tmp_path):
    root = write_rendered(render_lab(get_template("single-dc-l3ls"), {}, slug="cust"), tmp_path / "lab")
    res = build_lab_dir(root, log=lambda _l: None, segmentation=SEG)
    assert mss.rendered_on(res.structured_configs) == ["dc1-leaf1a", "dc1-leaf1b", "dc1-leaf2a", "dc1-leaf2b"]
    cfg = (root / "intended" / "configs" / "dc1-leaf2b.cfg").read_text()
    assert "router segment-security" in cfg and "match-list input prefix-ipv4 MSS-VRF10-DB" in cfg
    assert "router segment-security" not in (root / "intended" / "configs" / "dc1-spine1.cfg").read_text()
    written = yaml.safe_load((root / "intended" / "structured_configs" / "dc1-leaf2b.yml").read_text())
    assert written["router_segment_security"]["vrfs"][0]["name"] == "VRF10"


# ------------------------------------------------------------------ matrix + derivation
def test_policy_matrix_and_decisions():
    seg = mss.normalize_segmentation(SEG)
    assert mss.policy_matrix(seg) == {"VRF10": {"APP": {"APP": "policy-drop-all", "DB": "policy-forward-all"},
                                                "DB": {"APP": "WEB-ONLY", "DB": "policy-forward-all"}}}
    assert mss.decide(seg, "WEB-ONLY", "WEB") == "forward"
    assert mss.decide(seg, "WEB-ONLY", "PING") == "drop"
    assert mss.decide(seg, "WEB-ONLY", "DNS") == "drop"  # no matching rule
    assert mss.decide(seg, "policy-forward-all", "DNS") == "forward"
    assert mss.decide(seg, None, "WEB") is None
    assert mss.segment_of(seg, "VRF10", "10.10.12.7/24") == "DB" and mss.segment_of(seg, "VRF11", "10.10.12.7") is None
    summary = mss.mss_summary(SEG, {"a": {"router_segment_security": {"vrfs": [{"name": "VRF10"}]}}, "b": {}})
    assert set(summary) == {"enabled", "applications", "policies", "vrfs", "matrix", "rendered_on"}
    assert summary["rendered_on"] == ["a"]


def _plan(name: str, vid: int, octet: int, *, vrf: str = "VRF10", extra: list[int] = ()) -> dict[str, Any]:
    vlans = [{"vid": vid, "iface": "eth1", "ip": f"10.10.{vid}.{octet}/24", "gateway": f"10.10.{vid}.1",
              "default_route": True, "tenant": "TENANT1", "vrf": vrf}]
    for x in extra:
        vlans.append({"vid": x, "iface": f"eth1.{x}", "ip": f"10.10.{x}.{octet}/24", "gateway": f"10.10.{x}.1",
                      "default_route": False, "tenant": "TENANT1", "vrf": "VRF10"})
    return {"name": name, "image": "mt", "links": [{"eth": "eth1", "switch": "dc1-leaf1a", "switch_port": "Ethernet5"}],
            "bond": None, "bonds": [], "vlans": vlans}


PLANS = [_plan("app1", 11, 100), _plan("db1", 12, 101), _plan("web2", 11, 102), _plan("red1", 21, 103, vrf="VRF11"),
         _plan("both", 11, 104, extra=[12])]


def test_derive_cases():
    cases = mss_exec.derive_cases(SEG, PLANS)
    key = [(c["src"], c["dst"], c["application"], c["expect"], c["vlan"]) for c in cases]
    assert key == [
        ("app1", "db1", "WEB", "allow", 12), ("app1", "db1", "PING", "allow", 12), ("app1", "db1", "DNS", "allow", 12),
        ("db1", "app1", "WEB", "allow", 11), ("db1", "app1", "PING", "deny", 11), ("db1", "app1", "DNS", "deny", 11),
        ("db1", "web2", "WEB", "allow", 11), ("db1", "web2", "PING", "deny", 11), ("db1", "web2", "DNS", "deny", 11),
        ("web2", "db1", "WEB", "allow", 12), ("web2", "db1", "PING", "allow", 12), ("web2", "db1", "DNS", "allow", 12),
    ]  # app1<->web2 bridged, red1 other VRF without segments, `both` sits in VLAN 11 and 12 (asymmetric: skipped)
    a = mss_exec.annotate(mss.normalize_segmentation(SEG), PLANS, cases[3])
    assert (a["vrf"], a["src_segment"], a["dst_segment"], a["policy"], a["target_ip"]) == \
        ("VRF10", "DB", "APP", "WEB-ONLY", "10.10.11.100")
    assert mss_exec.app_port({"ports": "8000-8080,9000"}) == 8000 and mss_exec.app_port({"ports": None}) == 5201


# ------------------------------------------------------------------ verify job + API
class MssFakeSSH(TrafficFakeSSH):
    def __init__(self, lab_name: str) -> None:
        super().__init__(lab_name)
        mine = {"pgrep -f": (0, ""), "nc -z": self._nc, "ping -c": self._ping2, "iperf3 -c": self._udp}
        self.responses = {**mine, **{k: v for k, v in self.responses.items() if k not in mine}}

    def _ping2(self, fake, cmd: str) -> tuple[int, str]:
        return (1, UNREACHABLE) if cmd.startswith("docker exec db1 ") else (0, IPUTILS_OK)

    def _nc(self, fake, cmd: str) -> tuple[int, str]:
        if cmd.startswith("docker exec web2 "):
            return 126, "OCI runtime exec failed: exec: \"nc\": executable file not found"
        return 0, ""

    def _udp(self, fake, cmd: str) -> tuple[int, str]:
        return (1, json.dumps(IPERF_ERR)) if cmd.startswith("docker exec db1 ") else (0, json.dumps(IPERF_UDP))


async def _mss_lab(client, *, knobs: dict[str, Any] | None = None, state: LabState = LabState.deployed) -> dict[str, Any]:
    host_id = await mk_host()
    r = await client.post("/api/labs", json={"name": f"mss-{uuid.uuid4().hex[:6]}", "template": "single-dc-l3ls",
                                             "host_id": host_id, "knobs": {"segmentation": SEG, **(knobs or {})}})
    assert r.status_code == 201, r.text
    lab = r.json()
    _CREATED["labs"].append(lab["id"])
    fake_build(Path(lab["dir"]), lab["slug"])
    await set_state(lab["id"], state)
    write_plans(Path(lab["dir"]) / "clab", PLANS[:3])
    return lab


async def test_get_mss_and_knob_validation(client):
    lab = await _mss_lab(client, state=LabState.built)
    assert lab["knobs"]["segmentation"]["enabled"] is True
    sc = Path(lab["dir"]) / "intended" / "structured_configs" / "dc1-leaf1a.yml"
    sc.write_text(yaml.safe_dump({"hostname": "dc1-leaf1a", **mss.structured_config_for(SEG, ["VRF10"])}))
    body = (await client.get(f"/api/labs/{lab['id']}/mss")).json()
    assert body["enabled"] is True and [a["name"] for a in body["applications"]] == ["WEB", "PING", "DNS"]
    assert body["matrix"]["VRF10"]["DB"]["APP"] == "WEB-ONLY" and body["rendered_on"] == ["dc1-leaf1a"]
    assert body["last_verify"] is None and body["policies"][0]["name"] == "WEB-ONLY"
    r = await client.patch(f"/api/labs/{lab['id']}", json={"knobs": {"segmentation": {"enabled": True, "vrfs": [
        {"name": "VRF77", "segments": [{"name": "A", "prefixes": ["10.0.0.0/8"]}]}]}}})
    assert r.status_code == 422 and "VRF77" in r.text
    r = await client.patch(f"/api/labs/{lab['id']}", json={"knobs": {"segmentation": {}}})
    assert r.status_code == 200, r.text
    body = (await client.get(f"/api/labs/{lab['id']}/mss")).json()
    assert body["enabled"] is False and body["matrix"] == {}
    assert (await client.get("/api/labs/nope/mss")).status_code == 404
    # verify preconditions: not deployed
    assert (await client.post(f"/api/labs/{lab['id']}/mss/verify", json={"cases": None})).status_code == 409


async def test_verify_job_derived_and_explicit(client, monkeypatch):
    lab = await _mss_lab(client)
    fake = MssFakeSSH(lab["slug"])
    install(monkeypatch, fake)
    r = await client.post(f"/api/labs/{lab['id']}/mss/verify", json={"cases": None})
    assert r.status_code == 202, r.text
    res = (await run_job(client, r.json()["job_id"]))["result"]
    got = [(c["src"], c["dst"], c["application"], c["expect"], c["observed"], c["pass"]) for c in res["cases"]]
    assert got == [
        ("app1", "db1", "WEB", "allow", "allow", True), ("app1", "db1", "PING", "allow", "allow", True),
        ("app1", "db1", "DNS", "allow", "allow", True),
        ("db1", "app1", "WEB", "allow", "allow", True), ("db1", "app1", "PING", "deny", "deny", True),
        ("db1", "app1", "DNS", "deny", "deny", True),
        ("db1", "web2", "WEB", "allow", "allow", True), ("db1", "web2", "PING", "deny", "deny", True),
        ("db1", "web2", "DNS", "deny", "deny", True),
        ("web2", "db1", "WEB", "allow", "error", False), ("web2", "db1", "PING", "allow", "allow", True),
        ("web2", "db1", "DNS", "allow", "allow", True)]
    assert (res["passed"], res["failed"]) == (11, 1)
    c = res["cases"][4]
    assert (c["vrf"], c["src_segment"], c["dst_segment"], c["policy"], c["target_ip"]) == \
        ("VRF10", "DB", "APP", "WEB-ONLY", "10.10.11.100")
    assert ran(fake, "nc -z")[0][0] == "docker exec app1 nc -z -w2 10.10.12.101 80"
    assert ran(fake, "pgrep -f")[0][0] == "docker exec db1 sh -c 'pgrep -f '\"'\"'iperf3 -s -p 80'\"'\"' >/dev/null || iperf3 -s -p 80 -D'"
    assert "docker exec app1 iperf3 -c 10.10.12.101 -u -p 53 -t 1 -b 1M -J" in [x for x, _ in ran(fake, "iperf3 -c")]

    # explicit cases, one with the wrong expectation
    r = await client.post(f"/api/labs/{lab['id']}/mss/verify", json={"cases": [
        {"src": "db1", "dst": "app1", "application": "PING", "expect": "allow"},
        {"src": "app1", "dst": "db1", "application": "app-match-all", "expect": "allow"}]})
    res2 = (await run_job(client, r.json()["job_id"]))["result"]
    assert [(c["observed"], c["pass"]) for c in res2["cases"]] == [("deny", False), ("allow", True)]
    runs = (await client.get(f"/api/labs/{lab['id']}/traffic/runs")).json()
    assert [x["kind"] for x in runs[:2]] == ["mss", "mss"] and runs[0]["summary"] == {"total": 2, "ok": 1}
    full = (await client.get(f"/api/labs/{lab['id']}/traffic/runs/{res2['run_id']}")).json()
    assert full["params"]["derived"] is False and len(full["results"]) == 2
    body = (await client.get(f"/api/labs/{lab['id']}/mss")).json()
    assert body["last_verify"]["run_id"] == res2["run_id"] and body["last_verify"]["failed"] == 1

    # validation
    r = await client.post(f"/api/labs/{lab['id']}/mss/verify", json={"cases": [
        {"src": "ghost", "dst": "app1", "application": "PING", "expect": "allow"}]})
    assert r.status_code == 422 and "ghost" in r.text
    r = await client.post(f"/api/labs/{lab['id']}/mss/verify", json={"cases": [
        {"src": "db1", "dst": "app1", "application": "SSH", "expect": "allow"}]})
    assert r.status_code == 422 and "SSH" in r.text
    r = await client.post(f"/api/labs/{lab['id']}/mss/verify", json={"cases": [
        {"src": "db1", "dst": "app1", "application": "PING", "expect": "maybe"}]})
    assert r.status_code == 422


async def test_verify_no_derivable_cases(client, monkeypatch):
    install(monkeypatch, MssFakeSSH("x"))
    lab = await _mss_lab(client)
    write_plans(Path(lab["dir"]) / "clab", [PLANS[0], PLANS[2]])  # same subnet → bridged
    r = await client.post(f"/api/labs/{lab['id']}/mss/verify", json={})
    assert r.status_code == 409 and "derived" in r.text
