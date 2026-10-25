"""Phase 6 (A): update check (fake httpx), semver, can_self_update, the app.update helper job (fake docker), 409s,
boot-time bookkeeping."""
from __future__ import annotations

import json

import httpx
import pytest
from sqlalchemy import update as sa_update

from avdlab.core.config import settings
from avdlab.core.db import Job, JobStatus, SessionLocal
from avdlab.images import docker as docker_mod
from avdlab.images.docker import ProcResult
from avdlab.jobs.engine import read_log, runner
from avdlab.meta import update
from avdlab.meta.api import version


# ------------------------------------------------------------------ fixtures / fakes
@pytest.fixture(autouse=True)
def clean_state(monkeypatch, tmp_path):
    update._state.clear()
    for p in (update.update_json_path(), update.update_log_path(), update._check_file()):
        p.unlink(missing_ok=True)
    monkeypatch.setattr(settings, "update_source", "")
    monkeypatch.setattr(settings, "update_mode", "source")
    monkeypatch.setattr(settings, "deploy_dir", "")
    monkeypatch.setattr(settings, "update_docker_socket", tmp_path / "missing.sock")
    yield
    update._state.clear()
    for p in (update.update_json_path(), update.update_log_path(), update._check_file()):
        p.unlink(missing_ok=True)


def fake_http(monkeypatch, handler):
    monkeypatch.setattr(update, "http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))


class FakeDocker:
    binary = "sh"  # present on PATH → can_self_update's docker CLI check passes

    def __init__(self, rc: int = 0) -> None:
        self.calls: list[list[str]] = []
        self.rc = rc

    async def run_cli(self, args, *, log=None, timeout=None):
        self.calls.append(list(args))
        if args[0] == "run":
            return ProcResult(0, "0123456789abcdef\n", "")
        if args[:2] == ["logs", "-f"]:
            lines = ["+ git -C .. pull --ff-only", "Already up to date.", f"[avdlab-update] exit={self.rc}"]
            for line in lines:
                if log:
                    log(line)
            return ProcResult(0, "\n".join(lines), "")
        return ProcResult(1, "unexpected", "")


def self_update_ready(monkeypatch, tmp_path, mode: str = "source") -> FakeDocker:
    sock = tmp_path / "docker.sock"
    sock.write_text("")
    monkeypatch.setattr(settings, "update_docker_socket", sock)
    monkeypatch.setattr(settings, "deploy_dir", "/srv/avdlab/deploy")
    monkeypatch.setattr(settings, "update_mode", mode)
    fake = FakeDocker()
    monkeypatch.setattr(docker_mod, "backend", fake)
    return fake


async def clear_running_jobs() -> None:
    async with SessionLocal() as s:
        await s.execute(sa_update(Job).where(Job.status.in_([JobStatus.queued, JobStatus.running]))
                        .values(status=JobStatus.failed))
        await s.commit()


# ------------------------------------------------------------------ versions
@pytest.mark.parametrize("latest,current,newer", [
    ("0.7.0", "0.6.9", True),
    ("v0.7.0", "0.7.0", False),
    ("0.7", "0.6.12", True),
    ("0.10.0", "0.9.9", True),
    ("1.0.0", "1.0.0-rc.1", True),
    ("1.0.0-rc.2", "1.0.0-rc.1", True),
    ("1.0.0-rc.10", "1.0.0-rc.2", True),
    ("1.0.0-rc.1", "1.0.0", False),
    ("0.6.0", "0.7.0", False),
    ("garbage", "0.1.0", False),
    (None, "0.1.0", False),
])
def test_semver_compare(latest, current, newer):
    assert update.is_newer(latest, current) is newer


# ------------------------------------------------------------------ check (fake httpx)
async def test_github_release_check(monkeypatch):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"tag_name": "v99.1.0", "html_url": "https://github.com/Acme/avdlab/releases/tag/v99.1.0",
                                         "body": "### Added\n- things", "published_at": "2026-09-20T10:00:00Z"})

    fake_http(monkeypatch, handler)
    monkeypatch.setattr(settings, "update_source", "github:Acme/avdlab")
    res = await update.check_now()
    assert seen == ["https://api.github.com/repos/Acme/avdlab/releases/latest"]
    assert res["error"] is None and res["latest"] == "99.1.0"
    st = update.status()
    assert st["available"] is True and st["current"] == version()
    assert st["url"].endswith("/v99.1.0") and st["notes"].startswith("### Added")
    assert st["image"] == "ghcr.io/acme/avdlab:99.1.0" and st["published_at"] == "2026-09-20T10:00:00Z"
    assert st["checked_at"] and st["configured"] and st["mode"] == "source"
    # cached on disk for the next process
    assert json.loads(update._check_file().read_text())["latest"] == "99.1.0"


async def test_manifest_check_and_errors(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/manifest.json":
            return httpx.Response(200, json={"latest": "0.0.1", "url": "https://x/rel", "notes": "n",
                                             "published_at": "2026-01-01", "image": "ghcr.io/x/avdlab:0.0.1"})
        if request.url.path == "/bad.json":
            return httpx.Response(200, json={"nope": 1})
        return httpx.Response(500, text="boom")

    fake_http(monkeypatch, handler)
    monkeypatch.setattr(settings, "update_source", "https://updates.example/manifest.json")
    res = await update.check_now()
    assert res["latest"] == "0.0.1" and res["image"] == "ghcr.io/x/avdlab:0.0.1"
    assert update.status()["available"] is False  # older than VERSION
    monkeypatch.setattr(settings, "update_source", "https://updates.example/bad.json")
    assert "latest" in (await update.check_now())["error"]
    monkeypatch.setattr(settings, "update_source", "https://updates.example/down.json")
    res = await update.check_now()
    assert res["error"].startswith("HTTPStatusError") and update.status()["available"] is False
    monkeypatch.setattr(settings, "update_source", "ftp://nope")
    assert "use github" in (await update.check_now())["error"]


def test_parse_helpers():
    gh = update.parse_github_release({"tag_name": "0.8.0"}, "o", "r")
    assert gh["latest"] == "0.8.0" and gh["url"] == "https://github.com/o/r/releases/tag/0.8.0"
    with pytest.raises(ValueError):
        update.parse_manifest({"url": "x"})


# ------------------------------------------------------------------ can_self_update
def test_can_self_update_logic(monkeypatch, tmp_path):
    ok, reason = update.can_self_update()
    assert not ok and "docker socket" in reason
    sock = tmp_path / "docker.sock"
    sock.write_text("")
    monkeypatch.setattr(settings, "update_docker_socket", sock)
    ok, reason = update.can_self_update()
    assert not ok and "AVDLAB_DEPLOY_DIR" in reason
    monkeypatch.setattr(settings, "deploy_dir", "relative/deploy")
    assert "absolute" in update.can_self_update()[1]
    monkeypatch.setattr(settings, "deploy_dir", "/srv/avdlab/deploy")
    monkeypatch.setattr(docker_mod, "backend", FakeDocker())
    assert update.can_self_update() == (True, None)
    monkeypatch.setattr(settings, "update_mode", "manual")
    ok, reason = update.can_self_update()
    assert not ok and "manual" in reason


def test_helper_commands(monkeypatch):
    monkeypatch.setattr(settings, "deploy_dir", "/srv/avdlab/deploy")
    argv = update.helper_argv("source", name="avdlab-update-x")
    assert argv[:5] == ["run", "-d", "--rm", "--name", "avdlab-update-x"]
    assert f"{settings.update_docker_socket}:/var/run/docker.sock" in argv
    assert "/srv/avdlab:/srv/avdlab" in argv and argv[argv.index("-w") + 1] == "/srv/avdlab/deploy"
    assert argv[-4:-1] == [settings.update_helper_image, "sh", "-c"]
    script = argv[-1]
    assert "git -C .. pull --ff-only" in script and "docker compose up -d --build" in script
    assert script.index("git -C .. pull") < script.index("docker compose up")
    assert '[avdlab-update] exit=$rc' in script
    img = update.helper_script("image", image="ghcr.io/acme/avdlab:0.9.0")
    assert "AVDLAB_IMAGE=ghcr.io/acme/avdlab:0.9.0" in img
    assert "docker compose pull api && docker compose up -d api" in img
    with pytest.raises(ValueError):
        update.helper_script("image")
    with pytest.raises(ValueError):
        update.helper_script("manual")


# ------------------------------------------------------------------ API + job
async def test_update_endpoints_disabled(client):
    r = await client.get("/api/meta/update")
    assert r.status_code == 200, r.text
    st = r.json()
    for key in ("current", "latest", "available", "url", "notes", "published_at", "checked_at", "mode",
                "can_self_update", "reason", "last_update"):
        assert key in st, key
    assert st["configured"] is False and "AVDLAB_UPDATE_SOURCE" in st["hint"] and st["available"] is False
    assert (await client.post("/api/meta/update/check")).json()["error"] is None
    r = await client.post("/api/meta/update/apply", json={"force": True})
    assert r.status_code == 409 and "docker socket" in r.json()["detail"]["reason"]


async def test_apply_job_source_mode(client, monkeypatch, tmp_path):
    fake = self_update_ready(monkeypatch, tmp_path)
    fake_http(monkeypatch, lambda req: httpx.Response(200, json={"tag_name": "v99.0.0"}))
    monkeypatch.setattr(settings, "update_source", "github:acme/avdlab")
    await clear_running_jobs()

    st = (await client.post("/api/meta/update/check")).json()
    assert st["available"] and st["can_self_update"] and st["reason"] is None
    r = await client.post("/api/meta/update/apply", json={})
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    await runner.wait(job_id)
    job = (await client.get(f"/api/jobs/{job_id}")).json()
    assert job["status"] == "succeeded", (job["error"], read_log(job_id))
    assert job["type"] == "app.update" and job["result"]["to"] == "99.0.0"

    run, logs = fake.calls
    assert run[0] == "run" and "/srv/avdlab:/srv/avdlab" in run and "git -C .. pull --ff-only" in run[-1]
    assert logs == ["logs", "-f", "0123456789abcdef"]
    rec = json.loads(update.update_json_path().read_text())
    assert rec["from"] == version() and rec["to"] == "99.0.0" and rec["ok"] is True and rec["job_id"] == job_id
    log_text = (await client.get("/api/meta/update/log")).json()["log"]
    assert "Already up to date." in log_text and "[avdlab-update] exit=0" in log_text
    assert (await client.get("/api/meta/update")).json()["last_update"]["ok"] is True

    # nothing newer without force → 409
    monkeypatch.setattr(update, "_state", {"latest": version()})
    r = await client.post("/api/meta/update/apply", json={})
    assert r.status_code == 409 and "no newer release" in r.json()["detail"]["message"]


async def test_apply_failure_and_image_mode(client, monkeypatch, tmp_path):
    fake = self_update_ready(monkeypatch, tmp_path, mode="image")
    fake.rc = 3
    await clear_running_jobs()
    update._state.update({"latest": "99.0.0", "image": None})
    r = await client.post("/api/meta/update/apply", json={})
    assert r.status_code == 409 and "image" in r.json()["detail"]["message"]
    update._state.update({"image": "ghcr.io/acme/avdlab:99.0.0"})
    r = await client.post("/api/meta/update/apply", json={})
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    await runner.wait(job_id)
    job = (await client.get(f"/api/jobs/{job_id}")).json()
    assert job["status"] == "failed" and "exited with 3" in job["error"]
    script = fake.calls[0][-1]
    assert "AVDLAB_IMAGE=ghcr.io/acme/avdlab:99.0.0" in script and "docker compose pull api" in script
    assert json.loads(update.update_json_path().read_text())["ok"] is False


async def test_apply_409_while_job_running(client, monkeypatch, tmp_path):
    self_update_ready(monkeypatch, tmp_path)
    update._state.update({"latest": "99.0.0"})
    await clear_running_jobs()
    async with SessionLocal() as s:
        s.add(Job(type="lab.build", status=JobStatus.running))
        await s.commit()
    try:
        r = await client.post("/api/meta/update/apply", json={"force": True})
        assert r.status_code == 409 and "lab.build" in r.json()["detail"]["message"]
    finally:
        await clear_running_jobs()


async def test_finalize_previous_update(client):
    async with SessionLocal() as s:
        row = Job(type="app.update", status=JobStatus.running)
        s.add(row)
        await s.commit()
        job_id = row.id
    update.update_json_path().write_text(json.dumps(
        {"from": "0.0.1", "to": version(), "mode": "source", "at": "2026-09-26T10:00:00Z", "job_id": job_id, "ok": None}))
    update.update_log_path().write_text("+ docker compose up -d --build\n")  # API died before the exit sentinel
    data = await update.finalize_previous_update()
    assert data["ok"] is True
    job = (await client.get(f"/api/jobs/{job_id}")).json()
    assert job["status"] == "succeeded" and job["result"]["restarted"] is True
    lu = (await client.get("/api/meta/update")).json()["last_update"]
    assert lu == {**lu, "from": "0.0.1", "to": version(), "ok": True}

    # the new process runs an older version than the target → not ok, job failed
    async with SessionLocal() as s:
        row = Job(type="app.update", status=JobStatus.running)
        s.add(row)
        await s.commit()
        job_id = row.id
    update.update_json_path().write_text(json.dumps(
        {"from": version(), "to": "99.0.0", "mode": "image", "at": "x", "job_id": job_id, "ok": None}))
    assert (await update.finalize_previous_update())["ok"] is False
    assert (await client.get(f"/api/jobs/{job_id}")).json()["status"] == "failed"
