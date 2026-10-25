"""Module 6 — NetBox: plan from the single-dc-l3ls build, reconcile against a fake pynetbox (object set, idempotency,
dry_run, delete_stale), targets API with write-only tokens, lab binding and the sync job."""
from __future__ import annotations

import copy
import uuid
from pathlib import Path
from typing import Any

import pytest

from avdlab.jobs.engine import read_log, runner
from avdlab.labs.build import build_lab_dir
from avdlab.labs.templates import get_template, load_group_vars_dir, render_lab, serials_from_group_vars
from avdlab.netbox import service as nbs
from avdlab.netbox.sync import Reconciler, build_plan, ethernet_type, parse_vlan_list
from avdlab.topology.clab import build_clab_topology

TOKEN = "0123456789abcdef0123456789abcdef01234567"


# ------------------------------------------------------------------ fake pynetbox
class FakeRecord:
    def __init__(self, ep: "FakeEndpoint", data: dict[str, Any]) -> None:
        self._ep = ep
        self._data = data

    @property
    def id(self) -> int:
        return self._data["id"]

    def serialize(self) -> dict[str, Any]:
        return copy.deepcopy(self._data)

    def update(self, changes: dict[str, Any]) -> bool:
        self._ep.api.calls.append(("update", self._ep.name, self.id, dict(changes)))
        self._data.update(copy.deepcopy(changes))
        return True

    def delete(self) -> bool:
        self._ep.api.calls.append(("delete", self._ep.name, self.id))
        self._ep.rows.pop(self.id, None)
        return True


class FakeEndpoint:
    ALIASES = {"interface_id": "assigned_object_id"}

    def __init__(self, api: "FakeNetBox", name: str) -> None:
        self.api = api
        self.name = name
        self.rows: dict[int, dict[str, Any]] = {}

    def _match(self, row: dict[str, Any], key: str, want: Any) -> bool:
        if key == "tag":
            slugs = {self.api.tags.rows[t]["slug"] for t in row.get("tags") or [] if t in self.api.tags.rows}
            return want in slugs
        if key == "device_id" and self.name == "ip_addresses":
            intf = self.api.interfaces.rows.get(row.get("assigned_object_id"))
            return intf is not None and intf["device"] == want
        field = self.ALIASES.get(key, key[:-3] if key.endswith("_id") else key)
        have = row.get(field)
        if want == "null":
            return have is None
        return have == want

    def filter(self, **kw: Any) -> list[FakeRecord]:
        self.api.calls.append(("filter", self.name, kw))
        return [FakeRecord(self, r) for r in self.rows.values() if all(self._match(r, k, v) for k, v in kw.items())]

    def all(self) -> list[FakeRecord]:
        return [FakeRecord(self, r) for r in self.rows.values()]

    def get(self, oid: int) -> FakeRecord | None:
        r = self.rows.get(oid)
        return FakeRecord(self, r) if r is not None else None

    def create(self, data: dict[str, Any]) -> FakeRecord:
        self.api.calls.append(("create", self.name, dict(data)))
        self.api.next_id += 1
        row = {"id": self.api.next_id, **copy.deepcopy(data)}
        if self.name == "cables":  # NetBox sets `cable` on both terminations
            for end in ("a_terminations", "b_terminations"):
                for term in data[end]:
                    intf = self.api.interfaces.rows[term["object_id"]]
                    assert intf.get("cable") is None, f"interface {intf['name']} already cabled"
                    intf["cable"] = row["id"]
        if self.name == "ip_addresses":
            assert data["assigned_object_id"] in self.api.interfaces.rows
        self.rows[row["id"]] = row
        return FakeRecord(self, row)


class _App:
    def __init__(self, api: "FakeNetBox", names: list[str]) -> None:
        for n in names:
            setattr(self, n, getattr(api, n))


class FakeNetBox:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.next_id = 0
        for n in ("tags", "manufacturers", "platforms", "device_types", "device_roles", "sites", "locations", "racks",
                  "devices", "interfaces", "cables", "vlans", "vrfs", "prefixes", "ip_addresses"):
            setattr(self, n, FakeEndpoint(self, n))
        self.extras = _App(self, ["tags"])
        self.dcim = _App(self, ["manufacturers", "platforms", "device_types", "device_roles", "sites", "locations",
                                "racks", "devices", "interfaces", "cables"])
        self.ipam = _App(self, ["vlans", "vrfs", "prefixes", "ip_addresses"])

    def status(self) -> dict[str, Any]:
        return {"netbox-version": "4.2.3", "python-version": "3.12"}

    def writes(self) -> list[tuple]:
        return [c for c in self.calls if c[0] in ("create", "update", "delete")]

    def by_name(self, ep: str) -> dict[str, dict[str, Any]]:
        return {r["name"]: r for r in getattr(self, ep).rows.values()}


# ------------------------------------------------------------------ fixtures
@pytest.fixture(scope="module")
def built_lab(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("netbox") / "lab"
    for rel, content in render_lab(get_template("single-dc-l3ls"), {}, slug="nb").files().items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    build_lab_dir(root, log=lambda _l: None)
    build_clab_topology(root, "FABRIC", "arista/ceos:4.34.2F",
                        serials=serials_from_group_vars(load_group_vars_dir(root))).write(root / "clab")
    return root


def _knobs() -> dict[str, Any]:
    from avdlab.labs.templates import coerce_knobs

    return coerce_knobs(get_template("single-dc-l3ls"), {})


# ------------------------------------------------------------------ pure helpers
def test_helpers():
    assert parse_vlan_list("11-12,21-22,3401") == [11, 12, 21, 22, 3401]
    assert parse_vlan_list("all") is None and parse_vlan_list("none") == []
    assert ethernet_type("100gfull") == "100gbase-x-qsfp28" and ethernet_type(None) == "1000base-t"
    assert ethernet_type("forced 25gfull") == "25gbase-x-sfp28" and ethernet_type("100full") == "100base-tx"
    assert nbs.clean_url("netbox.lab:8000/api/") == "http://netbox.lab:8000"
    assert nbs.clean_url("https://nb.example.com/") == "https://nb.example.com"


def test_plan(built_lab):
    plan = build_plan(built_lab, fabric_name="FABRIC", knobs=_knobs())
    assert plan.site == "FABRIC"
    switches = [d for d in plan.devices if not d.client]
    clients = [d for d in plan.devices if d.client]
    assert len(switches) == 8 and len(clients) == 2
    assert {d.role for d in switches} == {"spine", "l3leaf", "l2leaf"} and {d.role for d in clients} == {"server"}
    assert {d.model for d in switches} == {"cEOS-lab"} and all(d.serial for d in switches)
    assert len(plan.cables) == 22 and not plan.skipped
    assert {11, 12, 21, 22, 3401, 3402} <= set(plan.vlans)
    assert {"VRF10", "VRF11"} <= set(plan.vrfs) and plan.vrfs["VRF10"]
    prefixes = {p for p, _vrf in plan.prefixes}
    assert ("10.10.11.0/24", "VRF10") in plan.prefixes and "10.255.255.0/31" in prefixes  # SVI + P2P
    assert "10.255.0.0/27" in prefixes and ("172.16.1.0/24", "MGMT") in plan.prefixes  # pool + mgmt
    leaf = plan.device("dc1-leaf1a")
    pc5 = leaf.intf("Port-Channel5")
    assert pc5.type == "lag" and pc5.mode == "tagged" and pc5.tagged_vids == [11, 12, 21, 22]
    assert leaf.intf("Ethernet5").lag == "Port-Channel5" and leaf.intf("Management1").mgmt_only
    ips = {(i.device, i.interface): i for i in plan.ips}
    assert ips[("dc1-leaf1a", "Vlan11")].role == "anycast"
    assert ips[("dc1-leaf1a", "Loopback0")].role == "loopback"
    assert ips[("dc1-leaf1a", "Loopback1")].role == "anycast"  # shared MLAG VTEP
    assert ips[("dc1-leaf1a", "Management1")].primary
    srv = plan.device("dc1-leaf1-server1")
    assert srv.intf("bond0").type == "lag" and srv.intf("eth1").lag == "bond0"
    assert ips[("dc1-leaf1-server1", "bond0.11")].address == "10.10.11.100/24"
    # server cables land on the client's eth ports
    assert ("dc1-leaf1a", "Ethernet5", "dc1-leaf1-server1", "eth1") in plan.cables \
        or ("dc1-leaf1-server1", "eth1", "dc1-leaf1a", "Ethernet5") in plan.cables
    no_clients = build_plan(built_lab, fabric_name="FABRIC", include_clients=False)
    assert len(no_clients.devices) == 8 and len(no_clients.cables) == 16 and len(no_clients.skipped) == 6


def test_sync_object_set_idempotent_dry_run_and_stale(built_lab):
    plan = build_plan(built_lab, fabric_name="FABRIC", knobs=_knobs())
    nb = FakeNetBox()

    # dry run on an empty NetBox: counts everything, writes nothing
    dry = Reconciler(nb, slug="nb", dry_run=True, log=lambda _l: None).run(plan, delete_stale=True)
    assert nb.writes() == [] and dry["dry_run"] is True
    assert dry["created"]["device"] == 10 and dry["created"]["cable"] == 22 and not dry["deleted"]

    res = Reconciler(nb, slug="nb", log=lambda _l: None).run(plan)
    assert res["created"]["device"] == 10 and res["created"]["cable"] == 22 and res["skipped"] == []
    assert res["created"] == {k: v for k, v in dry["created"].items()}
    devices = nb.by_name("devices")
    assert len(devices) == 10 and len(nb.cables.rows) == 22
    roles = {r["id"]: r["slug"] for r in nb.device_roles.rows.values()}
    assert sorted(roles[d["role"]] for d in devices.values()).count("server") == 2
    assert {r["vid"] for r in nb.vlans.rows.values()} >= {11, 12, 21, 22, 3401, 3402}
    assert {r["name"] for r in nb.vrfs.rows.values()} >= {"VRF10", "VRF11"}
    vrf_ids = {r["name"]: r["id"] for r in nb.vrfs.rows.values()}
    assert any(r["prefix"] == "10.10.11.0/24" and r["vrf"] == vrf_ids["VRF10"] for r in nb.prefixes.rows.values())
    assert {r["model"] for r in nb.device_types.rows.values()} == {"cEOS-lab", "Linux client"}
    assert [r["name"] for r in nb.platforms.rows.values()] == ["Arista EOS"]
    assert [r["name"] for r in nb.sites.rows.values()] == ["FABRIC"]
    # tags: every lab-owned object carries both tags
    tag_ids = {r["slug"]: r["id"] for r in nb.tags.rows.values()}
    assert set(tag_ids) == {"avdlab", "avdlab-nb"}
    for ep in ("devices", "interfaces", "vlans", "vrfs", "prefixes", "ip_addresses", "cables", "sites"):
        assert all(set(r["tags"]) == set(tag_ids.values()) for r in getattr(nb, ep).rows.values()), ep
    assert all(r["tags"] == [tag_ids["avdlab"]] for r in nb.manufacturers.rows.values())
    # device details: serial, primary IP = management, LAG membership
    leaf = devices["dc1-leaf1a"]
    assert leaf["serial"] == "NBXX0003"
    assert nb.ip_addresses.rows[leaf["primary_ip4"]]["address"] == "172.16.1.101/24"
    intfs = {(r["device"], r["name"]): r for r in nb.interfaces.rows.values()}
    assert intfs[(leaf["id"], "Ethernet5")]["lag"] == intfs[(leaf["id"], "Port-Channel5")]["id"]
    assert intfs[(leaf["id"], "Management1")]["mgmt_only"] is True
    anycast = [r for r in nb.ip_addresses.rows.values() if r["address"] == "10.10.11.1/24"]
    assert len(anycast) == 4 and {r["role"] for r in anycast} == {"anycast"}
    srv = devices["dc1-leaf1-server1"]
    assert nb.ip_addresses.rows[srv["primary_ip4"]]["address"] == "10.10.11.100/24"

    # second run: nothing created, nothing changed
    n_calls = len(nb.writes())
    again = Reconciler(nb, slug="nb", log=lambda _l: None).run(plan, delete_stale=True)
    assert again["created"] == {} and again["deleted"] == {} and again["updated"] == {}, again
    assert again["unchanged"]["device"] == 10 and again["unchanged"]["cable"] == 22
    assert len(nb.writes()) == n_calls

    # a changed field → update only
    nb.devices.rows[leaf["id"]]["serial"] = "WRONG"
    upd = Reconciler(nb, slug="nb", log=lambda _l: None).run(plan)
    assert upd["created"] == {} and upd["updated"] == {"device": 1}
    assert nb.devices.rows[leaf["id"]]["serial"] == "NBXX0003"

    # delete_stale without clients: the 2 clients (+ their cables, interfaces, IPs) go; dry run first
    small = build_plan(built_lab, fabric_name="FABRIC", knobs=_knobs(), include_clients=False)
    before = len(nb.writes())
    dry2 = Reconciler(nb, slug="nb", dry_run=True, log=lambda _l: None).run(small, delete_stale=True)
    assert len(nb.writes()) == before and dry2["deleted"]["device"] == 2 and dry2["deleted"]["cable"] == 6
    stale = Reconciler(nb, slug="nb", log=lambda _l: None).run(small, delete_stale=True)
    assert stale["deleted"]["device"] == 2 and stale["deleted"]["cable"] == 6 and stale["created"] == {}
    assert len(nb.devices.rows) == 8 and len(nb.cables.rows) == 16
    assert len(stale["skipped"]) == 6  # the server cables of the plan


def test_shared_object_is_untagged_not_deleted(built_lab):
    plan = build_plan(built_lab, fabric_name="FABRIC", knobs=_knobs())
    nb = FakeNetBox()
    Reconciler(nb, slug="nb", log=lambda _l: None).run(plan)
    extra = nb.vrfs.create({"name": "VRF99", "tags": []})
    t_other = nb.tags.create({"name": "avdlab:other", "slug": "avdlab-other", "tags": []})
    lab_tag = next(r["id"] for r in nb.tags.rows.values() if r["slug"] == "avdlab-nb")
    nb.vrfs.rows[extra.id]["tags"] = [lab_tag, t_other.id]
    res = Reconciler(nb, slug="nb", log=lambda _l: None).run(plan, delete_stale=True)
    assert res["updated"] == {"vrf": 1} and not res["deleted"]
    assert nb.vrfs.rows[extra.id]["tags"] == [t_other.id]


# ------------------------------------------------------------------ API
async def test_targets_api_tokens(client, monkeypatch):
    seen: dict[str, Any] = {}

    def fake_client(url, token, *, verify_certs=True, timeout=0):
        seen.update(url=url, token=token, verify=verify_certs)
        return FakeNetBox()

    monkeypatch.setattr(nbs, "api_client", fake_client)
    name = f"nb-{uuid.uuid4().hex[:6]}"
    r = await client.post("/api/netbox/targets", json={"name": name, "url": "netbox.lab:8000/", "token": TOKEN,
                                                       "verify_certs": False})
    assert r.status_code == 201, r.text
    t = r.json()
    assert t["url"] == "http://netbox.lab:8000" and t["has_token"] is True and t["labs"] == []
    assert TOKEN not in r.text
    for resp in (await client.get("/api/netbox/targets"), await client.get(f"/api/netbox/targets/{t['id']}"),
                 await client.get("/api/secrets")):
        assert resp.status_code == 200 and TOKEN not in resp.text
    assert (await client.post("/api/netbox/targets", json={"name": name, "url": "x"})).status_code == 409
    assert (await client.post("/api/netbox/targets", json={"name": "nb-bad", "url": "x", "token": "   "})).status_code == 422  # whitespace inside a token is now joined (wrapped paste); only an empty token is refused
    r = (await client.post(f"/api/netbox/targets/{t['id']}/test")).json()
    assert r == {"ok": True, "version": "4.2.3", "error": None}
    assert seen == {"url": "http://netbox.lab:8000", "token": TOKEN, "verify": False}

    def boom(url, token, **kw):
        raise RuntimeError(f"403 Forbidden: Invalid token {token}")

    monkeypatch.setattr(nbs, "api_client", boom)
    r = (await client.post(f"/api/netbox/targets/{t['id']}/test")).json()
    assert r["ok"] is False and "authentication failed" in r["error"] and TOKEN not in r["error"]
    r = await client.patch(f"/api/netbox/targets/{t['id']}", json={"token": None, "url": "https://nb.example.com"})
    assert r.status_code == 200 and r.json()["has_token"] is False and r.json()["url"] == "https://nb.example.com"
    r = (await client.post(f"/api/netbox/targets/{t['id']}/test")).json()
    assert r == {"ok": False, "version": None, "error": "no API token stored for this target"}
    assert (await client.delete(f"/api/netbox/targets/{t['id']}")).status_code == 204
    assert (await client.get(f"/api/netbox/targets/{t['id']}")).status_code == 404


async def test_lab_binding_and_sync_job(client, monkeypatch, built_lab):
    import shutil

    from avdlab.core.db import Lab, LabState, SessionLocal

    fake = FakeNetBox()
    monkeypatch.setattr(nbs, "api_client", lambda url, token, **kw: fake)
    t = (await client.post("/api/netbox/targets", json={"name": f"nb-{uuid.uuid4().hex[:6]}",
                                                        "url": "http://nb.local", "token": TOKEN})).json()
    r = await client.post("/api/labs", json={"name": f"nb-{uuid.uuid4().hex[:6]}", "template": "single-dc-l3ls",
                                             "knobs": {}})
    assert r.status_code == 201, r.text
    lab = r.json()
    assert lab["netbox_target_id"] is None
    # not built / no target → 409
    assert (await client.post(f"/api/labs/{lab['id']}/netbox/sync", json={})).status_code == 409
    assert (await client.patch(f"/api/labs/{lab['id']}", json={"netbox_target_id": "nope"})).status_code == 422
    r = await client.patch(f"/api/labs/{lab['id']}", json={"netbox_target_id": t["id"]})
    assert r.status_code == 200 and r.json()["netbox_target_id"] == t["id"]
    assert (await client.get(f"/api/netbox/targets/{t['id']}")).json()["labs"] == [{"id": lab["id"], "name": lab["name"]}]
    assert (await client.delete(f"/api/netbox/targets/{t['id']}")).status_code == 409
    st = (await client.get(f"/api/labs/{lab['id']}/netbox/status")).json()
    assert st["target"]["id"] == t["id"] and st["last_sync"] is None
    assert (await client.post(f"/api/labs/{lab['id']}/netbox/sync", json={})).status_code == 409  # draft

    # "build": copy the built fixture's outputs into the lab dir
    lab_dir = Path(lab["dir"])
    for sub in ("intended", "documentation", "clab"):
        shutil.copytree(built_lab / sub, lab_dir / sub, dirs_exist_ok=True)
    async with SessionLocal() as s:
        row = await s.get(Lab, lab["id"])
        row.state = LabState.built
        await s.commit()

    r = await client.post(f"/api/labs/{lab['id']}/netbox/sync", json={"dry_run": True})
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    await runner.wait(job_id)
    job = (await client.get(f"/api/jobs/{job_id}")).json()
    assert job["status"] == "succeeded", (job["error"], read_log(job_id)[-3000:])
    assert job["result"]["dry_run"] is True and fake.writes() == []
    slug = lab["slug"]

    r = await client.post(f"/api/labs/{lab['id']}/netbox/sync", json={"delete_stale": True})
    job_id = r.json()["job_id"]
    await runner.wait(job_id)
    job = (await client.get(f"/api/jobs/{job_id}")).json()
    assert job["status"] == "succeeded", (job["error"], read_log(job_id)[-3000:])
    res = job["result"]
    assert res["created"]["device"] == 10 and res["created"]["cable"] == 22
    assert res["netbox_url"] == f"http://nb.local/dcim/devices/?tag=avdlab-{slug}"
    assert TOKEN not in read_log(job_id)
    st = (await client.get(f"/api/labs/{lab['id']}/netbox/status")).json()
    assert st["last_sync"]["created"] == res["created"] and st["last_sync"]["target"]["id"] == t["id"]
    assert {r["slug"] for r in fake.tags.rows.values()} == {"avdlab", f"avdlab-{slug}"}
    # unbind
    r = await client.patch(f"/api/labs/{lab['id']}", json={"netbox_target_id": None})
    assert r.json()["netbox_target_id"] is None
    assert (await client.delete(f"/api/netbox/targets/{t['id']}")).status_code == 204


def test_token_normalisation_and_scheme():
    """0.10.0: NetBox 4.5 v2 tokens (nbt_<id>.<secret>) go out as Bearer; a pasted 'Token ' prefix is stripped."""
    from avdlab.netbox.service import auth_scheme, normalise_token

    assert normalise_token("  Token 0123456789abcdef0123456789abcdef01234567\n") == "0123456789abcdef0123456789abcdef01234567"
    assert normalise_token("Bearer nbt_42.s3cret") == "nbt_42.s3cret"
    assert normalise_token("Token Token nbt_42.s3c ret\n") == "nbt_42.s3cret"  # double prefix + wrapped paste
    assert auth_scheme("nbt_42.s3cret") == "Bearer"
    assert auth_scheme("0123456789abcdef0123456789abcdef01234567") == "Token"
    assert auth_scheme("nbt_nodot") == "Token"
