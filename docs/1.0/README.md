# avdlab 1.0 — documentation

This is the reference documentation for **avdlab 1.0.x**: the web-administered Arista AVD lab orchestrator built in
this repository (FastAPI backend + React SPA). Everything below is derived from the code at version `1.0.1`
(plus the host platform picker committed after it) and from `CHANGELOG.md`, which is the feature history.

## Elevator pitch

1. avdlab turns "I want an Arista EVPN/VXLAN lab" into a few clicks in a browser.
2. You pick one of the official **AVD 6.4 examples** as a template and adjust its **knobs** (spines, leaf pairs, pools, tenants, CloudVision).
3. The knobs write real **AVD inputs** (`inventory.yml` + `group_vars/`), validated by **pyavd** and committed to a git repo per lab.
4. **Build** runs pyavd in-process: structured configs, EOS CLI, documentation, ANTA catalogs and a **containerlab** topology.
5. **Deploy** copies the lab over SSH to a Linux **lab host** and starts the cEOS-lab switches with containerlab.
6. **Validate** runs **ANTA 1.10** from an agent container next to the switches and keeps every report.
7. Optionally the lab is onboarded into **CloudVision** (CVaaS or on-prem), synced into **NetBox**, or targeted at **Arista Cloud Test**.
8. Test hosts, traffic generators, web terminals, config diffs, incremental config pushes and an ANTA PSIRT scan make it a working lab, not a slide.
9. An optional **AI designer** (bring your own model) turns a sentence into validated knob changes — it never writes YAML.
10. Multi-user (admin / operator / viewer, TOTP MFA), runs on a Mac mini on a LAN or on an Azure VM behind Caddy with Let's Encrypt.

## The documents

| # | File | What it answers | Read it when |
|---|---|---|---|
| — | [README.md](README.md) | this index | first |
| 1 | [01-concepts.md](01-concepts.md) | the mental model: inputs as source of truth, template → knobs → build → deploy → validate → CloudVision, the objects (labs, hosts, images, jobs), beginner/advanced mode, roles | before you click anything |
| 2 | [02-architecture.md](02-architecture.md) | components, deployment topologies, the lab life-cycle sequence, the job engine, the data model, the security model, the request path | you operate, extend or audit the app |
| 3 | [03-installation.md](03-installation.md) | install on a Mac mini / LAN Docker host, on an Azure VM with public HTTPS; upgrades; backups; every `AVDLAB_*` setting | you install or upgrade |
| 4 | [04-lab-hosts.md](04-lab-hosts.md) | lab host requirements, the `clab-host` bootstrap recipe, Mac (OrbStack VM) and Windows (WSL2) beta hosts, jump hosts, probe output, sudo, images and CPU architecture | you add a lab host |
| 5 | [05-user-guide.md](05-user-guide.md) | page by page and lab tab by lab tab: what every control does, the typical workflow, the `single-dc-l3ls` knobs | you use the app |
| 6 | [06-ai-designer.md](06-ai-designer.md) | how *Design with AI* and *Start from a sentence* work: tools, dry run, repair round, apply, providers, privacy, limits | you want to use or approve the AI designer |
| 7 | [07-api.md](07-api.md) | REST and WebSocket reference: one table per router with method, path, role and purpose; all job types and their parameters | you script avdlab or debug the SPA |
| 8 | [08-operations.md](08-operations.md) | jobs and logs, a troubleshooting matrix built from every field fix in the changelog, monitoring, updating, the versioning rule | something is wrong, or you run releases |
| 9 | [09-development.md](09-development.md) | repo layout, running backend/frontend/mock mode, tests, how templates and knobs are defined, how to add a job, a page or a template, design contracts, release | you change the code |

## How to read

- **New user, network background:** 01 → 05 (follow the "typical workflow" at the top) → 04 when you add your first host.
- **Installer / operator:** 03 → 04 → 08, with 02 §(b) and §(f) for the network and security picture.
- **Developer:** 02 → 09 → 07; the design contracts in `docs/CONTRACT-*.md` are the original per-phase specifications.
- **Security review:** 02 §(f) security model, 03 (public profile), 06 (privacy of the AI designer), 07 (roles per route).

**PDF edition.** `python3 scripts/docs-pdf/build.py` renders this whole set (cover, contents with page numbers,
re-themed diagrams, screenshots of the UI in mock mode) to `dist/avdlab-<version>-documentation.pdf`;
`scripts/release.py patch --tar DIR --pdf` does it as part of a release. See `scripts/docs-pdf/README.md`.

Conventions used throughout:

- `monospace` = a literal value: a file path, a setting, an API path, a knob key, a job type.
- "Lab host" = the Linux machine (or Linux VM) that runs containerlab and the cEOS containers. "Orchestrator" = the
  machine that runs the avdlab container. They can be the same machine but usually are not.
- Every long-running action is a **job** (`lab.build`, `lab.deploy`, …). The UI shows its live log in the job drawer.
- *Beta* marks features that are implemented but not yet field-tested (macOS / Windows lab hosts, jump hosts).
  *Preview* marks upstream features that are themselves previews (ANTA `psirt`).

## Quick start (LAN, five minutes plus image download)

```bash
git clone <repo> ~/avdlab && cd ~/avdlab/deploy
cp .env.example .env            # set AVDLAB_ADMIN_PASSWORD, AVDLAB_SESSION_SECRET (openssl rand -hex 32)
docker compose up -d --build    # → http://<docker-host>:8080, sign in as admin
```

Then, in the browser: **Hosts** → *Generate SSH key* → put the public key on a Linux box → *Add host* → *Probe* →
*Bootstrap* → **Images** → save your arista.com token → *Download* `4.34.2F` → **Labs** → *New lab* → `single-dc-l3ls` →
*Create lab* → *Build* → *Deploy* → *Validate*. Chapter 3 covers the public HTTPS install, chapter 5 §5.0 the workflow in
detail.

## Feature map

| Feature | Since | Chapter |
|---|---|---|
| Hosts over SSH: probe, `clab-host` bootstrap recipe, verify, TOFU host keys, sudo password | 0.1.0 / 0.1.1 | 4 |
| Templates from all AVD 6.4 examples + `custom` import; pyavd build; git per lab; file editor | 0.1.0, 0.7.0 | 1, 5 |
| containerlab deploy / destroy / status, ANTA validation with stored reports | 0.2.0 | 2, 5 |
| CloudVision targets, onboarding (TerminAttr), `cv_deploy`, device tags, pre-flight | 0.3.0–0.8.0 | 5 |
| Linux clients, ping/iperf traffic, running configs, config diff, snapshots, web terminals | 0.4.0 | 5 |
| Per-lab git remote, archive import/export, host monitoring, cEOSarm, VS Code links | 0.4.0 | 4, 5 |
| ZTP provisioning, NetBox sync, MSS foundation, Arista Cloud Test target, kill job/container | 0.5.0 | 5 |
| Editable topology, changelog, feedback, About | 0.6.0 | 5 |
| Update check and self-update, in-app AVD learning, interactive test hosts | 0.7.0 | 3, 5, 8 |
| arista.com token management, image picker | 0.7.1 | 5 |
| Accounts, roles, password policy, TOTP MFA, audit log; public HTTPS with Caddy | 0.8.0 | 2, 3, 5 |
| CloudVision-inspired UI, beginner/advanced mode, account requests, admin settings, e-mail | 0.9.0 | 1, 5 |
| AI lab designer (bring your own model) | 0.10.0 | 6 |
| New lab from a sentence, Getting started, incremental config push, ANTA PSIRT scan, host health | 0.10.1 | 5, 6 |
| Lab hosts on macOS (OrbStack VM) and Windows (WSL2), beta; arm64 default for arm hosts | 1.0.0 | 4 |
| SSH jump host per lab host (beta) | 1.0.1 | 4 |
| Explicit platform choice when adding a host (Linux / Mac / Windows) with setup steps | after 1.0.1 (code comments say 1.0.2; not yet in the changelog) | 4 |

## What avdlab 1.0 does not do

- It does not run cEOS on macOS or Windows directly — always in a Linux VM (chapter 4).
- It does not resume jobs after an API restart; the job engine is in-process and single-node (chapter 2 §(d)).
- It does not import NetBox back into AVD, onboard ACT labs into CloudVision, or provide a full MSS design
  (the MSS tab is a foundation).
- It has no `local` host kind (containerlab through the orchestrator's own Docker socket without SSH) yet.

## Diagrams

Every diagram is kept as Mermaid source (`diagrams/<name>.mmd`, also inline in the documents so GitHub renders it)
and as a pre-rendered SVG (`diagrams/<name>.svg`) for viewers without Mermaid support.

| Diagram | Used in | Type |
|---|---|---|
| `concepts-pipeline` | 01 | flowchart: template → knobs → inputs → build → deploy/validate/CloudVision/NetBox |
| `lab-states` | 01 | state diagram of a lab (`draft` … `destroyed`) |
| `components` | 02 | component diagram |
| `deploy-lan`, `deploy-azure`, `deploy-jump` | 02 | deployment diagrams |
| `seq-lifecycle` | 02 | sequence: create lab → build → deploy → validate |
| `job-states` | 02, 08 | job engine state machine |
| `data-model` | 02 | ER diagram of all tables |
| `security` | 02 | where credentials live and travel |
| `request-path` | 02 | a request through Caddy and the middlewares |
| `host-setup` | 04 | add host → probe → bootstrap → verify |
| `ai-designer` | 06 | sequence of a design call |
| `self-update` | 03 | sequence of the in-app update |

To re-render after editing a `.mmd` file:

```bash
cd docs/1.0/diagrams
for f in *.mmd; do npx -y @mermaid-js/mermaid-cli@11 -i "$f" -o "${f%.mmd}.svg" -b transparent; done
```

(add `-p <puppeteer.json>` with `{"args": ["--no-sandbox"]}` when rendering as root in a container).

## Versions pinned by avdlab 1.0

| Component | Version | Where it is set |
|---|---|---|
| pyavd / AVD collection | 6.4.0 | `backend/pyproject.toml`, `AVDLAB_AVD_VERSION` (native toolchain + display) |
| ANTA | 1.10.0 | `AVDLAB_ANTA_VERSION`, `AVDLAB_ANTA_IMAGE=ghcr.io/aristanetworks/anta:1.10.0` |
| Agent image | `ghcr.io/aristanetworks/avd/universal:python3.11-avd-v6.4.0` | `AVDLAB_AGENT_IMAGE` |
| containerlab (container mode) | `ghcr.io/srl-labs/clab:latest` | `AVDLAB_CLAB_IMAGE` |
| Client containers | `ghcr.io/srl-labs/network-multitool:latest` | `AVDLAB_HOST_IMAGE`, knob `client_image` |
| Default cEOS-lab | 4.34.2F | knob `ceos_version` in every template |
| Python (API) | 3.11 | `deploy/Dockerfile.backend` |
| NetBox (optional profile) | v4.2 image | `NETBOX_VERSION` |

## Where this documentation comes from

| Fact | Source of truth in the repo |
|---|---|
| Feature history, field fixes | `CHANGELOG.md` |
| Routes and roles | `backend/avdlab/main.py`, every `**/api.py`, `core/security.py` (`role_denial`) |
| Settings | `backend/avdlab/core/config.py`, `core/passwords.py`, `deploy/.env.example`, `deploy/docker-compose.yml` |
| Templates and knobs | `templates/*/template.yml`, `backend/avdlab/labs/templates.py` |
| Navigation and pages | `frontend/src/components/Layout.tsx`, `frontend/src/pages/**` |
| Host platforms | `frontend/src/pages/Hosts.tsx` (`PLATFORMS`), `backend/avdlab/hosts/probe.py`, `docs/multi-os-hosts.md` |
| Release procedure | `scripts/release.py`, `.github/workflows/{ci,release}.yml` |

## Getting help

- In the app: the **Feedback** button on every page (with optional diagnostics) or its **E-mail** button.
- Start of every troubleshooting session: the job log of the failed job (job drawer, or `/jobs/<id>`), then the matrix
  in [08-operations.md](08-operations.md) §8.2.
- For field reports on the beta features (Mac/Windows lab hosts, jump hosts), include the probe output of the host.
