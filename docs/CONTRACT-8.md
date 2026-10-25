# CONTRACT-8 — v0.9.0: CloudVision-inspired look & feel, beginner/advanced mode, lean dashboard, account
# requests, admin settings (GitHub link, mail), feedback e-mail, intro video

Same rules as CONTRACT-7: parallel streams in `/home/claude/avdlab`, nobody commits, touch only owned files,
keep `pytest -q` (253) and `vitest run` (130) green plus your own tests, `ruff check avdlab` and
`npx tsc -p tsconfig.app.json --noEmit` clean. Current version 0.8.0 (accounts/roles/MFA, Caddy, see CONTRACT-7).

Shared facts both streams rely on:
- `GET /api/meta` (public, `backend/avdlab/meta/api.py`) is extended by **stream A** with
  `github_url: str | null`, `support_email: str | null`, `registration_enabled: bool`, `app_title: str`.
  **Stream B** reads these from the existing `useMeta()`/`metaApi` in the frontend (check `frontend/src/api/meta.ts`
  or wherever `/meta` is fetched) to render the GitHub button and the registration link. Until A lands, B treats
  missing fields as null/false (tolerant client).
- Copyright default changes to `© 2026 Dietrich De Bruyne — Systems Engineer, Arista Networks` (stream A,
  `core/config.py` `copyright` default + the About page text that mentions Defaultroute, + README/.env.example
  mentions). The licence stays "all rights reserved" in his name.
- UI mode: `localStorage['avdlab.uiMode']` ∈ {`beginner`, `advanced`} with a React context `useUiMode()` in
  `frontend/src/lib/uiMode.tsx` (**stream B** creates it). Default `beginner` for a fresh browser. Stream A does
  not depend on it.

---

## Stream A — account requests, admin settings, e-mail, GitHub link, copyright

### Owns
- Backend: new `backend/avdlab/core/app_settings.py` (key/value table `app_settings` + typed accessor with env
  defaults), new `backend/avdlab/api/registration.py`, new `backend/avdlab/core/mailer.py`,
  `backend/avdlab/api/users.py` (add approval endpoints), `backend/avdlab/feedback/` (send mail on new feedback),
  `backend/avdlab/meta/api.py` (extend `/meta`), `backend/avdlab/core/config.py` (**append** settings + change the
  `copyright` default), `backend/avdlab/core/db.py` (**add** `AccountRequest` + `AppSetting` models only),
  `backend/avdlab/main.py` (add routers to `FEATURE_MODULES` only), `backend/avdlab/core/security.py`
  (`role_denial`: allow anonymous/any-role `POST /api/register` and admin-only `/api/admin/*` — keep it minimal),
  tests `backend/tests/test_registration.py`, `backend/tests/test_mailer.py`.
- Frontend: new `frontend/src/pages/Register.tsx`, new `frontend/src/pages/AdminSettings.tsx`,
  `frontend/src/pages/Users.tsx` (pending-requests section), `frontend/src/pages/Login.tsx` (one line: link to
  `/register` when `meta.registration_enabled`), `frontend/src/pages/About.tsx` (copyright text / Defaultroute →
  Arista wording; the value itself comes from `/meta`), `frontend/src/api/{auth.ts,users.ts or new admin.ts,types.ts
  (append)}`, `frontend/src/App.tsx` (routes only: `/register` public, `/admin/settings` admin),
  `frontend/src/mocks/{handlers.ts,data.ts}` (append), test `frontend/src/test/registration.test.tsx`.
- README: append a section “Account requests, admin settings and e-mail”.

### Requirements
1. **App settings** (`app_settings` table, key → JSON value, admin-editable, env as default when unset):
   `github_url`, `support_email` (shown on the register page and About), `registration_enabled` (default true),
   `app_title` (default "avdlab"), `feedback_mail_to` (default `dietrich@arista.com`), `feedback_mail_enabled`
   (default true), `registration_mail_to` (default = feedback_mail_to), `smtp_host`, `smtp_port` (587),
   `smtp_user`, `smtp_password` (stored in the Fernet secret store as secret `smtp_password`, write-only),
   `smtp_starttls` (true), `smtp_from` (default `avdlab@<domain of public_url or localhost>`),
   `intro_video_url` (default `/intro/avdlab-intro.mp4`, stream B's Dashboard plays it; empty hides the player).
   API: `GET /api/admin/settings` (admin; `smtp_password` returned as `has_smtp_password: bool`),
   `PUT /api/admin/settings {…partial}` (admin), `POST /api/admin/settings/test-mail` → sends a test mail to
   `feedback_mail_to`, returns `{ok, error}`. Env equivalents `AVDLAB_GITHUB_URL`, `AVDLAB_SUPPORT_EMAIL`,
   `AVDLAB_FEEDBACK_MAIL_TO`, `AVDLAB_SMTP_*` seed the defaults (DB value wins).
2. **Mailer** (`core/mailer.py`): `async def send_mail(subject, text, to=None, html=None) -> bool` using
   `aiosmtplib` (add to pyproject) with STARTTLS/implicit TLS by port, 20 s timeout, never raises (logs and returns
   False), no-op returning False when `smtp_host` is empty. Unit-test with a fake SMTP class (monkeypatch
   `aiosmtplib.SMTP`), including that the password is never in the log.
3. **Feedback → mail**: on `POST /api/feedback` (existing endpoint) fire-and-forget `send_mail` with subject
   `[avdlab] <kind>: <title>`, body = title/kind/page/user/body + diagnostics summary (version, failed jobs
   count) and a link `<public_url>/feedback`. Tested with the fake SMTP. The feedback response must not wait on
   the mail (use `asyncio.create_task`) — test that a failing mailer does not fail the request.
4. **Account requests**: public `POST /api/register {username, email, full_name, reason}` → row in
   `account_requests` (status `pending`), 201 `{id, status}`; rate-limit 5/hour/IP (in-memory), username
   policy (3–32 chars `[a-z0-9._-]`, lowercased, not an existing user or pending request → 409), e-mail format
   check; notify admins by mail (`registration_mail_to`). When `registration_enabled` is false → 403.
   Admin: `GET /api/users/requests?status=pending|all`, `POST /api/users/requests/{id}/approve {role}` → creates
   the user with a generated temporary password (reuse the existing helper), `must_change_password=true`, status
   `approved`, mails the applicant the username + temporary password + login URL **if** SMTP is configured, and
   always returns `{user, temporary_password}` so the admin can hand it over otherwise; `POST …/reject {reason?}`
   → status `rejected`, optional mail to the applicant. Audit-log all three. `GET /api/register/status/{id}`
   (public, returns only `{status}`) so the applicant page can show progress.
5. **Frontend**: `/register` (public, uses the Logo, explains what avdlab is in two lines, form, success state
   with "you'll receive an e-mail / the administrator will contact you", link back to login; shows
   `support_email` and the GitHub button when set). Login page: "Request an account" link. Users page: a
   **Pending requests** card at the top with Approve (role select) / Reject; approving shows the temporary
   password once (same pattern as create user). `/admin/settings` page (admin, link from Users page header and
   from the user menu — the user menu lives in Layout which stream B owns, so ask B through the integrator;
   put the link in Users page header for now): sections *Project* (app_title, github_url, support_email,
   registration_enabled, intro_video_url), *E-mail* (smtp fields, feedback_mail_to, feedback_mail_enabled,
   registration_mail_to, "Send test mail" button with result).
6. Tests as described; MSW handlers for all new endpoints; frontend test: register flow + admin approve shows
   temp password + settings page saves.

---

## Stream B — CloudVision-inspired design system, beginner/advanced mode, lean dashboard

### Owns
- `frontend/src/index.css`, `frontend/tailwind.config.*`, `frontend/src/components/{ui.tsx,Layout.tsx,Brand.tsx}`,
  new `frontend/src/lib/uiMode.tsx`, new `frontend/src/components/{Hero.tsx,ModeToggle.tsx,IntroVideo.tsx}`,
  `frontend/src/pages/Dashboard.tsx`, `frontend/src/pages/lab/LabDetail.tsx` (tab list filtering by mode only),
  `frontend/src/pages/Learn.tsx` (header styling only), `frontend/public/intro/` (placeholder poster
  `poster.svg`; the integrator drops `avdlab-intro.mp4` there), `frontend/src/components/learn/Term.tsx`
  (visual only), test `frontend/src/test/design.test.tsx`. You may make **visual-only** class changes in other
  pages (no logic, no test-id changes) — keep a list in your report.
- `frontend/src/main.tsx` only to mount the `UiModeProvider`.

### Requirements
1. **Design system** inspired by Arista CloudVision's UI: a dark navy left navigation (`#0b1f3a`-ish) with light
   icon+label items and a highlighted active item, a slim white/zinc top bar with page title + breadcrumbs on
   the left and the user menu + mode toggle + GitHub button on the right, light grey content background
   (`zinc-50`/`slate-50`), white cards with 1px borders and soft shadow, Arista-blue primary (`#1b6ef3` →
   hover `#155cd0`) and the existing status colours, 13–14 px UI text, consistent 8 px spacing scale. Dark
   mode must still work (content dark, nav stays navy). Define the palette as CSS variables in `index.css`
   (`--cv-nav`, `--cv-nav-active`, `--cv-primary`, `--cv-surface`, …) and map them into Tailwind
   (`theme.extend.colors.cv.*`) so pages use `bg-cv-primary` etc. Update `btn-primary`, `btn-secondary`,
   `card`, `input`, `table`, `h2`, `label`, badges in `ui.tsx`/`index.css` so the whole app changes without
   touching every page. Icons: inline SVG set in `Brand.tsx`/`ui.tsx` (home, server, image, labs, cloud,
   book, users, cog, github) — no new npm dependency (lucide-react is fine ONLY if it is already in
   package.json; check).
2. **Navigation** grouped like CloudVision ("Overview", "Provisioning", "Learn", "Admin"): Dashboard | Hosts,
   Images, Labs, CloudVision, NetBox | Learn | Users, Admin settings (`/admin/settings`, admins only), Jobs,
   Feedback, Changelog, About. Beginner mode hides NetBox, Devcontainer, Jobs (they stay reachable by URL) and
   on the lab page hides the tabs MSS, ACT, NetBox, Git, ZTP-ish advanced ones (keep Overview, Settings, Build,
   Topology, Runtime, Validation, CloudVision, Clients, Devices, Jobs); a small "Show advanced" link at the end
   of the tab row switches mode. Advanced shows everything. `ModeToggle` in the top bar (segmented control
   "Beginner | Advanced") persists to localStorage and has `data-testid="mode-toggle"`.
3. **Lean dashboard**: hero card with the logo mark, one-sentence pitch ("Build, deploy and validate Arista AVD
   labs — and learn AVD while you do it"), two primary buttons **Start learning** (`/learn`) and **New lab**
   (`/labs/new`), a secondary **GitHub** button when `meta.github_url` is set; below it the intro video player
   (`IntroVideo`: `<video controls poster=/intro/poster.svg src={meta.intro_video_url}>` with a graceful
   fallback card when the URL is empty or the file 404s — check with a HEAD request or the `onError` event);
   then 3–4 compact stat tiles (labs, running labs, hosts online, images) and a short "Next steps" checklist
   driven by what exists (no host → "Add a lab host", no image → "Download a cEOS image", no lab → "Create
   your first lab", else "Deploy / Validate …") each linking to the page. In advanced mode add the existing
   recent-jobs table and labs table under a divider; in beginner mode leave them out. Keep every existing
   `data-testid` the dashboard tests rely on (run them; if a test only makes sense in advanced mode, set the
   mode in the test setup rather than changing the test's intent).
4. **Beginner hints**: a reusable `<Hint>` callout (light blue, ⓘ) that only renders in beginner mode; place
   one at the top of Hosts, Images, Labs/NewLab and the lab Build/Topology/Validation tabs explaining in one
   sentence what the page does and what to do first — these are visual-only additions in other owners' files,
   allowed by the owns clause.
5. Tests: mode toggle persists and filters nav + tabs; dashboard renders hero/buttons/next-steps in beginner
   mode and the tables in advanced mode; IntroVideo fallback. All existing frontend tests stay green.
6. Report the palette tokens and any page you changed visually.

---

## Stream C (integrator) — intro video, release

- Record a ~90 s captioned walkthrough of the mock-mode frontend (Playwright video → ffmpeg captions) into
  `frontend/public/intro/avdlab-intro.mp4` (+ `.webm`), update CHANGELOG/VERSION/status, commit, tarball.
