"""Multi-user session auth (roles, session invalidation, RoleGuard middleware) + Fernet secret store."""
from __future__ import annotations

import json
import secrets as pysecrets
from typing import Any, Callable

from cryptography.fernet import Fernet
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import db as _db
from .config import settings
from .db import Secret, SecretKind, User, get_session


# ------------------------------------------------------------- auth
# Session layout (signed cookie, starlette SessionMiddleware):
#   full session:    {"user": <username>, "uid": <user id>, "sv": <session_version>}
#   pending MFA:     {"mfa_pending_user_id": <user id>, "mfa_attempts": n}   (no "user" key → nothing granted)
#   MFA enrolment:   "mfa_setup_secret": Fernet(secret) until /auth/mfa/enable confirms it
# WebSocket endpoints check `ws.session.get("user")`; RoleGuard additionally rejects stale WS sessions.

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
ROLES = ("admin", "operator", "viewer")


async def session_user(session: dict[str, Any]) -> User | None:
    """The DB user behind a *full* session, or None (no/pending session, deleted, disabled, stale version)."""
    uid, name = session.get("uid"), session.get("user")
    if not uid or not name:
        return None
    async with _db.SessionLocal() as s:
        u = await s.get(User, uid)
    if u is None or u.disabled or u.username != name or int(session.get("sv") or 0) != int(u.session_version or 1):
        return None
    return u


async def current_user(request: Request) -> str:
    """Username of the signed-in user (401 for anonymous, pending-MFA, disabled/deleted or invalidated sessions)."""
    user = getattr(request.state, "auth_user", None)  # cached by RoleGuard for this request
    if user is None:
        user = await session_user(request.session)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not authenticated")
    request.state.auth_user = user
    return user.username


async def current_user_obj(request: Request) -> User:
    await current_user(request)
    return request.state.auth_user


def require_role(*roles: str) -> Callable[..., Any]:
    """Dependency: `user: str = Depends(require_role("admin"))` → username, 403 when the role is not allowed."""

    async def dep(request: Request) -> str:
        name = await current_user(request)
        u: User = request.state.auth_user
        if u.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"role {u.role} may not access this resource")
        return name

    return dep


def role_denial(role: str, method: str, path: str) -> str | None:
    """Why `role` may not do `method path` (None = allowed). Pure function → unit-testable."""
    if role == "admin" or not path.startswith("/api/"):
        return None
    if path == "/api/users" or path.startswith("/api/users/"):
        return f"role {role} may not access user administration"
    if path == "/api/admin" or path.startswith("/api/admin/"):  # v0.9.0: admin settings
        return f"role {role} may not access admin settings"
    if path == "/api/register" or path.startswith("/api/register/"):  # v0.9.0: public account requests
        return None
    if role == "operator":
        if method == "DELETE" and path.startswith("/api/hosts/"):
            return f"role operator may not DELETE {path}"
        return None
    # viewer (and any unknown role): read-only, plus the self-service auth endpoints. A web terminal is a shell,
    # not a read: refused for viewers (method WEBSOCKET is what RoleGuard passes for upgrades).
    if path.endswith("/terminal"):
        return f"role {role} may not open a terminal"
    if method in SAFE_METHODS or method == "WEBSOCKET" or (method == "POST" and path.startswith("/api/auth/")):
        return None
    return f"role {role} may not {method} {path}"


class RoleGuard:
    """ASGI middleware enforcing role permissions. Must sit *inside* SessionMiddleware (needs `scope["session"]`).

    Anonymous / pending / stale sessions pass through untouched (the routers' `current_user` answers 401), except
    WebSocket upgrades with a stale session, which are closed with 4401 before the endpoint accepts them.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] not in ("http", "websocket") or "session" not in scope or not scope["session"].get("user"):
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        if not path.startswith("/api/"):
            return await self.app(scope, receive, send)
        user = await session_user(scope["session"])
        if scope["type"] == "websocket":
            if user is None:
                await send({"type": "websocket.close", "code": 4401, "reason": "not authenticated"})
                return
            if role_denial(user.role, "WEBSOCKET", path):
                await send({"type": "websocket.close", "code": 4403, "reason": "forbidden for this role"})
                return
            return await self.app(scope, receive, send)
        if user is not None:
            scope.setdefault("state", {})["auth_user"] = user
            why = role_denial(user.role, scope.get("method", "GET").upper(), path)
            if why:
                body = json.dumps({"detail": why}).encode()
                await send({"type": "http.response.start", "status": 403,
                            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


# ------------------------------------------------------------- secret store
def _load_key() -> bytes:
    if settings.secret_key:
        return settings.secret_key.encode()
    key_file = settings.data_dir / "secret.key"
    if key_file.exists():
        return key_file.read_bytes().strip()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    key_file.write_bytes(key)
    key_file.chmod(0o600)
    return key


class SecretStore:
    """Encrypts values at rest; only `reveal()` returns plaintext (used by job code, never by list endpoints)."""

    def __init__(self) -> None:
        self._fernet: Fernet | None = None

    @property
    def fernet(self) -> Fernet:
        if self._fernet is None:
            self._fernet = Fernet(_load_key())
        return self._fernet

    def encrypt(self, value: str) -> str:
        return self.fernet.encrypt(value.encode()).decode()

    def decrypt(self, token: str) -> str:
        return self.fernet.decrypt(token.encode()).decode()

    async def create(self, session: AsyncSession, name: str, kind: SecretKind, value: str, meta: dict[str, Any] | None = None) -> Secret:
        sec = Secret(name=name, kind=kind, ciphertext=self.encrypt(value), meta=meta or {})
        session.add(sec)
        await session.commit()
        await session.refresh(sec)
        return sec

    async def reveal(self, session: AsyncSession, secret_id: str) -> str:
        sec = await session.get(Secret, secret_id)
        if sec is None:
            raise KeyError(secret_id)
        return self.decrypt(sec.ciphertext)

    async def by_name(self, session: AsyncSession, name: str) -> Secret | None:
        return (await session.execute(select(Secret).where(Secret.name == name))).scalar_one_or_none()


secret_store = SecretStore()


def generate_session_secret() -> str:
    return pysecrets.token_urlsafe(48)


async def db_dep(session: AsyncSession = Depends(get_session)) -> AsyncSession:  # convenience alias
    return session
