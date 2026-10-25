"""Public account requests + admin settings (v0.9.0, stream A).

Public (no session):
  POST /register {username, email, full_name, reason}  → 201 {id, status}; 403 when registration is disabled,
                                                          409 username taken/pending, 422 invalid, 429 rate limit
  GET  /register/status/{id}                            → {status}
Admin (`/admin/*` is admin-only in RoleGuard, plus `require_role("admin")` here):
  GET  /admin/settings                                  → effective settings, `has_smtp_password` (never the password)
  PUT  /admin/settings {…partial}                       → same shape; `smtp_password`: "" clears, null/absent keeps
  POST /admin/settings/test-mail                        → {ok, error, to}

Approve/reject of requests lives in `api/users.py` (`/users/requests…`).
"""
from __future__ import annotations

import re
import time
from collections import defaultdict, deque
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..core import app_settings, mailer
from ..core.config import settings
from ..core.db import AccountRequest, get_session
from ..core.security import require_role
from .users import audit, by_username, client_info, pending_request

USERNAME_RX = re.compile(r"^[a-z0-9._-]{3,32}$")
RATE_LIMIT = 5  # requests …
RATE_WINDOW = 3600.0  # … per hour per client IP (in-memory, per process)
_hits: dict[str, deque[float]] = defaultdict(deque)

register_router = APIRouter(prefix="/register", tags=["registration"])
admin_router = APIRouter(prefix="/admin", tags=["admin"])
router = APIRouter()


def reset_rate_limit() -> None:
    _hits.clear()


def _rate_limited(ip: str, now: float | None = None) -> bool:
    now = time.monotonic() if now is None else now
    q = _hits[ip]
    while q and now - q[0] > RATE_WINDOW:
        q.popleft()
    if len(q) >= RATE_LIMIT:
        return True
    q.append(now)
    return False


def base_url(request: Request) -> str:
    return settings.public_url.strip().rstrip("/") or str(request.base_url).rstrip("/")


# ================================================================ public: account requests
class RegisterIn(BaseModel):
    username: str = Field(max_length=64)
    email: str = Field(max_length=254)
    full_name: str = Field(default="", max_length=120)
    reason: str = Field(default="", max_length=2000)


@register_router.post("", status_code=201)
async def register(body: RegisterIn, request: Request, session: AsyncSession = Depends(get_session)) -> dict:
    cfg = await app_settings.load(session)
    if not cfg.registration_enabled:
        raise HTTPException(403, "account requests are disabled on this server")
    ip, _ = client_info(request)
    if _rate_limited(ip or "?"):
        raise HTTPException(429, "too many account requests from your address: try again later")
    name = body.username.strip().lower()
    if not USERNAME_RX.match(name):
        raise HTTPException(422, "username must be 3–32 characters: lowercase letters, digits, '.', '_' or '-'")
    email = body.email.strip()
    if not app_settings.is_email(email):
        raise HTTPException(422, "please enter a valid e-mail address")
    if await by_username(session, name) or await pending_request(session, name):
        raise HTTPException(409, f"the username {name} is not available")
    req = AccountRequest(username=name, email=email, full_name=body.full_name.strip(), reason=body.reason.strip(),
                         status="pending", ip=ip)
    session.add(req)
    await audit(session, "registration.requested", name, request, f"{email}", commit=False)
    await session.commit()
    link = f"{base_url(request)}/users"
    text = (f"New avdlab account request\n\nUsername:  {name}\nName:      {req.full_name or '-'}\nE-mail:    {email}\n"
            f"From IP:   {ip or '-'}\n\nReason:\n{req.reason or '-'}\n\nApprove or reject it on {link}\n")
    if cfg.smtp_configured and cfg.registration_mail_to:
        mailer.spawn(mailer.send_mail(f"[{cfg.app_title}] account request: {name}", text, to=cfg.registration_mail_to))
    return {"id": req.id, "status": req.status}


@register_router.get("/status/{req_id}")
async def register_status(req_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    r = await session.get(AccountRequest, req_id)
    if r is None:
        raise HTTPException(404, "request not found")
    return {"status": r.status}


# ================================================================ admin: settings
admin_only = require_role("admin")


@admin_router.get("/settings")
async def get_settings(_: str = Depends(admin_only), session: AsyncSession = Depends(get_session)) -> dict:
    return await app_settings.admin_view(session)


@admin_router.put("/settings")
async def put_settings(body: dict[str, Any], request: Request, admin: str = Depends(admin_only),
                       session: AsyncSession = Depends(get_session)) -> dict:
    allowed = set(app_settings.TYPES) | {"smtp_password"}
    unknown = sorted(set(body) - allowed - {"has_smtp_password", "smtp_configured", "overridden", "defaults"})
    if unknown:
        raise HTTPException(422, f"unknown setting(s): {', '.join(unknown)}")
    try:
        changed = await app_settings.update(session, {k: v for k, v in body.items() if k in allowed}, by=admin)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if changed:
        await audit(session, "settings.updated", admin, request, ", ".join(changed))
    return await app_settings.admin_view(session)


@admin_router.post("/settings/test-mail")
async def test_mail(request: Request, admin: str = Depends(admin_only),
                    session: AsyncSession = Depends(get_session)) -> dict:
    cfg = await app_settings.load(session)
    text = (f"This is a test message from {cfg.app_title} ({base_url(request)}), sent by {admin}.\n\n"
            "If you can read this, outgoing e-mail works.\n")
    ok, error = await mailer.send_mail_result(f"[{cfg.app_title}] test mail", text, to=cfg.feedback_mail_to)
    await audit(session, "settings.test_mail", admin, request, "ok" if ok else (error or "failed")[:500])
    return {"ok": ok, "error": error, "to": cfg.feedback_mail_to}


router.include_router(register_router)
router.include_router(admin_router)
