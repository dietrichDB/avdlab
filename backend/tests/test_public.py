"""v0.8.0 (stream B): public deployment — Secure session cookie, security headers, CORS, proxy headers."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Request
from httpx import ASGITransport, AsyncClient

from avdlab.core.config import Settings, settings
from avdlab.core.db import init_db
from avdlab.main import create_app  # import now: the module-level `app` (conftest `client`) must use unpatched settings


@asynccontextmanager
async def fresh_client(base_url: str = "http://test"):
    await init_db()
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url=base_url) as c:
        yield c


async def login(c: AsyncClient):
    r = await c.post("/api/auth/login", json={"username": "admin", "password": "test"})
    assert r.status_code == 200, r.text
    return r


def session_cookie(r) -> str:
    cookies = [v for v in r.headers.get_list("set-cookie") if v.startswith("session=")]
    assert cookies, r.headers
    return cookies[0].lower()


async def test_secure_cookie_with_https_public_url(monkeypatch):
    monkeypatch.setattr(settings, "public_url", "https://x")
    monkeypatch.setattr(settings, "secure_cookies", None)
    async with fresh_client() as c:
        cookie = session_cookie(await login(c))
    assert "; secure" in cookie
    assert "httponly" in cookie and "samesite=lax" in cookie


async def test_no_secure_cookie_by_default(monkeypatch):
    monkeypatch.setattr(settings, "public_url", "")
    monkeypatch.setattr(settings, "secure_cookies", None)
    async with fresh_client() as c:
        cookie = session_cookie(await login(c))
    assert "secure" not in cookie
    assert "httponly" in cookie and "samesite=lax" in cookie


async def test_secure_cookies_setting_wins(monkeypatch):
    monkeypatch.setattr(settings, "public_url", "http://lan-box:8080")
    monkeypatch.setattr(settings, "secure_cookies", True)
    async with fresh_client() as c:
        assert "; secure" in session_cookie(await login(c))
    monkeypatch.setattr(settings, "public_url", "https://x")
    monkeypatch.setattr(settings, "secure_cookies", False)
    async with fresh_client() as c:
        assert "secure" not in session_cookie(await login(c))


async def test_security_headers_present(client):
    for path in ("/api/auth/me", "/api/does-not-exist"):
        r = await client.get(path)
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["x-frame-options"] == "DENY"
        assert r.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    r = await client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
    assert r.headers["x-frame-options"] == "DENY"  # error responses too


def test_public_url_appended_to_cors_origins():
    s = Settings(public_url="https://avdlab.example.com/", cors_origins=["http://localhost:5173"])
    assert s.cors_origins == ["http://localhost:5173", "https://avdlab.example.com"]
    s = Settings(public_url="https://avdlab.example.com", cors_origins=["https://avdlab.example.com"])
    assert s.cors_origins == ["https://avdlab.example.com"]  # no duplicate
    assert Settings(public_url="").cors_origins == Settings().cors_origins
    assert Settings(public_url="https://x").session_https_only is True
    assert Settings(public_url="http://x").session_https_only is False


async def test_cors_allows_public_url(monkeypatch):
    fresh = Settings(public_url="https://x")
    monkeypatch.setattr(settings, "public_url", fresh.public_url)
    monkeypatch.setattr(settings, "cors_origins", fresh.cors_origins)
    async with fresh_client() as c:
        r = await c.options("/api/auth/me", headers={"Origin": "https://x", "Access-Control-Request-Method": "GET"})
        assert r.status_code == 200
        assert r.headers["access-control-allow-origin"] == "https://x"
        assert r.headers["access-control-allow-credentials"] == "true"
        r = await c.options("/api/auth/me", headers={"Origin": "https://evil", "Access-Control-Request-Method": "GET"})
        assert "access-control-allow-origin" not in r.headers


async def test_proxy_headers_only_when_trusted(monkeypatch):
    seen: dict[str, str] = {}

    def probe_app():
        app = create_app()

        @app.get("/__probe")
        async def probe(request: Request):
            seen["scheme"] = request.url.scheme
            seen["client"] = request.client.host if request.client else ""
            return {}

        # the SPA static mount (if built) is registered first and would shadow the probe route
        app.router.routes.insert(0, app.router.routes.pop())
        return app

    hdrs = {"X-Forwarded-Proto": "https", "X-Forwarded-For": "203.0.113.7"}
    for trusted, scheme, client_ip in ((False, "http", "127.0.0.1"), (True, "https", "203.0.113.7")):
        monkeypatch.setattr(settings, "trust_proxy_headers", trusted)
        transport = ASGITransport(app=probe_app(), client=("127.0.0.1", 50000))
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            assert (await c.get("/__probe", headers=hdrs)).status_code == 200
        assert (seen["scheme"], seen["client"]) == (scheme, client_ip)


async def test_spa_deep_link_fallback(tmp_path, monkeypatch):
    """0.10.0: a refresh on /labs/x/topology serves index.html; /api/* 404s stay JSON; real files are served as-is."""
    import avdlab.main as main_mod
    from httpx import ASGITransport, AsyncClient

    dist = tmp_path / "frontend" / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>spa</html>")
    (dist / "assets" / "app.js").write_text("console.log(1)")
    monkeypatch.setattr(main_mod, "Path", lambda *_a, **_k: _FakeFile(tmp_path, None))
    app = main_mod.create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/labs/abc/topology")).text == "<html>spa</html>"
        assert (await c.get("/admin/settings")).text == "<html>spa</html>"
        assert (await c.get("/assets/app.js")).text == "console.log(1)"
        r = await c.get("/api/labs/nope")
        assert r.status_code in (401, 404) and r.headers["content-type"].startswith("application/json")


class _FakeFile:
    """Stand-in for Path(__file__) whose .resolve().parents[2] is the temp root holding frontend/dist."""

    def __init__(self, root, _real):
        self.root = root

    def resolve(self):
        return self

    @property
    def parents(self):
        return [self.root, self.root, self.root]
