"""Phase 5: in-app feedback.

  POST   /feedback {kind: bug|suggestion|question, title, body, page?, include_diagnostics}  → 201 FeedbackOut
  GET    /feedback?status=                  list, newest first
  GET    /feedback/diagnostics              preview of the diagnostics a submission would include
  GET    /feedback/{id}   PATCH /feedback/{id} {status: new|triaged|done}   DELETE /feedback/{id} → 204

Every entry is also written to `<data_dir>/feedback/<id>.md`. With `settings.feedback_github_repo` (`owner/repo`,
env AVDLAB_FEEDBACK_GITHUB_REPO) and a secret named `github_token`, a GitHub issue (label `feedback`) is created and
its URL stored; failures are logged and never block the submission. HTTP goes through `http_client()` (tests swap in
an httpx.MockTransport). v0.9.0: every new entry is also mailed (fire-and-forget) to the admin setting
`feedback_mail_to` when `feedback_mail_enabled` and SMTP are configured (`core/mailer.py`).
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core import app_settings, mailer
from ..core.config import settings
from ..core.db import Feedback, Host, Job, JobStatus, get_session
from ..core.security import current_user, secret_store
from ..jobs.engine import read_log

router = APIRouter(prefix="/feedback", tags=["feedback"])
log = logging.getLogger("avdlab.feedback")
GITHUB_API = "https://api.github.com"
GITHUB_TOKEN_SECRET = "github_token"
LOG_HEAD_LINES = 15

_MASKS = [
    (re.compile(r"(?i)\b(authorization:\s*(?:bearer|token|basic)\s+)\S+"), r"\1****"),
    (re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"), r"\1****"),
    (re.compile(r"(?i)((?:password|passwd|secret|token|api[_-]?key|private[_-]?key)[\w-]*\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|\S+)"),
     r"\1****"),
    (re.compile(r"(?i)(https?://)[^/\s:@]+:[^/\s@]+@"), r"\1****@"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), "**** private key ****"),
    (re.compile(r"\b[A-Za-z0-9+/_-]{40,}={0,2}"), "****"),  # long opaque tokens (JWT parts, API keys, hashes)
]


def mask_secrets(text: str) -> str:
    for rx, repl in _MASKS:
        text = rx.sub(repl, text)
    return text


# ------------------------------------------------------------------------------------------------ schemas
class FeedbackIn(BaseModel):
    kind: Literal["bug", "suggestion", "question"]
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(default="", max_length=20000)
    page: str | None = Field(default=None, max_length=512)
    include_diagnostics: bool = False


class FeedbackPatch(BaseModel):
    status: Literal["new", "triaged", "done"]


def _out(fb: Feedback) -> dict[str, Any]:
    return {"id": fb.id, "kind": fb.kind, "title": fb.title, "body": fb.body, "page": fb.page,
            "diagnostics": fb.diagnostics, "status": fb.status, "github_issue_url": fb.github_issue_url,
            "created_at": fb.created_at.isoformat()}


# ------------------------------------------------------------------------------------------------ diagnostics
async def collect_diagnostics(session: AsyncSession, request: Request) -> dict[str, Any]:
    from ..meta.api import git_sha, version

    jobs = (await session.execute(select(Job).where(Job.status == JobStatus.failed)
                                  .order_by(Job.created_at.desc()).limit(5))).scalars().all()
    failed = []
    for j in jobs:
        head = (await asyncio.to_thread(read_log, j.id)).splitlines()[:LOG_HEAD_LINES]
        failed.append({"id": j.id, "type": j.type, "created_at": j.created_at.isoformat(),
                       "error": mask_secrets(j.error or "")[:2000], "log_head": [mask_secrets(x)[:500] for x in head]})
    hosts = []
    for h in (await session.execute(select(Host).order_by(Host.name))).scalars().all():
        caps = h.capabilities or {}

        def sub(key: str) -> dict[str, Any]:
            v = caps.get(key)
            return v if isinstance(v, dict) else {}

        hosts.append({
            "name": h.name, "kind": h.kind.value, "os": sub("os").get("name"), "kernel": sub("os").get("kernel"),
            "arch": sub("os").get("arch"), "cpus": sub("cpu").get("nproc"), "mem_gb": sub("mem").get("total_gb"),
            "docker": sub("docker").get("server_version") or sub("docker").get("client_version"),
            "docker_user_can_run": sub("docker").get("user_can_run"),
            "containerlab": sub("containerlab").get("version"),
            "recommendations": [mask_secrets(str(r)) for r in caps.get("recommendations") or []][:10],
            "last_probe_at": h.last_probe_at.isoformat() if h.last_probe_at else None,
        })
    return {"version": version(), "git_sha": git_sha(), "user_agent": request.headers.get("user-agent"),
            "failed_jobs": failed, "hosts": hosts}


# ------------------------------------------------------------------------------------------------ markdown / github
def markdown(fb: Feedback) -> str:
    import json

    lines = [f"# [{fb.kind}] {fb.title}", "", f"- id: `{fb.id}`", f"- created: {fb.created_at.isoformat()}",
             f"- page: {fb.page or '-'}", f"- status: {fb.status}"]
    if fb.github_issue_url:
        lines.append(f"- GitHub: {fb.github_issue_url}")
    lines += ["", fb.body or "_(no description)_", ""]
    if fb.diagnostics:
        lines += ["## Diagnostics", "", "```json", json.dumps(fb.diagnostics, indent=2, default=str), "```", ""]
    return "\n".join(lines)


def _write_md(fb: Feedback) -> None:
    d = settings.data_dir / "feedback"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{fb.id}.md").write_text(markdown(fb), encoding="utf-8")


def http_client() -> httpx.AsyncClient:
    """Factory for the GitHub client — tests monkeypatch this to inject an httpx.MockTransport."""
    return httpx.AsyncClient(timeout=10.0)


async def create_github_issue(session: AsyncSession, fb: Feedback) -> str | None:
    repo = settings.feedback_github_repo.strip()
    if not repo:
        return None
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
        log.warning("feedback: AVDLAB_FEEDBACK_GITHUB_REPO %r is not owner/repo", repo)
        return None
    sec = await secret_store.by_name(session, GITHUB_TOKEN_SECRET)
    if sec is None:
        log.warning("feedback: %s set but no secret named %r", repo, GITHUB_TOKEN_SECRET)
        return None
    try:
        token = secret_store.decrypt(sec.ciphertext)
        async with http_client() as client:
            r = await client.post(f"{GITHUB_API}/repos/{repo}/issues",
                                  headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                                           "X-GitHub-Api-Version": "2022-11-28"},
                                  json={"title": f"[{fb.kind}] {fb.title}", "body": markdown(fb), "labels": ["feedback"]})
        if r.status_code >= 300:
            log.warning("feedback: GitHub issue creation failed: HTTP %s %s", r.status_code, r.text[:300])
            return None
        return str(r.json().get("html_url") or "") or None
    except Exception as exc:  # noqa: BLE001 - never block a submission
        log.warning("feedback: GitHub issue creation failed: %s", exc)
        return None


# ------------------------------------------------------------------------------------------------ e-mail (v0.9.0)
def mail_text(fb: Feedback, user: str, failed_jobs: int, version: str, link: str) -> str:
    lines = [f"New feedback in avdlab: [{fb.kind}] {fb.title}", "", f"Kind:    {fb.kind}", f"Title:   {fb.title}",
             f"Page:    {fb.page or '-'}", f"User:    {user}", f"Created: {fb.created_at.isoformat()}", "",
             fb.body or "(no description)", "", "-- diagnostics summary",
             f"avdlab version: {version}", f"failed jobs:    {failed_jobs}"]
    if fb.diagnostics:
        lines.append("(full diagnostics attached to the entry)")
    if fb.github_issue_url:
        lines.append(f"GitHub issue:   {fb.github_issue_url}")
    lines += ["", f"Open: {link}", ""]
    return "\n".join(lines)


async def notify_feedback(session: AsyncSession, fb: Feedback, user: str, request: Request) -> None:
    """Queue the feedback e-mail; never raises and never waits for SMTP."""
    from ..meta.api import version

    try:
        cfg = await app_settings.load(session)
        if not (cfg.feedback_mail_enabled and cfg.smtp_configured and cfg.feedback_mail_to):
            return
        if fb.diagnostics:
            failed = len(fb.diagnostics.get("failed_jobs") or [])
        else:
            failed = int((await session.execute(select(func.count()).select_from(Job)
                                                .where(Job.status == JobStatus.failed))).scalar_one())
        link = (settings.public_url.strip().rstrip("/") or str(request.base_url).rstrip("/")) + "/feedback"
        text = mail_text(fb, user, failed, version(), link)
        mailer.spawn(mailer.send_mail(f"[avdlab] {fb.kind}: {fb.title}", text, to=cfg.feedback_mail_to))
    except Exception as exc:  # noqa: BLE001 - e-mail is best effort
        log.warning("feedback: e-mail notification failed: %s", exc)


# ------------------------------------------------------------------------------------------------ routes
async def _get(session: AsyncSession, fid: str) -> Feedback:
    fb = await session.get(Feedback, fid)
    if fb is None:
        raise HTTPException(404, "feedback not found")
    return fb


@router.post("", status_code=201)
async def create_feedback(body: FeedbackIn, request: Request, session: AsyncSession = Depends(get_session),
                          user: str = Depends(current_user)) -> dict[str, Any]:
    diagnostics = await collect_diagnostics(session, request) if body.include_diagnostics else None
    fb = Feedback(kind=body.kind, title=body.title.strip(), body=body.body, page=body.page, diagnostics=diagnostics,
                  status="new")
    session.add(fb)
    await session.commit()
    await session.refresh(fb)
    url = await create_github_issue(session, fb)
    if url:
        fb.github_issue_url = url
        await session.commit()
    await asyncio.to_thread(_write_md, fb)
    await notify_feedback(session, fb, user, request)
    return _out(fb)


@router.get("")
async def list_feedback(status: str | None = None, session: AsyncSession = Depends(get_session),
                        user: str = Depends(current_user)) -> list[dict[str, Any]]:
    q = select(Feedback).order_by(Feedback.created_at.desc())
    if status:
        q = q.where(Feedback.status == status)
    return [_out(f) for f in (await session.execute(q)).scalars().all()]


@router.get("/diagnostics")
async def preview_diagnostics(request: Request, session: AsyncSession = Depends(get_session),
                              user: str = Depends(current_user)) -> dict[str, Any]:
    return await collect_diagnostics(session, request)


@router.get("/{fid}")
async def get_feedback(fid: str, session: AsyncSession = Depends(get_session),
                       user: str = Depends(current_user)) -> dict[str, Any]:
    return _out(await _get(session, fid))


@router.patch("/{fid}")
async def patch_feedback(fid: str, body: FeedbackPatch, session: AsyncSession = Depends(get_session),
                         user: str = Depends(current_user)) -> dict[str, Any]:
    fb = await _get(session, fid)
    fb.status = body.status
    await session.commit()
    await asyncio.to_thread(_write_md, fb)
    return _out(fb)


@router.delete("/{fid}", status_code=204)
async def delete_feedback(fid: str, session: AsyncSession = Depends(get_session), user: str = Depends(current_user)) -> None:
    fb = await _get(session, fid)
    await session.delete(fb)
    await session.commit()
    (settings.data_dir / "feedback" / f"{fid}.md").unlink(missing_ok=True)
