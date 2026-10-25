# avdlab — AVD Lab Orchestrator v1.1.2

A web app that prepares lab hosts, builds Arista AVD labs from templates (pyavd 6.4), deploys them with containerlab
and validates them with ANTA 1.10. Design: `docs/10-app-architecture.md`; implementation contract: `docs/CONTRACT.md`.

```
backend/    FastAPI app `avdlab` (python 3.11) + pytest suite
frontend/   Vite + React + TypeScript SPA (built into frontend/dist, served by the API)
templates/  lab templates (AVD examples + template.yml knobs)
agent/      Dockerfile for the avdlab-agent image that runs on lab hosts
deploy/     docker-compose.yml, Dockerfile.backend, .env.example
```

## Templates

Every AVD 6.4 example is a template (`GET /api/labs/templates` → `tags`, `node_count`, `learn`, knobs):
`single-dc-l3ls` (generated from counts/pools), `dual-dc-l3ls`, `single-dc-multipod-l3ls`, `l2ls-fabric`,
`campus-fabric`, `cv-pathfinder` (WAN control plane only on cEOS), `isis-ldp-ipvpn`, `single-dc-l3ls-ipv6`
(optional dual-stack management via `mgmt_ipv6_subnet`), plus `custom` for your own inventory.

## Development

```bash
cd backend
python3.11 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
AVDLAB_DATA_DIR=$PWD/../data AVDLAB_DATABASE_URL=sqlite+aiosqlite:///$PWD/../data/avdlab.db \
  AVDLAB_ADMIN_PASSWORD=admin uvicorn avdlab.main:app --reload --port 8000
python -m pytest -q                        # no Docker or SSH needed; SSH is faked

cd ../frontend && npm install && npm run dev   # http://localhost:5173, proxies /api to :8000
```

## Unpacking a release tarball

Extract in a terminal so existing files are overwritten:

```bash
tar -xzf ~/Downloads/avdlab-vX.Y.Z.tar.gz -C ~        # writes into ~/avdlab
cd ~/avdlab/deploy && docker compose up -d --build
```

Double-clicking the archive in Finder unpacks *next to* the existing folder and renames conflicting files to `name 2.ts`,
`name 3.ts`; those stray copies break the TypeScript build. If that happened: `find ~/avdlab -name "* [0-9].*" -not -path "*/node_modules/*" -delete`.
Once the repo is on GitHub, prefer `git pull` or the in-app self-update.

The avdlab machine
 runs only the app (API + UI, and optionally Postgres) in Docker. The labs run on a **Linux lab host**
(a LAN box or cloud VM, x86_64) that the app reaches over SSH. cEOS-lab is an x86_64 Linux container, so it is not
meant to run on the Mac itself. The images below are multi-arch, so the app builds natively on Apple Silicon.

1. **Install a Docker engine.** Use Docker Desktop or OrbStack, and turn on "start at login". Stop the Mac from
   sleeping so jobs keep running: `sudo pmset -a sleep 0 disksleep 0` (or System Settings → Energy → "Prevent
   automatic sleeping").
2. **Configure the app:**
   ```bash
   git clone <repo> ~/avdlab && cd ~/avdlab/deploy
   cp .env.example .env
   # edit .env: AVDLAB_ADMIN_PASSWORD, AVDLAB_SESSION_SECRET (openssl rand -hex 32),
   #            AVDLAB_SECRET_KEY (Fernet key; see the comment in .env). Keep a copy of the key.
   ```
3. **Start it:** `docker compose up -d --build`, then open `http://<mac-mini>.local:8080` and log in as the admin.
   To use Postgres instead of SQLite, set
   `AVDLAB_DATABASE_URL=postgresql+asyncpg://avdlab:<pw>@db:5432/avdlab` and run `docker compose --profile postgres up -d`.
4. **Add a lab host.** Go to Hosts → *Generate key* and paste the public key into `~/.ssh/authorized_keys` on the
   Linux box. The user there needs root via sudo: either passwordless (`echo "$USER ALL=(ALL) NOPASSWD:ALL" | sudo tee /etc/sudoers.d/avdlab`) or give the host a *sudo password* in the host form (stored encrypted; with SSH password auth that password is reused automatically).
   Then:
   - Add the host.
   - *Probe* it. You get capabilities plus recommendations.
   - *Bootstrap* it with the `clab-host` recipe: Docker, containerlab (native or container mode), sysctls, `/opt/avdlab`,
     the agent image, and optionally the native pyavd/ANTA toolchain.
   - *Verify* it.

   The host key is pinned on first connect (TOFU) in `/data/known_hosts`.
5. **Optional: let the app use the Mac's Docker.** Uncomment the `/var/run/docker.sock` mount in `docker-compose.yml`.
   This lets the app cache and import cEOS images locally before pushing them to a host.
6. **Operate it:**
   - Update: `git pull && docker compose up -d --build`, or from the app (see **Updating** below).
   - Logs: `docker compose logs -f api`.
   - Back up: everything lives in the `avdlab-data` volume (labs are git repos, plus the database, `secret.key` and
     `known_hosts`), e.g. `docker run --rm -v avdlab_avdlab-data:/data -v $PWD:/b alpine tar czf /b/avdlab-data.tgz -C /data .`

### Updating

The version badge shows an "update available" pill when a newer release exists; About/Changelog have an Update panel.

- **Check:** set `AVDLAB_UPDATE_SOURCE=github:<owner>/<repo>` (GitHub Releases, latest release) or a manifest URL
  (`{"latest", "url", "notes", "published_at", "image"}`) in `.env`. The API checks once a day
  (`AVDLAB_UPDATE_CHECK_INTERVAL` hours) in the background, never at startup; *Check now* forces it.
  `GET /api/meta/update` returns `{current, latest, available, url, notes, published_at, checked_at, mode,
  can_self_update, reason, last_update}`.
- **Update now** (`POST /api/meta/update/apply`, job `app.update`) needs:
  1. the `/var/run/docker.sock` volume uncommented in `docker-compose.yml`,
  2. `AVDLAB_DEPLOY_DIR` = the host path of `deploy/` (compose passes `${PWD}` by default, so run compose from
     `deploy/`),
  3. `AVDLAB_UPDATE_MODE`: `source` (default; the checkout must be a git clone: `git pull --ff-only` +
     `docker compose up -d --build`) or `image` (writes `AVDLAB_IMAGE=ghcr.io/<owner>/avdlab:<version>` into `.env`,
     then `docker compose pull api && docker compose up -d api`). `manual` only notifies.

  The API starts a detached `docker:27-cli` helper container (repo mounted at its host path, working dir `deploy/`)
  that survives the API restart, and follows its output into `/data/update.log`. The restarted API records the result
  (`/data/update.json` → `last_update: {from, to, at, ok}`) and closes the job. The UI polls `/api/meta` until the
  new version answers. Updates are refused (409) while any job runs.
- **Releases** come from `.github/workflows/release.yml`: pushing a tag `v<VERSION>` runs the backend + frontend
  tests, pushes `ghcr.io/<owner>/avdlab:<version>` and `:latest` (amd64 + arm64) and creates the GitHub Release with
  a source tarball and the matching CHANGELOG section. `ci.yml` runs the tests and an image build on every push/PR.

**Devcontainer instead of the web UI.** Hosts → Devcontainer (`POST /api/hosts/devcontainer`) downloads a zip for VS
Code Dev Containers:
- `remote-host`: build locally, run labs on the SSH lab host.
- `local-clab`: Docker-in-Docker, for an x86_64 Linux machine.

**Custom agent image.** `docker build -t <registry>/avdlab-agent:6.4.0 agent/`, push it, and set `AVDLAB_AGENT_IMAGE`
in `.env`. It adds ANTA 1.10.0, eos-downloader, dnsmasq, iproute2, curl, jq, rsync and openssh-client.

## NetBox (optional)

avdlab can push a built lab into NetBox (Module 6): sites/locations/racks, devices (switches with their AVD serials,
plus the containerlab clients as role `server`), interfaces (LAGs with members, switchport mode and VLANs, VRFs,
management interfaces `mgmt_only`), VLANs, VRFs (with RD), prefixes (address pools, SVI subnets, P2P /31s),
IP addresses (anycast for SVI virtual IPs and shared VTEPs, `loopback` for Loopback0, management IP as primary IPv4)
and cables. Everything is tagged `avdlab` and `avdlab:<lab slug>`. The sync is idempotent: it looks objects up by
name/serial/natural key and only creates or updates what differs.

1. **Run NetBox next to the app** (or use any NetBox 4.x you already have):
   ```bash
   cd deploy
   # in .env: NETBOX_SECRET_KEY (python3 -c "import secrets; print(secrets.token_urlsafe(50))")
   #          NETBOX_API_TOKEN  (python3 -c "import secrets; print(secrets.token_hex(20))"), NETBOX_SUPERUSER_PASSWORD
   docker compose --profile netbox up -d      # netbox, netbox-worker, netbox-postgres, netbox-redis, netbox-redis-cache
   ```
   The first start runs the database migrations (a few minutes). NetBox is then at `http://<docker-host>:8000`
   (`NETBOX_PORT`), with the superuser and API token from `.env`.
2. **Add a target** on the NetBox page: URL `http://netbox:8080` (same compose project) or
   `http://<docker-host>:8000`, the API token (write-only, stored encrypted), then *Test* (`{ok, version, error}`).
3. **Sync a lab:** in the lab's NetBox tab pick the target (`PATCH /api/labs/{id} {netbox_target_id}`), then
   *Sync* (`POST /api/labs/{id}/netbox/sync {delete_stale, dry_run, include_clients}` → job `lab.netbox_sync`).
   The lab must be built. `dry_run` only counts what would change; `delete_stale` removes objects tagged with this
   lab that are no longer in it (an object also tagged by another lab only loses this lab's tag). The result counts
   and an "open in NetBox" link are kept in `GET /api/labs/{id}/netbox/status`.

Note: the site is the AVD `dc_name` (else the fabric name), so two labs with the same fabric name share a site and
their devices would collide by name: give each lab its own `dc_name`/fabric name, or its own NetBox.

## Users, roles and MFA

avdlab has individual accounts (table `users`), three roles and optional TOTP two-factor authentication.

**Upgrading / first start.** When the `users` table is empty, the first admin is created from
`AVDLAB_ADMIN_USER` / `AVDLAB_ADMIN_PASSWORD`, so an existing single-admin install keeps working with the same
credentials. If that password is the default `change-me` or does not meet the policy, the app still starts (a
warning is logged) but the admin must choose a new password at the first sign-in. After that, the env password
is no longer used: manage accounts under **Users** (admin only) and your own under **Account** (user menu, top right).

**Roles** (enforced by the backend for every `/api/*` request; a refused call answers `403 {"detail": "role viewer may not …"}`):

| role | may |
|---|---|
| `viewer` | read everything (GET), live job logs and WebSockets (note: this includes the web terminals), change their own password and MFA |
| `operator` | everything except user administration (`/api/users`) and deleting hosts |
| `admin` | everything, including users, password/MFA resets, unlocking and the audit log |

**Passwords**: at least 12 characters (`AVDLAB_PASSWORD_MIN_LENGTH`, never below 8), 3 of 4 character classes
(lower/upper/digit/symbol), must not contain the username, must not be a common password and may not repeat a
character more than 3 times in a row. Passwords are stored as argon2id hashes. An admin can create a user without
a password or reset one: avdlab then shows a 20-character temporary password **once**, and the user must change it
at the next sign-in. Changing a password signs out all other sessions of that user.

**Lockout**: after `AVDLAB_LOGIN_MAX_FAILURES` (default 5) wrong passwords the account is locked for
`AVDLAB_LOGIN_LOCKOUT_MINUTES` (default 15); an admin can unlock it earlier from the Users page.

**MFA**: Account → *Set up* shows a QR code (and the key, for manual entry) for any authenticator app
(Microsoft/Google Authenticator, 1Password, …; 6 digits, 30 s). After confirming a code you get 10 one-time
recovery codes — copy or download them, they are not shown again (you can regenerate them with your password).
Sign-in then asks for the 6-digit code or a recovery code; 5 wrong codes restart the sign-in. Lost the phone and
the codes? An admin uses *Reset MFA* on the Users page. The issuer label shown in the app is `AVDLAB_MFA_ISSUER`
(default `avdlab`). The TOTP secret is encrypted with the secret-store key (`AVDLAB_SECRET_KEY` / `secret.key`), so
keep that key with your backups.

**Audit log**: sign-ins, failures, lockouts, MFA and account changes (user, IP, user agent, time) are listed under
Users → Audit log (`GET /api/users/audit?limit=100`).

Lost the only admin password? Stop the app, empty the users table (`sqlite3 /data/avdlab.db "DELETE FROM users"` —
this removes all accounts) and start again: the admin is re-created from the env variables.

## Account requests, admin settings and e-mail

**Request an account.** When registration is enabled (default), the sign-in page shows *Request an account*
(`/register`). Visitors enter a username (3–32 characters: lowercase letters, digits, `.`, `_`, `-`), e-mail,
name and a short reason. The request lands in **Users → Pending requests**; the admins get an e-mail
(`registration_mail_to`) when SMTP is configured. Requests are limited to 5 per hour per IP address.
*Approve* (choose the role) creates the account with a 20-character temporary password that has to be changed at
the first sign-in. avdlab e-mails it to the applicant when SMTP is configured, and always shows it **once** to the
admin, so you can hand it over yourself. *Reject* (optional reason) e-mails the applicant when SMTP is configured.
The applicant's page polls `GET /api/register/status/{id}`, which returns only the status. All three actions are in
the audit log. Turn the form off with *Admin settings → Registration enabled* (the API then answers 403).

**Admin settings** (`/admin/settings`, admins only; link in the Users page header) are stored in the database
(table `app_settings`). An unset value falls back to the matching `AVDLAB_*` variable, so `.env` only provides
the defaults:

| setting | env default | purpose |
|---|---|---|
| `app_title` | `AVDLAB_APP_TITLE` (`avdlab`) | name used in e-mails and the UI |
| `github_url` | `AVDLAB_GITHUB_URL` | GitHub button in the top bar, dashboard and register page |
| `support_email` | `AVDLAB_SUPPORT_EMAIL` | contact address on the register and About pages |
| `registration_enabled` | `AVDLAB_REGISTRATION_ENABLED` (`true`) | public account requests |
| `intro_video_url` | `AVDLAB_INTRO_VIDEO_URL` (`/intro/avdlab-intro.mp4`) | dashboard intro video; empty hides it |
| `feedback_mail_to` | `AVDLAB_FEEDBACK_MAIL_TO` (`dietrich@arista.com`) | receives feedback e-mails and the test mail |
| `feedback_mail_enabled` | `AVDLAB_FEEDBACK_MAIL_ENABLED` (`true`) | mail every new feedback entry |
| `registration_mail_to` | `AVDLAB_REGISTRATION_MAIL_TO` (= `feedback_mail_to`) | receives account requests |
| `smtp_host` / `smtp_port` | `AVDLAB_SMTP_HOST` / `AVDLAB_SMTP_PORT` (`587`) | empty host = no e-mail |
| `smtp_user` / `smtp_password` | `AVDLAB_SMTP_USER` / `AVDLAB_SMTP_PASSWORD` | login (no login when the user is empty) |
| `smtp_starttls` | `AVDLAB_SMTP_STARTTLS` (`true`) | STARTTLS; port 465 always uses implicit TLS |
| `smtp_from` | `AVDLAB_SMTP_FROM` (`avdlab@<host of AVDLAB_PUBLIC_URL>`) | sender address |

The SMTP password entered in the UI is stored encrypted in the secret store (secret `smtp_password`). The API never
returns it, only `has_smtp_password`. *Send test mail* sends a message to `feedback_mail_to` and shows the SMTP
error if that fails. `app_title`, `github_url`, `support_email`, `registration_enabled` and `intro_video_url` are
also returned by the public `GET /api/meta`. API: `GET/PUT /api/admin/settings` (a `null` value resets a key to its
env default; `"smtp_password": ""` clears the password), `POST /api/admin/settings/test-mail`.

**E-mail behaviour.** Mail is sent in the background with a 20-second timeout: a slow or broken mail server never
delays or fails a request; failures are only logged (without the password). Every new feedback entry is mailed as
`[avdlab] <kind>: <title>` with the page, the user, the text, the version, the number of failed jobs and a link to
`<AVDLAB_PUBLIC_URL>/feedback`.

## Running on the public internet (Azure VM, HTTPS)

The compose profile `public` puts [Caddy](https://caddyserver.com) in front of the app. Caddy terminates TLS with
a Let's Encrypt certificate that it obtains and renews automatically, and forwards everything (WebSockets included)
to `api:8080`. Without the profile nothing changes: `docker compose up -d` is still the plain LAN install on port 8080.

**VM.** The orchestrator itself is small: an Azure **B2s** (2 vCPU / 4 GB) with Ubuntu 24.04 is enough. The
*lab host* is the big machine (cEOS needs about 1 GB RAM per node), and it can be a different VM or a box at home.

**Azure network security group (NSG).** Allow inbound **22** from your own IP only, plus **80** and **443** from anywhere. Port 80 is
needed for the ACME HTTP-01 challenge and for the redirect to https. Do not open 8080: with the public setup, the
API binds to `127.0.0.1:8080` only (`AVDLAB_BIND_ADDRESS=127.0.0.1`), so it is reachable only through Caddy, or
through `ssh -L 8080:127.0.0.1:8080 <vm>` for debugging.

**DNS first.** Create the DNS A record `<domain>` → VM public IP **before** the first start. Caddy requests the
certificate right away, and repeated failures run into Let's Encrypt rate limits. For a dry run, set
`AVDLAB_ACME_STAGING=true`. Caddy then uses the staging CA (`acme_ca https://acme-staging-v02.api.letsencrypt.org/directory`,
which the caddy entrypoint sets through `AVDLAB_ACME_CA`), and browsers will warn about the certificate. To switch to a real
certificate, remove the setting, then run `docker compose --profile public down` and `docker volume rm avdlab_caddy_data`.

**Setup.** On the VM, run:

```bash
curl -fsSLO https://raw.githubusercontent.com/<owner>/<repo>/main/deploy/azure/setup-azure-vm.sh   # or scp it
sudo bash setup-azure-vm.sh <git-url | release-tarball-url | tarball-path> avdlab.example.com you@example.com [admin-password]
```

The script is idempotent. It installs Docker from the official repo, puts the code in `/opt/avdlab`, writes
`deploy/.env` and runs `docker compose --profile public up -d --build`. It prints any generated admin password and
session secret once. To do the same by hand, set these keys in `deploy/.env`:

| key | value |
|---|---|
| `AVDLAB_DOMAIN` | `avdlab.example.com` (the A record) |
| `AVDLAB_ACME_EMAIL` | your address (Let's Encrypt account / expiry mails) |
| `AVDLAB_PUBLIC_URL` | `https://avdlab.example.com`: the session cookie becomes `Secure`, and the URL is added to the CORS origins |
| `AVDLAB_TRUST_PROXY_HEADERS` | `true`: the app trusts `X-Forwarded-Proto/For` from Caddy (real client IPs in the audit log) |
| `AVDLAB_BIND_ADDRESS` | `127.0.0.1` |
| `AVDLAB_ADMIN_PASSWORD` | a strong password (see *Users, roles and MFA*); turn on MFA for internet-facing installs |
| `AVDLAB_SESSION_SECRET` | `openssl rand -hex 32` |
| `AVDLAB_SECURE_COOKIES` | optional: empty means it follows `AVDLAB_PUBLIC_URL`; `true` or `false` forces it |

Then run `cd /opt/avdlab/deploy && docker compose --profile public up -d --build`.

Notes:
- With a `Secure` cookie, sign-in works only over https. Plain `http://<vm>:8080` (for example through the SSH
  tunnel) then shows the login page, but you do not stay signed in.
- `AVDLAB_TRUST_PROXY_HEADERS` is applied in the app: uvicorn's `ProxyHeadersMiddleware` is mounted with all hosts trusted when the
  setting is true. That is safe only because the API port is not exposed. The image's `uvicorn --proxy-headers`
  default still trusts only `127.0.0.1`.
- Headers: Caddy adds HSTS (`max-age=31536000`), `X-Content-Type-Options`, `X-Frame-Options: DENY` and
  `Referrer-Policy`. The app sends the last three itself as well.

**Check the certificate.** Run `docker compose --profile public logs caddy`; look for `certificate obtained successfully`.
Then run `curl -I https://avdlab.example.com`, which should answer `HTTP/2 200` with a `strict-transport-security` header.
Renewal is automatic (about 30 days before expiry). The certificates and the ACME account are kept in the
`caddy_data` volume.

**Reaching the lab host.** The orchestrator only needs **SSH** to the lab host (job logs and web terminals are
proxied through that SSH connection). Use either:
- a VPN or Tailscale between the Azure VM and the lab host (best when the lab host is at home or on a LAN), or
- SSH over the lab host's public IP with key auth only (Hosts → *Generate key*). On the lab host's firewall or NSG,
  allow 22 only from the Azure VM's IP.

If the Azure VM is also the lab host, that works too: `AVDLAB_REMOTE_ROOT` (default `/opt/avdlab`) is a path on the lab host.
In that case, set it to a different directory, such as `/opt/avdlab-labs`, so labs do not land in the app checkout.

**Backups.** All app state is in the `avdlab-data` volume: the database, labs, `secret.key` and `known_hosts`.

```bash
cd /opt/avdlab/deploy
docker run --rm -v avdlab_avdlab-data:/data -v $PWD:/b alpine tar czf /b/avdlab-data-$(date +%F).tgz -C /data .
```

Copy the archive off the VM, for example to Azure Blob Storage (`az storage blob upload`) or with an Azure Backup
policy on the VM disk. Keep `deploy/.env` with it.

## AI lab designer (bring your own model)

Describe the lab you want — *"3 spines, 4 leaf pairs with MLAG, 2 servers per pair, tenant ACME with VLANs 110/120,
CloudVision cvaas"* — and the **Design with AI** panel (sparkle button on the lab **Topology** and **Settings** tabs
and in step 2 of the **New lab** wizard) turns it into a proposal you review before anything changes.

**AVD inputs stay the source of truth.** The model never writes YAML. It can only call two tools: `propose_lab_changes`
(partial knob values of the lab's template + the topology edit operations `add_endpoint`, `update_endpoint`,
`remove_endpoint`, `set_uplink`, `add_leaf_pair`, `remove_last_leaf_pair`, `set_l2leaf_per_pair`, `set_spine_count`)
and `ask_user` (one clarifying question). The tool schema is generated from the template descriptor (types, min/max,
choices, help). Every proposal is dry-run through the same code paths as the Settings form and the topology editor
(knob coercion → render → pyavd validation → structured configs/topology preview) and shown with the knob changes
(old → new), the operations, the changed files and the diff. A proposal that fails validation is sent back to the model
once with the violations. **Apply** goes through `PATCH /labs/{id}` and `POST /topology/{id}/edit`, so every change is a
git commit of the lab (and an `ai.apply` audit event). In the New-lab wizard the proposal fills the knob form instead.

Link a model on **Account → AI assistant** (operators and admins; the API key is write-only and stored encrypted in the
secret store), or let an admin configure a shared default on **Admin settings → AI assistant** and tick *Allow users
without their own link to use this model*. **Test connection** lists the provider's models.

| Provider | Base URL | Model (example) | Key |
|---|---|---|---|
| Anthropic (Claude) | empty (`https://api.anthropic.com`) | `claude-sonnet-4-5` | Anthropic API key (`x-api-key`) |
| OpenAI | empty (`https://api.openai.com/v1`) | `gpt-4o` | OpenAI API key (Bearer) |
| Ollama | `http://<host>:11434/v1` | `llama3.1`, `qwen2.5` | none |
| LM Studio | `http://<host>:1234/v1` | the loaded model | none |
| vLLM | `http://<host>:8000/v1` | the served model | optional |
| Azure OpenAI | `https://<resource>.openai.azure.com/openai/deployments/<deployment>` | deployment model | Azure key (`api-key` header; `api-version` added) |

Ollama example (on the lab host or any machine the orchestrator can reach):

```bash
ollama pull qwen2.5:14b          # a model with tool calling; small models propose less reliably
OLLAMA_HOST=0.0.0.0 ollama serve # then: Account → AI assistant → OpenAI-compatible, http://<host>:11434/v1, qwen2.5:14b
```

Settings (env defaults; the admin page wins): `AVDLAB_AI_DEFAULT_PROVIDER` (`anthropic`|`openai`),
`AVDLAB_AI_DEFAULT_BASE_URL`, `AVDLAB_AI_DEFAULT_MODEL`, `AVDLAB_AI_DEFAULT_API_KEY`, `AVDLAB_AI_SHARED_ALLOWED`
(default false), `AVDLAB_AI_RATE_LIMIT` (design calls per user per hour, default 30), `AVDLAB_AI_TIMEOUT` (seconds,
default 180).

**Privacy.** Each design call sends to the configured provider: the template's knob descriptions, the lab's current
knob values *without* secret knobs (passwords, tokens), and a topology summary (hostnames, node types, endpoints, free
switch ports) plus your chat messages. Secrets, onboarding tokens, SSH keys and CloudVision/NetBox credentials are never
sent. API keys are only placed in request headers and are masked in every error message and log line. Use a local model
(Ollama, LM Studio, vLLM) when lab data must not leave your network.

## Lab hosts on macOS and Windows (beta)

**Beta — not yet field-tested; please report what breaks.** containerlab needs a Linux kernel, so on a Mac or a Windows PC the lab host is a **Linux VM on that machine**:
an OrbStack VM on macOS (arm64 → use cEOSarm images, `ceos_arch: arm64`), or the prebuilt *Containerlab* WSL2
distribution on Windows (WSL ≥ 2.4.4, Docker Desktop integration off, sshd + `networkingMode=mirrored` so the
orchestrator can reach it). Add the VM as a host like any Linux box and run the `clab-host` bootstrap. When the orchestrator cannot reach the VM
directly (OrbStack VMs and WSL distros are NATed behind the Mac/PC), set the host's **Jump host** to `user@<the Mac or PC>`
and use the VM's name as seen from there as the address (`avdlab-vm.orb.local`): avdlab tunnels through it.

macOS quick start (beta): install OrbStack → `orb create ubuntu avdlab-vm` → `orb -m avdlab-vm sudo apt-get install -y openssh-server`
→ `orb -m avdlab-vm sudo systemctl enable --now ssh` → in avdlab add host *Address* `avdlab-vm.orb.local`, *SSH user* your Mac
user name, *Jump host* `youruser@<mac>`, your usual SSH key → Probe → Bootstrap. On Apple Silicon use cEOSarm images. Details,
commands and the roadmap for a no-SSH `local` host kind: `docs/multi-os-hosts.md`.

