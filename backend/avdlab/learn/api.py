"""In-app AVD learning API (phase 6, backend B).

  GET  /learn/schema/search?q=&schema=eos_designs|eos_cli_config_gen|all&limit=25   fuzzy key search
  GET  /learn/schema/{eos_designs|eos_cli_config_gen}?key=<dotted path>                 one schema entry
  GET  /learn/explain?lab_id=&node=&line=                                              explain one config line
  POST /learn/explain {lab_id, node, lines?: [str|int], all?: bool}                    batch / whole file
  GET  /learn/lessons                    lesson list (+ per-lesson progress)
  GET  /learn/lessons/{id}               parsed lesson (markdown / try / check / quiz blocks)
  POST /learn/lessons/{id}/check {lab_id, checkpoint_id} → {passed, detail}  (a pass is stored in learn_progress)
  GET  /learn/progress                   passed checkpoints per lesson
  GET  /learn/glossary                   terms
  GET  /learn/tours                      tours keyed by page
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.db import Job, Lab, LearnProgress, ValidationRun, get_session
from ..core.security import current_user
from ..labs import service
from . import schema as schema_mod
from .explain import explain_file, explain_lines
from .lessons import CheckContext, LessonError, evaluate, load_lessons

router = APIRouter(prefix="/learn", tags=["learn"])
HERE = Path(__file__).parent


# ------------------------------------------------------------------------------------------------ schema
@router.get("/schema/search")
async def schema_search(q: str, schema: str = "all", limit: int = 25, user: str = Depends(current_user)) -> list[dict[str, Any]]:
    names = schema_mod.SCHEMA_NAMES if schema == "all" else (schema,)
    if any(n not in schema_mod.SCHEMA_NAMES for n in names):
        raise HTTPException(422, f"schema must be one of {schema_mod.SCHEMA_NAMES} or all")
    limit = max(1, min(limit, 200))

    def run() -> list[dict[str, Any]]:
        hits = [h for n in names for h in schema_mod.search(n, q, limit)]
        hits.sort(key=lambda h: -h["score"])
        return hits[:limit]

    return await asyncio.to_thread(run)


@router.get("/schema/{name}")
async def schema_lookup(name: str, key: str = "", user: str = Depends(current_user)) -> dict[str, Any]:
    if name not in schema_mod.SCHEMA_NAMES:
        raise HTTPException(404, f"unknown schema {name!r}; one of {schema_mod.SCHEMA_NAMES}")
    try:
        return await asyncio.to_thread(schema_mod.lookup, name, key)
    except schema_mod.SchemaKeyError as exc:
        raise HTTPException(404, {"message": f"no key {exc.key!r} in {name}", "suggestions": exc.suggestions}) from exc


# ------------------------------------------------------------------------------------------------ explain
class ExplainIn(BaseModel):
    lab_id: str
    node: str
    lines: list[str | int] | None = None
    all: bool = False


async def _lab(session: AsyncSession, lab_id: str) -> Lab:
    lab = await session.get(Lab, lab_id)
    if lab is None:
        raise HTTPException(404, "lab not found")
    return lab


def _template_knobs(lab: Lab) -> set[str]:
    from ..labs.templates import get_template

    try:
        return {k.key for k in get_template(lab.template).knobs}
    except KeyError:
        return set()


def _explain_kwargs(lab: Lab) -> dict[str, Any]:
    return {"knobs": dict((lab.settings or {}).get("knobs") or {}), "template": lab.template,
            "template_knobs": _template_knobs(lab)}


def _check_node(lab_dir: Path, node: str) -> None:
    if "/" in node or node.startswith(".") or not (lab_dir / "intended" / "configs" / f"{node}.cfg").is_file():
        raise HTTPException(404, f"no intended config for {node!r} — build the lab first")


@router.get("/explain")
async def explain_get(lab_id: str, node: str, line: str, session: AsyncSession = Depends(get_session),
                      user: str = Depends(current_user)) -> dict[str, Any]:
    lab = await _lab(session, lab_id)
    lab_dir = service.lab_dir(lab)
    _check_node(lab_dir, node)
    res = await asyncio.to_thread(explain_lines, lab_dir, node, [line], **_explain_kwargs(lab))
    return res[0]


@router.post("/explain")
async def explain_post(body: ExplainIn, session: AsyncSession = Depends(get_session),
                       user: str = Depends(current_user)) -> dict[str, Any]:
    lab = await _lab(session, body.lab_id)
    lab_dir = service.lab_dir(lab)
    _check_node(lab_dir, body.node)
    kw = _explain_kwargs(lab)
    if body.all or not body.lines:
        return await asyncio.to_thread(explain_file, lab_dir, body.node, **kw)
    if len(body.lines) > 2000:
        raise HTTPException(422, "at most 2000 lines per request")
    results = await asyncio.to_thread(explain_lines, lab_dir, body.node, list(body.lines), **kw)
    return {"host": body.node, "lines": results, "explained": sum(1 for r in results if r["why"]), "total": len(results)}


# ------------------------------------------------------------------------------------------------ lessons
class CheckIn(BaseModel):
    lab_id: str | None = None
    checkpoint_id: str = Field(min_length=1)


def _lessons() -> dict[str, Any]:
    try:
        return load_lessons()
    except LessonError as exc:  # a broken lesson file must not take the hub down silently
        raise HTTPException(500, f"lesson content error: {exc}") from exc


async def _progress_rows(session: AsyncSession, user: str) -> list[LearnProgress]:
    return list((await session.execute(select(LearnProgress).where(LearnProgress.username == user))).scalars().all())


def _progress_by_lesson(rows: list[LearnProgress]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for r in rows:
        out.setdefault(r.lesson_id, []).append(r.checkpoint_id)
    return out


@router.get("/lessons")
async def lessons(session: AsyncSession = Depends(get_session), user: str = Depends(current_user)) -> list[dict[str, Any]]:
    done = _progress_by_lesson(await _progress_rows(session, user))
    out = []
    for lesson in _lessons().values():
        s = lesson.summary()
        passed = [c for c in s["checkpoints"] if c in done.get(lesson.id, [])]
        out.append({**s, "progress": {"passed": passed, "total": len(s["checkpoints"]),
                                      "completed": bool(s["checkpoints"]) and len(passed) == len(s["checkpoints"])}})
    return out


@router.get("/lessons/{lesson_id}")
async def lesson(lesson_id: str, session: AsyncSession = Depends(get_session), user: str = Depends(current_user)) -> dict[str, Any]:
    ls = _lessons()
    if lesson_id not in ls:
        raise HTTPException(404, "lesson not found")
    ids = list(ls)
    i = ids.index(lesson_id)
    done = _progress_by_lesson(await _progress_rows(session, user)).get(lesson_id, [])
    return {**ls[lesson_id].public(), "passed": done,
            "prev": ids[i - 1] if i > 0 else None, "next": ids[i + 1] if i + 1 < len(ids) else None}


async def _check_context(session: AsyncSession, lab_id: str | None) -> CheckContext:
    if not lab_id:
        return CheckContext(lab_dir=None, knobs={}, jobs=[], validation=None)
    lab = await _lab(session, lab_id)
    jobs = (await session.execute(select(Job).where(Job.lab_id == lab.id).order_by(Job.created_at.desc()).limit(200))).scalars().all()
    run = (await session.execute(select(ValidationRun).where(ValidationRun.lab_id == lab.id)
                                 .order_by(ValidationRun.created_at.desc()).limit(1))).scalar_one_or_none()
    return CheckContext(lab_dir=service.lab_dir(lab), knobs=dict((lab.settings or {}).get("knobs") or {}),
                        jobs=[{"type": j.type, "status": j.status.value, "created_at": j.created_at.isoformat()} for j in jobs],
                        validation={"totals": run.totals, "report_path": run.report_path} if run else None,
                        state=lab.state.value)


@router.post("/lessons/{lesson_id}/check")
async def check(lesson_id: str, body: CheckIn, session: AsyncSession = Depends(get_session),
                user: str = Depends(current_user)) -> dict[str, Any]:
    ls = _lessons()
    if lesson_id not in ls:
        raise HTTPException(404, "lesson not found")
    cp = ls[lesson_id].checkpoint(body.checkpoint_id)
    if cp is None:
        raise HTTPException(404, "checkpoint not found")
    ctx = await _check_context(session, body.lab_id)
    try:
        passed, detail = await asyncio.to_thread(evaluate, cp, ctx)
    except LessonError as exc:
        raise HTTPException(400, str(exc)) from exc
    if passed:
        row = (await session.execute(select(LearnProgress).where(
            LearnProgress.username == user, LearnProgress.lesson_id == lesson_id,
            LearnProgress.checkpoint_id == body.checkpoint_id))).scalar_one_or_none()
        if row is None:
            row = LearnProgress(username=user, lesson_id=lesson_id, checkpoint_id=body.checkpoint_id)
            session.add(row)
        row.lab_id, row.detail, row.passed_at = body.lab_id, detail, datetime.now(timezone.utc)
        await session.commit()
    return {"lesson_id": lesson_id, "checkpoint_id": body.checkpoint_id, "passed": passed, "detail": detail,
            "hint": None if passed else cp.get("hint")}


@router.get("/progress")
async def progress(session: AsyncSession = Depends(get_session), user: str = Depends(current_user)) -> dict[str, Any]:
    rows = await _progress_rows(session, user)
    ls = _lessons()
    by = _progress_by_lesson(rows)
    lessons_out = {}
    for lid, lesson in ls.items():
        cps = lesson.summary()["checkpoints"]
        passed = [c for c in cps if c in by.get(lid, [])]
        lessons_out[lid] = {"passed": passed, "total": len(cps), "completed": bool(cps) and len(passed) == len(cps)}
    return {
        "lessons": lessons_out,
        "completed": sum(1 for v in lessons_out.values() if v["completed"]),
        "total": len(ls),
        "items": [{"lesson_id": r.lesson_id, "checkpoint_id": r.checkpoint_id, "lab_id": r.lab_id,
                   "passed_at": r.passed_at.isoformat() if r.passed_at else None} for r in rows],
    }


# ------------------------------------------------------------------------------------------------ glossary / tours
@lru_cache(maxsize=1)
def load_glossary() -> list[dict[str, Any]]:
    data = yaml.safe_load((HERE / "glossary.yml").read_text(encoding="utf-8")) or []
    for t in data:
        t.setdefault("aliases", [])
        t.setdefault("see", [])
        t.setdefault("lesson", None)
        t.setdefault("long", None)
        t.setdefault("doc_url", None)
    return sorted(data, key=lambda t: t["term"].lower())


@lru_cache(maxsize=1)
def load_tours() -> dict[str, Any]:
    return yaml.safe_load((HERE / "tours.yml").read_text(encoding="utf-8")) or {}


@router.get("/glossary")
async def glossary(user: str = Depends(current_user)) -> list[dict[str, Any]]:
    return load_glossary()


@router.get("/tours")
async def tours(user: str = Depends(current_user)) -> dict[str, Any]:
    return load_tours()
