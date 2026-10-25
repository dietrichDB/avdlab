"""v0.8.0 (stream A): users, roles, password policy, lockout, TOTP MFA, recovery codes, session invalidation."""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

import pyotp
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from avdlab.core import passwords
from avdlab.core.config import settings
from avdlab.core.db import Secret, SessionLocal, User
from avdlab.core.passwords import password_violations
from avdlab.core.security import role_denial

GOOD = "Blue-Lantern-42x"
GOOD2 = "Quiet-Harbor-77y"


def uname(prefix: str = "u") -> str:
    return f"{prefix}{uuid.uuid4().hex[:8]}"


@pytest_asyncio.fixture(autouse=True)
async def _drop_test_secrets():
    from avdlab.core.db import init_db

    await init_db()  # the pure-function tests do not use `client`, so make sure the schema exists
    yield  # secrets created by the role tests must not leak into other modules' listings
    async with SessionLocal() as s:
        await s.execute(delete(Secret).where(Secret.name.like("zz-auth-%")))
        await s.commit()


@asynccontextmanager
async def anon():
    from avdlab.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@asynccontextmanager
async def as_user(username: str, password: str):
    async with anon() as c:
        r = await c.post("/api/auth/login", json={"username": username, "password": password})
        assert r.status_code == 200, r.text
        yield c


async def mk_user(client, role: str = "viewer", password: str | None = GOOD) -> tuple[str, dict]:
    name = uname(role[:2])
    r = await client.post("/api/users", json={"username": name, "password": password, "role": role})
    assert r.status_code == 201, r.text
    return name, r.json()


# ---------------------------------------------------------------- policy (pure)
def test_password_policy():
    assert password_violations(GOOD, "alice") == []
    v = password_violations("short1A!", "")
    assert any("at least 12" in m for m in v)
    assert any("3 of" in m for m in password_violations("alllowercaseletters", ""))
    assert any("3 of" in m for m in password_violations("alllowercase123", ""))  # lower + digit only
    assert any("username" in m for m in password_violations("xAliceRocks-2024", "alice"))
    assert any("common" in m for m in password_violations("password1234", "", minimum=8))
    assert any("identical" in m for m in password_violations("Baaaaad-Pass-99", ""))
    assert password_violations("Baaad-Pass-991", "") == []  # 3 in a row is fine
    assert any("at least 8" in m for m in password_violations("Ab1!", "", minimum=4))  # floor 8


def test_password_policy_classes_and_env(monkeypatch):
    assert any("3 of" in m for m in password_violations("abcdefghijk12", ""))
    assert password_violations("abcdefghijK12", "") == []
    monkeypatch.setattr(passwords.auth_settings, "password_min_length", 20)
    assert any("at least 20" in m for m in password_violations(GOOD, ""))
    monkeypatch.setattr(passwords.auth_settings, "password_min_length", 4)
    assert passwords.min_length() == 8


def test_temp_password_and_codes():
    for _ in range(20):
        pw = passwords.generate_temp_password(20, "bob")
        assert len(pw) == 20 and password_violations(pw, "bob") == []
    codes = passwords.generate_recovery_codes()
    assert len(codes) == 10 and len(set(codes)) == 10
    hashes = [passwords.hash_recovery_code(c) for c in codes]
    assert passwords.match_recovery_code(hashes, codes[3].upper().replace("-", " ")) == 3
    assert passwords.match_recovery_code(hashes, "zzzzz-zzzzz") is None
    secret = passwords.new_totp_secret()
    assert passwords.verify_totp(secret, pyotp.TOTP(secret).now())
    assert not passwords.verify_totp(secret, "abc")
    assert passwords.qr_svg("otpauth://totp/x?secret=" + secret).lstrip().startswith("<")


def test_role_denial_matrix():
    assert role_denial("admin", "DELETE", "/api/users/x") is None
    assert role_denial("viewer", "GET", "/api/labs") is None
    assert role_denial("viewer", "POST", "/api/auth/password") is None
    assert role_denial("viewer", "POST", "/api/labs") == "role viewer may not POST /api/labs"
    assert role_denial("viewer", "GET", "/api/users") is not None
    assert role_denial("operator", "POST", "/api/labs") is None
    assert role_denial("operator", "GET", "/api/users") is not None
    assert role_denial("operator", "DELETE", "/api/hosts/abc") is not None
    assert role_denial("viewer", "WEBSOCKET", "/api/hosts/abc/terminal") is not None  # a shell is not read-only
    assert role_denial("viewer", "WEBSOCKET", "/api/jobs/abc/stream") is None
    assert role_denial("operator", "WEBSOCKET", "/api/labs/x/nodes/n/terminal") is None
    assert role_denial("operator", "DELETE", "/api/labs/abc") is None
    assert role_denial("viewer", "POST", "/") is None  # SPA / non-API untouched


# ---------------------------------------------------------------- bootstrap
async def test_bootstrap_env_admin(client, monkeypatch):
    r = await client.get("/api/auth/me")
    assert r.json() == {"user": "admin", "role": "admin", "must_change_password": True, "mfa_enabled": False,
                        "recovery_codes_left": 0}  # env password "test" fails the policy → forced change
    from avdlab.api.users import bootstrap_admin

    assert await bootstrap_admin() is None  # not empty → no-op
    async with SessionLocal() as s:  # fresh install with a strong env password
        await s.execute(delete(User))
        await s.commit()
    monkeypatch.setattr(settings, "admin_user", "Root")
    monkeypatch.setattr(settings, "admin_password", "Strong-Env-Pass-1")
    u = await bootstrap_admin()
    assert u is not None and u.username == "root" and u.role == "admin" and not u.must_change_password
    async with as_user("ROOT", "Strong-Env-Pass-1") as c:
        assert (await c.get("/api/auth/me")).json()["must_change_password"] is False
    async with SessionLocal() as s:  # back to the conftest admin (lazy bootstrap on next login)
        await s.execute(delete(User))
        await s.commit()
    monkeypatch.undo()
    async with as_user("admin", "test") as c:
        assert (await c.get("/api/auth/me")).json()["role"] == "admin"


# ---------------------------------------------------------------- login / lockout
async def test_login_generic_401_lockout_and_unlock(client):
    name, u = await mk_user(client, "operator")
    async with anon() as c:
        r1 = await c.post("/api/auth/login", json={"username": "nobody-here", "password": "x"})
        r2 = await c.post("/api/auth/login", json={"username": name, "password": "wrong"})
        assert r1.status_code == r2.status_code == 401 and r1.json() == r2.json()
        for _ in range(3):
            assert (await c.post("/api/auth/login", json={"username": name, "password": "wrong"})).status_code == 401
        r = await c.post("/api/auth/login", json={"username": name, "password": "wrong"})  # 5th failure
        assert r.status_code == 423 and r.json()["locked_until"]
        r = await c.post("/api/auth/login", json={"username": name, "password": GOOD})  # right password, still locked
        assert r.status_code == 423
    listed = {x["username"]: x for x in (await client.get("/api/users")).json()}
    assert listed[name]["locked"] is True
    assert (await client.post(f"/api/users/{u['id']}/unlock")).json()["locked"] is False
    async with as_user(name.upper(), GOOD) as c:  # usernames are case-insensitive
        me = (await c.get("/api/auth/me")).json()
        assert me["user"] == name and me["role"] == "operator"
    events = [a["event"] for a in (await client.get("/api/users/audit?limit=50")).json() if a["username"] == name]
    assert {"login.failed", "login.lockout", "login.locked", "user.unlocked", "login.ok"} <= set(events)


# ---------------------------------------------------------------- MFA
async def test_mfa_setup_enable_login_recovery_disable(client):
    name, _ = await mk_user(client, "operator")
    async with as_user(name, GOOD) as c:
        setup = (await c.post("/api/auth/mfa/setup")).json()
        assert setup["otpauth_url"].startswith("otpauth://totp/") and "<svg" in setup["qr_svg"]
        totp = pyotp.TOTP(setup["secret"])
        assert (await c.post("/api/auth/mfa/enable", json={"code": "000000" if totp.now() != "000000" else "111111"})).status_code == 400
        r = await c.post("/api/auth/mfa/enable", json={"code": totp.now()})
        assert r.status_code == 200, r.text
        codes = r.json()["recovery_codes"]
        assert len(codes) == 10
        assert (await c.get("/api/auth/me")).json()["mfa_enabled"] is True

    async with anon() as c:
        r = await c.post("/api/auth/login", json={"username": name, "password": GOOD})
        assert r.json() == {"mfa_required": True}
        assert (await c.get("/api/auth/me")).status_code == 401  # pending → nothing granted
        assert (await c.get("/api/labs")).status_code == 401
        assert (await c.post("/api/auth/mfa/verify", json={"code": "zzzzz-zzzzz"})).status_code == 400
        r = await c.post("/api/auth/mfa/verify", json={"code": codes[0]})
        assert r.status_code == 200 and r.json()["recovery_codes_left"] == 9
        assert (await c.get("/api/labs")).status_code == 200

    async with anon() as c:  # recovery code is single-use; TOTP works
        await c.post("/api/auth/login", json={"username": name, "password": GOOD})
        assert (await c.post("/api/auth/mfa/verify", json={"code": codes[0]})).status_code == 400
        assert (await c.post("/api/auth/mfa/verify", json={"code": totp.now()})).status_code == 200
        regen = await c.post("/api/auth/mfa/recovery-codes", json={"password": GOOD})
        assert len(regen.json()["recovery_codes"]) == 10
        assert (await c.post("/api/auth/mfa/disable", json={"password": GOOD, "code": "123"})).status_code == 400
        assert (await c.post("/api/auth/mfa/disable", json={"password": GOOD, "code": totp.now()})).json() == {"mfa_enabled": False}

    async with as_user(name, GOOD) as c:  # no MFA any more
        assert (await c.get("/api/auth/me")).json()["mfa_enabled"] is False


async def test_mfa_too_many_wrong_codes_and_admin_reset(client):
    name, u = await mk_user(client, "viewer")
    async with as_user(name, GOOD) as c:
        secret = (await c.post("/api/auth/mfa/setup")).json()["secret"]
        await c.post("/api/auth/mfa/enable", json={"code": pyotp.TOTP(secret).now()})
    async with anon() as c:
        await c.post("/api/auth/login", json={"username": name, "password": GOOD})
        for _ in range(4):
            assert (await c.post("/api/auth/mfa/verify", json={"code": "aaaaa-aaaaa"})).status_code == 400
        assert (await c.post("/api/auth/mfa/verify", json={"code": "aaaaa-aaaaa"})).status_code == 401
        # back to login: even a valid code no longer helps
        assert (await c.post("/api/auth/mfa/verify", json={"code": pyotp.TOTP(secret).now()})).status_code == 401
    assert (await client.post(f"/api/users/{u['id']}/mfa/reset")).json()["mfa_enabled"] is False
    async with as_user(name, GOOD) as c:
        assert (await c.get("/api/auth/me")).json()["mfa_enabled"] is False


# ---------------------------------------------------------------- roles
async def test_role_guard(client):
    viewer, _ = await mk_user(client, "viewer")
    operator, _ = await mk_user(client, "operator")
    async with as_user(viewer, GOOD) as c:
        assert (await c.get("/api/labs")).status_code == 200
        r = await c.post("/api/secrets", json={"name": "x", "kind": "generic", "value": "y"})
        assert r.status_code == 403 and r.json() == {"detail": "role viewer may not POST /api/secrets"}
        assert (await c.get("/api/users")).status_code == 403
        assert (await c.post("/api/auth/logout")).status_code == 200
    async with as_user(operator, GOOD) as c:
        assert (await c.get("/api/users")).status_code == 403
        assert (await c.delete("/api/hosts/does-not-exist")).status_code == 403
        assert (await c.post("/api/secrets", json={"name": uname("zz-auth-"), "kind": "generic", "value": "y"})).status_code == 201
    assert (await client.get("/api/users")).status_code == 200
    assert (await client.delete("/api/hosts/does-not-exist")).status_code == 404
    async with anon() as c:  # unauthenticated → guard passes through, router answers 401
        assert (await c.post("/api/secrets", json={"name": "x", "kind": "generic", "value": "y"})).status_code == 401


async def test_role_change_and_disable_apply_immediately(client):
    name, u = await mk_user(client, "operator")
    async with as_user(name, GOOD) as c:
        assert (await c.post("/api/secrets", json={"name": uname("zz-auth-"), "kind": "generic", "value": "y"})).status_code == 201
        await client.patch(f"/api/users/{u['id']}", json={"role": "viewer"})
        assert (await c.post("/api/secrets", json={"name": uname("zz-auth-"), "kind": "generic", "value": "y"})).status_code == 403
        await client.patch(f"/api/users/{u['id']}", json={"disabled": True})
        assert (await c.get("/api/labs")).status_code == 401
    async with anon() as c:
        assert (await c.post("/api/auth/login", json={"username": name, "password": GOOD})).status_code == 401


# ---------------------------------------------------------------- users CRUD
async def test_users_crud_and_last_admin(client):
    name, u = await mk_user(client, "viewer", password=None)
    temp = u["temporary_password"]
    assert len(temp) == 20 and u["must_change_password"] is True
    async with as_user(name, temp) as c:
        assert (await c.get("/api/auth/me")).json()["must_change_password"] is True
        r = await c.post("/api/auth/password", json={"current_password": temp, "new_password": "short"})
        assert r.status_code == 422 and r.json()["detail"]["violations"]
        r = await c.post("/api/auth/password", json={"current_password": temp, "new_password": GOOD2})
        assert r.status_code == 200 and r.json()["must_change_password"] is False
    assert (await client.post("/api/users", json={"username": name, "role": "viewer"})).status_code == 409
    assert (await client.post("/api/users", json={"username": uname(), "password": "weak", "role": "viewer"})).status_code == 422

    r = await client.patch(f"/api/users/{u['id']}", json={"role": "operator"})
    assert r.json()["role"] == "operator"
    reset = (await client.post(f"/api/users/{u['id']}/reset-password")).json()
    assert reset["must_change_password"] is True and reset["temporary_password"] != temp
    async with as_user(name, reset["temporary_password"]):
        pass

    me_id = next(x["id"] for x in (await client.get("/api/users")).json() if x["username"] == "admin")
    assert (await client.delete(f"/api/users/{me_id}")).status_code == 409  # not self
    assert (await client.patch(f"/api/users/{me_id}", json={"role": "viewer"})).status_code == 409  # not self/last admin
    assert (await client.patch(f"/api/users/{me_id}", json={"disabled": True})).status_code == 409

    # a second admin may not remove the only other admin's role if that leaves no admin — and can delete others
    boss, b = await mk_user(client, "admin")
    async with as_user(boss, GOOD) as c:
        assert (await c.delete(f"/api/users/{u['id']}")).json() == {"ok": True}
        assert (await c.patch(f"/api/users/{b['id']}", json={"role": "viewer"})).status_code == 409
    async with SessionLocal() as s:  # disable the bootstrap admin → boss is the last enabled admin
        adm = (await s.execute(select(User).where(User.username == "admin"))).scalar_one()
        adm.disabled = True
        await s.commit()
    try:
        async with as_user(boss, GOOD) as c:
            assert (await c.delete(f"/api/users/{me_id}")).json() == {"ok": True}  # a disabled admin can go
    finally:
        async with SessionLocal() as s:  # restore the conftest admin for later tests
            await s.execute(delete(User))
            await s.commit()
    assert (await client.get("/api/users")).status_code == 401  # our admin session points at a deleted user


async def test_users_admin_only(client):
    viewer, _ = await mk_user(client, "viewer")
    async with as_user(viewer, GOOD) as c:
        for method, url in (("get", "/api/users"), ("get", "/api/users/audit"), ("post", "/api/users/x/unlock")):
            assert (await getattr(c, method)(url)).status_code == 403


# ---------------------------------------------------------------- sessions
async def test_password_change_invalidates_other_sessions(client):
    name, _ = await mk_user(client, "operator")
    async with as_user(name, GOOD) as a, as_user(name, GOOD) as b:
        assert (await b.get("/api/auth/me")).status_code == 200
        r = await a.post("/api/auth/password", json={"current_password": "nope", "new_password": GOOD2})
        assert r.status_code == 400
        r = await a.post("/api/auth/password", json={"current_password": GOOD, "new_password": GOOD2})
        assert r.status_code == 200, r.text
        assert (await a.get("/api/auth/me")).status_code == 200  # this session stays valid
        assert (await b.get("/api/auth/me")).status_code == 401
        assert (await b.get("/api/labs")).status_code == 401
    async with anon() as c:
        assert (await c.post("/api/auth/login", json={"username": name, "password": GOOD})).status_code == 401
    async with as_user(name, GOOD2):
        pass


async def test_policy_endpoint_public():
    async with anon() as c:
        assert (await c.get("/api/auth/policy")).json()["min_length"] >= 8


async def test_stale_session_websocket_closed(client):
    import asyncio

    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    from avdlab.main import app

    name, u = await mk_user(client, "viewer")

    def connect(tc: TestClient) -> int:
        try:
            with tc.websocket_connect("/api/jobs/nope/stream") as ws:
                ws.receive_json()
                return 0
        except WebSocketDisconnect as exc:
            return exc.code

    tc = TestClient(app)
    assert (await asyncio.to_thread(tc.post, "/api/auth/login", json={"username": name, "password": GOOD})).status_code == 200
    assert await asyncio.to_thread(connect, tc) == 0  # valid session → endpoint accepts ({"done": true})
    await client.post(f"/api/users/{u['id']}/reset-password")  # bumps session_version
    assert await asyncio.to_thread(connect, tc) == 4401
