"""NetBox helpers: targets ↔ secrets and the pynetbox client factory (the only place that builds a real client, so
tests monkeypatch `api_client` with a fake).

The API token only ever lives in the secret store (encrypted), in memory, and in the `Authorization` header pynetbox
sends — never in a job log (see `masking_log`).
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from ..core.db import NetBoxTarget, Secret, SecretKind
from ..core.security import secret_store

TEST_BUDGET = 15.0
REQUEST_TIMEOUT = 30.0
MASK = "********"
LogFn = Callable[[str], None]


class NetBoxError(RuntimeError):
    pass


def clean_url(url: str) -> str:
    """'netbox.lab:8000/' → 'http://netbox.lab:8000'; a trailing '/api' is dropped (pynetbox appends it)."""
    u = url.strip().rstrip("/")
    if not u:
        raise NetBoxError("url is required")
    if not re.match(r"^https?://", u, flags=re.I):
        u = "http://" + u
    if u.lower().endswith("/api"):
        u = u[:-4]
    if not re.match(r"^https?://[^/\s]+", u, flags=re.I):
        raise NetBoxError("url must be http(s)://host[:port][/path]")
    return u


def secret_name(target_id: str) -> str:
    return f"netbox-target-{target_id}-token"


async def set_target_token(session: AsyncSession, target: NetBoxTarget, value: str | None) -> None:
    """value None → unchanged; '' → removed; else created/replaced (encrypted)."""
    if value is None:
        return
    sec = await session.get(Secret, target.token_secret_id) if target.token_secret_id else None
    if value == "":
        target.token_secret_id = None
        if sec is not None:
            await session.delete(sec)
        return
    value = normalise_token(value)
    if not value or "\n" in value or "\r" in value:
        raise NetBoxError("token must be a single non-empty line")
    meta = {"purpose": "netbox_api", "updated_at": datetime.now(timezone.utc).isoformat()}
    if sec is None:
        name = secret_name(target.id)
        existing = await secret_store.by_name(session, name)
        if existing is not None:
            await session.delete(existing)
            await session.flush()
        sec = Secret(name=name, kind=SecretKind.netbox_token, ciphertext=secret_store.encrypt(value), meta=meta)
        session.add(sec)
        await session.flush()
        target.token_secret_id = sec.id
    else:
        sec.ciphertext = secret_store.encrypt(value)
        sec.meta = {**(sec.meta or {}), **meta}


async def target_token(session: AsyncSession, target: NetBoxTarget) -> str | None:
    """Stored token, normalised again on the way out so a value saved before 0.10.0 (with a 'Token ' prefix or a
    wrapped paste) still produces a valid Authorization header."""
    if not target.token_secret_id:
        return None
    return normalise_token(await secret_store.reveal(session, target.token_secret_id)) or None


def masking_log(log: LogFn, secrets: list[str | None]) -> LogFn:
    values = [s for s in secrets if s]

    def _log(line: str) -> None:
        for v in values:
            line = line.replace(v, MASK)
        log(line)

    return _log


def normalise_token(value: str) -> str:
    """Strip whitespace and a pasted `Token ` / `Bearer ` scheme prefix. NetBox 4.5 v2 tokens look like
    `nbt_<id>.<secret>` and must be sent as `Bearer …` (pynetbox >= 7.8 does that); v1 tokens are 40 hex chars → `Token …`."""
    v = (value or "").strip()
    while True:  # "Token Token nbt_…" from a double paste
        for prefix in ("Token ", "token ", "Bearer ", "bearer "):
            if v.startswith(prefix):
                v = v[len(prefix):].strip()
                break
        else:
            break
    # NetBox splits the header on whitespace and rejects anything but "<scheme> <token>": inner spaces/newlines from a
    # copy-paste (e.g. a wrapped token) would otherwise give "Must be in the form ..."
    return "".join(v.split())


def auth_scheme(token: str) -> str:
    return "Bearer" if token.startswith("nbt_") and "." in token[4:] else "Token"


# ------------------------------------------------------------------------------------------------ pynetbox (fakeable)
def api_client(url: str, token: str, *, verify_certs: bool = True, timeout: float = REQUEST_TIMEOUT) -> Any:
    """Factory for the pynetbox API object — tests monkeypatch this to return a fake."""
    import pynetbox
    import requests

    scheme = auth_scheme(token)

    class _Session(requests.Session):
        def request(self, *args: Any, **kwargs: Any) -> Any:  # pynetbox sets no timeout of its own
            kwargs.setdefault("timeout", timeout)
            headers = kwargs.get("headers")
            if isinstance(headers, dict):  # belt and braces for pynetbox < 7.8: v2 tokens need the Bearer scheme
                for k in list(headers):
                    if k.lower() == "authorization":
                        headers[k] = f"{scheme} {token}"
            return super().request(*args, **kwargs)

    nb = pynetbox.api(url, token=token)
    session = _Session()
    session.verify = verify_certs
    nb.http_session = session
    return nb


def check_target(url: str, token: str | None, *, verify_certs: bool) -> dict[str, Any]:
    """Synchronous `status()` call → `{ok, version, error}` (run in a thread)."""
    if not token:
        return {"ok": False, "version": None, "error": "no API token stored for this target"}
    try:
        status = api_client(url, token, verify_certs=verify_certs, timeout=TEST_BUDGET).status()
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        msg = f"{type(exc).__name__}: {exc}".replace(token, MASK)
        low = msg.lower()
        if "403" in msg or "invalid token" in low:
            hint = ("NetBox 4.5+ tokens look like nbt_<id>.<secret> and are sent as Bearer; older tokens are 40 hex characters. "
                    "Paste the token only, without a 'Token ' prefix, and give it read permission")
            msg = f"authentication failed: API token invalid or without read permission — {hint} ({msg})"
        elif "certificate" in low:
            msg += " (self-signed certificate? untick 'verify certificates')"
        return {"ok": False, "version": None, "error": msg}
    version = (status or {}).get("netbox-version") if isinstance(status, dict) else None
    return {"ok": True, "version": version, "error": None}
