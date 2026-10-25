# CONTRACT-7 — v0.8.0: multi-user auth with MFA, public TLS deployment, CloudVision robustness

Three work streams in one repo checkout (`/home/claude/avdlab`), run in parallel. **Nobody commits**; the
integrator commits. Each stream only touches the files listed under *Owns*; shared files are listed with the
exact lines each stream may change.

Repo conventions (already in place — follow them): FastAPI + SQLAlchemy async (`core/db.py`, light
migrations via `_ADDED_COLUMNS` / `init_db`), settings `AVDLAB_*` in `core/config.py`, Fernet secret store
(`core/security.py`), tests in `backend/tests` (pytest-asyncio, `client` fixture logs in as the admin — see
`tests/conftest.py`), frontend React 18 / TS / Tailwind / TanStack Query, MSW mocks in `frontend/src/mocks/`
(`handlers*.ts`, `data.ts`), vitest in `frontend/src/test`. Lint: `ruff check`, `npx tsc -p tsconfig.app.json
--noEmit`. Every stream must leave `pytest -q` and `vitest run` green for its own tests and must not break the
existing ones (currently 231 backend / 121 frontend).

---

## Stream A — multi-user accounts, roles, password policy, TOTP MFA

### Owns
- `backend/avdlab/core/security.py` (auth part only; keep `SecretStore` untouched), `backend/avdlab/api/auth.py`,
  new `backend/avdlab/api/users.py`, new `backend/avdlab/core/passwords.py`, `backend/avdlab/core/db.py`
  (**add** the `User` model, `AuditLog` model, `_ADDED_COLUMNS` entries; do not change other models),
  `backend/pyproject.toml` (add deps only), `backend/tests/test_auth.py` (new), `backend/tests/conftest.py`
  (only if the login fixture must change — keep the `client` fixture name/behaviour).
- `frontend/src/api/auth.ts`, `frontend/src/hooks/useAuth.tsx`, `frontend/src/pages/Login.tsx`, new
  `frontend/src/pages/Account.tsx`, new `frontend/src/pages/Users.tsx`, `frontend/src/components/Layout.tsx`
  (user menu + Users link only), `frontend/src/App.tsx` (routes only), `frontend/src/api/types.ts` (**append**
  types only), `frontend/src/mocks/handlers.ts` (auth/users handlers only) and `frontend/src/mocks/data.ts`
  (append user fixtures), new `frontend/src/test/auth.test.tsx`.
- `backend/avdlab/main.py`: you may **add** the users router to `FEATURE_MODULES` and add one startup call
  `await users_service.bootstrap_admin()` in `lifespan`. Do **not** touch the `SessionMiddleware` line (stream B).

### Requirements
1. **Users table** `users`: id, username (unique, lowercase), password_hash (argon2id via `argon2-cffi`),
   role ∈ {`admin`, `operator`, `viewer`}, `disabled` bool, `must_change_password` bool, `totp_secret`
   (Fernet-encrypted with the secret store), `mfa_enabled` bool, `recovery_codes` (JSON list of argon2 hashes),
   `failed_logins` int, `locked_until` datetime|null, `password_changed_at`, `last_login_at`, `created_at`.
2. **Bootstrap**: on startup, if `users` is empty create the admin from `AVDLAB_ADMIN_USER` /
   `AVDLAB_ADMIN_PASSWORD` with role admin; if the password equals the default `change-me` or fails the policy,
   set `must_change_password=true` (log a warning, don't refuse to start). Existing single-admin installs
   must keep working after upgrade with the same credentials.
3. **Password policy** (`core/passwords.py`, pure functions, unit-tested): min 12 chars, at least 3 of 4
   classes (lower/upper/digit/symbol), not containing the username, not in a small built-in deny-list
   (top ~200 common passwords — embed a short list), no more than 3 identical consecutive chars. Return a list
   of human-readable violations. Enforced on create/change/reset. `AVDLAB_PASSWORD_MIN_LENGTH` overrides the
   minimum (never below 8).
4. **Login flow** (`/api/auth`):
   - `POST /auth/login {username, password}` → if bad: generic 401 (same message for unknown user / wrong
     password), increment `failed_logins`; after `AVDLAB_LOGIN_MAX_FAILURES` (default 5) lock for
     `AVDLAB_LOGIN_LOCKOUT_MINUTES` (default 15) → 423 with `locked_until`. If MFA enabled → 200
     `{mfa_required: true}` and a *pending* session marker (`session["mfa_pending_user_id"]`), nothing else
     granted. Else → full session `{user, role, must_change_password}`.
   - `POST /auth/mfa/verify {code}` — TOTP (pyotp, 30 s, ±1 step) **or** a recovery code (single use) →
     promotes the pending session; 5 wrong codes → back to login.
   - `POST /auth/logout`, `GET /auth/me` → `{user, role, must_change_password, mfa_enabled}`; keep the
     response field `user` (string username) because the whole frontend reads `me.user`.
   - `POST /auth/password {current_password, new_password}` — self-service; clears `must_change_password`;
     invalidates other sessions by bumping a per-user `session_version` stored in the session and checked in
     `current_user`.
   - `POST /auth/mfa/setup` → `{secret, otpauth_url, qr_svg}` (qrcode[svg] lib, SVG string; store the secret
     *pending* in the session until confirmed), `POST /auth/mfa/enable {code}` → enables + returns 10 recovery
     codes (plaintext, shown once), `POST /auth/mfa/disable {password, code}`,
     `POST /auth/mfa/recovery-codes {password}` → regenerate.
   - Login/lockout/MFA events go into `audit_log` (user, event, ip, user agent, at). `GET /users/audit?limit=`
     (admin).
5. **Roles**: `current_user(request) -> str` must keep returning the username (dozens of routers depend on it)
   and must raise 401 for a *pending* MFA session or a disabled/deleted user or a stale `session_version`.
   Add `require_role(*roles)` dependency and a **middleware** `RoleGuard` (register in `security.py`, mounted
   by `main.py` via a one-line `app.add_middleware(RoleGuard)` that you may add just after the CORS line):
   `viewer` → only GET/HEAD/OPTIONS and WebSocket upgrades, plus `POST /api/auth/*`; `operator` → everything
   except `/api/users*` and `DELETE /api/hosts/*`; `admin` → everything. 403 JSON `{detail: "role viewer may
   not ..."}`. Unauthenticated requests pass through the guard untouched (the routers' `current_user` 401s).
6. **Users admin** (`/api/users`, admin only): list, create `{username, password?, role, send_temp?}` (when no
   password is given generate a 20-char temporary one, return it once, set `must_change_password`), patch
   `{role, disabled}`, delete (not self, not the last admin), `POST /users/{id}/reset-password` → new
   temporary password returned once + `must_change_password`, `POST /users/{id}/mfa/reset` (admin clears MFA
   for a user who lost their phone), `POST /users/{id}/unlock`.
7. **Frontend**:
   - Login page: step 1 username/password; step 2 (when `mfa_required`) 6-digit code with "use a recovery code"
     toggle; lockout message with the time; when `must_change_password` → forced change-password screen before
     anything else (reuse Account's form).
   - `/account` page: change password (with live policy checklist), MFA section: *Set up* → shows the QR SVG +
     secret + code field → on enable shows the recovery codes once with a copy/download button; *Disable*;
     *Regenerate recovery codes*.
   - `/users` page (admin only, link in the Layout nav visible only to admins): table (username, role, MFA,
     disabled, locked, last login), create dialog (shows the temporary password once), role select, disable
     toggle, reset password, reset MFA, unlock, delete; audit log table below (last 100).
   - Layout: user menu top-right with username, role badge, *Account*, *Sign out*. Hide mutating buttons is
     **not** required for viewers (the backend 403s); but `ErrorBox` must render a 403 nicely (it already shows
     `detail`).
   - `useAuth` exposes `{user, role, mfaEnabled, mustChangePassword, refresh}`.
8. **Tests**: backend — policy unit tests; bootstrap migrates env admin; login/lockout/unlock; MFA setup →
   enable → login requires code → recovery code single-use → disable; role guard (viewer POST → 403, operator
   `/users` → 403, admin ok); users CRUD incl. last-admin protection; `current_user` 401 on pending MFA;
   session invalidation after password change. Frontend — login two-step, forced password change, account MFA
   setup flow, users page create shows temp password (all against MSW).
9. `README.md` — you may **append** a section “Users, roles and MFA” (stream B appends a different section;
   append yours under a heading of exactly that name and do not edit anything else in the file).
10. CHANGELOG is written by the integrator — report your changes in your final message instead.

---

## Stream B — public deployment: Caddy + Let's Encrypt, secure cookies, Azure notes

### Owns
- `deploy/docker-compose.yml`, new `deploy/Caddyfile`, `deploy/.env.example`, new `deploy/azure/` (a
  `cloud-init.yml` + `README.md` or a `setup-azure-vm.sh` that installs Docker, clones/unpacks avdlab, writes
  `.env`, opens nothing locally (NSG is in Azure), and starts compose), `backend/avdlab/core/config.py`
  (**add** settings only: `public_url: str = ""`, `secure_cookies: bool | None = None`,
  `trust_proxy_headers: bool = False`), `backend/avdlab/main.py` (only the `SessionMiddleware` line and a
  `ProxyHeadersMiddleware`/`uvicorn --proxy-headers` equivalent + one `@app.middleware("http")` adding
  security headers), `backend/tests/test_public.py` (new), `README.md` (**append** a section headed exactly
  “Running on the public internet (Azure VM, HTTPS)”), `deploy/Dockerfile.backend` only if the CMD needs
  `--proxy-headers --forwarded-allow-ips`.

### Requirements
1. Compose gains a `caddy` service (image `caddy:2`) under a compose **profile** `public`, so the current
   LAN install (`docker compose up -d`) is unchanged, and `docker compose --profile public up -d` adds TLS.
   Caddy publishes 80/443, reverse-proxies to `api:8080` (WebSockets work out of the box), persists
   `caddy_data`/`caddy_config` volumes (certificates), and reads `AVDLAB_DOMAIN` + `AVDLAB_ACME_EMAIL` from
   `.env`. Caddyfile: `{$AVDLAB_DOMAIN} { encode zstd gzip; reverse_proxy api:8080; header { Strict-Transport-Security
   "max-age=31536000"; X-Content-Type-Options nosniff; X-Frame-Options DENY; Referrer-Policy
   strict-origin-when-cross-origin } }` plus `tls {$AVDLAB_ACME_EMAIL}`. Optional `AVDLAB_ACME_STAGING=true`
   → Let's Encrypt staging CA (document the exact Caddy directive). With the `public` profile the `api`
   service must **not** publish 8080 on the host (use a second compose file `deploy/docker-compose.public.yml`
   that overrides `ports: []` for api, or an env-driven port binding `127.0.0.1:8080` — pick one and document
   it; the point is that the app is reachable only through Caddy).
2. Backend: when `AVDLAB_PUBLIC_URL` starts with `https://` (or `AVDLAB_SECURE_COOKIES=true`) the session
   cookie is `Secure`; always `HttpOnly`, `SameSite=lax`. Trust `X-Forwarded-Proto/For` only when
   `AVDLAB_TRUST_PROXY_HEADERS=true` (set it in the public compose). Add response headers
   `X-Content-Type-Options: nosniff`, `Referrer-Policy`, `X-Frame-Options: DENY` from the app too (defence in
   depth; cheap). `cors_origins` gets `public_url` appended automatically when set.
3. The frontend uses relative `/api` URLs and `ws(s)://` derived from `location` — verify
   (`frontend/src/api/client.ts`, `components/JobDrawer.tsx`, `Terminal*.tsx`) and fix only if a hard-coded
   `ws://` or `http://` exists (report it; do not refactor).
4. Azure notes (README section + `deploy/azure/`): VM size guidance (the orchestrator itself is small —
   B2s is fine; the *lab host* is the big one), NSG rules 22 (your IP only), 80, 443; DNS A record before first
   start (Caddy needs it for the HTTP-01 challenge); `.env` keys to set (`AVDLAB_DOMAIN`, `AVDLAB_ACME_EMAIL`,
   `AVDLAB_PUBLIC_URL=https://<domain>`, `AVDLAB_TRUST_PROXY_HEADERS=true`, strong `AVDLAB_ADMIN_PASSWORD`,
   `AVDLAB_SESSION_SECRET`); how to check the certificate (`docker compose logs caddy`, `curl -I`); renewal is
   automatic; how the lab host (`sandbox`) is reached from Azure (VPN/Tailscale or SSH over public IP with key
   auth — state that the orchestrator only needs SSH to the lab host); backups of the `avdlab-data` volume.
   Also a one-screen `deploy/azure/setup-azure-vm.sh` (idempotent, bash, Ubuntu 24.04) that installs Docker
   from the official repo, creates `/opt/avdlab`, unpacks a tarball or clones a repo URL passed as `$1`,
   copies `.env.example` → `.env` filling domain/email/admin password/session secret from `$2..$4` or
   generated values (print them once), and runs `docker compose --profile public up -d --build`.
5. Tests: `test_public.py` — with `AVDLAB_PUBLIC_URL=https://x`, the login response sets a cookie with
   `Secure`; without it, no `Secure`; security headers present; CORS includes the public URL. Use
   `monkeypatch.setattr(settings, ...)` + a fresh `create_app()`.

---

## Stream C (integrator, in this session) — CloudVision tab crash + error boundaries

- `CvLastDeployPanel` crashes when `last_deploy.workspace` is `null` (deploy failed before a workspace
  existed) → the whole SPA unmounts (“page is gone”). Make the panel null-safe, add a `ErrorBoundary`
  component around each lab tab and around the router, surface “no change control created: designed config
  equals running config” explicitly, and improve `cv_deploy` failure messages.
