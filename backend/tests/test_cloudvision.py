"""Module 2c: CloudVision knob rendering (real pyavd build), targets CRUD with write-only tokens, connection test,
lab binding, onboarding + cv_deploy jobs with a scripted fake SSH and a fake httpx transport."""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from avdlab.cloudvision import jobs as cv_jobs
from avdlab.cloudvision import service as cv_service
from avdlab.core.db import CVTarget, LabState, Secret, SecretKind, SessionLocal
from avdlab.jobs.engine import read_log
from avdlab.labs.build import build_lab_dir
from avdlab.labs.templates import (
    KnobError,
    coerce_knobs,
    get_template,
    load_group_vars_dir,
    render_lab,
    serials_from_group_vars,
)

from .test_executors import (  # noqa: F401 - _cleanup is an autouse fixture (hosts/labs created here are removed)
    _CREATED,
    NODES,
    PASSWORD,
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

CV_TOKEN = "svc-TOKEN-eyJhbGciOi.very-secret"
ONB_TOKEN = "onboard-TOKEN-4f9a.also-secret"
CVAAS = {"mode": "cvaas", "region": "auto", "device_tags": {"lab": "avdlab", "owner": "ops"}}


@pytest.fixture(autouse=True)
async def _cleanup_targets():
    yield
    from sqlalchemy import delete, select, update

    from avdlab.core.db import Lab, init_db

    await init_db()  # pure tests run without the client fixture
    async with SessionLocal() as s:
        ids = (await s.execute(select(CVTarget.id).where(CVTarget.name.like("cvt-%")))).scalars().all()
        if ids:
            await s.execute(update(Lab).where(Lab.cv_target_id.in_(ids)).values(cv_target_id=None))
            await s.execute(delete(CVTarget).where(CVTarget.id.in_(ids)))
            await s.execute(delete(Secret).where(Secret.name.like("cv-target-%")))
            await s.commit()


def write_rendered(rendered, root: Path) -> Path:
    for rel, content in rendered.files().items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return root


def fake_http(monkeypatch, handler) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(cv_service, "http_client",
                        lambda *, verify, timeout: httpx.AsyncClient(transport=httpx.MockTransport(_handler)))
    return seen


def inventory_ndjson(devices: list[tuple[str, str, str]]) -> str:
    return "\n".join(json.dumps({"result": {"value": {"key": {"deviceId": serial}, "hostname": host,
                                                      "streamingStatus": status},
                                            "time": "2026-09-22T10:00:00Z", "type": "INITIAL"}})
                     for serial, host, status in devices) + "\n"


# ------------------------------------------------------------------ knob (pure)
def test_cloudvision_knob_coercion():
    tpl = get_template("single-dc-l3ls")
    knob = next(k for k in tpl.public()["knobs"] if k["key"] == "cloudvision")
    assert knob["type"] == "object" and knob["default"] == {"mode": "none", "region": "auto", "servers": [],
                                                            "disable_aaa": True, "topology_hints": True, "device_tags": {}}
    v = coerce_knobs(tpl, {"cloudvision": {"mode": "onprem", "servers": "10.0.0.1, cv2.lab:8443",
                                           "device_tags": [{"name": "site", "value": "lab"}]}})["cloudvision"]
    assert v == {"mode": "onprem", "region": "auto", "servers": ["10.0.0.1", "cv2.lab:8443"], "disable_aaa": True,
                 "topology_hints": True, "device_tags": {"site": "lab"}}
    for bad in ({"mode": "cloud"}, {"mode": "onprem"}, {"mode": "cvaas", "region": "mars-1"}, {"bogus": 1},
                {"mode": "cvaas", "device_tags": {"x": ""}}):
        with pytest.raises(KnobError):
            coerce_knobs(tpl, {"cloudvision": bad})
    # every template carries the knob
    for name in ("dual-dc-l3ls", "l2ls-fabric", "single-dc-multipod-l3ls"):
        assert get_template(name).knob("cloudvision") is not None, name


def test_mode_none_keeps_render_identical():
    tpl = get_template("single-dc-l3ls")
    base = render_lab(tpl, {}, slug="same").files()
    assert render_lab(tpl, {"cloudvision": {"mode": "none", "device_tags": {"a": "b"}}}, slug="same").files() == base
    assert "generate_cv_tags" not in base["group_vars/FABRIC.yml"]


def test_onprem_render_group_vars():
    r = render_lab(get_template("single-dc-l3ls"), {"cloudvision": {"mode": "onprem", "servers": ["10.9.9.9", "cv2:8443"],
                                                                   "disable_aaa": False}}, slug="x")
    fab = r.group_vars["FABRIC"]
    assert fab["cv_settings"] == {"onprem_clusters": [{"name": "primary", "servers": [{"name": "10.9.9.9"},
                                                                                        {"name": "cv2", "port": 8443}],
                                                       "vrf": "use_mgmt_interface_vrf"}],
                                  "terminattr": {"disable_aaa": False}}
    assert fab["generate_cv_tags"] == {"topology_hints": True, "device_tags": []}
    # a template without NTP/DNS gets defaults (eos_designs requires them with CloudVision)
    dual = render_lab(get_template("dual-dc-l3ls"), {"cloudvision": CVAAS}, slug="d").group_vars["FABRIC"]
    assert dual["ntp_settings"]["servers"] and dual["dns_settings"]["servers"]


@pytest.mark.parametrize("cv,expect", [
    (CVAAS, ["-cvaddr=apiserver.arista.io:443", "-cvauth=token-secure,/tmp/cv-onboarding-token", "-cvvrf=MGMT",
             "-disableaaa"]),
    ({"mode": "onprem", "servers": ["10.9.9.9"]}, ["-cvaddr=10.9.9.9:9910", "-cvauth=token,/tmp/token"]),
])
def test_build_renders_terminattr(tmp_path, cv, expect):
    root = write_rendered(render_lab(get_template("single-dc-l3ls"), {"cloudvision": cv}, slug="cvlab"), tmp_path / "lab")
    build_lab_dir(root, log=lambda _l: None)
    cfg = (root / "intended/configs/dc1-spine1.cfg").read_text()
    assert "daemon TerminAttr" in cfg
    exec_line = next(line for line in cfg.splitlines() if "exec /usr/bin/TerminAttr" in line)
    for e in expect:
        assert e in exec_line, (e, exec_line)
    assert "192.168.1.12" not in cfg  # the example's placeholder cluster is replaced
    sc = yaml.safe_load((root / "intended/structured_configs/dc1-leaf1a.yml").read_text())
    tags = {t["name"]: t["value"] for t in sc["metadata"]["cv_tags"]["device_tags"]}
    assert tags["topology_hint_type"] == "leaf" and tags["topology_hint_fabric"] == "FABRIC"
    if cv.get("device_tags"):
        assert tags["lab"] == "avdlab" and tags["owner"] == "ops"
    assert sc["metadata"]["serial_number"].startswith("CVLA")


# ------------------------------------------------------------------ service helpers
def test_result_markers_and_summary():
    raw = {"failed": False, "errors": [], "warnings": ["w1"],
           "workspace": {"name": "AVD x", "id": "ws-1", "state": "submitted", "change_control_id": "cc-1"},
           "change_control": {"id": "cc-1", "name": "CC", "state": "pending approval"},
           "deployed_configs": [{"file": "a.cfg", "device": {"hostname": "dc1-spine1", "serial_number": "S1",
                                                              "exists_on_cv": True, "streaming": True}}],
           "skipped_configs": [{"file": "b.cfg", "device": {"hostname": "dc1-leaf1a", "serial_number": "S2",
                                                             "exists_on_cv": False, "streaming": None}}],
           "deployed_device_tags": [{}, {}]}
    ansible_out = "ok: [dc1-spine1] => " + json.dumps(
        {"msg": cv_service.RESULT_BEGIN + json.dumps(raw) + cv_service.RESULT_END}, indent=4)
    assert cv_service.extract_result_json(ansible_out) == raw  # escaped once inside the ansible JSON
    assert cv_service.extract_result_json(cv_service.RESULT_BEGIN + json.dumps(raw) + cv_service.RESULT_END) == raw
    assert cv_service.extract_result_json("no markers") is None
    s = cv_service.summarize_deploy(raw, server="https://www.arista.io/", rc=0)
    assert s["ok"] is True and s["workspace"] == {"id": "ws-1", "name": "AVD x", "state": "submitted",
                                                   "url": "https://www.arista.io/cv/provisioning/workspaces?ws=ws-1"}
    assert s["change_control"] == {"id": "cc-1", "name": "CC", "state": "pending approval"}
    assert s["devices"] == [
        {"hostname": "dc1-leaf1a", "serial": "S2", "exists_on_cv": False, "streaming": None, "deployed": False},
        {"hostname": "dc1-spine1", "serial": "S1", "exists_on_cv": True, "streaming": True, "deployed": True}]
    assert s["warnings"] == ["w1"] and s["deployed_device_tags"] == 2
    failed = cv_service.summarize_deploy(None, server="x", rc=2,
                                         output='fatal: [dc1-spine1 -> localhost]: FAILED! => {"msg": "boom"}')
    assert failed["ok"] is False and failed["errors"] == ["boom"] and failed["workspace"] is None
    assert cv_service.parse_inventory(inventory_ndjson([("S1", "a", "STREAMING_STATUS_ACTIVE"),
                                                        ("S2", "b", "STREAMING_STATUS_INACTIVE")])) == [
        {"serial": "S1", "hostname": "a", "streaming_status": "STREAMING_STATUS_ACTIVE", "streaming": True},
        {"serial": "S2", "hostname": "b", "streaming_status": "STREAMING_STATUS_INACTIVE", "streaming": False}]
    assert cv_jobs.container_name({"name": "l", "prefix": ""}, "n") == "n"
    assert cv_jobs.container_name({"name": "l"}, "n") == "clab-l-n"
    assert cv_jobs.terminattr_running("Agent: TerminAttr (running with PID 1234)")
    assert not cv_jobs.terminattr_running("% Invalid input (privileged mode required)")
    assert not cv_jobs.terminattr_running("Agent: TerminAttr (not running)")
    assert not cv_jobs.terminattr_running("Agent: TerminAttr (not running)")


# ------------------------------------------------------------------ targets API
async def mk_target(client, *, token: str | None = CV_TOKEN, onboarding: str | None = ONB_TOKEN,
                    kind: str = "cvaas") -> dict[str, Any]:
    body: dict[str, Any] = {"name": f"cvt-{uuid.uuid4().hex[:6]}", "kind": kind, "server": "https://www.arista.io/",
                            "verify_certs": True, "token": token, "onboarding_token": onboarding}
    if onboarding:
        body["onboarding_token_expires_at"] = "2026-12-31"
    r = await client.post("/api/cv/targets", json=body)
    assert r.status_code == 201, r.text
    return r.json()


async def test_targets_crud_hides_tokens(client):
    t = await mk_target(client)
    assert t["server"] == "https://www.arista.io" and t["kind"] == "cvaas" and t["labs"] == []
    assert t["has_token"] is True and t["has_onboarding_token"] is True
    assert t["onboarding_token_meta"]["expires_at"] == "2026-12-31"
    listing = await client.get("/api/cv/targets")
    for resp in (listing, await client.get(f"/api/cv/targets/{t['id']}"), await client.get("/api/secrets")):
        assert CV_TOKEN not in resp.text and ONB_TOKEN not in resp.text
    assert any(x["id"] == t["id"] for x in listing.json())
    async with SessionLocal() as s:
        row = await s.get(CVTarget, t["id"])
        sec = await s.get(Secret, row.token_secret_id)
        assert sec.kind == SecretKind.cv_token and CV_TOKEN not in sec.ciphertext
        onb = await s.get(Secret, row.onboarding_token_secret_id)
        assert onb.meta["purpose"] == "onboarding" and onb.meta["expires_at"] == "2026-12-31"
        old_token_secret = row.token_secret_id
    assert (await client.post("/api/cv/targets", json={"name": t["name"], "kind": "cvaas", "server": "x"})).status_code == 409
    assert (await client.post("/api/cv/targets", json={"name": "cvt-bad", "kind": "aws", "server": "x"})).status_code == 422

    # rotate token (same secret row), drop the onboarding token, rename, toggle verify
    r = await client.patch(f"/api/cv/targets/{t['id']}", json={"token": "new-token", "onboarding_token": "",
                                                                "verify_certs": False, "name": t["name"] + "-x"})
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["has_token"] and not p["has_onboarding_token"] and p["verify_certs"] is False and "new-token" not in r.text
    async with SessionLocal() as s:
        row = await s.get(CVTarget, t["id"])
        assert row.token_secret_id == old_token_secret and row.onboarding_token_secret_id is None
        from avdlab.core.security import secret_store

        assert await secret_store.reveal(s, row.token_secret_id) == "new-token"
    # a PATCH without token fields leaves them alone
    r = await client.patch(f"/api/cv/targets/{t['id']}", json={"server": "10.1.1.1"})
    assert r.json()["has_token"] and r.json()["server"] == "10.1.1.1"
    assert (await client.patch(f"/api/cv/targets/{t['id']}", json={"token": "a\nb"})).status_code == 422

    assert (await client.delete(f"/api/cv/targets/{t['id']}")).status_code == 204
    assert (await client.get(f"/api/cv/targets/{t['id']}")).status_code == 404
    async with SessionLocal() as s:
        assert await s.get(Secret, old_token_secret) is None


async def test_target_connection_test(client, monkeypatch):
    t = await mk_target(client)

    def ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=inventory_ndjson([("S1", "a", "STREAMING_STATUS_ACTIVE"),
                                                          ("S2", "b", "STREAMING_STATUS_INACTIVE")]))

    seen = fake_http(monkeypatch, ok)
    r = await client.post(f"/api/cv/targets/{t['id']}/test")
    assert r.json() == {"ok": True, "devices_seen": 2, "error": None}
    assert str(seen[0].url) == "https://www.arista.io/api/resources/inventory/v1/Device/all"
    assert seen[0].headers["authorization"] == f"Bearer {CV_TOKEN}"

    fake_http(monkeypatch, lambda req: httpx.Response(401, text="unauthorized"))
    r = (await client.post(f"/api/cv/targets/{t['id']}/test")).json()
    assert r["ok"] is False and "authentication failed" in r["error"] and CV_TOKEN not in json.dumps(r)

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    fake_http(monkeypatch, boom)
    r = (await client.post(f"/api/cv/targets/{t['id']}/test")).json()
    assert r["ok"] is False and "cannot reach www.arista.io" in r["error"]

    no_tok = await mk_target(client, token=None)
    r = (await client.post(f"/api/cv/targets/{no_tok['id']}/test")).json()
    assert r == {"ok": False, "devices_seen": 0, "error": "no service-account token stored for this target"}


# ------------------------------------------------------------------ lab binding
async def mk_cv_lab(client, *, host_id: str | None, cloudvision: dict[str, Any] | None = CVAAS,
                    state: LabState = LabState.built, built: bool = True) -> dict[str, Any]:
    knobs: dict[str, Any] = {"device_password": PASSWORD}
    if cloudvision is not None:
        knobs["cloudvision"] = cloudvision
    r = await client.post("/api/labs", json={"name": f"cv-{uuid.uuid4().hex[:6]}", "template": "single-dc-l3ls",
                                             "host_id": host_id, "knobs": knobs})
    assert r.status_code == 201, r.text
    lab = r.json()
    _CREATED["labs"].append(lab["id"])
    if built:
        fake_build(Path(lab["dir"]), lab["slug"])
    await set_state(lab["id"], state)
    return lab


async def bind(client, lab_id: str, target_id: str | None) -> httpx.Response:
    return await client.patch(f"/api/labs/{lab_id}", json={"cv_target_id": target_id})


async def test_lab_binding(client):
    t = await mk_target(client)
    lab = await mk_cv_lab(client, host_id=None, cloudvision=None, built=False, state=LabState.draft)
    assert lab["cv_target_id"] is None and lab["knobs"]["cloudvision"]["mode"] == "none"
    r = await bind(client, lab["id"], t["id"])
    assert r.status_code == 409 and "cloudvision" in r.text
    assert (await bind(client, lab["id"], "nope")).status_code == 422
    # knob + binding in one request
    r = await client.patch(f"/api/labs/{lab['id']}", json={"knobs": {"cloudvision": CVAAS}, "cv_target_id": t["id"]})
    assert r.status_code == 200, r.text
    assert r.json()["cv_target_id"] == t["id"] and r.json()["knobs"]["cloudvision"]["mode"] == "cvaas"
    fab = (await client.get(f"/api/labs/{lab['id']}/files/group_vars/FABRIC.yml")).json()["content"]
    assert "cvaas:" in fab and "generate_cv_tags:" in fab
    assert (await client.get(f"/api/labs/{lab['id']}")).json()["cv_target_id"] == t["id"]
    assert (await client.get(f"/api/cv/targets/{t['id']}")).json()["labs"] == [{"id": lab["id"], "name": lab["name"]}]
    assert (await client.delete(f"/api/cv/targets/{t['id']}")).status_code == 409  # still bound
    st = (await client.get(f"/api/labs/{lab['id']}/cv/status")).json()
    assert st["mode"] == "cvaas" and st["target"]["id"] == t["id"] and st["onboarding"] is None and st["last_deploy"] is None
    # switching the knob back to none drops the binding
    r = await client.patch(f"/api/labs/{lab['id']}", json={"knobs": {"cloudvision": {"mode": "none"}}})
    assert r.status_code == 200 and r.json()["cv_target_id"] is None
    r = await client.patch(f"/api/labs/{lab['id']}", json={"knobs": {"cloudvision": {"mode": "onprem", "servers": ["10.2.2.2"]}}})
    assert (await bind(client, lab["id"], t["id"])).status_code == 200
    assert (await bind(client, lab["id"], None)).json()["cv_target_id"] is None


# ------------------------------------------------------------------ onboarding job
class CVFakeSSH(ClabFakeSSH):
    def __init__(self, lab_name: str, *, terminattr: str = "Agent: TerminAttr (running with PID 4242)") -> None:
        super().__init__(lab_name, eapi_fail_first=0)
        # insert ahead of the generic ClabFakeSSH entries
        self.responses = {
            "docker exec -i": (0, ""),
            "configure\ndaemon TerminAttr": (0, ""),
            "test -s /tmp/": (0, "812\n"),
            "show daemon TerminAttr": (0, terminattr),
            "tail -n 25 /var/log/agents": (0, "TerminAttr log line\n"),
            "mktemp \"$HOME/.avdlab": (0, "/home/ubuntu/.avdlab/cv-env.Ab12Cd\n"),
            "ansible-playbook -i inventory.yml playbooks/avdlab-cv-deploy.yml": self._play,
            **self.responses,
        }
        self.play_output = ""
        self.play_rc = 0

    def _play(self, fake: "CVFakeSSH", cmd: str) -> tuple[int, str]:
        return self.play_rc, self.play_output


async def lab_serials(lab: dict[str, Any]) -> dict[str, str]:
    return serials_from_group_vars(load_group_vars_dir(Path(lab["dir"])))


async def deployed_cv_lab(client, host_id: str, target: dict[str, Any]) -> dict[str, Any]:
    """A deployed cvaas lab bound to `target`, with the containerlab inspect result the deploy job would have saved."""
    from avdlab.executors.clab import parse_inspect, update_lab

    from .test_executors import inspect_json

    lab = await mk_cv_lab(client, host_id=host_id, state=LabState.deployed)
    assert (await bind(client, lab["id"], target["id"])).status_code == 200
    await update_lab(lab["id"], last_inspect={"at": 0, "nodes": parse_inspect(inspect_json(lab["slug"]), lab["slug"])})
    return lab


async def test_onboard_job(client, monkeypatch):
    host_id = await mk_host()
    t = await mk_target(client)
    lab = await deployed_cv_lab(client, host_id, t)
    serials = await lab_serials(lab)
    fake = CVFakeSSH(lab["slug"])
    install(monkeypatch, fake)
    monkeypatch.setattr(cv_jobs, "ONBOARD_POLL_INTERVAL", 0)
    monkeypatch.setattr(cv_jobs, "RESTART_PAUSE", 0)
    streaming = [(serials[n], n, "STREAMING_STATUS_ACTIVE") for n, _ in NODES]
    calls = fake_http(monkeypatch, lambda req: httpx.Response(200, text=inventory_ndjson(streaming)))

    r = await client.post(f"/api/labs/{lab['id']}/cv/onboard")
    assert r.status_code == 202, r.text
    job = await run_job(client, r.json()["job_id"])
    res = job["result"]
    assert res["complete"] is True
    assert res["nodes"] == sorted(({"name": n, "serial": serials[n], "terminattr_running": True, "streaming": True}
                                   for n, _ in NODES), key=lambda x: x["name"])
    # token written from inside every cEOS node's namespace (docker exec -i, content on stdin), not the linux endpoint
    cps = ran(fake, "docker exec -i")
    assert len(cps) == 4 and all("/tmp/cv-onboarding-token" in c for c, _ in cps)
    assert any("docker exec -i dc1-spine1 " in c for c, _ in cps)
    assert all("umask 077" in c and "cat > " in c and "/mnt/flash/avdlab-cv-onboarding-token" in c for c, _ in cps)
    assert all(ONB_TOKEN not in c for c, _ in cps)  # token never on a command line
    assert len(ran(fake, "no shutdown")) == 4
    # tokens: stdin only, never a command line, never the log
    log = read_log(job["id"])
    assert all(ONB_TOKEN not in c and CV_TOKEN not in c for c, _ in fake.calls)
    assert ONB_TOKEN not in log and CV_TOKEN not in log
    assert sum(ONB_TOKEN in i for i in fake.inputs) == 4
    assert calls and calls[0].headers["authorization"] == f"Bearer {CV_TOKEN}"
    row = await lab_row(lab["id"])
    assert row.settings["cv_onboarding"]["complete"] is True and row.settings["cv_onboarding"]["cluster"] == "cvaas"
    st = (await client.get(f"/api/labs/{lab['id']}/cv/status")).json()
    assert st["onboarding"]["nodes"][0]["terminattr_running"] is True

    # onprem → /tmp/token; TerminAttr down and nothing streaming → incomplete after the (zero) timeout
    r = await client.patch(f"/api/labs/{lab['id']}", json={"knobs": {"cloudvision": {"mode": "onprem", "servers": ["10.3.3.3"]}}})
    assert r.status_code == 200, r.text
    await set_state(lab["id"], LabState.deployed)
    fake2 = CVFakeSSH(lab["slug"], terminattr="Agent: TerminAttr (shutdown)")
    install(monkeypatch, fake2)
    monkeypatch.setattr(cv_jobs, "ONBOARD_TIMEOUT", 0)
    fake_http(monkeypatch, lambda req: httpx.Response(200, text=inventory_ndjson([])))
    job = await run_job(client, (await client.post(f"/api/labs/{lab['id']}/cv/onboard")).json()["job_id"])
    assert job["result"]["complete"] is False
    assert all(n["terminattr_running"] is False and n["streaming"] is False for n in job["result"]["nodes"])
    assert all("/tmp/token" in c for c, _ in ran(fake2, "docker exec -i"))


async def test_onboard_guards(client, monkeypatch):
    install(monkeypatch, CVFakeSSH("x"))
    host_id = await mk_host()
    t_no_onb = await mk_target(client, onboarding=None)
    built = await mk_cv_lab(client, host_id=host_id, state=LabState.built)
    assert (await client.post(f"/api/labs/{built['id']}/cv/onboard")).status_code == 409  # no target
    await set_state(built["id"], LabState.deployed)
    assert (await client.post(f"/api/labs/{built['id']}/cv/onboard")).status_code == 409  # still no target
    assert (await bind(client, built["id"], t_no_onb["id"])).status_code == 200
    r = await client.post(f"/api/labs/{built['id']}/cv/onboard")
    assert r.status_code == 409 and "onboarding token" in r.text
    none_lab = await mk_cv_lab(client, host_id=host_id, cloudvision=None, state=LabState.deployed)
    r = await client.post(f"/api/labs/{none_lab['id']}/cv/onboard")
    assert r.status_code == 409 and "mode none" in r.text
    assert (await client.post("/api/labs/nope/cv/onboard")).status_code == 404


# ------------------------------------------------------------------ cv_deploy job
CV_RESULT = {
    "failed": False, "errors": [], "warnings": ["Device dc1-leaf1c not found on CloudVision: skipped"],
    "workspace": {"name": "AVD lab", "id": "ws-42", "state": "submitted", "change_control_id": "cc-7"},
    "change_control": {"id": "cc-7", "name": "AVD lab CC", "state": "pending approval"},
    "deployed_configs": [{"file": "/lab/intended/configs/dc1-spine1.cfg",
                          "device": {"hostname": "dc1-spine1", "serial_number": "CV0001", "exists_on_cv": True,
                                     "streaming": True}}],
    "skipped_configs": [{"file": "/lab/intended/configs/dc1-leaf1c.cfg",
                         "device": {"hostname": "dc1-leaf1c", "serial_number": "CV0009", "exists_on_cv": False,
                                    "streaming": None}}],
    "deployed_device_tags": [{"label": "lab", "value": "avdlab"}],
}


def ansible_output(result: dict[str, Any]) -> str:
    debug = json.dumps({"msg": cv_service.RESULT_BEGIN + json.dumps(result) + cv_service.RESULT_END}, indent=4)
    return ("PLAY [Deploy to CloudVision (generated by avdlab)]\n"
            "TASK [arista.avd.cv_deploy : Deploy device configurations and tags to CloudVision]\nchanged: [dc1-spine1]\n"
            f"TASK [avdlab result]\nok: [dc1-spine1] => {debug}\nPLAY RECAP\n")


async def test_cv_deploy_job(client, monkeypatch):
    host_id = await mk_host()
    t = await mk_target(client)
    lab = await mk_cv_lab(client, host_id=host_id, state=LabState.built)
    assert (await bind(client, lab["id"], t["id"])).status_code == 200
    fake = CVFakeSSH(lab["slug"])
    fake.play_output = ansible_output(CV_RESULT)
    install(monkeypatch, fake)

    body = {"submit_workspace": True, "run_change_control": True, "skip_missing_devices": True, "strict_tags": False,
            "workspace_name": "from avdlab"}
    # 0.8.0 pre-flight: an expired token fails fast with a precise cause, nothing runs on the host, last_deploy records it
    fake_http(monkeypatch, lambda req: httpx.Response(401, text="unauthorized"))
    r = await client.post(f"/api/labs/{lab['id']}/cv/deploy", json=body)
    assert r.status_code == 202, r.text
    job = await run_job(client, r.json()["job_id"], status="failed")
    assert "pre-flight failed" in job["error"] and "token rejected" in job["error"]
    assert not any("ansible-playbook" in c for c, _ in fake.calls)
    st = (await client.get(f"/api/labs/{lab['id']}/cv/status")).json()
    assert st["last_deploy"]["ok"] is False and st["last_deploy"]["workspace"] is None and st["last_deploy"]["errors"][0].startswith("pre-flight")

    fake_http(monkeypatch, lambda req: httpx.Response(200, text=inventory_ndjson([])))
    r = await client.post(f"/api/labs/{lab['id']}/cv/deploy", json=body)
    assert r.status_code == 202, r.text
    job = await run_job(client, r.json()["job_id"])
    res = job["result"]
    assert res["ok"] is True and res["params"] == body and res["target_id"] == t["id"]
    assert res["workspace"] == {"id": "ws-42", "name": "AVD lab", "state": "submitted",
                                "url": "https://www.arista.io/cv/provisioning/workspaces?ws=ws-42"}
    assert res["change_control"] == {"id": "cc-7", "name": "AVD lab CC", "state": "pending approval"}
    assert [d["hostname"] for d in res["devices"]] == ["dc1-leaf1c", "dc1-spine1"]
    assert res["devices"][0]["exists_on_cv"] is False and res["warnings"] and res["errors"] == []

    # playbook committed with env lookups (no token), role vars mapped from the body
    pb = yaml.safe_load((Path(lab["dir"]) / "playbooks/avdlab-cv-deploy.yml").read_text())[0]
    assert pb["hosts"] == "FABRIC" and pb["connection"] == "local"
    role_task = pb["tasks"][0]["block"][0]
    assert role_task["ansible.builtin.import_role"]["name"] == "arista.avd.cv_deploy"
    v = role_task["vars"]
    assert v["cv_server"] == "{{ lookup('env', 'AVDLAB_CV_SERVER') }}" and v["cv_token"] == "{{ lookup('env', 'AVDLAB_CV_TOKEN') }}"
    assert (v["cv_submit_workspace"], v["cv_run_change_control"], v["cv_skip_missing_devices"], v["cv_strict_tags"]) == \
        (True, True, True, False)
    assert v["cv_workspace_name"] == "from avdlab" and v["cv_register_detailed_results"] is True and v["cv_verify_certs"] is True
    assert "AVDLAB_CV_RESULT_BEGIN" in pb["tasks"][0]["always"][0]["ansible.builtin.debug"]["msg"]
    hist = (await client.get(f"/api/labs/{lab['id']}/history")).json()
    assert any(h["message"] == "cv_deploy: generated playbook" for h in hist)

    # env-file: created 0600, token on stdin only, used by docker run, removed afterwards
    env = "/home/ubuntu/.avdlab/cv-env.Ab12Cd"
    cmds = [c for c, _ in fake.calls]
    i_mk = next(i for i, c in enumerate(cmds) if "mktemp" in c)
    i_wr = next(i for i, c in enumerate(cmds) if f"cat > {env}" in c)
    i_run = next(i for i, c in enumerate(cmds) if "ansible-playbook" in c)
    i_rm = next(i for i, c in enumerate(cmds) if c == f"rm -f {env}")
    assert i_mk < i_wr < i_run < i_rm and "umask 077" in cmds[i_mk] and "chmod 600" in cmds[i_wr]
    assert f"--env-file {env}" in cmds[i_run] and f"-v /opt/avdlab/labs/{lab['slug']}:/lab -w /lab" in cmds[i_run]
    assert cmds[i_run].endswith("ansible-playbook -i inventory.yml playbooks/avdlab-cv-deploy.yml -v")
    assert any(f"AVDLAB_CV_TOKEN={CV_TOKEN}" in i and "AVDLAB_CV_SERVER=www.arista.io" in i for i in fake.inputs)
    log = read_log(job["id"])
    assert all(CV_TOKEN not in c for c in cmds) and CV_TOKEN not in log and ONB_TOKEN not in log
    assert fake.syncs and "workspace ws-42 state=submitted" in log

    row = await lab_row(lab["id"])
    assert row.state == LabState.built and row.settings["cv_last_deploy"]["workspace"]["id"] == "ws-42"
    st = (await client.get(f"/api/labs/{lab['id']}/cv/status")).json()
    assert st["last_deploy"]["change_control"]["id"] == "cc-7" and st["target"]["has_token"] is True

    # role failure: results still parsed + stored, job fails, env-file still removed
    failed = {**CV_RESULT, "failed": True, "errors": ["Workspace build failed: dc1-spine1 not streaming"],
              "workspace": {**CV_RESULT["workspace"], "state": "build failed"}}
    fake.play_output, fake.play_rc = ansible_output(failed) + "fatal: [dc1-spine1]: FAILED! => {}\n", 2
    fake.calls.clear()
    job = await run_job(client, (await client.post(f"/api/labs/{lab['id']}/cv/deploy", json={})).json()["job_id"], "failed")
    assert "not streaming" in job["error"]
    assert (await lab_row(lab["id"])).settings["cv_last_deploy"]["workspace"]["state"] == "build failed"
    assert any(c == f"rm -f {env}" for c, _ in fake.calls)

    # SSH error during the run → env-file removed in the finally
    def explode(f, cmd):
        raise RuntimeError("ssh connection lost")

    fake.responses["ansible-playbook -i inventory.yml playbooks/avdlab-cv-deploy.yml"] = explode
    fake.calls.clear()
    job = await run_job(client, (await client.post(f"/api/labs/{lab['id']}/cv/deploy")).json()["job_id"], "failed")
    assert "ssh connection lost" in job["error"] and fake.calls[-1][0] == f"rm -f {env}"


async def test_cv_deploy_guards(client, monkeypatch):
    install(monkeypatch, CVFakeSSH("x"))
    host_id = await mk_host()
    lab = await mk_cv_lab(client, host_id=host_id, state=LabState.built)
    r = await client.post(f"/api/labs/{lab['id']}/cv/deploy")
    assert r.status_code == 409 and "target" in r.text
    no_tok = await mk_target(client, token=None)
    assert (await bind(client, lab["id"], no_tok["id"])).status_code == 200
    r = await client.post(f"/api/labs/{lab['id']}/cv/deploy")
    assert r.status_code == 409 and "service-account token" in r.text
    draft = await mk_cv_lab(client, host_id=host_id, state=LabState.draft, built=False)
    assert (await client.post(f"/api/labs/{draft['id']}/cv/deploy")).status_code == 409
    no_host = await mk_cv_lab(client, host_id=None)
    assert (await client.post(f"/api/labs/{no_host['id']}/cv/deploy")).status_code == 409
    assert (await client.post(f"/api/labs/{lab['id']}/cv/deploy", json={"strict_tags": "maybe"})).status_code == 422
