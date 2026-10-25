"""In-process async job engine with persisted status, file-backed logs and live subscribers.

Usage:
    from avdlab.jobs.engine import job, runner, JobContext

    @job("host.probe")
    async def probe(ctx: JobContext) -> dict:
        host = await ctx.get_host()
        ctx.log(f"probing {host.address}")
        ...
        return {"ok": True}

    job_id = await runner.submit("host.probe", host_id=..., params={...})

Single-node design (one API process). Jobs run as asyncio tasks; log lines are appended to
<data_dir>/jobs/<id>/log.txt and fanned out to WebSocket subscribers. Swap for arq/Redis later if needed.
"""
from __future__ import annotations

import asyncio
import traceback
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import settings
from ..core.db import Host, Job, JobStatus, Lab, SessionLocal

JobFunc = Callable[["JobContext"], Awaitable[dict[str, Any] | None]]
_REGISTRY: dict[str, JobFunc] = {}


def job(job_type: str) -> Callable[[JobFunc], JobFunc]:
    def deco(fn: JobFunc) -> JobFunc:
        if job_type in _REGISTRY:
            raise RuntimeError(f"job type already registered: {job_type}")
        _REGISTRY[job_type] = fn
        return fn

    return deco


def registered_job_types() -> list[str]:
    return sorted(_REGISTRY)


class JobCancelled(Exception):
    pass


class JobContext:
    def __init__(self, job_id: str, job_type: str, params: dict[str, Any], lab_id: str | None, host_id: str | None) -> None:
        self.job_id = job_id
        self.type = job_type
        self.params = params
        self.lab_id = lab_id
        self.host_id = host_id
        self.dir: Path = settings.jobs_dir / job_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.dir / "log.txt"
        self.artefacts: list[dict[str, str]] = []
        self._subscribers: set[asyncio.Queue[str | None]] = set()
        self._cancel = asyncio.Event()
        self._ssh: list[Any] = []  # open SSHClient(s) — closed by JobRunner.cancel() so remote commands die
        self.started = False
        self._log_fh = self.log_path.open("a", encoding="utf-8")

    # ---- logging
    def log(self, line: str) -> None:
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        for part in str(line).rstrip("\n").split("\n"):
            msg = f"[{ts}] {part}"
            self._log_fh.write(msg + "\n")
            self._log_fh.flush()
            for q in list(self._subscribers):
                q.put_nowait(msg)

    def subscribe(self) -> asyncio.Queue[str | None]:
        q: asyncio.Queue[str | None] = asyncio.Queue()
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[str | None]) -> None:
        self._subscribers.discard(q)

    def _close(self) -> None:
        self._log_fh.close()
        for q in list(self._subscribers):
            q.put_nowait(None)

    # ---- artefacts / cancellation
    def artefact(self, name: str, path: Path | str) -> None:
        self.artefacts.append({"name": name, "path": str(path)})

    def cancel(self) -> None:
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def check_cancelled(self) -> None:
        if self._cancel.is_set():
            raise JobCancelled()

    # ---- SSH connections held by the job (registered by `SSHClient.connect(..., ctx=ctx)`)
    def attach_ssh(self, client: Any) -> None:
        if client not in self._ssh:
            self._ssh.append(client)

    def detach_ssh(self, client: Any) -> None:
        if client in self._ssh:
            self._ssh.remove(client)

    @property
    def ssh_clients(self) -> list[Any]:
        return list(self._ssh)

    # ---- db helpers
    def session(self) -> AsyncSession:
        return SessionLocal()

    async def get_host(self) -> Host:
        async with SessionLocal() as s:
            host = await s.get(Host, self.host_id) if self.host_id else None
        if host is None:
            raise RuntimeError("job has no host")
        return host

    async def get_lab(self) -> Lab:
        async with SessionLocal() as s:
            lab = await s.get(Lab, self.lab_id) if self.lab_id else None
        if lab is None:
            raise RuntimeError("job has no lab")
        return lab


class JobRunner:
    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._contexts: dict[str, JobContext] = {}

    async def fail_orphans(self) -> int:
        """1.0.2: jobs still queued/running in the DB when the API starts belonged to a previous process (restart, crash)
        and can never finish — mark them failed so labs are not stuck behind "<job> is already running"."""
        from sqlalchemy import select, update

        from ..core.db import JobStatus, utcnow

        async with SessionLocal() as s:
            ids = list((await s.execute(select(Job.id).where(Job.status.in_([JobStatus.queued, JobStatus.running])))).scalars())
            if ids:
                await s.execute(update(Job).where(Job.id.in_(ids)).values(
                    status=JobStatus.failed, ended_at=utcnow(), error="interrupted: the avdlab server restarted while this job was running"))
                await s.commit()
        return len(ids)

    async def submit(self, job_type: str, *, params: dict[str, Any] | None = None, lab_id: str | None = None, host_id: str | None = None) -> str:
        if job_type not in _REGISTRY:
            raise KeyError(f"unknown job type {job_type}; known: {registered_job_types()}")
        async with SessionLocal() as s:
            row = Job(type=job_type, params=params or {}, lab_id=lab_id, host_id=host_id)
            s.add(row)
            await s.commit()
            await s.refresh(row)
            job_id = row.id
        ctx = JobContext(job_id, job_type, params or {}, lab_id, host_id)
        self._contexts[job_id] = ctx
        self._tasks[job_id] = asyncio.create_task(self._run(ctx), name=f"job-{job_id}")
        return job_id

    async def _set(self, job_id: str, **fields: Any) -> None:
        async with SessionLocal() as s:
            row = await s.get(Job, job_id)
            if row:
                for k, v in fields.items():
                    setattr(row, k, v)
                await s.commit()

    async def _run(self, ctx: JobContext) -> None:
        try:
            ctx.started = True
            await self._set(ctx.job_id, status=JobStatus.running, started_at=datetime.now(timezone.utc))
            ctx.log(f"job {ctx.job_id} ({ctx.type}) started")
            result = await _REGISTRY[ctx.type](ctx) or {}
            ctx.log("job finished: success")
            await self._set(ctx.job_id, status=JobStatus.succeeded, result=result, artefacts=ctx.artefacts, ended_at=datetime.now(timezone.utc))
        except (JobCancelled, asyncio.CancelledError):
            ctx.log("job cancelled")
            # shield: the row must be updated even though this task is being cancelled
            await asyncio.shield(self._set(ctx.job_id, status=JobStatus.cancelled, artefacts=ctx.artefacts,
                                           ended_at=datetime.now(timezone.utc)))
        except Exception as exc:  # noqa: BLE001
            if ctx.cancelled:  # e.g. the SSH session was closed under a running command by cancel()
                ctx.log(f"job cancelled ({exc.__class__.__name__}: {exc})")
                ctx.log("job cancelled")
                await asyncio.shield(self._set(ctx.job_id, status=JobStatus.cancelled, artefacts=ctx.artefacts,
                                               ended_at=datetime.now(timezone.utc)))
                return
            ctx.log(f"job failed: {exc!r}")
            ctx.log(traceback.format_exc())
            await self._set(ctx.job_id, status=JobStatus.failed, error=str(exc), artefacts=ctx.artefacts, ended_at=datetime.now(timezone.utc))
        finally:
            ctx._close()
            self._contexts.pop(ctx.job_id, None)
            self._tasks.pop(ctx.job_id, None)

    def context(self, job_id: str) -> JobContext | None:
        return self._contexts.get(job_id)

    async def cancel(self, job_id: str) -> dict[str, Any]:
        """Kill a job: set the cancel event, cancel the asyncio task and close any SSH connection the job holds
        (remote commands die with the session). Only jobs owned by this runner can be killed; queued rows that are
        not (e.g. after a restart) and finished jobs → `{"cancelled": false}`."""
        ctx = self._contexts.get(job_id)
        task = self._tasks.get(job_id)
        if ctx is None or task is None or task.done():
            return {"cancelled": False}
        was_running = ctx.started
        ctx.cancel()
        task.cancel()
        for client in ctx.ssh_clients:
            try:
                await asyncio.wait_for(client.close(), timeout=5)
            except Exception:  # noqa: BLE001 - best effort, the connection may already be gone
                pass
            ctx.detach_ssh(client)
        if not was_running:  # cancelled before _run started: the task body never runs, update the row here
            ctx.log("job cancelled")
            await self._set(job_id, status=JobStatus.cancelled, ended_at=datetime.now(timezone.utc))
            ctx._close()
            self._contexts.pop(job_id, None)
            self._tasks.pop(job_id, None)
        return {"cancelled": True, "was_running": was_running}

    async def wait(self, job_id: str) -> None:
        task = self._tasks.get(job_id)
        if task:
            try:
                await task
            except asyncio.CancelledError:
                pass


runner = JobRunner()


def read_log(job_id: str) -> str:
    p = settings.jobs_dir / job_id / "log.txt"
    return p.read_text(encoding="utf-8") if p.exists() else ""
