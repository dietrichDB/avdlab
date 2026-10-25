import asyncio
import pytest
from avdlab.jobs.engine import job, runner, JobContext, read_log


@job("test.echo")
async def _echo(ctx: JobContext):
    ctx.log("hello")
    await asyncio.sleep(0.01)
    ctx.artefact("out", ctx.dir / "x.txt")
    return {"echo": ctx.params.get("msg")}


async def test_job_engine_and_api(client):
    jid = await runner.submit("test.echo", params={"msg": "hi"})
    await runner.wait(jid)
    r = await client.get(f"/api/jobs/{jid}")
    body = r.json()
    assert body["status"] == "succeeded" and body["result"] == {"echo": "hi"} and body["artefacts"][0]["name"] == "out"
    assert "hello" in read_log(jid)
    assert "test.echo" in (await client.get("/api/jobs/types")).json()


async def test_secrets_roundtrip(client):
    r = await client.post("/api/secrets", json={"name": "tok", "kind": "arista_token", "value": "s3cr3t"})
    assert r.status_code == 201
    listed = (await client.get("/api/secrets")).json()
    tok = next(x for x in listed if x["name"] == "tok")
    assert "value" not in tok and "ciphertext" not in tok
    from avdlab.core.security import secret_store
    from avdlab.core.db import SessionLocal
    async with SessionLocal() as s:
        assert await secret_store.reveal(s, r.json()["id"]) == "s3cr3t"


async def test_auth_required():
    from avdlab.main import app
    from httpx import ASGITransport, AsyncClient
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        assert (await c.get("/api/jobs")).status_code == 401


async def test_orphaned_jobs_failed_at_startup(client):
    """1.0.2: queued/running rows left by a previous process are failed on startup (they can never finish)."""
    from avdlab.core.db import Job, JobStatus, SessionLocal
    from avdlab.jobs.engine import runner

    async with SessionLocal() as s:
        a = Job(type="lab.build", params={}, status=JobStatus.running)
        b = Job(type="lab.deploy", params={}, status=JobStatus.queued)
        c = Job(type="lab.validate", params={}, status=JobStatus.succeeded)
        s.add_all([a, b, c])
        await s.commit()
        ids = (a.id, b.id, c.id)
    assert await runner.fail_orphans() == 2
    async with SessionLocal() as s:
        rows = [await s.get(Job, i) for i in ids]
    assert [r.status for r in rows] == [JobStatus.failed, JobStatus.failed, JobStatus.succeeded]
    assert "restarted" in rows[0].error and rows[0].ended_at is not None
    assert await runner.fail_orphans() == 0
