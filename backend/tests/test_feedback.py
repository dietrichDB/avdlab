"""Phase 5: feedback entries, diagnostics (masked), markdown dump, optional GitHub issue (faked)."""
from __future__ import annotations

from pathlib import Path

import json

import httpx

from avdlab.core.config import settings
from avdlab.core.db import Job, JobStatus, SessionLocal
from avdlab.feedback import api as fb_api


async def _failed_job() -> str:
    async with SessionLocal() as s:
        job = Job(type="lab.deploy", status=JobStatus.failed, error="deploy failed: password=hunter2 token: abc")
        s.add(job)
        await s.commit()
        job_id = job.id
    d = settings.jobs_dir / job_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "log.txt").write_text("step 1\nAuthorization: Bearer supersecretvalue123\nkey " + "A" * 48 + "\n")
    return job_id


async def test_feedback_crud_and_markdown(client):
    r = await client.post("/api/feedback", json={"kind": "suggestion", "title": "Dark mode for the terminal",
                                                 "body": "please", "page": "/labs/x"})
    assert r.status_code == 201, r.text
    fb = r.json()
    assert fb["status"] == "new" and fb["diagnostics"] is None and fb["github_issue_url"] is None
    md = (settings.data_dir / "feedback" / f"{fb['id']}.md").read_text()
    assert md.startswith("# [suggestion] Dark mode for the terminal") and "/labs/x" in md
    assert any(f["id"] == fb["id"] for f in (await client.get("/api/feedback")).json())
    r = await client.patch(f"/api/feedback/{fb['id']}", json={"status": "triaged"})
    assert r.status_code == 200 and r.json()["status"] == "triaged"
    assert (await client.patch(f"/api/feedback/{fb['id']}", json={"status": "bogus"})).status_code == 422
    assert [f["id"] for f in (await client.get("/api/feedback?status=triaged")).json()] == [fb["id"]]
    assert (await client.post("/api/feedback", json={"kind": "rant", "title": "x"})).status_code == 422
    assert (await client.delete(f"/api/feedback/{fb['id']}")).status_code == 204
    assert (await client.get(f"/api/feedback/{fb['id']}")).status_code == 404
    assert not (settings.data_dir / "feedback" / f"{fb['id']}.md").exists()


async def test_feedback_diagnostics_masked(client):
    job_id = await _failed_job()
    r = await client.post("/api/feedback", json={"kind": "bug", "title": "deploy broke", "body": "see diag",
                                                 "page": "/labs/1", "include_diagnostics": True},
                          headers={"user-agent": "pytest-browser/1.0"})
    assert r.status_code == 201, r.text
    diag = r.json()["diagnostics"]
    assert diag["version"] == (Path(__file__).resolve().parents[2] / "VERSION").read_text().strip() and diag["user_agent"] == "pytest-browser/1.0"
    job = next(j for j in diag["failed_jobs"] if j["id"] == job_id)
    text = json.dumps(job)
    assert "hunter2" not in text and "supersecretvalue123" not in text and "A" * 48 not in text
    assert job["type"] == "lab.deploy" and job["log_head"][0] == "step 1"
    assert len(diag["failed_jobs"]) <= 5 and isinstance(diag["hosts"], list)
    preview = (await client.get("/api/feedback/diagnostics")).json()
    assert set(preview) == set(diag)


def test_mask_secrets():
    m = fb_api.mask_secrets
    assert m("ansible_password: arista") == "ansible_password: ****"
    assert "tok" not in m("https://user:tok@github.com/x.git")
    assert m("nothing to see") == "nothing to see"


async def test_feedback_github_issue(client, monkeypatch):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"html_url": "https://github.com/acme/avdlab/issues/7"})

    monkeypatch.setattr(fb_api, "http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(settings, "feedback_github_repo", "acme/avdlab")
    # repo configured but no token → no issue, still created
    r = await client.post("/api/feedback", json={"kind": "question", "title": "no token yet"})
    assert r.status_code == 201 and r.json()["github_issue_url"] is None and not seen
    assert (await client.post("/api/secrets", json={"name": "github_token", "kind": "generic", "value": "ghp_x"})).status_code == 201
    r = await client.post("/api/feedback", json={"kind": "bug", "title": "crash", "body": "trace",
                                                 "include_diagnostics": True})
    assert r.status_code == 201, r.text
    assert r.json()["github_issue_url"] == "https://github.com/acme/avdlab/issues/7"
    assert seen["url"] == "https://api.github.com/repos/acme/avdlab/issues" and seen["auth"] == "Bearer ghp_x"
    assert seen["body"]["title"] == "[bug] crash" and seen["body"]["labels"] == ["feedback"]
    assert "## Diagnostics" in seen["body"]["body"]

    # GitHub failure never blocks
    monkeypatch.setattr(fb_api, "http_client", lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(500, text="boom"))))
    r = await client.post("/api/feedback", json={"kind": "bug", "title": "second"})
    assert r.status_code == 201 and r.json()["github_issue_url"] is None

    def raise_(req):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(fb_api, "http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(raise_)))
    r = await client.post("/api/feedback", json={"kind": "bug", "title": "third"})
    assert r.status_code == 201 and r.json()["github_issue_url"] is None
