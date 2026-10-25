"""User administration (admin only) + the user service shared with `api/auth.py` (v0.8.0, stream A).

`/api/users`: list, create (optional generated temporary password), patch role/disabled, delete (not self, not the
last admin), reset password, reset MFA, unlock, audit log. v0.9.0: `/api/users/requests` — approve/reject public
account requests (`api/registration.py`). `bootstrap_admin()` runs at startup (and lazily on the
first login, so test clients without lifespan work too).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core import app_settings, mailer
from ..core import db as _db
from ..core.config import settings
from ..core.db import AccountRequest, AuditLog, User, get_session, utcnow
from ..core.passwords import (
    DEFAULT_ADMIN_PASSWORD,
    auth_settings,
    generate_temp_password,
    hash_password,
    password_violations,
)
from ..core.security import ROLES, require_role

log = logging.getLogger("avdlab.users")

router = APIRouter(prefix="/users", tags=["users"])
Role = Literal["admin", "operator", "viewer"]


# ================================================================ service
def aware(dt: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; treat them as UTC."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def is_locked(u: User, now: datetime | None = None) -> bool:
    lu = aware(u.locked_until)
    return lu is not None and lu > (now or utcnow())


def normalize_username(name: str) -> str:
    return (name or "").strip().lower()


def user_out(u: User) -> dict[str, Any]:
    lu = aware(u.locked_until)
    return {
        "id": u.id, "username": u.username, "role": u.role, "disabled": bool(u.disabled),
        "must_change_password": bool(u.must_change_password), "mfa_enabled": bool(u.mfa_enabled),
        "recovery_codes_left": len(u.recovery_codes or []) if u.mfa_enabled else 0,
        "failed_logins": int(u.failed_logins or 0),
        "locked": is_locked(u), "locked_until": lu.isoformat() if lu and is_locked(u) else None,
        "last_login_at": aware(u.last_login_at).isoformat() if u.last_login_at else None,  # type: ignore[union-attr]
        "password_changed_at": aware(u.password_changed_at).isoformat() if u.password_changed_at else None,  # type: ignore[union-attr]
        "created_at": aware(u.created_at).isoformat() if u.created_at else None,  # type: ignore[union-attr]
    }


def client_info(request: Request | None) -> tuple[str | None, str | None]:
    if request is None:
        return None, None
    ip = request.client.host if request.client else None
    return ip, (request.headers.get("user-agent") or "")[:512] or None


async def audit(session: AsyncSession, event: str, username: str = "", request: Request | None = None,
                detail: str | None = None, *, commit: bool = True) -> None:
    ip, ua = client_info(request)
    session.add(AuditLog(event=event, username=username, ip=ip, user_agent=ua, detail=detail))
    if commit:
        await session.commit()


async def by_username(session: AsyncSession, username: str) -> User | None:
    return (await session.execute(select(User).where(User.username == normalize_username(username)))).scalar_one_or_none()


async def count_admins(session: AsyncSession, *, enabled_only: bool = True) -> int:
    q = select(func.count()).select_from(User).where(User.role == "admin")
    if enabled_only:
        q = q.where(User.disabled.is_(False))
    return int((await session.execute(q)).scalar_one())


def set_password(u: User, password: str, *, must_change: bool) -> None:
    u.password_hash = hash_password(password)
    u.password_changed_at = utcnow()
    u.must_change_password = must_change
    u.session_version = int(u.session_version or 1) + 1  # every existing session of this user becomes stale
    u.failed_logins = 0
    u.locked_until = None


def check_policy(password: str, username: str) -> None:
    v = password_violations(password, username)
    if v:
        raise HTTPException(422, {"message": "password " + "; ".join(v), "violations": v})


async def bootstrap_admin() -> User | None:
    """Create the first admin from AVDLAB_ADMIN_USER / AVDLAB_ADMIN_PASSWORD when `users` is empty.

    A default or weak env password still works (upgrade path for single-admin installs) but forces a change.
    """
    async with _db.SessionLocal() as s:
        if int((await s.execute(select(func.count()).select_from(User))).scalar_one()):
            return None
        name = normalize_username(settings.admin_user) or "admin"
        pw = settings.admin_password
        weak = pw == DEFAULT_ADMIN_PASSWORD or bool(password_violations(pw, name))
        if weak:
            log.warning("bootstrap admin %r: AVDLAB_ADMIN_PASSWORD is the default or fails the password policy — "
                        "the password must be changed at first sign-in", name)
        u = User(username=name, password_hash=hash_password(pw), role="admin", must_change_password=weak,
                 password_changed_at=utcnow(), recovery_codes=[])
        s.add(u)
        s.add(AuditLog(event="user.bootstrap", username=name, detail="weak password → must change" if weak else None))
        try:
            await s.commit()
        except Exception:  # noqa: BLE001 - a concurrent bootstrap (2 workers) won the race
            await s.rollback()
            return None
        await s.refresh(u)
        return u


# ================================================================ admin API
admin_only = require_role("admin")


class UserCreate(BaseModel):
    username: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._@-]*$")
    password: str | None = None
    role: Role = "viewer"
    send_temp: bool = False  # kept for API compatibility: a temp password is generated whenever none is given


class UserPatch(BaseModel):
    role: Role | None = None
    disabled: bool | None = None


async def _get(session: AsyncSession, user_id: str) -> User:
    u = await session.get(User, user_id)
    if u is None:
        raise HTTPException(404, "user not found")
    return u


@router.get("")
async def list_users(_: str = Depends(admin_only), session: AsyncSession = Depends(get_session)) -> list[dict]:
    rows = (await session.execute(select(User).order_by(User.username))).scalars().all()
    return [user_out(u) for u in rows]


@router.get("/audit")
async def audit_log(limit: int = Query(100, ge=1, le=1000), _: str = Depends(admin_only),
                    session: AsyncSession = Depends(get_session)) -> list[dict]:
    rows = (await session.execute(select(AuditLog).order_by(AuditLog.id.desc()).limit(limit))).scalars().all()
    return [{"id": a.id, "at": aware(a.at).isoformat() if a.at else None, "username": a.username, "event": a.event,  # type: ignore[union-attr]
             "ip": a.ip, "user_agent": a.user_agent, "detail": a.detail} for a in rows]


@router.post("", status_code=201)
async def create_user(body: UserCreate, request: Request, admin: str = Depends(admin_only),
                      session: AsyncSession = Depends(get_session)) -> dict:
    name = normalize_username(body.username)
    if await by_username(session, name):
        raise HTTPException(409, f"user {name} already exists")
    temp: str | None = None
    if body.password:
        check_policy(body.password, name)
        pw, must = body.password, False
    else:
        temp = pw = generate_temp_password(20, name)
        must = True
    u = User(username=name, role=body.role, recovery_codes=[])
    set_password(u, pw, must_change=must)
    u.session_version = 1
    session.add(u)
    await audit(session, "user.created", name, request, f"by {admin}, role {body.role}", commit=False)
    await session.commit()
    await session.refresh(u)
    return {**user_out(u), "temporary_password": temp}


# ================================================================ v0.9.0: account requests (approve / reject)
def request_out(r: AccountRequest) -> dict[str, Any]:
    return {"id": r.id, "username": r.username, "email": r.email, "full_name": r.full_name, "reason": r.reason,
            "status": r.status, "ip": r.ip, "role": r.role, "user_id": r.user_id, "decided_by": r.decided_by,
            "decided_at": aware(r.decided_at).isoformat() if r.decided_at else None,  # type: ignore[union-attr]
            "decision_reason": r.decision_reason,
            "created_at": aware(r.created_at).isoformat() if r.created_at else None}  # type: ignore[union-attr]


async def pending_request(session: AsyncSession, username: str) -> AccountRequest | None:
    q = select(AccountRequest).where(AccountRequest.username == username, AccountRequest.status == "pending")
    return (await session.execute(q)).scalars().first()


class ApproveIn(BaseModel):
    role: Role = "viewer"


class RejectIn(BaseModel):
    reason: str | None = Field(default=None, max_length=2000)
    notify: bool = True  # mail the applicant (only when SMTP is configured)


async def _pending(session: AsyncSession, req_id: str) -> AccountRequest:
    r = await session.get(AccountRequest, req_id)
    if r is None:
        raise HTTPException(404, "account request not found")
    if r.status != "pending":
        raise HTTPException(409, f"request already {r.status}")
    return r


def _login_url(request: Request) -> str:
    return (settings.public_url.strip().rstrip("/") or str(request.base_url).rstrip("/")) + "/login"


@router.get("/requests")
async def list_requests(status: Literal["pending", "all", "approved", "rejected"] = "pending",
                        _: str = Depends(admin_only), session: AsyncSession = Depends(get_session)) -> list[dict]:
    q = select(AccountRequest).order_by(AccountRequest.created_at.desc())
    if status != "all":
        q = q.where(AccountRequest.status == status)
    return [request_out(r) for r in (await session.execute(q)).scalars().all()]


@router.post("/requests/{req_id}/approve")
async def approve_request(req_id: str, request: Request, body: ApproveIn | None = None,
                          admin: str = Depends(admin_only), session: AsyncSession = Depends(get_session)) -> dict:
    body = body or ApproveIn()
    r = await _pending(session, req_id)
    if await by_username(session, r.username):
        raise HTTPException(409, f"user {r.username} already exists")
    temp = generate_temp_password(20, r.username)
    u = User(username=r.username, role=body.role, recovery_codes=[])
    set_password(u, temp, must_change=True)
    u.session_version = 1
    session.add(u)
    await session.flush()
    r.status, r.role, r.user_id, r.decided_by, r.decided_at = "approved", body.role, u.id, admin, utcnow()
    await audit(session, "user.created", r.username, request, f"by {admin}, role {body.role} (account request)",
                commit=False)
    await audit(session, "registration.approved", r.username, request, f"by {admin}, role {body.role}", commit=False)
    await session.commit()
    await session.refresh(u)
    cfg = await app_settings.load(session)
    mailed = cfg.smtp_configured
    if mailed:
        text = (f"Hello {r.full_name or r.username},\n\nyour {cfg.app_title} account request was approved.\n\n"
                f"Username:            {r.username}\nTemporary password:  {temp}\nSign in at:          "
                f"{_login_url(request)}\n\nYou will be asked to choose your own password at the first sign-in.\n")
        mailer.spawn(mailer.send_mail(f"[{cfg.app_title}] your account is ready", text, to=r.email))
    return {"user": user_out(u), "temporary_password": temp, "request": request_out(r), "mail_queued": mailed}


@router.post("/requests/{req_id}/reject")
async def reject_request(req_id: str, request: Request, body: RejectIn | None = None,
                         admin: str = Depends(admin_only), session: AsyncSession = Depends(get_session)) -> dict:
    body = body or RejectIn()
    r = await _pending(session, req_id)
    reason = (body.reason or "").strip() or None
    r.status, r.decided_by, r.decided_at, r.decision_reason = "rejected", admin, utcnow(), reason
    await audit(session, "registration.rejected", r.username, request, f"by {admin}" + (f": {reason}" if reason else ""),
                commit=False)
    await session.commit()
    cfg = await app_settings.load(session)
    mailed = cfg.smtp_configured and body.notify
    if mailed:
        text = (f"Hello {r.full_name or r.username},\n\nyour {cfg.app_title} account request ({r.username}) was "
                "not approved." + (f"\n\nReason: {reason}" if reason else "")
                + (f"\n\nQuestions? Contact {cfg.support_email}." if cfg.support_email else "") + "\n")
        mailer.spawn(mailer.send_mail(f"[{cfg.app_title}] account request", text, to=r.email))
    return {**request_out(r), "mail_queued": mailed}


@router.patch("/{user_id}")
async def patch_user(user_id: str, body: UserPatch, request: Request, admin: str = Depends(admin_only),
                     session: AsyncSession = Depends(get_session)) -> dict:
    u = await _get(session, user_id)
    demote = body.role is not None and body.role != "admin" and u.role == "admin"
    disable = bool(body.disabled) and not u.disabled
    if u.username == admin and (demote or disable):
        raise HTTPException(409, "you cannot demote or disable yourself")
    if (demote or (disable and u.role == "admin")) and not u.disabled and await count_admins(session) <= 1:
        raise HTTPException(409, "cannot demote or disable the last admin")
    changes = []
    if body.role is not None and body.role != u.role:
        changes.append(f"role {u.role}→{body.role}")
        u.role = body.role
    if body.disabled is not None and body.disabled != u.disabled:
        changes.append("disabled" if body.disabled else "enabled")
        u.disabled = body.disabled
    if changes:
        await audit(session, "user.updated", u.username, request, f"by {admin}: {', '.join(changes)}", commit=False)
    await session.commit()
    return user_out(u)


@router.delete("/{user_id}")
async def delete_user(user_id: str, request: Request, admin: str = Depends(admin_only),
                      session: AsyncSession = Depends(get_session)) -> dict:
    u = await _get(session, user_id)
    if u.username == admin:
        raise HTTPException(409, "you cannot delete yourself")
    if u.role == "admin" and await count_admins(session) - (0 if u.disabled else 1) < 1:
        raise HTTPException(409, "cannot delete the last admin")
    await session.delete(u)
    await audit(session, "user.deleted", u.username, request, f"by {admin}", commit=False)
    await session.commit()
    return {"ok": True}


@router.post("/{user_id}/reset-password")
async def reset_password(user_id: str, request: Request, admin: str = Depends(admin_only),
                         session: AsyncSession = Depends(get_session)) -> dict:
    u = await _get(session, user_id)
    temp = generate_temp_password(20, u.username)
    set_password(u, temp, must_change=True)
    await audit(session, "user.password_reset", u.username, request, f"by {admin}", commit=False)
    await session.commit()
    return {**user_out(u), "temporary_password": temp}


@router.post("/{user_id}/mfa/reset")
async def reset_mfa(user_id: str, request: Request, admin: str = Depends(admin_only),
                    session: AsyncSession = Depends(get_session)) -> dict:
    u = await _get(session, user_id)
    u.mfa_enabled, u.totp_secret, u.recovery_codes = False, None, []
    u.session_version = int(u.session_version or 1) + 1
    await audit(session, "mfa.reset", u.username, request, f"by {admin}", commit=False)
    await session.commit()
    return user_out(u)


@router.post("/{user_id}/unlock")
async def unlock(user_id: str, request: Request, admin: str = Depends(admin_only),
                 session: AsyncSession = Depends(get_session)) -> dict:
    u = await _get(session, user_id)
    u.failed_logins, u.locked_until = 0, None
    await audit(session, "user.unlocked", u.username, request, f"by {admin}", commit=False)
    await session.commit()
    return user_out(u)


def lockout_until() -> datetime:
    return utcnow() + timedelta(minutes=auth_settings.login_lockout_minutes)


__all__ = ["router", "bootstrap_admin", "ROLES"]
