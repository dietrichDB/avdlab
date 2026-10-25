"""Images module with docker, eos-downloader and SSH faked (no docker/network in CI)."""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from avdlab.core.config import settings
from avdlab.hosts.ssh import CompletedProcess, SSHClient
from avdlab.images import docker as docker_mod
from avdlab.images import service
from avdlab.jobs.engine import read_log, runner


class FakeDocker:
    def __init__(self, available: bool = True) -> None:
        self._available = available
        self.images: set[str] = set()
        self.calls: list[tuple[str, Any]] = []

    async def available(self) -> bool:
        return self._available

    async def image_exists(self, tag: str) -> bool:
        return tag in self.images

    async def import_tarball(self, path: Path, tag: str, *, log=None) -> None:
        self.calls.append(("import", tag))
        self.images.add(tag)

    async def save(self, tag: str, dest: Path, *, log=None) -> Path:
        self.calls.append(("save", tag))
        dest.write_bytes(b"docker-save")
        return dest

    async def run_ardl(self, args, *, token: str, log=None, cwd=None) -> docker_mod.ProcResult:
        self.calls.append(("ardl", list(args), token))
        out = Path(args[args.index("--output") + 1])
        fmt, version = args[args.index("--format") + 1], args[args.index("--version") + 1]
        (out / service.image_filename(fmt, version)).write_bytes(b"fake-ceos-tarball")
        if "--import-docker" in args:
            self.images.add(f"arista/ceos:{version}")
        if log:
            log("downloaded")
        return docker_mod.ProcResult(0, "ok", "")


class FakeSSH:
    def __init__(self) -> None:
        self.cmds: list[tuple[str, bool, str | None]] = []
        self.puts: list[tuple[str, str]] = []
        self.images: set[str] = set()

    async def run(self, cmd: str, *, sudo: bool = False, timeout=600, log=None, input: str | None = None) -> CompletedProcess:
        self.cmds.append((cmd, sudo, input))
        if cmd.startswith("docker image inspect"):
            tag = cmd.split()[3]
            return CompletedProcess(0 if tag in self.images else 1, "", "", command=cmd)
        if cmd.startswith("docker import") or cmd.startswith("docker load") or "ardl get eos" in cmd:
            self.images.add("arista/ceos:4.34.2F")
        return CompletedProcess(0, "", "", command=cmd)

    async def put(self, local, remote) -> None:
        self.puts.append((str(local), remote))


@pytest.fixture
def fake_docker(monkeypatch):
    fake = FakeDocker()
    monkeypatch.setattr(docker_mod, "backend", fake)
    return fake


@pytest.fixture
def fake_ssh(monkeypatch):
    fake = FakeSSH()

    @asynccontextmanager
    async def connect(cls, host, session, **kw):  # noqa: ANN001
        yield fake

    monkeypatch.setattr(SSHClient, "connect", classmethod(connect))
    return fake


async def ensure_token(client) -> None:
    r = await client.post("/api/secrets", json={"name": "arista_token", "kind": "arista_token", "value": "tok-123"})
    assert r.status_code in (201, 409)


async def wait_ok(client, job_id: str) -> dict:
    await runner.wait(job_id)
    job = (await client.get(f"/api/jobs/{job_id}")).json()
    assert job["status"] == "succeeded", (job["error"], read_log(job_id)[-2000:])
    return job


def test_naming():
    assert service.image_filename("cEOS", "4.34.2F") == "cEOS-lab-4.34.2F.tar.xz"
    assert service.image_filename("cEOS64", "4.34.2F") == "cEOS64-lab-4.34.2F.tar.xz"
    assert service.docker_tag("cEOS", "4.34.2F") == "arista/ceos:4.34.2F"
    assert service.docker_tag("vEOS64-lab", "4.34.2F") == ""
    with pytest.raises(ValueError):
        service.validate_version("4.34; rm -rf /")


async def test_catalog_no_token_cache_and_upstream_error(client, monkeypatch):
    service.clear_cache()
    sec = [s for s in (await client.get("/api/secrets")).json() if s["name"] == "arista_token" or s["kind"] == "arista_token"]
    for s in sec:
        await client.delete(f"/api/secrets/{s['id']}")
    r = await client.get("/api/images/catalog?fmt=cEOS")
    assert r.status_code == 409 and "hint" in r.json()["detail"]

    await ensure_token(client)
    calls: list[tuple[str, str]] = []

    def fake_fetch(token: str, fmt: str) -> list[str]:
        calls.append((token, fmt))
        return ["4.34.2F", "4.33.1F"]

    monkeypatch.setattr(service, "fetch_versions", fake_fetch)
    r = await client.get("/api/images/catalog?fmt=cEOS")
    assert r.status_code == 200 and r.json()["versions"] == ["4.34.2F", "4.33.1F"] and r.json()["cached"] is False
    r = await client.get("/api/images/catalog?fmt=cEOS")
    assert r.json()["cached"] is True and calls == [("tok-123", "cEOS")]
    assert (await client.get("/api/images/catalog?fmt=bogus")).status_code == 422

    def failing(token: str, fmt: str) -> list[str]:
        raise service.CatalogError("arista.com catalogue unavailable: boom")

    monkeypatch.setattr(service, "fetch_versions", failing)
    r = await client.get("/api/images/catalog?fmt=cEOS&refresh=true")
    assert r.status_code == 502
    service.clear_cache()


async def test_download_push_upload(client, fake_docker, fake_ssh):
    await ensure_token(client)
    assert (await client.post("/api/images/download", json={"fmt": "cEOS", "version": "bad version"})).status_code == 422
    r = await client.post("/api/images/download", json={"fmt": "cEOS", "version": "4.34.2F"})
    assert r.status_code == 202
    job = await wait_ok(client, r.json()["job_id"])
    img = job["result"]["image"]
    assert job["result"]["imported"] is True
    assert img["docker_tag"] == "arista/ceos:4.34.2F" and img["size_bytes"] == len(b"fake-ceos-tarball") and img["sha256"]
    assert Path(img["local_path"]) == settings.images_dir / "cEOS-lab-4.34.2F.tar.xz"
    ardl = [c for c in fake_docker.calls if c[0] == "ardl"][0]
    assert ardl[2] == "tok-123" and "tok-123" not in " ".join(ardl[1]) and "--import-docker" in ardl[1]
    assert "tok-123" not in read_log(r.json()["job_id"])
    assert [i["id"] for i in (await client.get("/api/images")).json()] == [img["id"]]

    # second download is skipped (file present)
    job2 = await wait_ok(client, (await client.post("/api/images/download", json={"version": "4.34.2F"})).json()["job_id"])
    assert job2["result"]["image"]["id"] == img["id"]
    assert len([c for c in fake_docker.calls if c[0] == "ardl"]) == 1

    # push to a host: upload tarball + docker import over (fake) SSH
    key = (await client.post("/api/hosts/keys/generate", json={"name": "img-key"})).json()
    host = (await client.post("/api/hosts", json={"name": "img-host", "address": "192.0.2.50",
                                                  "ssh_key_secret_id": key["secret_id"]})).json()
    r = await client.post(f"/api/images/{img['id']}/push/{host['id']}", json={"options": {}})
    assert r.status_code == 202
    job = await wait_ok(client, r.json()["job_id"])
    assert job["result"]["method"] == "import"
    assert fake_ssh.puts and fake_ssh.puts[0][1].endswith("/images/cEOS-lab-4.34.2F.tar.xz")
    assert (await client.get(f"/api/images/{img['id']}")).json()["present_on_hosts"] == [host["id"]]

    # remote download: token goes over stdin, never into the command
    fake_ssh.images.clear()
    r = await client.post(f"/api/images/{img['id']}/push/{host['id']}", json={"options": {"remote_download": True}})
    job = await wait_ok(client, r.json()["job_id"])
    assert job["result"]["method"] == "remote_download"
    ardl_cmd = [c for c in fake_ssh.cmds if "ardl get eos" in c[0]][-1]
    assert "tok-123" not in ardl_cmd[0] and ardl_cmd[2] == "tok-123\n"
    # already present → no-op
    job = await wait_ok(client, (await client.post(f"/api/images/{img['id']}/push/{host['id']}")).json()["job_id"])
    assert job["result"]["method"] == "present"
    assert (await client.post(f"/api/images/{img['id']}/push/nohost")).status_code == 404

    # manual upload
    files = {"file": ("cEOS-lab-4.33.1F.tar.xz", b"uploaded-bytes", "application/x-xz")}
    r = await client.post("/api/images/upload", files=files, data={"fmt": "cEOS", "version": "4.33.1F"})
    assert r.status_code == 201, r.text
    up = r.json()
    assert up["imported"] is True and up["size_bytes"] == len(b"uploaded-bytes")
    assert ("import", "arista/ceos:4.33.1F") in fake_docker.calls
    r = await client.post("/api/images/upload", files={"file": ("x.zip", b"z", "application/zip")},
                          data={"fmt": "cEOS", "version": "4.33.1F"})
    assert r.status_code == 422
    assert (await client.delete(f"/api/images/{up['id']}")).status_code == 204
    assert not (settings.images_dir / "cEOS-lab-4.33.1F.tar.xz").exists()


async def test_download_without_docker(client, monkeypatch):
    fake = FakeDocker(available=False)
    monkeypatch.setattr(docker_mod, "backend", fake)
    await ensure_token(client)
    job = await wait_ok(client, (await client.post("/api/images/download",
                                                   json={"fmt": "vEOS64-lab", "version": "4.32.1F"})).json()["job_id"])
    assert job["result"]["imported"] is False and job["result"]["image"]["docker_tag"] == ""
    args = [c for c in fake.calls if c[0] == "ardl"][0][1]
    assert "--import-docker" not in args


# ------------------------------------------------------------------ phase 3: architectures / formats
def test_arch_mapping_and_tags():
    assert service.docker_tag("cEOSarm", "4.34.2F") == "arista/ceos:4.34.2F-arm64"
    assert service.docker_tag("cEOS64", "4.34.2F") == "arista/ceos:4.34.2F"
    assert service.format_arch("cEOSarm") == "arm64" and service.format_arch("cEOS64") == "x86_64"
    assert service.ceos_image_tag("4.34.2F", "aarch64") == "arista/ceos:4.34.2F-arm64"
    assert service.tag_arch("arista/ceos:4.34.2F-arm64") == "arm64" and service.tag_arch("arista/ceos:4.34.2F") == "x86_64"
    assert service.arch_mismatch({"os": {"arch": "x86_64"}}, "arm64") == "host is x86_64, image is arm64"
    assert service.arch_mismatch({"os": {"arch": "aarch64"}}, "arm64") is None
    assert service.arch_mismatch({}, "arm64") is None  # never probed → no opinion
    assert service.image_filename("cEOSarm", "4.34.2F") == "cEOSarm-lab-4.34.2F.tar.xz"


async def test_formats_endpoint_and_catalog_accepts_arm(client, monkeypatch):
    r = await client.get("/api/images/formats")
    assert r.json() == [{"fmt": "cEOS", "arch": "x86_64", "label": "cEOS-lab (x86_64)"},
                        {"fmt": "cEOS64", "arch": "x86_64", "label": "cEOS64-lab (x86_64, 64-bit)"},
                        {"fmt": "cEOSarm", "arch": "arm64", "label": "cEOSarm-lab (arm64)"}]
    await ensure_token(client)
    monkeypatch.setattr(service, "fetch_versions", lambda token, fmt: ["4.34.2F"])
    service.clear_cache()
    for fmt in ("cEOS", "cEOS64", "cEOSarm"):
        assert (await client.get(f"/api/images/catalog?fmt={fmt}")).status_code == 200
    service.clear_cache()


async def test_arm_image_upload_arch_and_push_mismatch(client, fake_docker, fake_ssh):
    from avdlab.core.db import Host, SessionLocal

    files = {"file": ("cEOSarm-lab-4.34.9F.tar.xz", b"arm-bytes", "application/x-xz")}
    up = (await client.post("/api/images/upload", files=files, data={"fmt": "cEOSarm", "version": "4.34.9F"})).json()
    assert up["arch"] == "arm64" and up["docker_tag"] == "arista/ceos:4.34.9F-arm64"
    assert ("import", "arista/ceos:4.34.9F-arm64") in fake_docker.calls
    async with SessionLocal() as s:
        h = Host(name="x86-host-arch", address="192.0.2.61", capabilities={"os": {"arch": "x86_64"}})
        s.add(h)
        await s.commit()
        host_id = h.id
    try:
        r = await client.post(f"/api/images/{up['id']}/push/{host_id}", json={"options": {}})
        assert r.status_code == 409 and r.json()["detail"] == "host is x86_64, image is arm64"
    finally:
        async with SessionLocal() as s:
            await s.delete(await s.get(Host, host_id))
            await s.commit()
        assert (await client.delete(f"/api/images/{up['id']}")).status_code == 204


async def test_deploy_arch_check(client, tmp_path):
    """The lab knob ceos_arch picks the topology tag; deploy refuses an image of the wrong arch (API 409 + executor)."""
    import uuid

    import yaml

    from avdlab.core.db import Host, Lab, LabState, SessionLocal
    from avdlab.executors import clab as clab_exec
    from avdlab.executors.base import ExecutorError
    from avdlab.topology.clab import image_for

    async with SessionLocal() as s:
        h = Host(name=f"arch-{uuid.uuid4().hex[:6]}", address="192.0.2.62", capabilities={"os": {"arch": "x86_64"}})
        s.add(h)
        await s.commit()
        host_id = h.id
    r = await client.post("/api/labs", json={"name": f"arm-{uuid.uuid4().hex[:6]}", "template": "single-dc-l3ls",
                                             "host_id": host_id, "knobs": {"ceos_arch": "arm64"}})
    assert r.status_code == 201, r.text
    lab = r.json()
    try:
        async with SessionLocal() as s:
            row = await s.get(Lab, lab["id"])
            assert image_for(row) == "arista/ceos:4.34.2F-arm64"
            row.state = LabState.built
            await s.commit()
        topo = {"name": lab["slug"], "topology": {"kinds": {"arista_ceos": {"image": "arista/ceos:4.34.2F-arm64"}}, "nodes": {}}}
        (Path(lab["dir"]) / "clab").mkdir(exist_ok=True)
        (Path(lab["dir"]) / "clab" / "topology.clab.yml").write_text(yaml.safe_dump(topo))
        r = await client.post(f"/api/labs/{lab['id']}/deploy", json={})
        assert r.status_code == 409 and "host is x86_64, image is arm64" in r.json()["detail"]
        async with SessionLocal() as s:
            host = await s.get(Host, host_id)
        with pytest.raises(ExecutorError, match="host is x86_64, image is arm64"):
            clab_exec.check_image_arch(host, "arista/ceos:4.34.2F-arm64")
        clab_exec.check_image_arch(host, "arista/ceos:4.34.2F")
    finally:
        async with SessionLocal() as s:
            row = await s.get(Lab, lab["id"])
            row.state = LabState.draft
            await s.commit()
        assert (await client.delete(f"/api/labs/{lab['id']}")).status_code == 204
        async with SessionLocal() as s:
            await s.delete(await s.get(Host, host_id))
            await s.commit()


async def test_token_endpoint_replace_and_remove(client, monkeypatch):
    """0.7.1: GET/PUT/DELETE /images/token — replace a mistyped token in place (same secret row), drop the catalogue cache."""
    service.clear_cache()
    for s in (await client.get("/api/secrets")).json():
        if s["name"] == "arista_token" or s["kind"] == "arista_token":
            await client.delete(f"/api/secrets/{s['id']}")
    r = await client.get("/api/images/token")
    assert r.status_code == 200 and r.json()["configured"] is False and r.json()["hint"]

    assert (await client.put("/api/images/token", json={"value": "short"})).status_code == 422
    r = await client.put("/api/images/token", json={"value": "  wrong-token-1  "})
    assert r.status_code == 200 and r.json()["configured"] is True and r.json()["name"] == "arista_token"
    first_id = r.json()["secret_id"]
    assert "value" not in r.json() and "wrong" not in r.text

    seen: list[str] = []

    def fake_fetch(token: str, fmt: str) -> list[str]:
        seen.append(token)
        if token != "good-token-123":
            raise service.CatalogError("arista.com catalogue unavailable: 401")
        return ["4.34.2F"]

    monkeypatch.setattr(service, "fetch_versions", fake_fetch)
    assert (await client.get("/api/images/catalog?fmt=cEOS")).status_code == 502
    assert seen == ["wrong-token-1"]  # stripped

    r = await client.put("/api/images/token", json={"value": "good-token-123"})
    assert r.json()["secret_id"] == first_id  # replaced in place, no duplicate secret
    secrets = [s for s in (await client.get("/api/secrets")).json() if s["kind"] == "arista_token"]
    assert len(secrets) == 1
    r = await client.get("/api/images/catalog?fmt=cEOS")
    assert r.status_code == 200 and r.json()["versions"] == ["4.34.2F"] and r.json()["cached"] is False

    assert (await client.delete("/api/images/token")).status_code == 204
    assert (await client.get("/api/images/token")).json()["configured"] is False
    assert (await client.get("/api/images/catalog?fmt=cEOS")).status_code == 409
    assert (await client.delete("/api/images/token")).status_code == 204  # idempotent
