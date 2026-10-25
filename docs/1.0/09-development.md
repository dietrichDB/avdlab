# 9. Development

This chapter is for people who change avdlab: where things are, how to run and test them, and the recipes for the
usual extensions (a job, a page, a template, a knob). The design contracts in `docs/CONTRACT-*.md` are the original
per-phase specifications and remain the best explanation of *why* a module looks the way it does.

## 9.1 Repository layout

```
avdlab/
├── VERSION, CHANGELOG.md, LICENSE, THIRD_PARTY_NOTICES.md   served by /api/meta*
├── backend/                 FastAPI app `avdlab` (Python 3.11) + pytest suite
│   ├── pyproject.toml       dependencies (pyavd[ansible]==6.4.0, anta==1.10.0, asyncssh, sqlalchemy, …)
│   ├── avdlab/
│   │   ├── main.py          app factory, middleware, FEATURE_MODULES, SPA mount
│   │   ├── core/            config (settings), db (all models + light migrations), security (RoleGuard, secret store),
│   │   │                    passwords (policy, argon2id, TOTP), app_settings, mailer
│   │   ├── api/             auth, jobs, secrets, users, registration (admin settings)
│   │   ├── jobs/engine.py   @job registry, JobRunner, JobContext
│   │   ├── hosts/           api, ssh (asyncssh wrapper, TOFU, jump host), probe, recipes, monitor, devcontainer
│   │   ├── images/          api, service (catalogue, tags, arch), docker (local docker / ardl wrapper)
│   │   ├── labs/            api, service (lab dirs + git), templates (knobs + rendering), build (pyavd), git, archive,
│   │   │                    integrations_api, mss, mss_api, ztp_api, act_api
│   │   ├── topology/        clab (AVD → containerlab), clients, edit (topology ops), ztp, models, api
│   │   ├── executors/       base (interface), clab, act, common, configs, traffic, hot_clients, push, psirt, mss,
│   │   │                    terminal, lab_routes, client_routes
│   │   ├── cloudvision/     api, service (targets, REST, cv_deploy playbook), jobs (onboard, deploy)
│   │   ├── netbox/          api, service (client factory, tokens), sync (plan + reconciler), jobs
│   │   ├── learn/           api, lessons/*.md, glossary.yml, tours.yml, knob_learn.yml, schema/*.json, explain(+_rules)
│   │   ├── ai/              api, config (links, shared default), designer (prompt, tools, dry run), providers
│   │   ├── meta/            api (version, changelog, notices), update (check + self-update)
│   │   └── feedback/        api
│   └── tests/               37 files, pytest-asyncio, fake SSH / docker / HTTP
├── frontend/                Vite + React 18 + TypeScript + Tailwind SPA
│   └── src/
│       ├── App.tsx, main.tsx     routes, MSW start in mock mode
│       ├── api/                  typed fetch clients per area + types.ts
│       ├── components/           Layout (navigation), ui, KnobForm, TopologyGraph/Editor, JobDrawer, ai/, learn/, …
│       ├── pages/ + pages/lab/   one file per page and per lab tab
│       ├── hooks/, lib/          auth, meta, job streams; uiMode, labActions, formatting, domain helpers
│       ├── mocks/                MSW handlers + fixtures (mock mode, tests)
│       └── test/                 19 vitest files
├── templates/<name>/        AVD example (inventory.yml, group_vars/, playbooks/) + template.yml (knobs)
├── agent/Dockerfile         optional custom agent image (universal image + ANTA 1.10 + eos-downloader + dnsmasq …)
├── deploy/                  docker-compose.yml, Dockerfile.backend, Caddyfile, .env.example, azure/setup-azure-vm.sh
├── scripts/                 release.py, export_schema.py
├── docs/                    design notes, CONTRACT-*.md, multi-os-hosts.md, this 1.0/ set
├── cli/                     empty placeholder (pyproject declares `avdlab = "avdlab.cli:app"`, which does not exist yet)
└── .github/workflows/       ci.yml, release.yml
```

## 9.2 Running it locally

### Backend

```bash
cd backend
python3.11 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
AVDLAB_DATA_DIR=$PWD/../data AVDLAB_DATABASE_URL=sqlite+aiosqlite:///$PWD/../data/avdlab.db \
  AVDLAB_ADMIN_PASSWORD=admin uvicorn avdlab.main:app --reload --port 8000
```

Templates are read from `<repo>/templates` by default (`AVDLAB_TEMPLATES_DIR`). With `frontend/dist` present the API also
serves the built SPA. SSH-based features need a real lab host; everything else (templates, build, topology, learn, AI
with a reachable provider) works on a laptop.

### Frontend

```bash
cd frontend && npm install
npm run dev            # http://localhost:5173, proxies /api (incl. WebSockets) to :8000
VITE_MOCK=1 npm run dev   # mock mode: MSW intercepts /api in the browser, no backend needed (amber "mock" tag in the sidebar)
npm run build          # tsc -b && vite build → frontend/dist
npm run lint           # eslint
npm run typecheck      # tsc -b
```

Mock mode uses the handlers and fixtures in `src/mocks/` (`handlers*.ts`, `data.ts`, `phase*.ts`). It is how the intro
video was recorded and how the UI can be demoed without a host. A reload resets the in-memory mock database.

### Docker image

```bash
cd deploy && docker compose up -d --build     # same image as production
```

## 9.3 Tests

| Suite | Command | Size | Notes |
|---|---|---|---|
| backend | `cd backend && python -m pytest -q` | 302 tests in 37 files (`testpaths = tests`, `asyncio_mode = auto`) | no Docker, no SSH: `SSHClient.connect` is replaced by fakes (`FakeSSH` maps command substrings to `(rc, stdout)`), local Docker by `FakeDocker`, HTTP by `httpx.MockTransport`; the `client` fixture logs in as the bootstrap admin |
| frontend | `cd frontend && npm test` (`vitest run`) | 163 tests in 19 files | jsdom, Testing Library, MSW handlers |
| lint | `cd backend && ruff check avdlab`; `npx tsc -p tsconfig.app.json --noEmit` | | as required by the contracts |

Notable test guarantees: the pyavd build of the default `single-dc-l3ls` template is byte-identical to Arista's shipped
`intended/configs`; the generated containerlab topology equals the shipped example link for link; every template builds
(`test_templates_all.py`).

Run time: the backend suite takes several minutes (pyavd builds). In the run made for this documentation 301 passed and
`tests/test_phase3b.py::test_sshclient_connect_attaches_to_ctx` failed (it also fails alone with *no such table: jobs*, so
it depends on another test initialising the database) — check before relying on a green suite.

## 9.4 How templates and knobs are defined

A template is a directory `templates/<name>/` with the AVD example files and a descriptor `template.yml`:

```yaml
name: single-dc-l3ls
title: Single DC L3LS (EVPN/VXLAN, 2 spines, 2 MLAG leaf pairs, 2 L2 leafs)
description: |
  eBGP underlay + eBGP EVPN overlay …
tags: [l3ls, evpn, vxlan, mlag, ebgp]
notes: ...                 # optional caveats shown with the template
learn: |                   # what it teaches + AVD doc URL
  Teaches the reference AVD design end to end: …
fabric_name: FABRIC
generator: l3ls            # l3ls | static | none
base_mgmt_subnet: 172.16.1.0/24   # static: the example's mgmt subnet (re-based onto knob mgmt_subnet)
l3ls: {…}                  # l3ls generator only: group names, mgmt offsets, L2 leaf MLAG pool
knobs:
  - key: leaf_pairs
    type: int              # int | string | cidr | ipv4 | pool | secret | object | yaml | bool
    default: 2
    min: 1
    max: 8
    label: L3 leaf MLAG pairs
    help: Spine downlinks are extended beyond Ethernet1-8 automatically …
    learn: {summary, avd_keys, renders, pitfalls, doc_url}
  - key: tenants
    type: yaml
    default_from: "NETWORK_SERVICES:tenants"   # default read from the example's group_vars
  - key: something
    type: string
    path: "FABRIC:some.dotted.key"             # static generator: where the value is written
```

Knob model fields (`labs/templates.py` `Knob`): `key`, `type`, `label`, `help`, `default`, `default_from`
(`GROUP:dotted.path` into the base group_vars), `min`, `max`, `path` (static generator target), `choices`, `value_type`
(type every value of an object knob must satisfy, e.g. `pool`). `coerce_knobs()` merges user values over the defaults,
coerces types and checks ranges (raising `KnobError` → 422); `render_lab()` produces the complete file set:

- generator `l3ls` builds inventory and node-type group_vars from counts and pools;
- generator `static` loads the example and applies targeted edits (mgmt re-basing, password, serials, platform, knobs
  with a `path`);
- both end with a common pass (passwords, serial numbers, platform, eAPI);
- `apply_topology_overrides` re-applies topology-editor overrides afterwards.

Cross-cutting knobs (`cloudvision`, `dns_servers`, `device_tags`, `provisioning`, `segmentation`, `client_image`,
`ceos_arch`, `act`) are handled by name in the renderer, the build (`labs/build.py`) or the topology generator. Every knob
should carry a `learn` block; `python3 -m avdlab.learn.knobs --write [templates_dir]` inserts missing blocks from
`learn/knob_learn.yml` without disturbing comments or key order. `GET /api/labs/templates` serves the descriptors with
resolved defaults and `learn` blocks; the frontend `KnobForm` renders them by type.

### Adding a template

1. Copy the AVD example into `templates/<name>/` (`inventory.yml`, `group_vars/`, `playbooks/`).
2. Write `template.yml`: `generator: static` (start here), `fabric_name` (the inventory group the build and ANTA use),
   `base_mgmt_subnet`, tags, `learn`, and the standard knobs (copy them from an existing static template: `mgmt_subnet`,
   `mgmt_gateway`, `ceos_version`, `ceos_arch`, `device_password`, `serial_scheme`, `dns_servers`, `cloudvision`,
   `client_image`, `device_tags`, `provisioning`, `segmentation`, `act`).
3. Replace hardware platforms cEOS cannot emulate with cEOSLab where needed (see `campus-fabric`), and make passwords
   literal (see the `cv-pathfinder` notes in the changelog).
4. Run `python -m pytest -q tests/test_templates_all.py` — it renders and builds every template.
5. Build it once in the UI or `build_lab_dir()` and check the containerlab topology (`clab/topology.clab.yml`).
6. The AI designer and the from-a-sentence catalogue pick it up automatically (descriptor + knobs).

## 9.5 How to add a job

1. **Write the job** in the feature's module and register it with the decorator:
   ```python
   from ..jobs.engine import JobContext, job

   @job("lab.something")
   async def lab_something(ctx: JobContext) -> dict:
       lab = await ctx.get_lab()
       ctx.log("[something] starting")
       async with ctx.session() as s:
           async with SSHClient.connect(host, s, log=ctx.log, ctx=ctx) as ssh:   # ctx= → Kill closes it
               res = await ssh.run("…", sudo=True, timeout=600, log=ctx.log, input=secret_on_stdin)
       ctx.check_cancelled()
       return {"ok": True}       # stored as the job result
   ```
   Raise an exception (e.g. `ExecutorError("clear, actionable message")`) to fail the job; the first line becomes the
   job's `error`. Put CPU-bound work in `asyncio.to_thread`.
2. **Start it from a route** that answers 202:
   ```python
   @router.post("/{lab_id}/something", status_code=202)
   async def start(lab_id: str, body: SomethingIn | None = None, session=Depends(get_session), user=Depends(current_user)):
       lab = await _lab(session, lab_id)
       await _busy(session, lab, ("lab.deploy", "lab.something"))     # 409 while a conflicting job runs
       return {"job_id": await runner.submit("lab.something", lab_id=lab.id, host_id=lab.host_id,
                                             params=(body or SomethingIn()).model_dump())}
   ```
3. **Register the module** in `main.FEATURE_MODULES` (append a line; importing the module registers its jobs and router).
4. **Secrets:** never put them on a command line or in the log. Use `input=` (stdin), a 0600 env-file removed in
   `finally`, or `executors.common.ansible_stdin_cmd()` for Ansible extra vars (never `-e @/dev/stdin`). Wrap the log
   with a masking logger when tokens are in play.
5. **Tests:** fake `SSHClient.connect` with a scripted runner, submit through the API with the `client` fixture, `await
   runner.wait(job_id)`, assert on the job row and `read_log(job_id)`.
6. **Frontend:** call the endpoint via an `api/*.ts` client and wrap it with `drawer.track(...)` from `useJobDrawer()` so
   the job drawer follows the log.
7. **Changelog:** add an entry under `## [Unreleased]`.

Role checks come for free: a `POST` is operator-only through RoleGuard. Use `require_role("admin")` for admin-only routes
(and keep them under `/api/admin` or `/api/users` so RoleGuard agrees).

## 9.6 How to add a page or a lab tab

| Step | Page | Lab tab |
|---|---|---|
| component | `frontend/src/pages/<Name>.tsx` | `frontend/src/pages/lab/<Name>Tab.tsx` taking `{ lab }` |
| route / registration | `<Route path="…">` inside the layout route in `App.tsx` | add `{ id, label }` to `TABS` in `LabDetail.tsx` and the render switch; add the id to `ADVANCED_TABS` to hide it in Beginner mode |
| navigation | add a `NavItem` to `NAV_GROUPS` in `components/Layout.tsx` (`advanced: true` hides it in Beginner mode, `adminOnly: true` for admins); add a breadcrumb label to `SECTION` | — |
| data | an `api/*.ts` client with types in `api/types.ts`; TanStack Query hooks in the component | same |
| mocks | handlers in `src/mocks/handlers*.ts` so mock mode and tests work | same |
| help | a `<Hint>` for Beginner mode; optional tour steps (`learn/tours.yml`, `data-tour` attributes) | same |
| tests | a vitest file in `src/test/` rendering with the MSW handlers | same |
| Getting started | add a card to the menu list in `pages/GettingStarted.tsx` | — |

Styling uses the `--cv-*` CSS variables mapped into Tailwind (`cv.*`) and the shared classes in `components/ui.tsx`
(`btn-primary`, `card`, `input`, `table`, `badge`, `Tabs`, `Field`, `ErrorBox`, `ConfirmButton`, …). Each routed page and
each lab tab already sits inside an error boundary.

## 9.7 Conventions that keep the codebase coherent

| Convention | Why |
|---|---|
| AVD inputs are the only source of truth; new features derive from build outputs | one model, no drift (chapter 1) |
| every slow action is a job with a clear log and an actionable error | the UI is a job runner with forms on top |
| secrets: write-only APIs, Fernet at rest, stdin / 0600 files in transit, masked logs | chapter 2 §(f) |
| SSH, Docker and HTTP go through one seam each (`SSHClient.connect`, `images.docker.backend`, `http_client()`) | tests fake them |
| new columns go into `core/db._ADDED_COLUMNS` | `create_all` never alters existing tables |
| each workstream owns its router module, appended to `FEATURE_MODULES` | parallel work without merge conflicts (contracts) |
| settings are `AVDLAB_*` fields in `core/config.py`; admin-editable ones also in `core/app_settings.py` | one place for defaults |
| every user-visible change gets a CHANGELOG line; every shipped fix a patch version | the version badge tells the truth |

## 9.8 Design contracts

| Contract | Version | Scope |
|---|---|---|
| `CONTRACT.md` | 0.1.0 | phase 0 + module 1 + module 2a: repo, job engine, settings, hosts and bootstrap, templates, build, topology, images, first UI |
| `CONTRACT-2b.md` | 0.2.0 | containerlab executor: deploy, destroy, status, validate; remote layout; Runtime and Validation tabs |
| `CONTRACT-2c.md` | 0.3.0 | CloudVision: targets, onboarding token, TerminAttr, cv_deploy, tags, `cloudvision` knob |
| `CONTRACT-3.md` | 0.4.0 | clients and traffic, git coupling, running configs and snapshots, custom labs, host monitoring, cEOS arm, web terminal, VS Code |
| `CONTRACT-3b.md` | 0.5.0 | per-device tags, kill job, kill container |
| `CONTRACT-4.md` | 0.5.0 | ZTP (module 3), NetBox (module 6), MSS foundation (module 4), ACT executor |
| `CONTRACT-5.md` | 0.6.0 | editable topology, changelog, feedback, About/copyright, version badge, logo |
| `CONTRACT-6.md` | 0.7.0 | updates, all AVD examples, in-app learning, interactive test hosts |
| `CONTRACT-7.md` | 0.8.0 | multi-user auth with MFA, public TLS deployment, CloudVision robustness, error boundaries |
| `CONTRACT-8.md` | 0.9.0 | CloudVision-inspired UI, beginner/advanced mode, lean dashboard, account requests, admin settings, mail, intro video |

The architecture note `docs/10-app-architecture.md` (design v0.1) explains the original decisions; where it differs from
the code (it planned arq/Redis and Postgres as default; the code uses an in-process engine and SQLite by default), the
code and this documentation win. `docs/multi-os-hosts.md` is the research behind the Mac/Windows beta hosts.

## 9.9 Releasing

1. Put the changes under `## [Unreleased]` in `CHANGELOG.md` (Added / Changed / Fixed).
2. Run the suites (`pytest -q`, `npm test`, `npm run build`).
3. `scripts/release.py patch` (or `minor` / `major` / `X.Y.Z`; `--tar <dir>` for a tarball): bumps `VERSION`,
   `backend/pyproject.toml`, `frontend/package.json`, rolls the changelog section, commits `release <version>` and tags
   `v<version>` (moving an existing tag).
4. `git push && git push --tags` → `release.yml` tests, checks the tag against `VERSION`, publishes
   `ghcr.io/<owner>/avdlab:<version>` + `:latest` (amd64 + arm64) and a GitHub Release with the changelog section.
5. Installs with `AVDLAB_UPDATE_SOURCE=github:<owner>/<repo>` see the new version within a day (or on *Check now*).

Details of the script and the workflows: chapter 8 §8.5.
