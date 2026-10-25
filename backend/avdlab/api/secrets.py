from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.db import Secret, SecretKind, get_session
from ..core.security import current_user, secret_store

router = APIRouter(prefix="/secrets", tags=["secrets"])


class SecretIn(BaseModel):
    name: str
    kind: SecretKind = SecretKind.generic
    value: str
    meta: dict = {}


class SecretOut(BaseModel):
    id: str
    name: str
    kind: SecretKind
    meta: dict
    created_at: str


def _out(s: Secret) -> SecretOut:
    return SecretOut(id=s.id, name=s.name, kind=s.kind, meta=s.meta, created_at=s.created_at.isoformat())


@router.get("", response_model=list[SecretOut])
async def list_secrets(session: AsyncSession = Depends(get_session), user: str = Depends(current_user)):
    return [_out(s) for s in (await session.execute(select(Secret).order_by(Secret.name))).scalars()]


@router.post("", response_model=SecretOut, status_code=201)
async def create_secret(body: SecretIn, session: AsyncSession = Depends(get_session), user: str = Depends(current_user)):
    if await secret_store.by_name(session, body.name):
        raise HTTPException(409, "secret name exists")
    return _out(await secret_store.create(session, body.name, body.kind, body.value, body.meta))


@router.delete("/{secret_id}", status_code=204)
async def delete_secret(secret_id: str, session: AsyncSession = Depends(get_session), user: str = Depends(current_user)):
    s = await session.get(Secret, secret_id)
    if not s:
        raise HTTPException(404)
    await session.delete(s)
    await session.commit()
