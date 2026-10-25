"""FastAPI application factory."""
from __future__ import annotations

import importlib
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from .core.config import settings
from .core.db import init_db
from .core.security import RoleGuard

# routers that always exist
from .api import auth, jobs, secrets

# feature routers — each module registers its own job types on import
FEATURE_MODULES = ["avdlab.hosts.api", "avdlab.labs.api", "avdlab.images.api", "avdlab.topology.api",
                   "avdlab.cloudvision.api",
                   # phase 3 (backend B): git/import/export/VS Code links, host monitoring
                   "avdlab.labs.integrations_api", "avdlab.hosts.monitor"]
FEATURE_MODULES.append("avdlab.labs.ztp_api")  # phase 4 (backend A): GET /labs/{id}/ztp
FEATURE_MODULES.append("avdlab.netbox.api")  # phase 4 (backend B): NetBox targets + lab.netbox_sync
FEATURE_MODULES.append("avdlab.labs.mss_api")  # phase 4 (backend C): MSS foundation (GET /labs/{id}/mss, lab.mss_verify)
FEATURE_MODULES.append("avdlab.labs.act_api")  # phase 4 (backend D): ACT topology + hints (GET /labs/{id}/act)
FEATURE_MODULES += ["avdlab.meta.api", "avdlab.feedback.api"]  # phase 5: version/changelog/notices, feedback
FEATURE_MODULES.append("avdlab.executors.client_routes")
FEATURE_MODULES.append("avdlab.executors.push")
FEATURE_MODULES.append("avdlab.executors.psirt")  # 0.10.0: ANTA security advisories (anta psirt) demo  # 0.10.0: incremental config push (GET /labs/{id}/push/plan, POST /labs/{id}/push)  # phase 6 (backend C): hot-added test hosts
FEATURE_MODULES.append("avdlab.learn.api")  # phase 6 (backend B): schema browser, explainer, lessons, glossary, tours
FEATURE_MODULES.append("avdlab.api.users")  # v0.8.0 (stream A): multi-user admin, roles, audit log
FEATURE_MODULES.append("avdlab.api.registration")  # v0.9.0 (stream A): account requests, admin settings
FEATURE_MODULES.append("avdlab.ai.api")  # v0.10.0: AI lab designer (bring your own model)

# v0.8.0 (B): sent on every HTTP response (HSTS is left to the TLS proxy, see deploy/Caddyfile)
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    from .api import users as users_service  # v0.8.0 (stream A): first admin from AVDLAB_ADMIN_USER/PASSWORD

    await users_service.bootstrap_admin()
    from .hosts import monitor  # phase 3 (backend B): host metrics sampler (no-op when settings.monitor_enabled=false)

    await monitor.start_monitor()
    from .meta import update  # phase 6 (A): close a self-update started by the previous process + daily check

    try:
        await update.finalize_previous_update()
    except Exception as exc:  # noqa: BLE001 - bookkeeping must never block startup
        print(f"update bookkeeping failed: {exc}")
    update.start_checker()
    try:
        yield
    finally:
        await update.stop_checker()
        await monitor.stop_monitor()


def create_app() -> FastAPI:
    from .meta.api import version as app_version  # phase 5: VERSION file at the repo root

    app = FastAPI(title="AVD Lab Orchestrator", version=app_version(), lifespan=lifespan)
    app.add_middleware(RoleGuard)  # v0.8.0 (A): role permissions; added first = innermost, so it sees scope["session"]
    app.add_middleware(SessionMiddleware, secret_key=settings.session_secret, max_age=settings.session_max_age, same_site="lax", https_only=settings.session_https_only)  # v0.8.0 (B): Secure behind https; always HttpOnly
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

    @app.middleware("http")
    async def security_headers(request: Request, call_next):  # v0.8.0 (B): defence in depth (Caddy sets them too)
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response

    if settings.trust_proxy_headers:  # v0.8.0 (B): added last = outermost, so every layer sees the client scheme/IP
        app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")

    app.include_router(auth.router, prefix="/api")
    app.include_router(jobs.router, prefix="/api")
    app.include_router(secrets.router, prefix="/api")
    for mod in FEATURE_MODULES:
        try:
            m = importlib.import_module(mod)
        except ModuleNotFoundError as exc:  # feature not built yet
            if exc.name and exc.name.startswith("avdlab"):
                continue
            raise
        app.include_router(m.router, prefix="/api")

    # serve built SPA if present (single container deployment)
    static = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    if static.exists():
        index = static / "index.html"

        class SPAStaticFiles(StaticFiles):
            """0.10.0: a page refresh on /labs/<id>/topology used to answer {"detail": "Not Found"} — any path that is
            not a real file (and not /api) gets index.html so the client-side router can take over."""

            async def get_response(self, path: str, scope):  # type: ignore[override]
                try:
                    response = await super().get_response(path, scope)
                except StarletteHTTPException as exc:
                    if exc.status_code != 404 or path.startswith("api/"):
                        raise
                    return await super().get_response("index.html", scope)
                if response.status_code == 404 and not path.startswith("api/") and index.exists():
                    return await super().get_response("index.html", scope)
                return response

        app.mount("/", SPAStaticFiles(directory=static, html=True), name="spa")
    return app


app = create_app()
