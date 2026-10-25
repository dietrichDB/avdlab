"""Admin-editable application settings (v0.9.0, stream A).

A small key → JSON-value table (`app_settings`). Every key has a typed default taken from the `AVDLAB_*` environment
(`core/config.py`); a value stored in the DB wins. `smtp_password` is never stored in the table: it lives in the Fernet
secret store as the secret named `smtp_password` (write-only — the API only reports `has_smtp_password`), with
`AVDLAB_SMTP_PASSWORD` as fallback.

    await load()                  → AppConfig (all keys, effective values, without the SMTP password)
    await smtp_password()         → str (secret store, else env)
    await update({...}, by=user)  → AppConfig (partial; validates types; `smtp_password`: str sets, "" clears)
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import db as _db
from .config import settings
from .db import AppSetting, SecretKind

SMTP_PASSWORD_SECRET = "smtp_password"


@dataclass
class AppConfig:
    app_title: str
    github_url: str
    support_email: str
    registration_enabled: bool
    intro_video_url: str
    feedback_mail_to: str
    feedback_mail_enabled: bool
    registration_mail_to: str
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_starttls: bool
    smtp_from: str

    @property
    def smtp_configured(self) -> bool:
        return bool(self.smtp_host.strip())

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# key → python type (bool before int: bool is a subclass of int)
TYPES: dict[str, type] = {
    "app_title": str, "github_url": str, "support_email": str, "registration_enabled": bool, "intro_video_url": str,
    "feedback_mail_to": str, "feedback_mail_enabled": bool, "registration_mail_to": str,
    "smtp_host": str, "smtp_port": int, "smtp_user": str, "smtp_starttls": bool, "smtp_from": str,
}
_EMAIL = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")


def is_email(value: str) -> bool:
    return len(value) <= 254 and bool(_EMAIL.match(value))


def split_addresses(value: str) -> list[str]:
    return [a.strip() for a in re.split(r"[,;\s]+", value or "") if a.strip()]


def default_from() -> str:
    host = urlparse(settings.public_url.strip()).hostname if settings.public_url.strip() else None
    return f"avdlab@{host or 'localhost'}"


def env_defaults() -> dict[str, Any]:
    return {k: getattr(settings, k) for k in TYPES}


def _effective(raw: dict[str, Any]) -> AppConfig:
    v = {**env_defaults(), **{k: raw[k] for k in TYPES if k in raw and raw[k] is not None}}
    if not str(v["registration_mail_to"]).strip():
        v["registration_mail_to"] = v["feedback_mail_to"]
    if not str(v["smtp_from"]).strip():
        v["smtp_from"] = default_from()
    return AppConfig(**v)


async def _stored(session: AsyncSession) -> dict[str, Any]:
    return {r.key: r.value for r in (await session.execute(select(AppSetting))).scalars()}


async def load(session: AsyncSession | None = None) -> AppConfig:
    if session is not None:
        return _effective(await _stored(session))
    async with _db.SessionLocal() as s:
        return _effective(await _stored(s))


async def smtp_password(session: AsyncSession | None = None) -> str:
    from .security import secret_store

    async def _get(s: AsyncSession) -> str:
        sec = await secret_store.by_name(s, SMTP_PASSWORD_SECRET)
        return secret_store.decrypt(sec.ciphertext) if sec else settings.smtp_password

    if session is not None:
        return await _get(session)
    async with _db.SessionLocal() as s:
        return await _get(s)


async def has_smtp_password(session: AsyncSession) -> bool:
    return bool(await smtp_password(session))


def coerce(key: str, value: Any) -> Any:
    """Validate/normalise one value; raises ValueError with a human-readable message."""
    t = TYPES.get(key)
    if t is None:
        raise ValueError(f"unknown setting {key!r}")
    if value is None:
        return None  # → back to the env default
    if t is bool:
        if not isinstance(value, bool):
            raise ValueError(f"{key} must be true or false")
        return value
    if t is int:
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
            raise ValueError(f"{key} must be a port number 1–65535")
        return value
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    value = value.strip()
    if len(value) > 512:
        raise ValueError(f"{key} is too long")
    if key == "github_url" and value and not re.match(r"^https?://\S+$", value):
        raise ValueError("github_url must be an http(s) URL")
    if key == "intro_video_url" and value and not re.match(r"^(https?://|/)\S*$", value):
        raise ValueError("intro_video_url must be an absolute path or an http(s) URL")
    if key in ("support_email", "smtp_from") and value and not is_email(value):
        raise ValueError(f"{key} must be an e-mail address")
    if key in ("feedback_mail_to", "registration_mail_to") and value:
        bad = [a for a in split_addresses(value) if not is_email(a)]
        if bad:
            raise ValueError(f"{key}: not an e-mail address: {', '.join(bad)}")
    if key == "app_title" and not value:
        raise ValueError("app_title must not be empty")
    return value


async def update(session: AsyncSession, changes: dict[str, Any], by: str | None = None) -> list[str]:
    """Apply a partial update (validated first, all-or-nothing). Returns the changed keys (password as
    `smtp_password`). `None` resets a key to its env default."""
    from .db import Secret
    from .security import secret_store

    clean: dict[str, Any] = {}
    for k, v in changes.items():
        if k == "smtp_password":
            continue
        clean[k] = coerce(k, v)
    pw = changes.get("smtp_password")
    if pw is not None and not isinstance(pw, str):
        raise ValueError("smtp_password must be a string")
    stored = {r.key: r for r in (await session.execute(select(AppSetting))).scalars()}
    changed: list[str] = []
    for k, v in clean.items():
        row = stored.get(k)
        if v is None:
            if row is not None:
                await session.delete(row)
                changed.append(k)
        elif row is None:
            session.add(AppSetting(key=k, value=v, updated_by=by))
            changed.append(k)
        elif row.value != v:
            row.value, row.updated_by = v, by
            changed.append(k)
    if pw is not None:
        sec = await secret_store.by_name(session, SMTP_PASSWORD_SECRET)
        if pw == "":
            if sec is not None:
                await session.execute(delete(Secret).where(Secret.id == sec.id))
                changed.append("smtp_password")
        elif sec is None:
            session.add(Secret(name=SMTP_PASSWORD_SECRET, kind=SecretKind.password, ciphertext=secret_store.encrypt(pw),
                               meta={"purpose": "SMTP password (admin settings)"}))
            changed.append("smtp_password")
        else:
            sec.ciphertext = secret_store.encrypt(pw)
            changed.append("smtp_password")
    await session.commit()
    return changed


async def admin_view(session: AsyncSession) -> dict[str, Any]:
    """`GET /admin/settings`: effective values + env defaults; the password only as a flag."""
    cfg = await load(session)
    stored = await _stored(session)
    return {**cfg.as_dict(), "has_smtp_password": await has_smtp_password(session),
            "smtp_configured": cfg.smtp_configured, "overridden": sorted(k for k in stored if k in TYPES),
            "defaults": {**env_defaults(), "smtp_from": settings.smtp_from or default_from()}}


async def public_view() -> dict[str, Any]:
    """Fields added to the public `GET /meta`."""
    try:
        cfg = await load()
    except Exception:  # noqa: BLE001 - /meta must answer even before init_db (fresh install, broken DB)
        cfg = _effective({})
    return {"app_title": cfg.app_title, "github_url": cfg.github_url or None, "support_email": cfg.support_email or None,
            "registration_enabled": cfg.registration_enabled, "intro_video_url": cfg.intro_video_url or None,
            # 0.10.0: the "e-mail feedback" button opens the user's mail client with this recipient (first address)
            "feedback_email": (str(cfg.feedback_mail_to or "").split(",")[0].strip() or None)}
