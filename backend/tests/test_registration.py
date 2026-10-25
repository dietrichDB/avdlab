"""v0.9.0 (stream A): public account requests, admin approve/reject, rate limit, /meta fields."""
from __future__ import annotations

import uuid

import aiosmtplib
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from avdlab.api import registration
from avdlab.core import app_settings, mailer
from avdlab.core.db import AccountRequest, AppSetting, Secret, SessionLocal


class FakeSMTP:
    sent: list = []

    def __init__(self, **kw):
        pass

    async def connect(self):
        pass

    async def login(self, *a):
        pass

    async def send_message(self, msg):
        FakeSMTP.sent.append(msg)

    async def quit(self):
        pass


@pytest_asyncio.fixture(autouse=True)
async def _clean(monkeypatch):
    from avdlab.core.db import init_db

    await init_db()
    registration.reset_rate_limit()
    FakeSMTP.sent = []
    monkeypatch.setattr(aiosmtplib, "SMTP", FakeSMTP)
    yield
    await mailer.drain()
    registration.reset_rate_limit()
    async with SessionLocal() as s:
        await s.execute(delete(AppSetting))
        await s.execute(delete(AccountRequest))
        await s.execute(delete(Secret).where(Secret.name == app_settings.SMTP_PASSWORD_SECRET))
        await s.commit()


def anon() -> AsyncClient:
    from avdlab.main import app

    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def uname() -> str:
    return f"req{uuid.uuid4().hex[:8]}"


def req(name: str, **kw) -> dict:
    return {"username": name, "email": f"{name}@example.com", "full_name": "Ada Lovelace", "reason": "learning AVD", **kw}


async def test_register_and_status_public(client):
    name = uname()
    async with anon() as c:
        r = await c.post("/api/register", json=req(name.upper()))
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["status"] == "pending" and set(body) == {"id", "status"}
        assert (await c.get(f"/api/register/status/{body['id']}")).json() == {"status": "pending"}
        assert (await c.get("/api/register/status/nope")).status_code == 404
        # same username again (pending) / existing user → 409
        assert (await c.post("/api/register", json=req(name))).status_code == 409
        assert (await c.post("/api/register", json=req("admin"))).status_code == 409
        # policy
        for bad in (req("ab"), req("x" * 33), req("bad name"), req("ümlaut"), req(uname(), email="not-an-email")):
            registration.reset_rate_limit()  # every attempt counts towards the limit (also invalid/409 ones)
            assert (await c.post("/api/register", json=bad)).status_code == 422, bad
        assert (await c.get("/api/users/requests")).status_code == 401  # admin listing is not public
    rows = (await client.get("/api/users/requests")).json()
    row = next(x for x in rows if x["id"] == body["id"])
    assert row["username"] == name and row["email"] == f"{name.upper()}@example.com" and row["status"] == "pending"
    assert any(a["event"] == "registration.requested" for a in (await client.get("/api/users/audit")).json())


async def test_register_as_logged_in_viewer_allowed(client):
    viewer = uname()
    await client.post("/api/users", json={"username": viewer, "password": "Blue-Lantern-42x", "role": "viewer"})
    async with anon() as c:
        await c.post("/api/auth/login", json={"username": viewer, "password": "Blue-Lantern-42x"})
        assert (await c.post("/api/register", json=req(uname()))).status_code == 201
        assert (await c.get("/api/users/requests")).status_code == 403


async def test_register_disabled_and_meta(client):
    async with anon() as c:
        m = (await c.get("/api/meta")).json()
        assert m["registration_enabled"] is True and m["app_title"] == "avdlab" and "github_url" in m
        assert m["copyright"].endswith("Systems Engineer, Arista Networks")
    await client.put("/api/admin/settings", json={"registration_enabled": False})
    async with anon() as c:
        assert (await c.get("/api/meta")).json()["registration_enabled"] is False
        assert (await c.post("/api/register", json=req(uname()))).status_code == 403


async def test_rate_limit():
    async with anon() as c:
        for _ in range(registration.RATE_LIMIT):
            assert (await c.post("/api/register", json=req(uname()))).status_code == 201
        assert (await c.post("/api/register", json=req(uname()))).status_code == 429
    registration.reset_rate_limit()
    assert registration._rate_limited("10.0.0.1", now=0.0) is False
    for _ in range(registration.RATE_LIMIT - 1):
        registration._rate_limited("10.0.0.1", now=1.0)
    assert registration._rate_limited("10.0.0.1", now=2.0) is True
    assert registration._rate_limited("10.0.0.1", now=2.0 + registration.RATE_WINDOW) is False  # window slid


async def test_approve_creates_user_with_temp_password(client):
    name = uname()
    async with anon() as c:
        rid = (await c.post("/api/register", json=req(name))).json()["id"]
    r = await client.post(f"/api/users/requests/{rid}/approve", json={"role": "operator"})
    assert r.status_code == 200, r.text
    out = r.json()
    temp = out["temporary_password"]
    assert out["user"]["username"] == name and out["user"]["role"] == "operator"
    assert out["user"]["must_change_password"] is True and len(temp) == 20 and out["mail_queued"] is False
    assert (await client.post(f"/api/users/requests/{rid}/approve", json={"role": "viewer"})).status_code == 409
    assert (await client.post(f"/api/users/requests/{rid}/reject", json={})).status_code == 409
    async with anon() as c:
        assert (await c.get(f"/api/register/status/{rid}")).json() == {"status": "approved"}
        r = await c.post("/api/auth/login", json={"username": name, "password": temp})
        assert r.status_code == 200 and r.json()["must_change_password"] is True
    assert all(x["id"] != rid for x in (await client.get("/api/users/requests")).json())
    assert any(x["id"] == rid for x in (await client.get("/api/users/requests?status=all")).json())
    events = [a["event"] for a in (await client.get("/api/users/audit")).json()]
    assert "registration.approved" in events
    assert (await client.post("/api/users/requests/nope/approve", json={"role": "viewer"})).status_code == 404


async def test_approve_and_reject_mail_applicant(client):
    await client.put("/api/admin/settings", json={"smtp_host": "smtp.example.com", "registration_mail_to": "adm@example.com"})
    name, other = uname(), uname()
    async with anon() as c:
        rid = (await c.post("/api/register", json=req(name))).json()["id"]
        rid2 = (await c.post("/api/register", json=req(other))).json()["id"]
    await mailer.drain()
    admin_mails = [m for m in FakeSMTP.sent if m["To"] == "adm@example.com"]
    assert len(admin_mails) == 2 and name in admin_mails[0]["Subject"] and "learning AVD" in admin_mails[0].get_content()
    out = (await client.post(f"/api/users/requests/{rid}/approve", json={"role": "viewer"})).json()
    assert out["mail_queued"] is True
    await mailer.drain()
    m = FakeSMTP.sent[-1]
    assert m["To"] == f"{name}@example.com" and out["temporary_password"] in m.get_content()
    assert "http://test/login" in m.get_content()
    r = await client.post(f"/api/users/requests/{rid2}/reject", json={"reason": "no seats left"})
    assert r.status_code == 200 and r.json()["status"] == "rejected" and r.json()["decision_reason"] == "no seats left"
    await mailer.drain()
    assert FakeSMTP.sent[-1]["To"] == f"{other}@example.com" and "no seats left" in FakeSMTP.sent[-1].get_content()
    async with anon() as c:
        assert (await c.get(f"/api/register/status/{rid2}")).json() == {"status": "rejected"}
    assert "registration.rejected" in [a["event"] for a in (await client.get("/api/users/audit")).json()]
    # a rejected username may be requested again
    async with anon() as c:
        assert (await c.post("/api/register", json=req(other))).status_code == 201


async def test_approve_conflict_when_user_created_meanwhile(client):
    name = uname()
    async with anon() as c:
        rid = (await c.post("/api/register", json=req(name))).json()["id"]
    await client.post("/api/users", json={"username": name, "password": "Blue-Lantern-42x", "role": "viewer"})
    assert (await client.post(f"/api/users/requests/{rid}/approve", json={"role": "viewer"})).status_code == 409
