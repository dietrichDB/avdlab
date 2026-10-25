"""Which model a user's design call goes to: their own link (`ai_links`) or the admin's shared default.

Shared default = app settings `ai_default_provider`, `ai_default_base_url`, `ai_default_model`, `ai_shared_allowed`
(rows of the `app_settings` table, env `AVDLAB_AI_*` as defaults) + the secret `ai_default_api_key` (Fernet secret
store, env `AVDLAB_AI_DEFAULT_API_KEY` as fallback). A user without a link uses the shared model only when
`ai_shared_allowed` is true. Keys are write-only: nothing here returns them except `resolve()` (server-side use).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import settings
from ..core.db import AILink, AppSetting, Secret, SecretKind, User
from ..core.security import secret_store
from .providers import PROVIDERS

SHARED_KEY_SECRET = "ai_default_api_key"
SHARED_TYPES: dict[str, type] = {"ai_default_provider": str, "ai_default_base_url": str, "ai_default_model": str,
                                 "ai_shared_allowed": bool}
URL_RX = re.compile(r"^https?://[^\s/]+\S*$")


@dataclass
class Resolved:
    source: str  # user | shared
    provider: str
    base_url: str
    model: str
    api_key: str


def user_secret_name(user_id: str) -> str:
    return f"ai_key_{user_id}"


def check_provider(provider: Any) -> str:
    if provider not in PROVIDERS:
        raise ValueError(f"provider must be one of {list(PROVIDERS)}")
    return str(provider)


def check_base_url(url: Any) -> str:
    url = str(url or "").strip()
    if url and not URL_RX.match(url):
        raise ValueError("base_url must be an http(s) URL")
    if len(url) > 512:
        raise ValueError("base_url is too long")
    return url


def check_model(model: Any) -> str:
    model = str(model or "").strip()
    if not model:
        raise ValueError("model is required")
    if len(model) > 200:
        raise ValueError("model is too long")
    return model


# ------------------------------------------------------------------------------------------------ per-user link
async def get_link(session: AsyncSession, user: User) -> AILink | None:
    return (await session.execute(select(AILink).where(AILink.user_id == user.id))).scalar_one_or_none()


async def _secret_value(session: AsyncSession, secret_id: str | None) -> str:
    if not secret_id:
        return ""
    sec = await session.get(Secret, secret_id)
    return secret_store.decrypt(sec.ciphertext) if sec is not None else ""


async def link_key(session: AsyncSession, link: AILink | None) -> str:
    return await _secret_value(session, link.api_key_secret_id if link else None)


async def has_secret(session: AsyncSession, secret_id: str | None) -> bool:
    return bool(secret_id) and await session.get(Secret, secret_id) is not None


async def save_link(session: AsyncSession, user: User, provider: str, base_url: str, model: str,
                    api_key: str | None) -> AILink:
    """`api_key`: None keeps the stored key, "" removes it, a string replaces it."""
    link = await get_link(session, user)
    if link is None:
        link = AILink(user_id=user.id, provider=provider, base_url=base_url, model=model)
        session.add(link)
    else:
        link.provider, link.base_url, link.model = provider, base_url, model
    if api_key is not None:
        name = user_secret_name(user.id)
        sec = await secret_store.by_name(session, name)
        if api_key == "":
            if sec is not None:
                await session.delete(sec)
            link.api_key_secret_id = None
        else:
            if sec is None:
                sec = Secret(name=name, kind=SecretKind.generic, ciphertext=secret_store.encrypt(api_key),
                             meta={"purpose": f"AI designer API key of {user.username}"})
                session.add(sec)
                await session.flush()
            else:
                sec.ciphertext = secret_store.encrypt(api_key)
            link.api_key_secret_id = sec.id
    await session.commit()
    await session.refresh(link)
    return link


async def delete_link(session: AsyncSession, user: User) -> bool:
    link = await get_link(session, user)
    await session.execute(delete(Secret).where(Secret.name == user_secret_name(user.id)))
    if link is not None:
        await session.delete(link)
    await session.commit()
    return link is not None


# ------------------------------------------------------------------------------------------------ shared default
async def shared(session: AsyncSession) -> dict[str, Any]:
    """Effective shared-default settings (without the key)."""
    rows = {r.key: r.value for r in (await session.execute(
        select(AppSetting).where(AppSetting.key.in_(list(SHARED_TYPES))))).scalars()}
    out: dict[str, Any] = {}
    for k in SHARED_TYPES:
        v = rows.get(k)
        out[k] = getattr(settings, k) if v is None else v
    return out


async def shared_key(session: AsyncSession) -> str:
    sec = await secret_store.by_name(session, SHARED_KEY_SECRET)
    return secret_store.decrypt(sec.ciphertext) if sec is not None else settings.ai_default_api_key


def shared_configured(cfg: dict[str, Any]) -> bool:
    return cfg.get("ai_default_provider") in PROVIDERS and bool(str(cfg.get("ai_default_model") or "").strip())


async def admin_view(session: AsyncSession) -> dict[str, Any]:
    cfg = await shared(session)
    return {**cfg, "has_ai_default_api_key": bool(await shared_key(session)), "configured": shared_configured(cfg)}


async def update_shared(session: AsyncSession, changes: dict[str, Any], by: str | None = None) -> list[str]:
    """Partial update (validated first). `None` resets a key to its env default; `ai_default_api_key`: "" clears."""
    clean: dict[str, Any] = {}
    for k, v in changes.items():
        if k == SHARED_KEY_SECRET:
            continue
        if k not in SHARED_TYPES:
            raise ValueError(f"unknown setting {k!r}")
        if v is None:
            clean[k] = None
        elif SHARED_TYPES[k] is bool:
            if not isinstance(v, bool):
                raise ValueError(f"{k} must be true or false")
            clean[k] = v
        elif k == "ai_default_provider":
            clean[k] = "" if v == "" else check_provider(v)
        elif k == "ai_default_base_url":
            clean[k] = check_base_url(v)
        else:
            clean[k] = str(v).strip()[:200]
    key = changes.get(SHARED_KEY_SECRET)
    if key is not None and not isinstance(key, str):
        raise ValueError("ai_default_api_key must be a string")
    stored = {r.key: r for r in (await session.execute(
        select(AppSetting).where(AppSetting.key.in_(list(SHARED_TYPES))))).scalars()}
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
    if key is not None:
        sec = await secret_store.by_name(session, SHARED_KEY_SECRET)
        if key == "":
            if sec is not None:
                await session.delete(sec)
                changed.append(SHARED_KEY_SECRET)
        elif sec is None:
            session.add(Secret(name=SHARED_KEY_SECRET, kind=SecretKind.generic, ciphertext=secret_store.encrypt(key),
                               meta={"purpose": "AI designer shared API key (admin settings)"}))
            changed.append(SHARED_KEY_SECRET)
        else:
            sec.ciphertext = secret_store.encrypt(key)
            changed.append(SHARED_KEY_SECRET)
    await session.commit()
    return changed


# ------------------------------------------------------------------------------------------------ resolution
async def resolve(session: AsyncSession, user: User) -> Resolved | None:
    link = await get_link(session, user)
    if link is not None:
        return Resolved("user", link.provider, link.base_url, link.model, await link_key(session, link))
    cfg = await shared(session)
    if cfg["ai_shared_allowed"] and shared_configured(cfg):
        return Resolved("shared", cfg["ai_default_provider"], cfg["ai_default_base_url"], cfg["ai_default_model"],
                        await shared_key(session))
    return None


async def link_view(session: AsyncSession, user: User) -> dict[str, Any]:
    """`GET /ai/link`: the user's own link (never the key) + whether a shared model is available."""
    link = await get_link(session, user)
    cfg = await shared(session)
    shared_ok = bool(cfg["ai_shared_allowed"]) and shared_configured(cfg)
    own = None
    if link is not None:
        own = {"provider": link.provider, "base_url": link.base_url, "model": link.model,
               "has_api_key": await has_secret(session, link.api_key_secret_id),
               "created_at": link.created_at.isoformat() if link.created_at else None,
               "updated_at": link.updated_at.isoformat() if link.updated_at else None}
    return {
        "linked": link is not None, "link": own,
        "shared": {"available": shared_ok, "provider": cfg["ai_default_provider"] if shared_ok else None,
                   "model": cfg["ai_default_model"] if shared_ok else None},
        "effective": "user" if link is not None else ("shared" if shared_ok else None),
    }
