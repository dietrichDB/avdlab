"""AI lab designer API (bring your own model).

  GET    /ai/link                    {linked, link: {provider, base_url, model, has_api_key, …} | null,
                                      shared: {available, provider, model}, effective: user|shared|null}
  PUT    /ai/link                    {provider, base_url?, model, api_key?}  (api_key write-only: absent/null keeps,
                                      "" removes)
  DELETE /ai/link                    → 204 (link + stored key)
  POST   /ai/link/test               {provider?, base_url?, model?, api_key?} (unsaved form values win; the stored
                                      key is used when api_key is absent) → {ok, models, model_listed, error}
  POST   /ai/design                  {lab_id?, template?, prompt, history?, current_knobs?}
                                      → {kind: proposal|question|text, summary, assumptions, knobs, knob_changes,
                                         topology_ops, dry_run: {valid, violations, changed_files, diff, graph?, …},
                                         question?, text?, assistant_message, repaired, source, provider, model}
                                      409 {code: no_ai_link} · 429 rate limit · 502 provider error (key masked)
  POST   /ai/design/apply            {lab_id, knobs, topology_ops} → re-validated, then PATCH /labs/{id} (knobs) and
                                      POST /topology/{id}/edit (ops) — both commit to the lab's git; audit `ai.apply`
  GET/PUT /admin/ai                  shared default (admin): ai_default_provider/base_url/model, ai_shared_allowed,
                                      ai_default_api_key (write-only; reported as has_ai_default_api_key)
  POST   /admin/ai/test              probe the shared default

Viewers: RoleGuard refuses every POST/PUT/DELETE here (design and apply are operator+).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..api.users import audit
from ..core.db import Job, JobStatus, Lab, User, get_session
from ..core.security import current_user_obj, require_role
from . import config as ai_config
from . import designer
from .providers import PROVIDERS, ProviderError, make_provider, mask

log = logging.getLogger("avdlab.ai")

router = APIRouter()
ai_router = APIRouter(prefix="/ai", tags=["ai"])
admin_router = APIRouter(prefix="/admin/ai", tags=["ai"])
admin_only = require_role("admin")


# ------------------------------------------------------------------------------------------------ schemas
class LinkIn(BaseModel):
    provider: str
    base_url: str = Field(default="", max_length=512)
    model: str = Field(max_length=200)
    api_key: str | None = Field(default=None, max_length=4096)


class LinkTestIn(BaseModel):
    provider: str | None = None
    base_url: str | None = Field(default=None, max_length=512)
    model: str | None = Field(default=None, max_length=200)
    api_key: str | None = Field(default=None, max_length=4096)


class HistoryItem(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    content: str = Field(max_length=20000)


class DesignIn(BaseModel):
    lab_id: str | None = None
    template: str | None = None
    prompt: str = Field(min_length=1, max_length=8000)
    history: list[HistoryItem] = Field(default=[], max_length=40)
    current_knobs: dict[str, Any] | None = None  # New-lab flow: the wizard's current values


class ApplyIn(BaseModel):
    lab_id: str
    knobs: dict[str, Any] = {}
    topology_ops: list[dict[str, Any]] = []


def _err(exc: ValueError) -> HTTPException:
    return HTTPException(422, {"message": str(exc), "violations": [{"message": str(exc)}]})


async def _probe(provider: str, base_url: str, model: str, api_key: str) -> dict[str, Any]:
    try:
        p = make_provider(provider, base_url, model, api_key)
        res = await p.probe()
    except ProviderError as exc:
        return {"ok": False, "models": [], "model_listed": False, "error": mask(str(exc), api_key)}
    models = res.get("models") or []
    return {"ok": True, "models": models[:100], "model_listed": model in models if models else None, "error": None}


# ------------------------------------------------------------------------------------------------ per-user link
@ai_router.get("/link")
async def get_link(user: User = Depends(current_user_obj), session: AsyncSession = Depends(get_session)) -> dict:
    return await ai_config.link_view(session, user)


@ai_router.put("/link")
async def put_link(body: LinkIn, request: Request, user: User = Depends(current_user_obj),
                   session: AsyncSession = Depends(get_session)) -> dict:
    try:
        provider = ai_config.check_provider(body.provider)
        base_url = ai_config.check_base_url(body.base_url)
        model = ai_config.check_model(body.model)
    except ValueError as exc:
        raise _err(exc) from exc
    await ai_config.save_link(session, user, provider, base_url, model, body.api_key)
    await audit(session, "ai.link.saved", user.username, request, f"{provider} {model}")
    return await ai_config.link_view(session, user)


@ai_router.delete("/link", status_code=204)
async def delete_link(request: Request, user: User = Depends(current_user_obj),
                      session: AsyncSession = Depends(get_session)) -> Response:
    if await ai_config.delete_link(session, user):
        await audit(session, "ai.link.removed", user.username, request)
    return Response(status_code=204)


@ai_router.post("/link/test")
async def test_link(body: LinkTestIn | None = None, user: User = Depends(current_user_obj),
                    session: AsyncSession = Depends(get_session)) -> dict:
    body = body or LinkTestIn()
    link = await ai_config.get_link(session, user)
    if body.provider is None and link is None:
        resolved = await ai_config.resolve(session, user)
        if resolved is None:
            raise HTTPException(409, {"message": "no AI model linked", "code": "no_ai_link"})
        return await _probe(resolved.provider, resolved.base_url, resolved.model, resolved.api_key)
    try:
        provider = ai_config.check_provider(body.provider if body.provider is not None else link.provider)  # type: ignore[union-attr]
        base_url = ai_config.check_base_url(body.base_url if body.base_url is not None else (link.base_url if link else ""))
        model = ai_config.check_model(body.model if body.model is not None else (link.model if link else ""))
    except ValueError as exc:
        raise _err(exc) from exc
    key = body.api_key if body.api_key else await ai_config.link_key(session, link)
    return await _probe(provider, base_url, model, key)


# ------------------------------------------------------------------------------------------------ design
async def _resolved(session: AsyncSession, user: User) -> ai_config.Resolved:
    r = await ai_config.resolve(session, user)
    if r is None:
        raise HTTPException(409, {"message": "no AI model linked: link Claude, OpenAI or a local model on your Account page",
                                  "code": "no_ai_link"})
    return r


@ai_router.post("/design")
async def design(body: DesignIn, user: User = Depends(current_user_obj),
                 session: AsyncSession = Depends(get_session)) -> dict:
    from ..labs.api import _stored_knobs

    lab: Lab | None = None
    chosen: dict[str, Any] | None = None
    if not body.lab_id and not body.template:
        # 0.10.0 "from scratch": a first tool call picks the template, then the normal template-mode design runs
        resolved0 = await _resolved(session, user)
        if designer.rate_limited(user.username):
            raise HTTPException(429, "AI design rate limit reached: try again later")
        provider0 = make_provider(resolved0.provider, resolved0.base_url, resolved0.model, resolved0.api_key)
        try:
            chosen = await designer.choose_template(provider0, body.prompt, [h.model_dump() for h in body.history])
        except ProviderError as exc:
            log.warning("ai choose_template failed for %s: %s", user.username, mask(str(exc), resolved0.api_key))
            raise HTTPException(502, {"message": mask(str(exc), resolved0.api_key), "violations": []}) from None
        body.template = chosen["template"]
    if body.lab_id:
        lab = await session.get(Lab, body.lab_id)
        if lab is None:
            raise HTTPException(404, "lab not found")
        tpl = designer.template_for(lab.template)
        base = _stored_knobs(lab)
    else:
        tpl = designer.template_for(str(body.template))
        if tpl is None:
            raise HTTPException(422, {"message": f"unknown template {body.template!r}", "violations": []})
        base = dict(body.current_knobs or {})
    resolved = await _resolved(session, user)
    if designer.rate_limited(user.username):
        raise HTTPException(429, "AI design rate limit reached: try again later")
    values = designer.current_values(tpl, base)
    l3ls = designer.is_l3ls(tpl)
    topo = await asyncio.to_thread(designer.topology_summary, lab, tpl, values if lab is None else base)
    system = designer.system_prompt(tpl, lab=lab, values=values, topo=topo, l3ls=l3ls)
    tool_list = designer.tools(tpl, lab_mode=lab is not None, l3ls=l3ls)
    messages = designer._clean_messages([h.model_dump() for h in body.history], body.prompt)
    provider = make_provider(resolved.provider, resolved.base_url, resolved.model, resolved.api_key)
    try:
        out = await designer.run_design(provider, system=system, messages=messages, tool_list=tool_list,
                                        validate=designer.validator_for(lab, tpl, base))
    except ProviderError as exc:
        log.warning("ai design failed for %s (%s %s): %s", user.username, resolved.provider, resolved.model,
                    mask(str(exc), resolved.api_key))
        raise HTTPException(502, {"message": mask(str(exc), resolved.api_key), "violations": []}) from None
    out.update(source=resolved.source, provider=resolved.provider, model=resolved.model)
    if chosen is not None:
        out["template"] = chosen["template"]
        out["template_reason"] = chosen["reason"]
        out["lab_name"] = chosen["lab_name"]
    out.setdefault("assumptions", [])
    out.setdefault("knobs", {})
    out.setdefault("knob_changes", [])
    out.setdefault("topology_ops", [])
    out.setdefault("dry_run", None)
    log.info("ai design by %s: %s (%s)", user.username, out["kind"], resolved.provider)
    return out


@ai_router.post("/design/apply")
async def apply(body: ApplyIn, request: Request, user: User = Depends(current_user_obj),
                session: AsyncSession = Depends(get_session)) -> dict:
    from ..labs import api as labs_api
    from ..topology import api as topo_api

    lab = await session.get(Lab, body.lab_id)
    if lab is None:
        raise HTTPException(404, "lab not found")
    tpl = designer.template_for(lab.template)
    knobs, ops, notes, violations = designer.sanitize(tpl, body.knobs, body.topology_ops)
    if violations:
        raise HTTPException(422, {"message": "invalid proposal", "violations": violations})
    if not knobs and not ops:
        raise HTTPException(422, {"message": "nothing to apply", "violations": []})
    running = (await session.execute(select(Job.type).where(
        Job.lab_id == lab.id, Job.type.in_(("lab.build", "lab.deploy", "lab.destroy")),
        Job.status.in_([JobStatus.queued, JobStatus.running])))).first()
    if running:
        raise HTTPException(409, {"message": f"{running[0]} is running for this lab", "violations": []})
    dry = await asyncio.to_thread(designer.dry_run_lab, lab, knobs, ops)  # all-or-nothing: never half-apply
    if not dry["valid"]:
        raise HTTPException(422, {"message": f"{len(dry['violations'])} violation(s)", "violations": dry["violations"]})
    changed = {c["key"]: knobs[c["key"]] for c in dry["knob_changes"] if c["key"] in knobs}
    result: dict[str, Any] = {"knob_changes": dry["knob_changes"], "topology": None, "notes": notes}
    if changed:
        await labs_api.patch_lab(lab.id, labs_api.LabPatchIn(knobs=changed), session=session, user=user.username)
    if ops:
        result["topology"] = await topo_api.edit_topology(lab.id, topo_api.EditIn(ops=ops, dry_run=False),
                                                          session=session, user=user.username)
        result["topology"].pop("graph", None)
    detail = f"lab {lab.name}: knobs {', '.join(changed) or '-'}; ops {', '.join(dry.get('ops') or []) or '-'}"
    await audit(session, "ai.apply", user.username, request, detail[:2000])
    result["lab"] = await labs_api.get_lab(lab.id, session=session, user=user.username)
    return result


# ------------------------------------------------------------------------------------------------ admin: shared default
@admin_router.get("")
async def get_shared(_: str = Depends(admin_only), session: AsyncSession = Depends(get_session)) -> dict:
    return await ai_config.admin_view(session)


@admin_router.put("")
async def put_shared(body: dict[str, Any], request: Request, admin: str = Depends(admin_only),
                     session: AsyncSession = Depends(get_session)) -> dict:
    allowed = set(ai_config.SHARED_TYPES) | {ai_config.SHARED_KEY_SECRET}
    ignored = {"has_ai_default_api_key", "configured"}
    unknown = sorted(set(body) - allowed - ignored)
    if unknown:
        raise HTTPException(422, f"unknown setting(s): {', '.join(unknown)}")
    try:
        changed = await ai_config.update_shared(session, {k: v for k, v in body.items() if k in allowed}, by=admin)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if changed:
        await audit(session, "settings.ai_updated", admin, request, ", ".join(changed))
    return await ai_config.admin_view(session)


@admin_router.post("/test")
async def test_shared(_: str = Depends(admin_only), session: AsyncSession = Depends(get_session)) -> dict:
    cfg = await ai_config.shared(session)
    if not ai_config.shared_configured(cfg):
        return {"ok": False, "models": [], "model_listed": False, "error": "set a provider and a model first"}
    return await _probe(cfg["ai_default_provider"], cfg["ai_default_base_url"], cfg["ai_default_model"],
                        await ai_config.shared_key(session))


router.include_router(ai_router)
router.include_router(admin_router)
__all__ = ["router", "PROVIDERS"]
