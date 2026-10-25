"""Executors (Module 2b): containerlab deploy / destroy / status / validate with a scripted fake SSH, plus the labs API."""
from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
import yaml

from avdlab.core.db import Host, Image, Lab, LabState, SessionLocal, ValidationRun
from avdlab.executors import base as exec_base
from avdlab.executors import clab as clab_exec
from avdlab.hosts.ssh import CompletedProcess, SSHClient
from avdlab.jobs.engine import read_log, runner

from .test_hosts import FakeSSH

PASSWORD = "Sup3r-S3cret!"
IMAGE = "arista/ceos:4.34.2F"
NODES = [("dc1-spine1", "172.16.1.11"), ("dc1-spine2", "172.16.1.12"), ("dc1-leaf1a", "172.16.1.101"),
         ("dc1-leaf1b", "172.16.1.102")]


def inspect_json(lab_name: str) -> str:
    """containerlab >= 0.60 `inspect --format json` shape: {"<lab>": [containers]} (+ one linux endpoint)."""
    rows = [{"lab_name": lab_name, "labPath": "clab/topology.clab.yml", "name": n, "container_id": f"c0ffee{i}",
             "image": IMAGE, "kind": "arista_ceos", "state": "running", "status": "Up 2 minutes",
             "ipv4_address": f"{ip}/24", "ipv6_address": "N/A", "owner": "ubuntu"} for i, (n, ip) in enumerate(NODES)]
    rows.append({"lab_name": lab_name, "name": "dc1-leaf1-server1", "container_id": "beef", "image": "alpine:3",
                 "kind": "linux", "state": "running", "status": "Up 2 minutes", "ipv4_address": "N/A",
                 "ipv6_address": "N/A", "owner": "ubuntu"})
    return json.dumps({lab_name: rows})


ANTA_REPORT: list[dict[str, Any]] = [
    {"name": "dc1-spine1", "test": "VerifyBGPSpecificPeers", "categories": ["bgp"], "description": "BGP peers",
     "result": "success", "messages": [], "custom_field": None},
    {"name": "dc1-spine1", "test": "VerifyInterfacesStatus", "categories": ["interfaces"], "description": "intf",
     "result": "success", "messages": [], "custom_field": None},
    {"name": "dc1-leaf1a", "test": "VerifyMlagStatus", "categories": ["mlag"], "description": "MLAG",
     "result": "success", "messages": [], "custom_field": None},
    {"name": "dc1-leaf1a", "test": "VerifyLLDPNeighbors", "categories": ["connectivity"], "description": "LLDP",
     "result": "skipped", "messages": ["no LLDP"], "custom_field": None},
]


class ClabFakeSSH(FakeSSH):
    """FakeSSH + file ops + stdin capture; curl answers eAPI after `eapi_fail_first` failed probes."""

    def __init__(self, lab_name: str, *, image_present: bool = True, eapi_fail_first: int = 3,
                 report: list[dict[str, Any]] | None = None, remote_topo: bool = True) -> None:
        super().__init__(default_rc=0)
        self.inputs: list[str] = []
        self.puts: list[tuple[str, str]] = []
        self.gets: list[str] = []
        self.syncs: list[tuple[str, str, list[str]]] = []
        self.curl_calls = 0
        self.eapi_fail_first = eapi_fail_first
        self.report = ANTA_REPORT if report is None else report
        self.image_present = image_present
        self.responses.update({
            "docker image inspect": lambda f, c: (0, "") if f.image_present else (1, ""),
            "docker import": self._import,
            "test -f": (0, "") if remote_topo else (1, ""),
            "containerlab deploy": (0, "INFO Creating lab...\nINFO Adding containerlab host entries\n"),
            "containerlab inspect": (0, inspect_json(lab_name)),
            "command-api": self._curl,
            "containerlab destroy": (0, "INFO Destroying lab\n"),
            "import anta": (0, "1.10.0\n"),
            "id -u": (0, "1000\n1000\n"),
            "ansible-playbook": (2, "PLAY [Fabric validation]\nTASK [arista.avd.anta_runner : Run ANTA]\nfatal: ...\n"),
            "--entrypoint chown": (0, ""),
        })

    def _import(self, fake: "ClabFakeSSH", cmd: str) -> tuple[int, str]:
        self.image_present = True
        return 0, "sha256:1234"

    def _curl(self, fake: "ClabFakeSSH", cmd: str) -> tuple[int, str]:
        self.curl_calls += 1
        if self.curl_calls <= self.eapi_fail_first:
            return 7, ""
        return 0, json.dumps({"jsonrpc": "2.0", "id": 1, "result": [{"version": "4.34.2F"}]})

    async def run(self, cmd: str, *, sudo: bool = False, timeout: float | None = 600, log: Any = None,
                  input: str | None = None) -> CompletedProcess:
        if input is not None:
            self.inputs.append(input)
        return await super().run(cmd, sudo=sudo, timeout=timeout, log=log, input=input)

    async def put(self, local: str | Path, remote: str) -> None:
        self.puts.append((str(local), remote))

    async def get(self, remote: str, local: str | Path) -> None:
        self.gets.append(remote)
        fmt = remote.rsplit(".", 1)[-1]
        content = {"json": json.dumps(self.report), "md": "# ANTA Report\n\n| Device | Test |\n",
                   "csv": "name,test,result\n"}[fmt]
        Path(local).write_text(content)

    async def rsync_dir(self, local_dir: str | Path, remote_dir: str, *, exclude: Any = (), delete: bool = False,
                        log: Any = None) -> None:
        self.syncs.append((str(local_dir), remote_dir, list(exclude)))


def install(monkeypatch: pytest.MonkeyPatch, fake: FakeSSH) -> None:
    @asynccontextmanager
    async def fake_connect(cls, host, session, **kw):  # noqa: ANN001
        yield fake

    monkeypatch.setattr(SSHClient, "connect", classmethod(fake_connect))
    monkeypatch.setattr(clab_exec, "EAPI_POLL_INTERVAL", 0)


def ran(fake: FakeSSH, needle: str) -> list[tuple[str, bool]]:
    return [(c, s) for c, s in fake.calls if needle in c]


_CREATED: dict[str, list[str]] = {"hosts": [], "labs": [], "images": []}


@pytest.fixture(autouse=True)
async def _cleanup():
    """The test DB is shared by all modules: remove the hosts/labs/images created here so other tests' listings stay exact."""
    yield
    if not any(_CREATED.values()):
        return
    from sqlalchemy import delete, update

    from avdlab.core.db import Job
    from avdlab.labs import service

    async with SessionLocal() as s:
        labs = [await s.get(Lab, i) for i in _CREATED["labs"]]
        await s.execute(delete(ValidationRun).where(ValidationRun.lab_id.in_(_CREATED["labs"])))
        await s.execute(update(Job).where(Job.lab_id.in_(_CREATED["labs"])).values(lab_id=None))
        await s.execute(update(Job).where(Job.host_id.in_(_CREATED["hosts"])).values(host_id=None))
        await s.execute(delete(Lab).where(Lab.id.in_(_CREATED["labs"])))
        await s.execute(delete(Host).where(Host.id.in_(_CREATED["hosts"])))
        await s.execute(delete(Image).where(Image.id.in_(_CREATED["images"])))
        await s.commit()
    for lab in labs:
        if lab is not None:
            service.delete_lab_dir(lab.slug)
    for v in _CREATED.values():
        v.clear()


CAPS = {"docker": {"installed": True, "user_can_run": True}, "containerlab": {"installed": True, "version": "0.68.0"}}


async def mk_host(caps: dict[str, Any] | None = None) -> str:
    async with SessionLocal() as s:
        h = Host(name=f"clab-{uuid.uuid4().hex[:6]}", address="192.0.2.10", ssh_user="ubuntu",
                 capabilities=CAPS if caps is None else caps)
        s.add(h)
        await s.commit()
        _CREATED["hosts"].append(h.id)
        return h.id


async def set_state(lab_id: str, state: LabState) -> None:
    async with SessionLocal() as s:
        row = await s.get(Lab, lab_id)
        row.state = state
        await s.commit()


def fake_build(lab_dir: Path, topo_name: str, image: str = IMAGE) -> None:
    """What lab.build leaves behind that the executor needs: the clab topology and structured configs."""
    topo = {"name": topo_name, "prefix": "", "mgmt": {"network": f"clab-mgmt-{topo_name}", "ipv4-subnet": "172.16.1.0/24"},
            "topology": {"kinds": {"arista_ceos": {"image": image}},
                         "nodes": {n: {"kind": "arista_ceos", "mgmt-ipv4": ip,
                                       "startup-config": f"../intended/configs/{n}.cfg"} for n, ip in NODES}}}
    (lab_dir / "clab").mkdir(parents=True, exist_ok=True)
    (lab_dir / "clab" / "topology.clab.yml").write_text(yaml.safe_dump(topo))
    sc = lab_dir / "intended" / "structured_configs"
    sc.mkdir(parents=True, exist_ok=True)
    for n, _ip in NODES:
        (sc / f"{n}.yml").write_text(f"hostname: {n}\n")


async def mk_lab(client, *, host_id: str | None, state: LabState = LabState.built, built: bool = True,
                 image: str = IMAGE) -> dict[str, Any]:
    name = f"exec-{uuid.uuid4().hex[:6]}"
    r = await client.post("/api/labs", json={"name": name, "template": "single-dc-l3ls", "host_id": host_id,
                                             "knobs": {"device_password": PASSWORD}})
    assert r.status_code == 201, r.text
    lab = r.json()
    _CREATED["labs"].append(lab["id"])
    if built:
        fake_build(Path(lab["dir"]), lab["slug"], image)
    await set_state(lab["id"], state)
    return lab


async def run_job(client, job_id: str, status: str = "succeeded") -> dict[str, Any]:
    await runner.wait(job_id)
    job = (await client.get(f"/api/jobs/{job_id}")).json()
    assert job["status"] == status, (job["error"], read_log(job_id)[-3000:])
    return job


async def lab_row(lab_id: str) -> Lab:
    async with SessionLocal() as s:
        return await s.get(Lab, lab_id)


# ------------------------------------------------------------------ pure helpers
def test_parse_inspect_shapes():
    nodes = clab_exec.parse_inspect(inspect_json("demo"), "demo")
    assert [n["name"] for n in nodes] == ["dc1-leaf1-server1", "dc1-leaf1a", "dc1-leaf1b", "dc1-spine1", "dc1-spine2"]
    spine = next(n for n in nodes if n["name"] == "dc1-spine1")
    assert spine == {"name": "dc1-spine1", "kind": "arista_ceos", "state": "running", "mgmt_ip": "172.16.1.11",
                     "image": IMAGE, "eapi": False}
    server = next(n for n in nodes if n["kind"] == "linux")
    assert server["mgmt_ip"] is None and server["eapi"] is None
    # older shape with prefixed names
    old = json.dumps({"containers": [{"lab_name": "demo", "name": "clab-demo-dc1-spine1", "kind": "ceos", "state": "running",
                                      "image": IMAGE, "ipv4_address": "172.16.1.11/24"}]})
    assert clab_exec.parse_inspect(old)[0]["name"] == "dc1-spine1"
    assert clab_exec.parse_inspect("INFO no containers found") == []


def test_anta_totals_and_playbook():
    totals, by_dev = exec_base.parse_anta_results(ANTA_REPORT + [{"name": "dc1-leaf1a", "test": "X", "result": "failure"}])
    assert totals == {"success": 3, "failure": 1, "error": 0, "skipped": 1, "unset": 0, "total": 5}
    assert by_dev["dc1-leaf1a"]["failure"] == 1 and by_dev["dc1-spine1"]["success"] == 2 and by_dev["dc1-spine1"]["total"] == 2
    pb = yaml.safe_load(clab_exec.validate_playbook("FABRIC", exec_base.ValidateParams(tags=["leaf"], skip_tests=["VerifyNTP"])))
    play = pb[0]
    assert play["hosts"] == "FABRIC" and play["tasks"][0]["ansible.builtin.import_role"]["name"] == "arista.avd.anta_runner"
    assert play["vars"] == {"avd_catalogs_extra_fabric_validation": True, "user_catalogs_enabled": False,
                            "anta_report_exclude_statuses": [], "avd_catalogs_filters": [{"skip_tests": ["VerifyNTP"]}],
                            "anta_runner_tags": ["leaf"]}
    bare = yaml.safe_load(clab_exec.validate_playbook("F", exec_base.ValidateParams(skip_tests=[])))[0]["vars"]
    assert "avd_catalogs_filters" not in bare and "anta_runner_tags" not in bare


def test_get_executor_targets():
    assert isinstance(exec_base.get_executor(Lab(target="clab")), clab_exec.ClabExecutor)
    from avdlab.executors.act import ActExecutor  # phase 4 (D): stage 2 implemented

    assert isinstance(exec_base.get_executor(Lab(target="act")), ActExecutor)


def test_templates_carry_httpapi_vars():
    from avdlab.labs.templates import get_template, render_lab

    for name in ("single-dc-l3ls", "dual-dc-l3ls", "l2ls-fabric", "single-dc-multipod-l3ls"):
        hv = render_lab(get_template(name), {"device_password": "pw"}, slug="t").hostvars()
        any_host = next(iter(hv.values()))
        assert any_host["ansible_connection"] == "ansible.netcommon.httpapi", name
        assert any_host["ansible_httpapi_use_ssl"] is True and any_host["ansible_password"] == "pw", name


# ------------------------------------------------------------------ deploy
async def test_deploy_happy_path(client, monkeypatch):
    host_id = await mk_host()
    lab = await mk_lab(client, host_id=host_id)
    fake = ClabFakeSSH(lab["slug"], eapi_fail_first=3)
    install(monkeypatch, fake)

    r = await client.post(f"/api/labs/{lab['id']}/deploy", json={"reconfigure": True, "wait_eapi": True, "wait_timeout": 60})
    assert r.status_code == 202, r.text
    job = await run_job(client, r.json()["job_id"])
    res = job["result"]
    assert sorted(res["ready"]) == sorted(n for n, _ in NODES) and res["pending"] == []
    assert len(res["nodes"]) == 5 and {n["name"]: n["eapi"] for n in res["nodes"]}["dc1-spine2"] is True

    remote = f"/opt/avdlab/labs/{lab['slug']}"
    assert fake.syncs and fake.syncs[0][1] == remote and ".git" in fake.syncs[0][2] and "anta/reports" in fake.syncs[0][2]
    deploy_cmd = ran(fake, "containerlab deploy")
    assert deploy_cmd == [(f"cd {remote} && containerlab deploy -t clab/topology.clab.yml --reconfigure", True)]
    assert ran(fake, "containerlab inspect")[0][1] is True
    assert fake.curl_calls == 3 + 4  # 3 failed probes, then one per node
    # the device password travels on stdin only: never on a command line, never in the log
    log = read_log(job["id"])
    assert all(PASSWORD not in c for c, _ in fake.calls) and PASSWORD not in log
    assert any(PASSWORD in i for i in fake.inputs)
    assert "[eapi] pending:" in log and "[eapi] dc1-spine1 (172.16.1.11) ready" in log

    row = await lab_row(lab["id"])
    assert row.state == LabState.deployed and row.settings["deployed_at"] and len(row.settings["last_inspect"]["nodes"]) == 5
    # a deployed lab cannot be deleted
    assert (await client.delete(f"/api/labs/{lab['id']}")).status_code == 409


async def test_deploy_refused(client, monkeypatch):
    install(monkeypatch, ClabFakeSSH("x"))
    host_id = await mk_host()
    draft = await mk_lab(client, host_id=host_id, state=LabState.draft, built=False)
    r = await client.post(f"/api/labs/{draft['id']}/deploy")
    assert r.status_code == 409 and "build" in r.text
    no_host = await mk_lab(client, host_id=None)
    assert (await client.post(f"/api/labs/{no_host['id']}/deploy")).status_code == 409
    no_topo = await mk_lab(client, host_id=host_id, built=False)
    assert (await client.post(f"/api/labs/{no_topo['id']}/deploy")).status_code == 409
    assert (await client.post(f"/api/labs/{draft['id']}/validate")).status_code == 409


async def test_deploy_needs_bootstrapped_host(client, monkeypatch):
    host_id = await mk_host({"docker": {"installed": True}, "containerlab": {"installed": False}})
    lab = await mk_lab(client, host_id=host_id)
    install(monkeypatch, ClabFakeSSH(lab["slug"]))
    job = await run_job(client, (await client.post(f"/api/labs/{lab['id']}/deploy")).json()["job_id"], "failed")
    assert "containerlab" in job["error"] and "Bootstrap" in job["error"]


async def test_deploy_image_missing(client, monkeypatch):
    host_id = await mk_host()
    lab = await mk_lab(client, host_id=host_id, image="arista/ceos:9.99.1Z")  # no Image row for this tag
    fake = ClabFakeSSH(lab["slug"], image_present=False)
    install(monkeypatch, fake)
    job = await run_job(client, (await client.post(f"/api/labs/{lab['id']}/deploy")).json()["job_id"], "failed")
    assert "image arista/ceos:9.99.1Z not on host" in job["error"] and "push it from Images" in job["error"]
    assert not ran(fake, "containerlab deploy")
    assert (await lab_row(lab["id"])).state == LabState.built


async def test_deploy_imports_known_image(client, monkeypatch, tmp_path):
    TAG = "arista/ceos:9.99.2Z"
    tarball = tmp_path / "cEOS-lab-9.99.2Z.tar.xz"
    tarball.write_bytes(b"rootfs")
    async with SessionLocal() as s:
        from sqlalchemy import select

        img = (await s.execute(select(Image).where(Image.docker_tag == TAG))).scalars().first()
        if img is None:
            img = Image(fmt="cEOS", version="9.99.2Z", docker_tag=TAG, present_on_hosts=[])
            s.add(img)
            await s.flush()
            _CREATED["images"].append(img.id)
        img.local_path = str(tarball)
        await s.commit()
    host_id = await mk_host()
    lab = await mk_lab(client, host_id=host_id, image=TAG)
    fake = ClabFakeSSH(lab["slug"], image_present=False, eapi_fail_first=0)
    install(monkeypatch, fake)
    await run_job(client, (await client.post(f"/api/labs/{lab['id']}/deploy", json={"wait_eapi": False})).json()["job_id"])
    assert fake.puts and fake.puts[0][0] == str(tarball)
    assert ran(fake, f"docker import /opt/avdlab/images/{tarball.name} {TAG}")
    assert fake.curl_calls == 0
    async with SessionLocal() as s:
        from sqlalchemy import select

        img = (await s.execute(select(Image).where(Image.docker_tag == TAG))).scalars().first()
        assert host_id in img.present_on_hosts


# ------------------------------------------------------------------ destroy / status
async def test_destroy_and_status(client, monkeypatch):
    host_id = await mk_host()
    lab = await mk_lab(client, host_id=host_id)
    # never deployed → fast answer without SSH
    fake = ClabFakeSSH(lab["slug"], eapi_fail_first=0)
    install(monkeypatch, fake)
    r = await client.get(f"/api/labs/{lab['id']}/status")
    assert r.status_code == 200 and r.json()["deployed"] is False and r.json()["nodes"] == [] and fake.calls == []

    await set_state(lab["id"], LabState.deployed)
    st = (await client.get(f"/api/labs/{lab['id']}/status")).json()
    assert st["deployed"] is True and st["cached"] is False and st["error"] is None
    nodes = {n["name"]: n for n in st["nodes"]}
    assert nodes["dc1-leaf1a"] == {"name": "dc1-leaf1a", "kind": "arista_ceos", "state": "running", "mgmt_ip": "172.16.1.101",
                                   "image": IMAGE, "eapi": True}
    assert nodes["dc1-leaf1-server1"]["eapi"] is None
    assert (await lab_row(lab["id"])).settings["last_status"]["deployed"] is True

    job = await run_job(client, (await client.post(f"/api/labs/{lab['id']}/status/refresh")).json()["job_id"])
    assert job["result"]["deployed"] is True and len(job["result"]["nodes"]) == 5

    # destroy tolerates "lab not found"
    fake.responses["containerlab destroy"] = (1, "Error: lab not found")
    job = await run_job(client, (await client.post(f"/api/labs/{lab['id']}/destroy")).json()["job_id"])
    assert ran(fake, "containerlab destroy -t clab/topology.clab.yml --cleanup")[0][1] is True
    row = await lab_row(lab["id"])
    assert row.state == LabState.destroyed and row.settings["last_status"]["deployed"] is False

    fake.responses["containerlab destroy"] = (1, "Error: permission denied")
    job = await run_job(client, (await client.post(f"/api/labs/{lab['id']}/destroy")).json()["job_id"], "failed")
    assert "destroy failed" in job["error"]

    # status with ssh failing → last known status + error
    async def boom(*a, **kw):
        raise RuntimeError("ssh unreachable")

    monkeypatch.setattr(clab_exec.ClabExecutor, "status", boom)
    await set_state(lab["id"], LabState.deployed)
    st = (await client.get(f"/api/labs/{lab['id']}/status")).json()
    assert st["cached"] is True and "ssh unreachable" in st["error"]


# ------------------------------------------------------------------ validate
async def test_validate_flow(client, monkeypatch):
    host_id = await mk_host()
    lab = await mk_lab(client, host_id=host_id, state=LabState.deployed)
    fake = ClabFakeSSH(lab["slug"])
    install(monkeypatch, fake)

    r = await client.post(f"/api/labs/{lab['id']}/validate",
                          json={"extra_fabric_validation": False, "skip_tests": ["VerifyNTP"], "tags": [], "user_catalogs": False})
    assert r.status_code == 202, r.text
    job = await run_job(client, r.json()["job_id"])
    res = job["result"]
    assert res["totals"] == {"success": 3, "failure": 0, "error": 0, "skipped": 1, "unset": 0, "total": 4}
    assert res["by_device"]["dc1-spine1"]["success"] == 2 and res["ansible_rc"] == 2
    remote = f"/opt/avdlab/labs/{lab['slug']}"
    docker = ran(fake, "ansible-playbook")[0][0]
    assert f"--network clab-mgmt-{lab['slug']}" in docker and f"-v {remote}:/lab" in docker
    assert "ansible-playbook -i inventory.yml playbooks/avdlab-validate.yml" in docker
    assert sorted(fake.gets) == sorted(f"{remote}/anta/reports/anta_report.{f}" for f in ("json", "md", "csv"))
    assert ran(fake, "--entrypoint chown") and fake.syncs

    root = Path(lab["dir"])
    pb = yaml.safe_load((root / "playbooks/avdlab-validate.yml").read_text())[0]
    assert pb["vars"]["avd_catalogs_filters"] == [{"skip_tests": ["VerifyNTP"]}]
    run_id = res["validation_run_id"]
    assert res["report_paths"]["json"] == f"anta/runs/{run_id}/anta_report.json" and (root / res["report_paths"]["md"]).exists()
    assert (await lab_row(lab["id"])).state == LabState.validated
    async with SessionLocal() as s:
        vr = await s.get(ValidationRun, run_id)
        assert vr.job_id == job["id"] and vr.totals["success"] == 3

    runs = (await client.get(f"/api/labs/{lab['id']}/validations")).json()
    assert runs[0]["id"] == run_id and runs[0]["totals"]["total"] == 4 and runs[0]["job_id"] == job["id"]
    rep = await client.get(f"/api/labs/{lab['id']}/validations/{run_id}/report")
    assert rep.status_code == 200 and rep.json()[0]["test"] == "VerifyBGPSpecificPeers"
    md = await client.get(f"/api/labs/{lab['id']}/validations/{run_id}/report", params={"format": "md"})
    assert md.status_code == 200 and md.text.startswith("# ANTA Report") and md.headers["content-type"].startswith("text/markdown")
    assert (await client.get(f"/api/labs/{lab['id']}/validations/{run_id}/report", params={"format": "pdf"})).status_code == 422
    assert (await client.get(f"/api/labs/{lab['id']}/validations/nope/report")).status_code == 404
    hist = (await client.get(f"/api/labs/{lab['id']}/history")).json()
    assert hist[0]["message"].startswith("validate: 3/4 passed")

    # a failing run keeps the lab "deployed"
    fake.report = ANTA_REPORT + [{"name": "dc1-leaf1b", "test": "VerifyBGPPeerCount", "categories": ["bgp"],
                                  "result": "failure", "messages": ["down"], "description": "", "custom_field": None}]
    job = await run_job(client, (await client.post(f"/api/labs/{lab['id']}/validate", json={})).json()["job_id"])
    assert job["result"]["totals"]["failure"] == 1
    assert (await lab_row(lab["id"])).state == LabState.deployed
    assert len((await client.get(f"/api/labs/{lab['id']}/validations")).json()) == 2

    # delete refused while deployed; allowed after destroy (validation runs removed with the lab)
    assert (await client.delete(f"/api/labs/{lab['id']}")).status_code == 409
    await run_job(client, (await client.post(f"/api/labs/{lab['id']}/destroy")).json()["job_id"])
    assert (await client.delete(f"/api/labs/{lab['id']}")).status_code == 204


async def test_validate_errors(client, monkeypatch):
    host_id = await mk_host()
    lab = await mk_lab(client, host_id=host_id, state=LabState.deployed)
    fake = ClabFakeSSH(lab["slug"])
    fake.responses["import anta"] = (0, "1.4.0\n")
    install(monkeypatch, fake)
    job = await run_job(client, (await client.post(f"/api/labs/{lab['id']}/validate")).json()["job_id"], "failed")
    assert "lacks ANTA >= 1.8" in job["error"] and not ran(fake, "ansible-playbook")

    fake.responses["import anta"] = (0, "1.10.0\n")

    async def no_report(remote: str, local: str | Path) -> None:
        raise FileNotFoundError(remote)

    monkeypatch.setattr(fake, "get", no_report)
    job = await run_job(client, (await client.post(f"/api/labs/{lab['id']}/validate")).json()["job_id"], "failed")
    assert "no ANTA JSON report" in job["error"] and "rc=2" in job["error"]
    assert (await lab_row(lab["id"])).state == LabState.deployed


async def test_deploy_clears_stale_network_on_same_subnet(client, monkeypatch):
    """0.10.0: a docker network left behind by a crashed/undestroyed lab on our mgmt subnet is removed when empty,
    refused with a precise message when another lab still runs on it."""
    from avdlab.executors.clab import parse_networks

    host_id = await mk_host()
    lab = await mk_lab(client, host_id=host_id)
    fake = ClabFakeSSH(lab["slug"], eapi_fail_first=0)
    fake.responses["docker network inspect"] = (0, "clab-mgmt-old-lab|172.16.1.0/24 |0\nclab-mgmt-other|10.9.0.0/24 |3\n")
    fake.responses["docker network rm"] = (0, "clab-mgmt-old-lab\n")
    install(monkeypatch, fake)
    r = await client.post(f"/api/labs/{lab['id']}/deploy", json={"wait_eapi": False})
    job = await run_job(client, r.json()["job_id"])
    assert ran(fake, "docker network rm clab-mgmt-old-lab") and "stale after a crash" in read_log(job["id"])
    assert not ran(fake, "docker network rm clab-mgmt-other")

    await set_state(lab["id"], LabState.built)
    fake.calls.clear()
    fake.responses["docker network inspect"] = (0, "clab-mgmt-evpn-dual-dc-dci-lab|172.16.1.0/24 |8\n")
    r = await client.post(f"/api/labs/{lab['id']}/deploy", json={"wait_eapi": False})
    job = await run_job(client, r.json()["job_id"], status="failed")
    assert "in use by the running lab 'evpn-dual-dc-dci-lab' (8 containers)" in job["error"] and "mgmt_subnet" in job["error"]
    assert not ran(fake, "containerlab deploy") and not ran(fake, "docker network rm")

    assert parse_networks("a|172.16.1.0/24 fd00::/64 |2\nbroken line\n") == [{"name": "a", "subnets": ["172.16.1.0/24", "fd00::/64"], "containers": 2}]
