"""Phase 6 (backend B): in-app learning — schema browser, knob help, config explainer, lessons/checkpoints, glossary, tours."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

from avdlab.jobs.engine import read_log, runner
from avdlab.labs.build import build_lab_dir
from avdlab.labs.templates import get_template, load_templates, render_lab
from avdlab.learn import schema
from avdlab.learn.explain import explain_file, explain_lines, parse_config
from avdlab.learn.knobs import LEARN_FIELDS, catalog, insert_learn_blocks, raw_knob_learn
from avdlab.learn.lessons import (CHECK_TYPES, RUN_JOBS, TRY_ACTIONS, CheckContext, LessonError, evaluate, load_lessons,
                                  parse_lesson)

SINGLE_DC_KNOBS = {k.key for k in get_template("single-dc-l3ls").knobs}


@pytest.fixture(scope="module")
def built(tmp_path_factory) -> Path:
    """The default single-dc-l3ls lab, rendered + built with pyavd (no API)."""
    root = tmp_path_factory.mktemp("learn") / "lab"
    for rel, content in render_lab(get_template("single-dc-l3ls"), {}, slug="learn").files().items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    build_lab_dir(root, log=lambda _l: None)
    return root


def _explain(root: Path, host: str, lines: list[str]) -> list[dict]:
    return explain_lines(root, host, lines, template="single-dc-l3ls", template_knobs=SINGLE_DC_KNOBS)


# ------------------------------------------------------------------------------------------------ schema
def test_schema_lookup_eos_designs_node_type_key():
    e = schema.lookup("eos_designs", "l3leaf.defaults.loopback_ipv4_pool")
    assert e["type"] == "str" and e["format"] == "ipv4_pool"
    assert "Loopback0" in e["description"]
    assert e["table"] == "node-type-loopback-vtep-configuration"
    assert e["doc_url"] == ("https://avd.arista.com/6.4/ansible_collections/arista/avd/roles/eos_designs/docs/tables/"
                            "node-type-loopback-vtep-configuration.html")
    assert e["section_url"].endswith("data-models.html#node-type-loopback-and-vtep-configuration")
    assert e["alias_of"] == "<node_type_keys.key>.defaults.loopback_ipv4_pool"
    # list indices / [] / [name=..] spellings are normalised
    assert schema.lookup("eos_designs", "l3leaf.node_groups[0].nodes[].id")["type"] == "int"
    svis = schema.lookup("eos_designs", "tenants[name=TENANT1].vrfs[].svis")
    assert svis["type"] == "list" and svis["items"]["primary_key"] == "id"
    assert {"id", "name", "ip_address_virtual", "enabled"} <= {k["name"] for k in svis["keys"]}
    dep = schema.lookup("eos_designs", "design")
    assert dep["deprecation"]["removed"] is True


def test_schema_lookup_eos_cli_config_gen_peer_groups():
    e = schema.lookup("eos_cli_config_gen", "router_bgp.peer_groups")
    assert e["type"] == "list" and e["items"] == {**e["items"], "type": "dict", "primary_key": "name"}
    names = {k["name"] for k in e["keys"]}
    assert {"name", "remote_as", "update_source", "ebgp_multihop", "next_hop_unchanged", "bfd"} <= names
    assert e["doc_url"].endswith("/eos_cli_config_gen/docs/tables/router-bgp.html")
    assert schema.lookup("eos_cli_config_gen", "router_bgp.peer_groups.remote_as")["type"] == "str"


def test_schema_errors_and_search():
    with pytest.raises(schema.SchemaKeyError) as exc:
        schema.lookup("eos_designs", "l3leaf.defaults.loopbak_ipv4_pool")
    assert "l3leaf.defaults.loopback_ipv4_pool" in exc.value.suggestions or exc.value.suggestions
    hits = [h["key"] for h in schema.search("eos_designs", "loopback_ipv4_pool")]
    assert hits[0] == "l3leaf.defaults.loopback_ipv4_pool"
    assert schema.search("eos_designs", "mlag peer pool")[0]["key"].startswith("l3leaf.defaults.mlag_peer")
    assert schema.search("eos_cli_config_gen", "peer_groups")[0]["key"] == "router_bgp.peer_groups"
    root = schema.lookup("eos_designs", "")
    assert "fabric_name" in {k["name"] for k in root["keys"]}


def test_schema_export_is_vendored_and_reproducible(tmp_path):
    """The committed JSON works without the AVD repo; the export script regenerates the same keys from the checkout."""
    doc = json.loads((schema.SCHEMA_DIR / "eos_designs.json").read_text())
    assert doc["avd_version"].startswith("6.4") and len(doc["nodes"]) > 5000
    repo = Path("/home/claude/arista/avd")
    if not repo.is_dir():
        pytest.skip("AVD repo checkout not available")
    import importlib.util

    spec = importlib.util.spec_from_file_location("export_schema", Path(__file__).resolve().parents[2] / "scripts" / "export_schema.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    sizes = mod.export(repo, tmp_path)
    assert sizes["eos_designs"] == len(doc["nodes"])
    fresh = json.loads((tmp_path / "eos_cli_config_gen.json").read_text())
    assert fresh["nodes"]["router_bgp.peer_groups"] == json.loads(
        (schema.SCHEMA_DIR / "eos_cli_config_gen.json").read_text())["nodes"]["router_bgp.peer_groups"]


# ------------------------------------------------------------------------------------------------ knob help
def test_every_template_knob_has_a_learn_block():
    for name, tpl in load_templates().items():
        raw = raw_knob_learn(tpl.dir)
        for knob in tpl.knobs:
            block = raw.get(knob.key)
            assert block, f"{name}: knob {knob.key} has no learn block in template.yml"
            assert set(LEARN_FIELDS) <= set(block), (name, knob.key)
            assert block["summary"] and block["renders"] and block["doc_url"].startswith("https://"), (name, knob.key)
            assert isinstance(block["avd_keys"], list) and isinstance(block["pitfalls"], list)
    # the catalogue covers every knob key in use (fallback for new templates)
    keys = {k.key for t in load_templates().values() for k in t.knobs}
    assert keys <= set(catalog())


def test_knob_learn_avd_keys_exist_in_schema():
    """avd_keys are real eos_designs keys (placeholders like <node_type> resolved to l3leaf)."""
    for key, entry in catalog().items():
        for k in entry["avd_keys"]:
            path = k.replace("<node_type>", "l3leaf").replace("<peer group>", "evpn_overlay_peers")
            path = re.sub(r"\.structured_config\..*$", ".structured_config", path)
            path = re.sub(r"aaa_settings\.local_users.*$", "aaa_settings.local_users", path)
            if path in ("ansible_password",):
                continue
            schema.lookup("eos_designs", path)  # raises if unknown


def test_insert_learn_blocks_preserves_descriptor():
    text = "---\nname: t\nknobs:\n  - key: mgmt_gateway   # comment\n    type: ipv4\n\n  - key: bogus\n    type: string\nother: 1\n"
    new, written = insert_learn_blocks(text, "static")
    assert written == ["mgmt_gateway"]
    data = yaml.safe_load(new)
    assert data["other"] == 1 and data["knobs"][0]["learn"]["avd_keys"] == ["mgmt_gateway"]
    assert "# comment" in new and "learn" not in data["knobs"][1]
    assert insert_learn_blocks(new, "static")[1] == []  # idempotent


# ------------------------------------------------------------------------------------------------ explainer
def test_parse_config_sections():
    lines = parse_config("hostname x\n!\nrouter bgp 1\n   vlan 11\n      rd 1.1.1.1:11\n!\nend\n")
    rd = lines[4]
    assert rd.stripped == "rd 1.1.1.1:11" and lines[rd.section - 1].stripped == "router bgp 1"
    assert lines[rd.parent - 1].stripped == "vlan 11"


# (line, host, substrings the explanation must contain) — values come from the lab, not from the rules
EXPECTED = [
    ("hostname dc1-leaf1a", "dc1-leaf1a", ["dc1-leaf1a", "l3leaf"]),
    ("interface Loopback0", "dc1-leaf1a", ["10.255.0.3", "10.255.0.0/27", "id 1", "loopback_ipv4_offset 2"]),
    ("interface Loopback1", "dc1-leaf1b", ["10.255.1.3", "10.255.1.0/27", "primary id 1", "dc1-leaf1a"]),
    ("interface Loopback10", "dc1-leaf1a", ["VRF10", "10.255.10.3", "10.255.10.0/27"]),
    ("interface Ethernet1", "dc1-leaf1a", ["10.255.255.0/26", "#0", "10.255.255.1", "dc1-spine1"]),
    ("   ip address 10.255.255.7/31", "dc1-leaf1b", ["#3", "(id 2 - 1)", "10.255.255.6", "uplink index 1"]),
    ("interface Ethernet3", "dc1-spine1", ["dc1-leaf2a", "#4", "10.255.255.8", "parent"]),
    ("interface Vlan4094", "dc1-leaf1b", ["10.255.1.65", "10.255.1.64/27", "primary id 1"]),
    ("interface Vlan4093", "dc1-leaf1a", ["10.255.1.96", "mlag_peer_l3_ipv4_pool"]),
    ("vlan 3009", "dc1-leaf1a", ["3000", "vrf_id (10)", "3009"]),
    ("   domain-id DC1_L3_LEAF1", "dc1-leaf1a", ["DC1_L3_LEAF1"]),
    ("   peer-address 10.255.1.65", "dc1-leaf1a", ["dc1-leaf1b"]),
    ("interface Port-Channel3", "dc1-leaf1a", ["peer-link", "dc1-leaf1b", "DC1_L3_LEAF1"]),
    ("interface Port-Channel5", "dc1-leaf1a", ["dc1-leaf1-server1", "Port-Channel5", "mlag 5"]),
    ("   vxlan vlan 11 vni 10011", "dc1-leaf1a", ["10011", "mac_vrf_vni_base 10000", "TENANT1"]),
    ("   vxlan vrf VRF10 vni 10", "dc1-leaf1a", ["VRF10", "vrf_vni"]),
    ("      rd 10.255.0.3:10011", "dc1-leaf1a", ["10.255.0.3:10011", "10011:10011"]),
    ("      rd 10.255.0.3:10", "dc1-leaf1a", ["VRF10", "10.255.0.3:10", "10:10"]),
    ("router bgp 65101", "dc1-leaf1a", ["65101", "bgp_as"]),
    ("router bgp 65100", "dc1-spine1", ["65100", "spine.defaults.bgp_as"]),
    ("   neighbor 10.255.0.1 peer group EVPN-OVERLAY-PEERS", "dc1-leaf1a", ["dc1-spine1", "route server", "uplink_switches"]),
    ("   neighbor 10.255.0.5 peer group EVPN-OVERLAY-PEERS", "dc1-spine1", ["dc1-leaf2a", "evpn_role: server"]),
    ("   neighbor EVPN-OVERLAY-PEERS next-hop-unchanged", "dc1-spine1", ["next-hop-unchanged"]),
    ("   neighbor 10.255.255.0 peer group IPv4-UNDERLAY-PEERS", "dc1-leaf1a", ["dc1-spine1", "10.255.255.0"]),
    ("   neighbor 10.255.1.97 peer group MLAG-IPv4-UNDERLAY-PEER", "dc1-leaf1a", ["dc1-leaf1b", "10.255.1.96"]),
    ("interface Vlan11", "dc1-leaf1a", ["VRF10", "ip address virtual", "00:1c:73:00:00:99"]),
    ("ip virtual-router mac-address 00:1c:73:00:00:99", "dc1-leaf1a", ["virtual_router_mac_address"]),
    ("interface Management1", "dc1-leaf1a", ["172.16.1.101/24"]),
    ("ip route vrf MGMT 0.0.0.0/0 172.16.1.1", "dc1-leaf1a", ["172.16.1.1", "mgmt_gateway"]),
    ("ip name-server vrf MGMT 192.168.1.1", "dc1-leaf1a", ["192.168.1.1", "dns_servers"]),
    ("management api http-commands", "dc1-leaf1a", ["management_eapi.enabled"]),
    ("service routing protocols model multi-agent", "dc1-leaf1a", ["multi-agent"]),
    ("vlan internal order ascending range 1006 1199", "dc1-leaf1a", ["1006", "1199"]),
    ("spanning-tree mst 0 priority 4096", "dc1-leaf1a", ["4096", "spanning_tree_priority"]),
    ("   seq 10 permit 10.255.0.0/27 eq 32", "dc1-leaf1a", ["10.255.0.0/27", "loopback_ipv4_pool"]),
    ("route-map RM-CONN-2-BGP permit 10", "dc1-leaf1a", ["PL-LOOPBACKS-EVPN-OVERLAY"]),
    ("ip address virtual source-nat vrf VRF10 address 10.255.10.3", "dc1-leaf1a", ["10.255.10.3", "VRF10"]),
    ("   maximum-paths 4", "dc1-leaf1a", ["4", "bgp_maximum_paths"]),
    ("interface Vxlan1", "dc1-leaf1a", ["VTEP"]),
    ("   reload-delay mlag 300", "dc1-leaf1a", ["300", "cEOSLab"]),
]


def test_explain_rules_on_single_dc_build(built):
    by_host: dict[str, list[tuple[str, list[str]]]] = {}
    for line, host, must in EXPECTED:
        by_host.setdefault(host, []).append((line, must))
    explained = set()
    for host, items in by_host.items():
        res = _explain(built, host, [line for line, _ in items])
        for (line, must), r in zip(items, res):
            assert r["why"], f"{host}: no explanation for {line!r}"
            for s in must:
                assert s in r["why"] or s in json.dumps(r["inputs"]), f"{host} {line!r}: {s!r} not in {r['why']!r}"
            assert r["section"] and r["rule"]
            explained.add((host, r["line"]))
    assert len(explained) >= 20
    # inputs carry key / value / where (+ knob when the template exposes it)
    lo0 = _explain(built, "dc1-leaf1a", ["interface Loopback0"])[0]
    assert lo0["avd_input"] == {"key": "l3leaf.defaults.loopback_ipv4_pool", "value": "10.255.0.0/27",
                                "where": "group_vars/DC1_L3_LEAVES.yml", "knob": "pools"}
    assert lo0["doc_url"].endswith("node-type-loopback-vtep-configuration.html")
    assert any("10.255.0.3" in r["text"] for r in lo0["related_lines"])


def test_explain_whole_files_and_unknown_lines(built):
    for host in ("dc1-leaf1a", "dc1-spine1", "dc1-leaf1c"):
        res = explain_file(built, host, template="single-dc-l3ls", template_knobs=SINGLE_DC_KNOBS)
        assert res["explained"] == res["total"], [r["line"] for r in res["lines"] if not r["why"]]
        assert all(s["lines"] >= s["explained"] for s in res["sections"])
    sections = {s["section"]: s for s in explain_file(built, "dc1-leaf1a")["sections"]}
    assert sections["router bgp 65101"]["explained"] > 50 and "overlay" in sections["router bgp 65101"]["topics"]
    unknown = _explain(built, "dc1-leaf1a", ["this is not a line", "!"])
    assert unknown[0]["why"] is None and unknown[0]["error"]
    assert unknown[1]["why"] is None
    # line numbers work too
    text = (built / "intended/configs/dc1-leaf1a.cfg").read_text().splitlines()
    n = text.index("interface Loopback0") + 1
    assert _explain(built, "dc1-leaf1a", [n])[0]["rule"] == "loopback0"


def test_explain_follows_knob_changes(tmp_path):
    """Values come from the lab: a different loopback pool / ASN shows up in the explanation."""
    root = tmp_path / "lab"
    files = render_lab(get_template("single-dc-l3ls"), {"pools": {"loopback": "10.250.0.0/26"}, "leaf_asn_base": 65201},
                       slug="knobs").files()
    for rel, content in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(content)
    build_lab_dir(root, log=lambda _l: None)
    lo0, bgp = _explain(root, "dc1-leaf2b", ["interface Loopback0", "router bgp 65202"])
    assert "10.250.0.6" in lo0["why"] and "10.250.0.0/26" in lo0["why"] and "id 4" in lo0["why"]
    assert "65202" in bgp["why"]


# ------------------------------------------------------------------------------------------------ lessons
def test_lessons_parse_and_are_well_formed():
    lessons = load_lessons()
    assert len(lessons) >= 12
    levels = {lesson.meta["level"] for lesson in lessons.values()}
    assert levels == {"intro", "core", "advanced"}
    for lesson in lessons.values():
        kinds = [b["kind"] for b in lesson.blocks]
        assert 2 <= kinds.count("try") <= 4, (lesson.id, kinds.count("try"))
        assert kinds.count("check") >= 2, lesson.id
        assert 1 <= kinds.count("quiz") <= 2, lesson.id
        for b in lesson.blocks:
            if b["kind"] == "try":
                assert b["action"] in TRY_ACTIONS and b["label"]
                if b["action"] == "run_job":
                    assert b["endpoint"] == RUN_JOBS[b["args"]["job"]]
            elif b["kind"] == "check":
                assert b["type"] in CHECK_TYPES and b["label"]
            elif b["kind"] == "quiz":
                assert 0 <= b["answer"] < len(b["options"]) and b["explanation"]
        last = next(b for b in reversed(lesson.blocks) if b["kind"] == "markdown")
        assert "Takeaway" in last["text"], lesson.id
        assert lesson.meta["minutes"] > 0 and lesson.meta["title"]


def test_lesson_parser_rejects_bad_blocks():
    good = "---\nid: x\ntitle: X\nlevel: intro\nminutes: 1\n---\ntext\n"
    assert parse_lesson(good).blocks[0] == {"kind": "markdown", "text": "text"}
    for bad in (":::try\nid: a\naction: fly\n:::\n", ":::check\nid: c\ntype: config_contains\nargs: {node: x}\n:::\n",
                ":::quiz\nid: q\nquestion: Q\noptions: [a]\nanswer: 0\nexplanation: e\n:::\n",
                ":::try\nid: a\naction: run_job\nargs: {job: lab.nope}\n:::\n", ":::check\nid: c\n", ":::quiz\nid: q\nquestion: ?\n:::\n"):
        with pytest.raises(LessonError):
            parse_lesson(good + bad)


def test_lesson_file_references_exist_in_a_built_lab(built):
    """open_file tries point at files of the default lab; lines named in them exist (except ones a lesson adds)."""
    added_by_lesson = {"banner motd", "daemon TerminAttr"}
    for lesson in load_lessons().values():
        for b in lesson.blocks:
            if b["kind"] != "try" or b["action"] != "open_file" or b["args"]["path"].startswith("clab/"):
                continue
            path = built / b["args"]["path"]
            assert path.is_file(), (lesson.id, b["args"]["path"])
            line = b["args"].get("line")
            if line and line.strip() not in added_by_lesson:
                assert line.strip() in [x.strip() for x in path.read_text().splitlines()], (lesson.id, line)
                assert _explain(built, path.stem, [line])[0]["why"], (lesson.id, line)


def test_checkpoints_evaluate_against_built_lab(built):
    ctx = CheckContext(lab_dir=built, knobs={"cloudvision": {"mode": "none"}, "provisioning": {"mode": "startup-config"},
                                             "segmentation": {}},
                       jobs=[{"type": "lab.build", "status": "succeeded"}], validation=None)
    results = {(lesson.id, b["id"]): evaluate(b, ctx) for lesson in load_lessons().values()
               for b in lesson.blocks if b["kind"] == "check"}
    must_pass = {("what-is-avd", "structured-exists"), ("what-is-avd", "loopback0-rendered"),
                 ("inventory-group-vars", "fabric-name"), ("inventory-group-vars", "route-rendered"),
                 ("node-types-ids", "leaf2a-id"), ("node-types-ids", "l2leaf-no-routing"),
                 ("pools-ip-math", "lo0-leaf1b"), ("pools-ip-math", "shared-vtep"), ("pools-ip-math", "p2p-leaf1b"),
                 ("mlag", "domain"), ("mlag", "peer-address"), ("mlag", "mlag-po"),
                 ("bgp-underlay-overlay", "nhu"), ("bgp-underlay-overlay", "leaf-rs"),
                 ("tenants-vrfs-vnis", "l2vni"), ("tenants-vrfs-vnis", "anycast"), ("tenants-vrfs-vnis", "vrf-rd"),
                 ("connected-endpoints", "bond"), ("connected-endpoints", "ilo"),
                 ("generated-documentation", "p2p-row"), ("generated-documentation", "build-ok"),
                 ("anta-validation", "catalog-exists"), ("cloudvision", "ntp"), ("segmentation", "spine-clean")}
    for key in must_pass:
        assert results[key][0], (key, results[key][1])
    must_fail = {("escape-hatches", "banner"), ("anta-validation", "validated"), ("anta-validation", "clean-report"),
                 ("cloudvision", "cv-mode"), ("cloudvision", "terminattr"), ("ztp", "ztp-mode"),
                 ("segmentation", "seg-enabled"), ("segmentation", "seg-rendered")}
    for key in must_fail:
        assert not results[key][0], key
    assert "no ANTA validation" in results[("anta-validation", "clean-report")][1]
    # anta_status reads totals and the JSON report
    report = built / "anta" / "reports" / "run.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps([{"name": "dc1-leaf1a", "test": "VerifyBGPPeerSession", "result": "success"},
                                  {"name": "dc1-leaf1b", "test": "VerifyBGPPeerSession", "result": "failure"}]))
    ctx.validation = {"totals": {"success": 1, "failure": 1, "error": 0}, "report_path": "anta/reports/run.json"}
    cp = next(b for b in load_lessons()["bgp-underlay-overlay"].blocks if b.get("id") == "bgp-anta")
    assert evaluate(cp, ctx)[0] is True
    assert evaluate({"type": "anta_status", "args": {}}, ctx)[0] is False
    assert evaluate({"type": "anta_status", "args": {"test": "VerifyBGPPeerSession"}}, ctx) == (False, evaluate(
        {"type": "anta_status", "args": {"test": "VerifyBGPPeerSession"}}, ctx)[1])
    with pytest.raises(LessonError):
        evaluate({"type": "file_exists", "args": {"path": "../../etc/passwd"}}, ctx)


# ------------------------------------------------------------------------------------------------ glossary / tours
def test_glossary_and_tours_load():
    from avdlab.learn.api import load_glossary, load_tours

    terms = load_glossary()
    assert len(terms) >= 40
    names = {t["term"] for t in terms}
    assert {"VTEP", "VNI", "RD", "RT", "MLAG", "ESI", "EVPN route types", "underlay", "overlay", "route server", "iBGP",
            "eBGP", "VARP", "SVI", "L2VNI", "L3VNI", "ZTP", "TerminAttr", "structured config", "eos_designs",
            "eos_cli_config_gen", "node_type_keys", "pool"} <= names
    lesson_ids = set(load_lessons())
    for t in terms:
        assert t["short"] and (t["lesson"] is None or t["lesson"] in lesson_ids), t["term"]
        assert all(s in names for s in t["see"]), t["term"]
    tours = load_tours()
    assert {"first-lab", "hosts", "topology-editor", "validation", "cloudvision"} <= set(tours)
    for tour in tours.values():
        assert tour["steps"] and all(re.match(r'^\[data-tour="[\w-]+"\]$', s["selector"]) and s["text"] for s in tour["steps"])


# ------------------------------------------------------------------------------------------------ API
async def _wait(client, job_id: str) -> dict:
    await runner.wait(job_id)
    job = (await client.get(f"/api/jobs/{job_id}")).json()
    assert job["status"] == "succeeded", (job["error"], read_log(job_id)[-2000:])
    return job


async def test_learn_api_flow(client):
    # templates expose knob learn blocks
    tpls = {t["name"]: t for t in (await client.get("/api/labs/templates")).json()}
    for t in tpls.values():
        for k in t["knobs"]:
            assert k["learn"] and k["learn"]["summary"] and isinstance(k["learn"]["avd_keys"], list), (t["name"], k["key"])
    pools = next(k for k in tpls["single-dc-l3ls"]["knobs"] if k["key"] == "pools")
    assert "l3leaf.defaults.vtep_loopback_ipv4_pool" in pools["learn"]["avd_keys"]

    # schema
    r = await client.get("/api/learn/schema/eos_designs", params={"key": "l3leaf.defaults.loopback_ipv4_pool"})
    assert r.status_code == 200 and r.json()["type"] == "str"
    r = await client.get("/api/learn/schema/eos_cli_config_gen", params={"key": "router_bgp.peer_groups"})
    assert r.status_code == 200 and r.json()["items"]["primary_key"] == "name"
    r = await client.get("/api/learn/schema/eos_designs", params={"key": "nope.nothing"})
    assert r.status_code == 404 and "suggestions" in r.json()["detail"]
    assert (await client.get("/api/learn/schema/bogus", params={"key": "x"})).status_code == 404
    hits = (await client.get("/api/learn/schema/search", params={"q": "vtep_loopback_ipv4_pool"})).json()
    assert hits[0]["key"].endswith("vtep_loopback_ipv4_pool")

    # glossary / tours / lessons
    assert len((await client.get("/api/learn/glossary")).json()) >= 40
    assert "first-lab" in (await client.get("/api/learn/tours")).json()
    lessons = (await client.get("/api/learn/lessons")).json()
    assert len(lessons) >= 12 and lessons[0]["id"] == "what-is-avd" and lessons[0]["progress"]["passed"] == []
    one = (await client.get("/api/learn/lessons/pools-ip-math")).json()
    assert one["prev"] == "node-types-ids" and one["next"] == "mlag" and any(b["kind"] == "quiz" for b in one["blocks"])
    assert (await client.get("/api/learn/lessons/nope")).status_code == 404

    # a lab: create → build → explain → checkpoints → progress
    r = await client.post("/api/labs", json={"name": "Learn Lab", "template": "single-dc-l3ls", "knobs": {}})
    assert r.status_code == 201, r.text
    lab_id = r.json()["id"]
    r = await client.get("/api/learn/explain", params={"lab_id": lab_id, "node": "dc1-leaf1a", "line": "interface Loopback0"})
    assert r.status_code == 404  # not built yet
    job = (await client.post(f"/api/labs/{lab_id}/build")).json()
    await _wait(client, job["job_id"])

    r = await client.get("/api/learn/explain", params={"lab_id": lab_id, "node": "dc1-leaf1a", "line": "interface Loopback0"})
    assert r.status_code == 200, r.text
    e = r.json()
    assert "10.255.0.3" in e["why"] and e["avd_input"]["knob"] == "pools" and e["section"] == "interface Loopback0"
    r = await client.post("/api/learn/explain", json={"lab_id": lab_id, "node": "dc1-spine1",
                                                      "lines": ["router bgp 65100", "nonsense"]})
    body = r.json()
    assert body["explained"] == 1 and body["lines"][1]["why"] is None
    whole = (await client.post("/api/learn/explain", json={"lab_id": lab_id, "node": "dc1-leaf1a", "all": True})).json()
    assert whole["explained"] >= 200 and whole["sections"]
    assert (await client.get("/api/learn/explain", params={"lab_id": lab_id, "node": "../x", "line": "x"})).status_code == 404

    r = await client.post("/api/learn/lessons/pools-ip-math/check", json={"lab_id": lab_id, "checkpoint_id": "lo0-leaf1b"})
    assert r.status_code == 200 and r.json()["passed"] is True, r.text
    r = await client.post("/api/learn/lessons/ztp/check", json={"lab_id": lab_id, "checkpoint_id": "ztp-mode"})
    assert r.json()["passed"] is False and r.json()["hint"] is None and "startup-config" in r.json()["detail"]
    r = await client.post("/api/learn/lessons/generated-documentation/check", json={"lab_id": lab_id, "checkpoint_id": "build-ok"})
    assert r.json()["passed"] is True
    assert (await client.post("/api/learn/lessons/ztp/check", json={"lab_id": lab_id, "checkpoint_id": "nope"})).status_code == 404
    # re-passing keeps one row
    await client.post("/api/learn/lessons/pools-ip-math/check", json={"lab_id": lab_id, "checkpoint_id": "lo0-leaf1b"})
    prog = (await client.get("/api/learn/progress")).json()
    assert prog["lessons"]["pools-ip-math"]["passed"] == ["lo0-leaf1b"] and prog["lessons"]["pools-ip-math"]["total"] == 3
    assert len([i for i in prog["items"] if i["checkpoint_id"] == "lo0-leaf1b"]) == 1
    assert prog["items"][0]["lab_id"] == lab_id
    lessons = {x["id"]: x for x in (await client.get("/api/learn/lessons")).json()}
    assert lessons["pools-ip-math"]["progress"]["passed"] == ["lo0-leaf1b"]
    assert (await client.delete(f"/api/labs/{lab_id}")).status_code == 204
