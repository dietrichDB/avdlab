"""v0.9.0 (stream A): SMTP mailer (fake aiosmtplib.SMTP), admin settings, feedback → e-mail."""
from __future__ import annotations

import asyncio
import logging
import time

import aiosmtplib
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from avdlab.core import app_settings, mailer
from avdlab.core.config import settings
from avdlab.core.db import AppSetting, Secret, SessionLocal

PASSWORD = "S3cret-smtp-Pa55"


class FakeSMTP:
    """Stands in for aiosmtplib.SMTP; class-level `sent`/`instances` collect what happened."""

    sent: list = []
    instances: list = []
    fail_login: bool = False
    delay: float = 0.0

    def __init__(self, **kw):
        self.kw = kw
        self.logged_in = None
        FakeSMTP.instances.append(self)

    async def connect(self):
        if self.delay:
            await asyncio.sleep(self.delay)

    async def login(self, user, password):
        if FakeSMTP.fail_login:
            raise aiosmtplib.SMTPAuthenticationError(535, f"authentication failed for {user}:{password}")
        self.logged_in = (user, password)

    async def send_message(self, msg):
        FakeSMTP.sent.append(msg)

    async def quit(self):
        pass


@pytest_asyncio.fixture(autouse=True)
async def fake_smtp(monkeypatch):
    from avdlab.core.db import init_db

    await init_db()
    FakeSMTP.sent, FakeSMTP.instances, FakeSMTP.fail_login, FakeSMTP.delay = [], [], False, 0.0
    monkeypatch.setattr(aiosmtplib, "SMTP", FakeSMTP)
    yield FakeSMTP
    await mailer.drain()
    async with SessionLocal() as s:
        await s.execute(delete(AppSetting))
        await s.execute(delete(Secret).where(Secret.name == app_settings.SMTP_PASSWORD_SECRET))
        await s.commit()


async def configure(**kw):
    async with SessionLocal() as s:
        await app_settings.update(s, {"smtp_host": "smtp.example.com", **kw})


# ---------------------------------------------------------------- mailer
async def test_send_mail_noop_without_host():
    assert await mailer.send_mail("x", "y") is False
    ok, err = await mailer.send_mail_result("x", "y")
    assert ok is False and "not configured" in err and FakeSMTP.instances == []


async def test_send_mail_starttls_login_and_message():
    await configure(smtp_user="mailer", smtp_password=PASSWORD, smtp_from="lab@example.com",
                    feedback_mail_to="a@example.com, b@example.com")
    assert await mailer.send_mail("Hello", "body text") is True
    smtp = FakeSMTP.instances[-1]
    assert smtp.kw["hostname"] == "smtp.example.com" and smtp.kw["port"] == 587
    assert smtp.kw["start_tls"] is True and smtp.kw["use_tls"] is False and smtp.kw["timeout"] == 20.0
    assert smtp.logged_in == ("mailer", PASSWORD)
    msg = FakeSMTP.sent[-1]
    assert msg["Subject"] == "Hello" and msg["From"] == "lab@example.com" and msg["To"] == "a@example.com, b@example.com"
    assert "body text" in msg.get_content()
    assert await mailer.send_mail("Hi", "x", to="c@example.com", html="<b>x</b>")
    assert FakeSMTP.sent[-1]["To"] == "c@example.com" and FakeSMTP.sent[-1].is_multipart()


async def test_implicit_tls_on_465_and_no_login_without_user():
    await configure(smtp_port=465)
    assert await mailer.send_mail("x", "y")
    smtp = FakeSMTP.instances[-1]
    assert smtp.kw["use_tls"] is True and smtp.kw["start_tls"] is False and smtp.logged_in is None


async def test_failure_never_raises_and_hides_password(caplog):
    await configure(smtp_user="mailer", smtp_password=PASSWORD)
    FakeSMTP.fail_login = True
    with caplog.at_level(logging.DEBUG):
        ok, err = await mailer.send_mail_result("x", "y")
    assert ok is False and "authentication failed" in err and PASSWORD not in err
    assert PASSWORD not in caplog.text and "****" in caplog.text


async def test_default_from_uses_public_url(monkeypatch):
    monkeypatch.setattr(settings, "public_url", "https://avdlab.example.org")
    assert (await app_settings.load()).smtp_from == "avdlab@avdlab.example.org"
    monkeypatch.setattr(settings, "public_url", "")
    assert (await app_settings.load()).smtp_from == "avdlab@localhost"


# ---------------------------------------------------------------- admin settings API
async def test_admin_settings_roundtrip(client, monkeypatch):
    monkeypatch.setattr(settings, "github_url", "https://github.com/env/default")
    r = await client.get("/api/admin/settings")
    assert r.status_code == 200, r.text
    s = r.json()
    assert s["github_url"] == "https://github.com/env/default" and s["registration_enabled"] is True
    assert s["feedback_mail_to"] == "dietrich@arista.com" and s["registration_mail_to"] == "dietrich@arista.com"
    assert s["smtp_port"] == 587 and s["has_smtp_password"] is False and "smtp_password" not in s
    r = await client.put("/api/admin/settings", json={"github_url": "https://github.com/acme/avdlab", "app_title": "Lab",
                                                      "smtp_host": "smtp.example.com", "smtp_password": PASSWORD})
    assert r.status_code == 200, r.text
    s = r.json()
    assert s["github_url"] == "https://github.com/acme/avdlab" and s["app_title"] == "Lab"
    assert s["has_smtp_password"] is True and PASSWORD not in r.text and "github_url" in s["overridden"]
    assert await app_settings.smtp_password() == PASSWORD
    secrets = (await client.get("/api/secrets")).json()
    assert any(x["name"] == "smtp_password" for x in secrets) and PASSWORD not in str(secrets)
    meta = (await client.get("/api/meta")).json()
    assert meta["github_url"] == "https://github.com/acme/avdlab" and meta["app_title"] == "Lab"
    assert meta["intro_video_url"] == "/intro/avdlab-intro.mp4" and meta["support_email"] is None
    # validation
    for bad in ({"smtp_port": 70000}, {"github_url": "ftp://x"}, {"support_email": "nope"}, {"bogus": 1},
                {"registration_enabled": "yes"}, {"feedback_mail_to": "a@b.c, nope"}):
        assert (await client.put("/api/admin/settings", json=bad)).status_code == 422, bad
    # null resets to the env default, "" clears the password
    s = (await client.put("/api/admin/settings", json={"github_url": None, "smtp_password": ""})).json()
    assert s["github_url"] == "https://github.com/env/default" and s["has_smtp_password"] is False
    audit = (await client.get("/api/users/audit")).json()
    assert any(a["event"] == "settings.updated" for a in audit)


async def test_admin_settings_test_mail(client):
    r = await client.post("/api/admin/settings/test-mail")
    assert r.json()["ok"] is False and "not configured" in r.json()["error"]
    await client.put("/api/admin/settings", json={"smtp_host": "smtp.example.com", "feedback_mail_to": "ops@example.com"})
    r = (await client.post("/api/admin/settings/test-mail")).json()
    assert r == {"ok": True, "error": None, "to": "ops@example.com"}
    assert FakeSMTP.sent[-1]["Subject"] == "[avdlab] test mail" and FakeSMTP.sent[-1]["To"] == "ops@example.com"


async def test_admin_settings_admin_only(client):
    name = f"op{int(time.time() * 1000) % 10**8}"
    r = await client.post("/api/users", json={"username": name, "password": "Blue-Lantern-42x", "role": "operator"})
    assert r.status_code == 201
    from avdlab.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        assert (await c.get("/api/admin/settings")).status_code == 401
        await c.post("/api/auth/login", json={"username": name, "password": "Blue-Lantern-42x"})
        assert (await c.get("/api/admin/settings")).status_code == 403
        assert (await c.put("/api/admin/settings", json={"app_title": "x"})).status_code == 403
        assert (await c.post("/api/admin/settings/test-mail")).status_code == 403


def test_role_denial_admin_and_register():
    from avdlab.core.security import role_denial

    assert role_denial("operator", "GET", "/api/admin/settings") is not None
    assert role_denial("viewer", "PUT", "/api/admin/settings") is not None
    assert role_denial("admin", "PUT", "/api/admin/settings") is None
    assert role_denial("viewer", "POST", "/api/register") is None
    assert role_denial("operator", "POST", "/api/register") is None


# ---------------------------------------------------------------- feedback → e-mail
async def test_feedback_mail_sent(client):
    await configure(feedback_mail_to="fb@example.com")
    r = await client.post("/api/feedback", json={"kind": "bug", "title": "Build fails", "body": "stack trace here",
                                                 "page": "/labs/1"})
    assert r.status_code == 201, r.text
    await mailer.drain()
    msg = FakeSMTP.sent[-1]
    assert msg["Subject"] == "[avdlab] bug: Build fails" and msg["To"] == "fb@example.com"
    body = msg.get_content()
    for part in ("Build fails", "/labs/1", "User:    admin", "stack trace here", "avdlab version:", "failed jobs:",
                 "http://test/feedback"):
        assert part in body, part


async def test_feedback_mail_disabled(client):
    await configure(feedback_mail_enabled=False)
    assert (await client.post("/api/feedback", json={"kind": "question", "title": "q"})).status_code == 201
    await mailer.drain()
    assert FakeSMTP.sent == []


async def test_feedback_does_not_wait_for_or_fail_on_mail(client, monkeypatch):
    await configure()
    FakeSMTP.delay = 2.0
    t0 = time.monotonic()
    assert (await client.post("/api/feedback", json={"kind": "question", "title": "slow smtp"})).status_code == 201
    assert time.monotonic() - t0 < 1.5
    await mailer.drain()
    assert FakeSMTP.sent and FakeSMTP.sent[-1]["Subject"] == "[avdlab] question: slow smtp"

    async def boom(*a, **kw):
        raise RuntimeError("mail server exploded")

    monkeypatch.setattr(mailer, "send_mail", boom)
    assert (await client.post("/api/feedback", json={"kind": "bug", "title": "still works"})).status_code == 201
    await mailer.drain()

    def broken_load(*a, **kw):
        raise RuntimeError("settings broken")

    monkeypatch.setattr(app_settings, "load", broken_load)
    assert (await client.post("/api/feedback", json={"kind": "bug", "title": "still works 2"})).status_code == 201


@pytest.mark.parametrize("port,tls,starttls", [(587, False, True), (25, False, True), (465, True, False)])
async def test_tls_mode_by_port(port, tls, starttls):
    await configure(smtp_port=port)
    assert await mailer.send_mail("x", "y")
    kw = FakeSMTP.instances[-1].kw
    assert (kw["use_tls"], kw["start_tls"]) == (tls, starttls)
