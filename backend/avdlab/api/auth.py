"""/api/auth — login (+ lockout), TOTP MFA (setup/enable/verify/disable, recovery codes), self-service password."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.db import User, get_session, utcnow
from ..core.passwords import (
    auth_settings,
    dummy_verify,
    generate_recovery_codes,
    hash_recovery_code,
    match_recovery_code,
    needs_rehash,
    hash_password,
    new_totp_secret,
    policy_description,
    qr_svg,
    totp_uri,
    verify_password,
    verify_totp,
)
from ..core.security import current_user, current_user_obj, secret_store
from .users import (
    audit,
    aware,
    bootstrap_admin,
    by_username,
    check_policy,
    is_locked,
    lockout_until,
    normalize_username,
    set_password,
)

router = APIRouter(prefix="/auth", tags=["auth"])

MFA_MAX_ATTEMPTS = 5
INVALID = "invalid username or password"


class LoginIn(BaseModel):
    username: str
    password: str


class CodeIn(BaseModel):
    code: str


class PasswordChangeIn(BaseModel):
    current_password: str
    new_password: str


class PasswordIn(BaseModel):
    password: str


class MfaDisableIn(BaseModel):
    password: str
    code: str


def me_out(u: User) -> dict:
    return {"user": u.username, "role": u.role, "must_change_password": bool(u.must_change_password),
            "mfa_enabled": bool(u.mfa_enabled)}


def _grant(request: Request, u: User) -> None:
    request.session.clear()
    request.session.update({"user": u.username, "uid": u.id, "sv": int(u.session_version or 1)})


def _check_code(u: User, code: str) -> str | None:
    """'totp' / 'recovery' when `code` is valid for `u` (a recovery code is consumed), else None."""
    if u.totp_secret and verify_totp(secret_store.decrypt(u.totp_secret), code):
        return "totp"
    idx = match_recovery_code(list(u.recovery_codes or []), code)
    if idx is not None:
        codes = list(u.recovery_codes or [])
        codes.pop(idx)
        u.recovery_codes = codes  # reassign → JSON column is flagged dirty
        return "recovery"
    return None


@router.post("/login")
async def login(body: LoginIn, request: Request, session: AsyncSession = Depends(get_session)):
    await bootstrap_admin()  # no-op once a user exists; covers ASGI test clients that skip the lifespan
    name = normalize_username(body.username)
    u = await by_username(session, name)
    if u is None or u.disabled:
        dummy_verify(body.password)  # same cost as a wrong password → no user enumeration by timing
        await audit(session, "login.failed", name, request, "disabled user" if u else "unknown user")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, INVALID)
    if is_locked(u):
        await audit(session, "login.locked", name, request, "attempt while locked")
        return JSONResponse({"detail": f"account locked until {aware(u.locked_until).isoformat()}",  # type: ignore[union-attr]
                             "locked_until": aware(u.locked_until).isoformat()}, status_code=423)  # type: ignore[union-attr]
    if not verify_password(u.password_hash, body.password):
        if aware(u.locked_until) is not None:  # previous lock expired → start counting afresh
            u.locked_until, u.failed_logins = None, 0
        u.failed_logins = int(u.failed_logins or 0) + 1
        if u.failed_logins >= auth_settings.login_max_failures:
            u.locked_until = lockout_until()
            await audit(session, "login.lockout", name, request, f"{u.failed_logins} failed attempts")
            until = aware(u.locked_until).isoformat()  # type: ignore[union-attr]
            return JSONResponse({"detail": f"too many failed attempts: account locked until {until}", "locked_until": until},
                                status_code=423)
        await audit(session, "login.failed", name, request, f"wrong password ({u.failed_logins})")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, INVALID)
    u.failed_logins, u.locked_until = 0, None
    if needs_rehash(u.password_hash):
        u.password_hash = hash_password(body.password)
    if u.mfa_enabled:
        request.session.clear()
        request.session.update({"mfa_pending_user_id": u.id, "mfa_attempts": 0})
        await audit(session, "login.mfa_pending", name, request)
        return {"mfa_required": True}
    u.last_login_at = utcnow()
    _grant(request, u)
    await audit(session, "login.ok", name, request)
    return me_out(u)


@router.post("/mfa/verify")
async def mfa_verify(body: CodeIn, request: Request, session: AsyncSession = Depends(get_session)) -> dict:
    uid = request.session.get("mfa_pending_user_id")
    u = await session.get(User, uid) if uid else None
    if u is None or u.disabled or not u.mfa_enabled:
        request.session.clear()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "no pending sign-in: start again")
    kind = _check_code(u, body.code)
    if kind is None:
        attempts = int(request.session.get("mfa_attempts") or 0) + 1
        await audit(session, "mfa.failed", u.username, request, f"attempt {attempts}")
        if attempts >= MFA_MAX_ATTEMPTS:
            request.session.clear()
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "too many wrong codes: sign in again")
        request.session["mfa_attempts"] = attempts
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid code")
    u.last_login_at = utcnow()
    _grant(request, u)
    await audit(session, "login.ok", u.username, request, f"mfa ({kind})"
                + (f", {len(u.recovery_codes or [])} recovery codes left" if kind == "recovery" else ""))
    return {**me_out(u), "recovery_codes_left": len(u.recovery_codes or [])}


@router.post("/logout")
async def logout(request: Request, session: AsyncSession = Depends(get_session)) -> dict:
    if request.session.get("user"):
        await audit(session, "logout", request.session["user"], request)
    request.session.clear()
    return {"ok": True}


@router.get("/me")
async def me(u: User = Depends(current_user_obj)) -> dict:
    return {**me_out(u), "recovery_codes_left": len(u.recovery_codes or []) if u.mfa_enabled else 0}


@router.get("/policy")
async def policy() -> dict:
    """Password policy parameters for the live checklist (public: also used by the forced-change screen)."""
    return policy_description()


async def _load(session: AsyncSession, request: Request) -> User:
    await current_user(request)
    u = await session.get(User, request.state.auth_user.id)
    if u is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not authenticated")
    return u


@router.post("/password")
async def change_password(body: PasswordChangeIn, request: Request, session: AsyncSession = Depends(get_session)) -> dict:
    u = await _load(session, request)
    if not verify_password(u.password_hash, body.current_password):
        await audit(session, "password.change_failed", u.username, request, "wrong current password")
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "current password is wrong")
    if body.new_password == body.current_password:
        raise HTTPException(422, {"message": "the new password must differ from the current one",
                                                                   "violations": ["must differ from the current password"]})
    check_policy(body.new_password, u.username)
    set_password(u, body.new_password, must_change=False)  # bumps session_version → other sessions are logged out
    await audit(session, "password.changed", u.username, request, commit=False)
    await session.commit()
    _grant(request, u)  # keep *this* session valid
    return me_out(u)


# ---------------------------------------------------------------- MFA enrolment / management
@router.post("/mfa/setup")
async def mfa_setup(request: Request, session: AsyncSession = Depends(get_session)) -> dict:
    u = await _load(session, request)
    if u.mfa_enabled:
        raise HTTPException(409, "MFA is already enabled: disable it first")
    secret = new_totp_secret()
    request.session["mfa_setup_secret"] = secret_store.encrypt(secret)  # cookie is signed, not encrypted
    url = totp_uri(secret, u.username)
    return {"secret": secret, "otpauth_url": url, "qr_svg": qr_svg(url)}


@router.post("/mfa/enable")
async def mfa_enable(body: CodeIn, request: Request, session: AsyncSession = Depends(get_session)) -> dict:
    u = await _load(session, request)
    token = request.session.get("mfa_setup_secret")
    if not token:
        raise HTTPException(409, "no MFA setup in progress: call /auth/mfa/setup first")
    secret = secret_store.decrypt(token)
    if not verify_totp(secret, body.code):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid code: check the clock of your phone and try again")
    codes = generate_recovery_codes()
    u.totp_secret = secret_store.encrypt(secret)
    u.mfa_enabled = True
    u.recovery_codes = [hash_recovery_code(c) for c in codes]
    request.session.pop("mfa_setup_secret", None)
    await audit(session, "mfa.enabled", u.username, request)
    return {"mfa_enabled": True, "recovery_codes": codes}


def _require_password(u: User, password: str) -> None:
    if not verify_password(u.password_hash, password):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "password is wrong")


@router.post("/mfa/disable")
async def mfa_disable(body: MfaDisableIn, request: Request, session: AsyncSession = Depends(get_session)) -> dict:
    u = await _load(session, request)
    if not u.mfa_enabled:
        raise HTTPException(409, "MFA is not enabled")
    _require_password(u, body.password)
    if _check_code(u, body.code) is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid code")
    u.mfa_enabled, u.totp_secret, u.recovery_codes = False, None, []
    await audit(session, "mfa.disabled", u.username, request)
    return {"mfa_enabled": False}


@router.post("/mfa/recovery-codes")
async def mfa_recovery_codes(body: PasswordIn, request: Request, session: AsyncSession = Depends(get_session)) -> dict:
    u = await _load(session, request)
    if not u.mfa_enabled:
        raise HTTPException(409, "MFA is not enabled")
    _require_password(u, body.password)
    codes = generate_recovery_codes()
    u.recovery_codes = [hash_recovery_code(c) for c in codes]
    await audit(session, "mfa.recovery_regenerated", u.username, request)
    return {"recovery_codes": codes}
