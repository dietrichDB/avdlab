"""Phase 3b: per-device CloudVision tags (render → real pyavd build → structured configs / devices endpoint),
kill job (asyncio cancel + SSH close), kill docker containers on a host (monitor endpoint)."""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
import yaml
from sqlalchemy import delete

from avdlab.core.db import Host, Job, JobStatus, Lab, LabState, SessionLocal
from avdlab.hosts import monitor as mon
from avdlab.hosts import ssh as ssh_mod
from avdlab.hosts.ssh import CompletedProcess, SSHClient
from avdlab.jobs.engine import JobContext, job, read_log, runner
from avdlab.labs.build import build_lab_dir, device_summary
from avdlab.labs.templates import KnobError, coerce_knobs, get_template, render_lab

GLOBAL_CV = {"mode": "cvaas", "device_tags": {"site": "lab", "env": "dev"}}
PER_DEVICE = {"dc1-leaf1a": {"rack": "r1", "env": "prod"}, "dc1-spine1": {"role": "core"}}


def write_rendered(rendered, root: Path) -> Path:
    for rel, content in rendered.files().items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return root


def cv_tags(root: Path, host: str) -> list[dict[str, Any]]:
    sc = yaml.safe_load((root / "intended/structured_configs" / f"{host}.yml").read_text())
    return sc["metadata"]["cv_tags"]["device_tags"]


# ------------------------------------------------------------------ 1. per-device tags
def test_device_tags_knob_coercion_and_unknown_hosts():
    tpl = get_template("single-dc-l3ls")
    assert tpl.knob("device_tags") is not None
    for name in ("dual-dc-l3ls", "l2ls-fabric", "single-dc-multipod-l3ls", "custom"):
        assert get_template(name).knob("device_tags") is not None, name
    v = coerce_knobs(tpl, {"device_tags": "dc1-leaf1a: {rack: 1}\n"})
    assert v["device_tags"] == {"dc1-leaf1a": {"rack": "1"}}
    assert coerce_knobs(tpl, {})["device_tags"] == {}
    with pytest.raises(KnobError):
        coerce_knobs(tpl, {"device_tags": ["nope"]})
    with pytest.raises(KnobError):
        coerce_knobs(tpl, {"device_tags": {"dc1-leaf1a": {"rack": ""}}})
    with pytest.raises(KnobError) as ei:
        render_lab(tpl, {"device_tags": {"dc1-leaf9a": {"x": "y"}, "ghost": {"x": "y"}}})
    assert "dc1-leaf9a" in str(ei.value) and "ghost" in str(ei.value)
    rendered = render_lab(tpl, {"device_tags": PER_DEVICE})
    node = next(n for n in rendered.group_vars["DC1_L3_LEAVES"]["l3leaf"]["node_groups"][0]["nodes"] if n["name"] == "dc1-leaf1a")
    assert node["structured_config"] == {"metadata": {"cv_tags": {"device_tags": [
        {"name": "rack", "value": "r1"}, {"name": "env", "value": "prod"}]}}}


def test_device_tags_real_pyavd_build(tmp_path):
    """Global (cloudvision.device_tags → generate_cv_tags) and per-device (node structured_config) tags both land in
    intended/structured_configs/<host>.yml metadata.cv_tags; a per-device label overrides the global one."""
    from pyavd import validate_structured_config

    tpl = get_template("single-dc-l3ls")
    root = write_rendered(render_lab(tpl, {"cloudvision": GLOBAL_CV, "device_tags": PER_DEVICE}, slug="tags"), tmp_path / "lab")
    build_lab_dir(root, log=lambda _l: None)

    leaf1a = {t["name"]: t["value"] for t in cv_tags(root, "dc1-leaf1a")}
    assert leaf1a["site"] == "lab" and leaf1a["rack"] == "r1" and leaf1a["env"] == "prod"
    assert [t["name"] for t in cv_tags(root, "dc1-leaf1a")].count("env") == 1  # pyavd appends; the build dedupes
    assert leaf1a["topology_hint_type"] == "leaf"
    leaf1b = {t["name"]: t["value"] for t in cv_tags(root, "dc1-leaf1b")}
    assert leaf1b["env"] == "dev" and "rack" not in leaf1b
    assert {t["name"]: t["value"] for t in cv_tags(root, "dc1-spine1")}["role"] == "core"

    # cv_deploy input: metadata.cv_tags is schema-valid and every tag is {name: str, value: str}
    for f in (root / "intended/structured_configs").glob("*.yml"):
        sc = yaml.safe_load(f.read_text())
        res = validate_structured_config(sc)
        assert not res.validation_result.violations, (f.name, [v.message for v in res.validation_result.violations])
        for t in sc["metadata"]["cv_tags"]["device_tags"]:
            assert set(t) == {"name", "value"} and isinstance(t["name"], str) and isinstance(t["value"], str)

    rows = {r["hostname"]: r for r in device_summary(root)}
    assert rows["dc1-leaf1a"]["tags"] == {"site": "lab", "env": "prod", "rack": "r1"}
    assert rows["dc1-leaf1b"]["tags"] == {"site": "lab", "env": "dev"}


def test_device_tags_without_cloudvision_and_post_build_injection(tmp_path):
    """No cloudvision (no generate_cv_tags): per-device tags still render. The post-build injection (custom labs)
    merges over whatever the group_vars produced."""
    tpl = get_template("single-dc-l3ls")
    root = write_rendered(render_lab(tpl, {"device_tags": {"dc1-leaf2a": {"rack": "r2"}}}, slug="nocv"), tmp_path / "lab")
    build_lab_dir(root, log=lambda _l: None, device_tags={"dc1-leaf2b": {"rack": "r9"}, "dc1-leaf2a": {"rack": "r3"}})
    assert cv_tags(root, "dc1-leaf2a") == [{"name": "rack", "value": "r3"}]
    assert cv_tags(root, "dc1-leaf2b") == [{"name": "rack", "value": "r9"}]
    sc = yaml.safe_load((root / "intended/structured_configs/dc1-spine1.yml").read_text())
    assert "cv_tags" not in (sc.get("metadata") or {})


async def test_device_tags_api_422_and_devices_endpoint(client):
    r = await client.post("/api/labs", json={"name": "Tags bad", "template": "single-dc-l3ls",
                                             "knobs": {"device_tags": {"nope-1": {"a": "b"}}}})
    assert r.status_code == 422, r.text
    assert "nope-1" in json.dumps(r.json())

    r = await client.post("/api/labs", json={"name": "Tags Lab", "template": "single-dc-l3ls",
                                             "knobs": {"cloudvision": GLOBAL_CV, "device_tags": PER_DEVICE}})
    assert r.status_code == 201, r.text
    lab = r.json()
    assert lab["knobs"]["device_tags"] == PER_DEVICE
    r = await client.patch(f"/api/labs/{lab['id']}", json={"knobs": {"device_tags": {"ghost": {"a": "b"}}}})
    assert r.status_code == 422 and "ghost" in json.dumps(r.json())
    jid = (await client.post(f"/api/labs/{lab['id']}/build")).json()["job_id"]
    await runner.wait(jid)
    assert (await client.get(f"/api/jobs/{jid}")).json()["status"] == "succeeded", read_log(jid)[-2000:]
    devs = {d["hostname"]: d for d in (await client.get(f"/api/labs/{lab['id']}/devices")).json()}
    assert devs["dc1-leaf1a"]["tags"] == {"site": "lab", "env": "prod", "rack": "r1"}
    assert devs["dc1-spine1"]["tags"] == {"site": "lab", "env": "dev", "role": "core"}
    assert (await client.delete(f"/api/labs/{lab['id']}")).status_code == 204


# ------------------------------------------------------------------ 2. kill job
@job("test.sleeper")
async def _sleeper(ctx: JobContext) -> dict:
    while True:
        await asyncio.sleep(0.05)


class FakeConnSSH:
    """Stands in for an SSHClient attached to the job context."""

    def __init__(self) -> None:
        self.closed = 0

    async def close(self) -> None:
        self.closed += 1


_FAKES: dict[str, FakeConnSSH] = {}


@job("test.ssh_sleeper")
async def _ssh_sleeper(ctx: JobContext) -> dict:
    fake = _FAKES[ctx.params["key"]]
    ctx.attach_ssh(fake)
    while True:
        await asyncio.sleep(0.05)


class _AsyncsshConn:
    def __init__(self) -> None:
        self.closed = 0

    def close(self) -> None:
        self.closed += 1

    async def wait_closed(self) -> None:
        return None


_CONNS: dict[str, _AsyncsshConn] = {}


@job("test.real_connect")
async def _real_connect(ctx: JobContext) -> dict:
    host = Host(name="h", address="192.0.2.1", ssh_user="u", ssh_port=22, password_secret_id="pw")
    async with SSHClient.connect(host, None, ctx=ctx):  # type: ignore[arg-type]
        assert len(ctx.ssh_clients) == 1
        while True:
            await asyncio.sleep(0.05)


async def _wait_status(job_id: str, status: JobStatus) -> None:
    for _ in range(100):
        async with SessionLocal() as s:
            if (await s.get(Job, job_id)).status == status:
                return
        await asyncio.sleep(0.02)
    raise AssertionError(f"job {job_id} never reached {status}")


async def test_kill_running_job(client):
    jid = await runner.submit("test.sleeper")
    await _wait_status(jid, JobStatus.running)
    r = await client.post(f"/api/jobs/{jid}/cancel")
    assert r.json() == {"cancelled": True, "was_running": True}
    await runner.wait(jid)
    j = (await client.get(f"/api/jobs/{jid}")).json()
    assert j["status"] == "cancelled" and j["ended_at"]
    assert "job cancelled" in read_log(jid)
    assert (await client.post(f"/api/jobs/{jid}/cancel")).json() == {"cancelled": False}  # finished
    assert (await client.post("/api/jobs/nope/cancel")).json() == {"cancelled": False}


async def test_kill_job_before_it_starts():
    jid = await runner.submit("test.sleeper")
    res = await runner.cancel(jid)  # the task has not run a single step yet
    assert res == {"cancelled": True, "was_running": False}
    await runner.wait(jid)
    async with SessionLocal() as s:
        row = await s.get(Job, jid)
        assert row.status == JobStatus.cancelled and row.ended_at is not None
    assert "job cancelled" in read_log(jid)


async def test_kill_job_closes_attached_ssh():
    key = uuid.uuid4().hex
    _FAKES[key] = fake = FakeConnSSH()
    jid = await runner.submit("test.ssh_sleeper", params={"key": key})
    await _wait_status(jid, JobStatus.running)
    await asyncio.sleep(0.1)
    assert (await runner.cancel(jid))["cancelled"] is True
    await runner.wait(jid)
    assert fake.closed >= 1
    async with SessionLocal() as s:
        assert (await s.get(Job, jid)).status == JobStatus.cancelled


async def test_sshclient_connect_attaches_to_ctx(monkeypatch):
    from avdlab.core.db import init_db

    await init_db()  # runs standalone too
    key = uuid.uuid4().hex
    _CONNS[key] = conn = _AsyncsshConn()

    async def reveal(session, secret_id):  # noqa: ANN001
        return "pw"

    async def known_host(address, port, log=None, **kw):  # noqa: ANN001, ANN003 - 1.0.1 passes tunnel=
        return Path("/dev/null")

    async def fake_connect(*a, **kw):  # noqa: ANN002, ANN003
        return conn

    monkeypatch.setattr(ssh_mod.secret_store, "reveal", reveal)
    monkeypatch.setattr(ssh_mod, "ensure_known_host", known_host)
    monkeypatch.setattr(ssh_mod.asyncssh, "connect", fake_connect)
    jid = await runner.submit("test.real_connect")
    await _wait_status(jid, JobStatus.running)
    await asyncio.sleep(0.1)
    await runner.cancel(jid)
    await runner.wait(jid)
    assert conn.closed >= 1
    async with SessionLocal() as s:
        assert (await s.get(Job, jid)).status == JobStatus.cancelled


# ------------------------------------------------------------------ 3. kill containers
class FakeDockerSSH:
    def __init__(self, slug: str) -> None:
        self.cmds: list[str] = []
        self.ps = "\n".join(json.dumps(d) for d in [
            {"Names": f"clab-{slug}-dc1-spine1", "Image": "arista/ceos:4.34.2F", "Status": "Up", "State": "running",
             "Labels": f"containerlab={slug},avdlab.role=spine,clab-node-name=dc1-spine1"},
            {"Names": "portainer", "Image": "portainer/portainer-ce", "Status": "Up", "State": "running", "Labels": ""},
        ])

    async def run(self, cmd: str, **kw: Any) -> CompletedProcess:
        self.cmds.append(cmd)
        if "docker ps -a" in cmd:
            return CompletedProcess(0, self.ps, "", command=cmd)
        if "docker rm -f" in cmd or "docker stop" in cmd:
            return CompletedProcess(0, cmd.split()[-2] + "\n", "", command=cmd)
        return CompletedProcess(1, "", "unexpected", command=cmd)


def connector(fake: FakeDockerSSH):
    @asynccontextmanager
    async def connect(host, session):  # noqa: ANN001
        yield fake
    return connect


async def test_kill_container_endpoint(client, caplog):
    slug = f"k{uuid.uuid4().hex[:6]}"
    async with SessionLocal() as s:
        h = Host(name=f"kill-{slug}", address="192.0.2.88", ssh_user="ubuntu",
                 capabilities={"docker": {"installed": True, "user_can_run": True}})
        s.add(h)
        await s.commit()
        lab = Lab(name=f"Lab {slug}", slug=slug, template="single-dc-l3ls", host_id=h.id, state=LabState.deployed)
        s.add(lab)
        await s.commit()
        host_id, lab_id = h.id, lab.id
    fake = FakeDockerSSH(slug)
    old = mon.monitor
    m = mon.set_monitor(mon.HostMonitor(interval=20, connect=connector(fake), persist=False))
    cname = f"clab-{slug}-dc1-spine1"
    url = f"/api/hosts/{host_id}/containers"
    try:
        assert (await client.delete(f"{url}/ghost?force=true")).status_code == 404
        # lab container of a deployed lab: refused without force + typed confirmation
        r = await client.delete(f"{url}/{cname}?force=true")
        assert r.status_code == 409 and r.json()["detail"]["lab_id"] == lab_id
        assert (await client.delete(f"{url}/{cname}?force=false")).status_code == 409
        r = await client.request("DELETE", f"{url}/{cname}?force=true", json={"confirm": "wrong"})
        assert r.status_code == 409
        assert not any("docker rm" in c or "docker stop" in c for c in fake.cmds)
        with caplog.at_level(logging.INFO, logger="avdlab.audit"):
            r = await client.request("DELETE", f"{url}/{cname}?force=true", json={"confirm": cname})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["name"] == cname and body["removed"] is True and body["action"] == "rm" and cname in body["output"]
        assert any(c == f"docker rm -f {cname} 2>&1" for c in fake.cmds)
        assert any("container rm" in rec.getMessage() and cname in rec.getMessage() for rec in caplog.records)
        r = await client.request("DELETE", f"{url}/{cname}?force=false", json={"confirm": cname})
        assert r.status_code == 200 and r.json()["action"] == "stop"
        # unrelated container: plain stop, no confirmation needed
        r = await client.delete(f"{url}/portainer?force=false")
        assert r.status_code == 200 and r.json()["action"] == "stop" and r.json()["removed"] is True
        assert any(c == "docker stop portainer 2>&1" for c in fake.cmds)
        # lab not deployed any more → no confirmation needed
        async with SessionLocal() as s:
            (await s.get(Lab, lab_id)).state = LabState.destroyed
            await s.commit()
        assert (await client.delete(f"{url}/{cname}?force=true")).status_code == 200
    finally:
        await m.stop()
        mon.set_monitor(old)
        async with SessionLocal() as s:
            await s.execute(delete(Lab).where(Lab.id == lab_id))
            await s.execute(delete(Host).where(Host.id == host_id))
            await s.commit()
