from __future__ import annotations


from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.db import Job, get_session
from ..core.security import current_user
from ..jobs.engine import read_log, registered_job_types, runner

router = APIRouter(prefix="/jobs", tags=["jobs"])


class JobOut(BaseModel):
    id: str
    type: str
    status: str
    lab_id: str | None
    host_id: str | None
    params: dict
    result: dict
    artefacts: list
    error: str | None
    created_at: str
    started_at: str | None
    ended_at: str | None

    @classmethod
    def from_row(cls, j: Job) -> "JobOut":
        return cls(
            id=j.id, type=j.type, status=j.status.value, lab_id=j.lab_id, host_id=j.host_id, params=j.params,
            result=j.result, artefacts=j.artefacts, error=j.error, created_at=j.created_at.isoformat(),
            started_at=j.started_at.isoformat() if j.started_at else None,
            ended_at=j.ended_at.isoformat() if j.ended_at else None,
        )


@router.get("/types")
async def job_types(user: str = Depends(current_user)) -> list[str]:
    return registered_job_types()


@router.get("", response_model=list[JobOut])
async def list_jobs(lab_id: str | None = None, host_id: str | None = None, limit: int = 100,
                    session: AsyncSession = Depends(get_session), user: str = Depends(current_user)):
    q = select(Job).order_by(Job.created_at.desc()).limit(limit)
    if lab_id:
        q = q.where(Job.lab_id == lab_id)
    if host_id:
        q = q.where(Job.host_id == host_id)
    return [JobOut.from_row(j) for j in (await session.execute(q)).scalars()]


@router.get("/{job_id}", response_model=JobOut)
async def get_job(job_id: str, session: AsyncSession = Depends(get_session), user: str = Depends(current_user)):
    j = await session.get(Job, job_id)
    if not j:
        raise HTTPException(404, "job not found")
    return JobOut.from_row(j)


@router.get("/{job_id}/log")
async def get_log(job_id: str, user: str = Depends(current_user)) -> dict:
    return {"log": read_log(job_id)}


@router.post("/{job_id}/cancel")
async def cancel(job_id: str, user: str = Depends(current_user)) -> dict:
    """Kill a queued/running job → `{"cancelled": true, "was_running": bool}`; unknown/finished → `{"cancelled": false}`."""
    return await runner.cancel(job_id)


@router.websocket("/{job_id}/stream")
async def stream(ws: WebSocket, job_id: str) -> None:
    """Sends existing log first, then live lines; closes with a final status frame."""
    if not ws.session.get("user"):
        await ws.close(code=4401)
        return
    await ws.accept()
    for line in read_log(job_id).splitlines():
        await ws.send_json({"line": line})
    ctx = runner.context(job_id)
    if ctx is None:
        await ws.send_json({"done": True})
        await ws.close()
        return
    q = ctx.subscribe()
    try:
        while True:
            item = await q.get()
            if item is None:
                await ws.send_json({"done": True})
                break
            await ws.send_json({"line": item})
    except WebSocketDisconnect:
        pass
    finally:
        ctx.unsubscribe(q)
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass
