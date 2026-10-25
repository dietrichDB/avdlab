# Changelog

All notable changes to avdlab are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.5] - 2026-10-22

### Fixed
- Release tarball excludes editor/linter caches (`.ruff_cache`, `.mypy_cache`, `*.tsbuildinfo`, `.DS_Store`) so it contains exactly the tracked source tree.

## [1.0.4] - 2026-10-22

### Fixed
- Release tarball no longer includes the PDF builder's `.work/` cache (25 MB of screenshots and fonts).

## [1.0.3] - 2026-10-22

### Added
- `scripts/docs-pdf/build.py`: renders `docs/1.0/` to one A4 PDF in the app's look — cover, contents with page numbers, the 14 diagrams re-themed in the app colours (wide ones on landscape pages), 25 screenshots of the UI taken in mock mode. `scripts/release.py … --pdf` builds it next to the release tarball.

## [1.0.2] - 2026-10-21

### Added
- **Add host asks what the machine is**: Linux server/VM, Mac (OrbStack VM, beta) or Windows (WSL2, beta). The Mac and Windows choices show the one-time setup steps inline, relabel the address field ("address of the VM as seen from the Mac") and offer the jump host; the probe checks the declaration and says precisely what is wrong (e.g. "this address is the Mac itself, not the VM"). Replaces the silent auto-detection of 1.0.0.
- `docs/1.0/`: the 1.0 documentation set — concepts, architecture with 14 rendered diagrams (components, deployments, lab lifecycle, job states, data model, security, request path), installation, lab hosts, page-by-page user guide, AI designer, API reference (176 routes, 28 job types), operations/troubleshooting, development.

### Fixed
- Jobs still "running" in the database after the API restarted (crash, `docker compose restart`) blocked their lab with *&lt;job&gt; is already running* forever; they are now marked failed at startup with an explanatory message.
- Removed the broken `avdlab` console entry point from `pyproject.toml`; `AVDLAB_AI_TIMEOUT` documented with its real default (180 s); Getting started no longer says Deploy pushes configs over eAPI (switches boot from startup-config; *Push config changes* is the eAPI path).

## [1.0.1] - 2026-10-21

### Added
- **SSH jump host** per lab host (beta): `Jump host = user@mac-or-pc[:port]` — avdlab connects to that machine first (same key/password) and reaches the Linux VM behind it (`Address` = the VM as seen from there, e.g. `avdlab-vm.orb.local`). All traffic (commands, file sync, terminals) goes through the one tunnelled connection; host keys of both hops are pinned.

### Fixed
- Adding a Mac (or a Windows PC) itself as a lab host failed with *unsupported or undetectable OS*: probe and bootstrap now recognise macOS/Windows and explain the beta setup — OrbStack VM on the Mac (`orb create ubuntu avdlab-vm`, sshd inside, jump host = the Mac) or the Containerlab WSL2 distribution on Windows.

## [1.0.0] - 2026-10-21

### Added
- **Lab hosts on macOS and Windows (beta)**: the probe recognises a Linux VM on a desktop OS — WSL2 on Windows or an OrbStack VM on macOS — shows it as a violet *beta* badge on the Hosts list and host page, and gives platform-specific advice (Docker inside the distro with Docker Desktop integration off, sshd + WSL mirrored networking, VM memory, cEOSarm on Apple Silicon). Picking an arm64 host in *New lab* switches the image knob to arm64. Write-up: `docs/multi-os-hosts.md`. Untested in the field, hence beta.

### Changed
- **1.0.0**: first release recommended for other users — accounts with roles and MFA, public HTTPS deployment, account requests, AI designer, incremental push, PSIRT scan, Getting started. Everything learned in the 0.x field tests is in the changelog below.

## [0.10.1] - 2026-10-21

### Added
- **New lab from a sentence**: the first step of *New lab* has a "Start from a sentence" card — the model first picks the template (one `choose_template` tool call over the template catalogue), then proposes the knobs; the wizard continues at step 2 with the knobs filled, a suggested lab name and a note explaining the choice. Nothing is created until *Create lab*.
- **Getting started** page (`/getting-started`, in the Learn group and on the dashboard banner): the seven stations of a lab — host, image, lab, build, deploy, validate, CloudVision — as a hoverable journey with "what it is / why it matters / how in three moves", done-state taken from this server, guided-tour buttons, a card for every menu item (with advanced/admin badges) and the five core words.
- Feedback modal: **E-mail** button that opens the user's mail client with the feedback address (`feedback_mail_to`, exposed as `feedback_email` on `/api/meta`), a subject and the page context (route, breadcrumb trail, URL, version, user, browser, time) prefilled — works without SMTP.
- **Push config changes** (incremental deploy): after a Build on a running lab, the Runtime and Build tabs show which devices' intended config changed since they last received it and push only those over eAPI as a config replace (`lab.push_configs`, `GET /labs/{id}/push/plan`, `POST /labs/{id}/push {hosts?}`) — seconds, no container restart, the fabric keeps running. `lab.deploy` records the config hash per device; labs deployed before this version show their devices as "not tracked" and push them all once.
- **Security advisories (ANTA PSIRT, preview)** on the Validation tab: *Run advisory scan* runs `anta psirt` with the dedicated ANTA image (`AVDLAB_ANTA_IMAGE`, default `ghcr.io/aristanetworks/anta:1.10.0`, pulled on the lab host on first use; the AVD universal image bundles an older ANTA) against the running switches — optionally only selected advisories (`SA117 SA140`) — and shows, per device and advisory, affected / mitigated / not affected / inconclusive with severity, vulnerability details and the remediation plan, plus the Markdown report (`lab.psirt`, `GET/POST /labs/{id}/psirt`, `GET /labs/{id}/psirt/report.md`). Credentials travel through a 0600 env-file; reports are kept under `anta/psirt/` in the lab repo.
- Dashboard: **Lab hosts** panel with CPU, memory and disk bars and the container count per host, refreshed every 30 s from the monitor samples.

### Changed
- Lab Settings and Admin settings: the Save/Reset bar moved to the top of the page (sticky, left-aligned) — the bottom-right spot was covered by the Feedback button, which is now smaller and translucent.
- Every shipped fix now gets its own patch version (`scripts/release.py patch`), so the sidebar badge and `GET /api/meta` say exactly what is deployed.

### Fixed
- Test-host add/remove on a lab whose switches are not actually running (host rebooted) failed with a cryptic ansible *No route to host* buried under deprecation warnings: the config push now checks the target containers first and says which switch is down and to Redeploy; removing a client still completes (with a warning) when its switch is unreachable; ansible errors are reported from the `fatal:` line.
- Deploy after a host crash failed with *Subnet 172.16.1.0/24 already in use by Docker network "clab-mgmt-<other lab>"*: a lab's management network survived without its containers. Deploy now removes such empty leftover networks on its subnet, and when the subnet is held by a lab that is actually running it says which one and suggests the `mgmt_subnet` knob.
- A page refresh (or a direct link) on any route other than `/` answered `{"detail": "Not Found"}`: the SPA fallback now serves `index.html` for every non-API path that is not a real file.
- NetBox 4.5+ API tokens (`nbt_<id>.<secret>`) were sent with the old `Token` scheme and refused with *Invalid authorization header*; they now go out as `Bearer` (pynetbox ≥ 7.8 pinned, plus a header override), a pasted `Token `/`Bearer ` prefix is stripped, and the connection-test error explains both token formats.
- CloudVision knob: the region dropdown offered `eu-west-2` (and other names AVD does not know); the list now equals AVD 6.4's `cv_settings.cvaas.clusters[].region` valid values (`euwest-2`, `us-4`, `apnortheast-1`, …), matching the backend validation.
- CloudVision deploy: the change-control option is now labelled "approve & execute the change control" with an explanation of the two states (a submitted workspace with changes always creates one; unchecked = pending approval, checked = completed), and the job log states the requested behaviour.
- ANTA security advisory scan: runs with `--enable` (privileged mode), without which every `show running-config` based check errored; uses the dedicated ANTA 1.10 image.
- AI designer: provider errors (502) were shown as a bare "HTTP 502" — the client now surfaces the server's message for every structured error; the model field offers the models listed by *Test connection*; unsaved-changes badges on the AI forms.
- NetBox tokens saved before 0.10.0 with a `Token ` prefix or wrapped whitespace are normalised on read.

## [0.10.0] - 2026-10-15

### Added
- **AI lab designer (bring your own model)**: a *Design with AI* panel on the lab Topology and Settings tabs and in step 2 of the New lab wizard turns a sentence ("3 spines, 4 leaf pairs with MLAG, tenant ACME with VLANs 110/120, CVaaS") into **validated AVD inputs** — template knobs and topology edit ops, never raw YAML. Every proposal is dry-run with pyavd and shown as knob changes (old → new), ops, changed files and diff; an invalid proposal gets one automatic repair round; *Apply* goes through the existing knob/topology code paths (git-committed, audit-logged).
- Providers: Anthropic Messages API and any OpenAI-compatible Chat Completions endpoint (OpenAI, Ollama, LM Studio, vLLM, Azure OpenAI). Each user links their own model on **Account → AI assistant** (key write-only, encrypted; *Test connection*); admins may offer a shared default on **Admin settings → AI assistant** (`AVDLAB_AI_*`). 30 design calls per user per hour; operator or admin role required. Only knob values (without secrets) and a topology summary are sent to the provider.
- Templates: with more than 2 spines the leaf uplinks grow (Ethernet1-4) and the MLAG peer-link moves up (Ethernet5-6); server ports that would collide are now shifted automatically (Ethernet5 → Ethernet7) instead of failing at build time.

### Changed
- Author title corrected to "Systems Engineer, Arista Networks" (About page, `LICENSE`, `AVDLAB_COPYRIGHT` default).

## [0.9.0] - 2026-10-10

### Added
- **CloudVision-inspired look**: dark navy navigation grouped into Overview / Provisioning / Learn / Admin with icons, a slim top bar with breadcrumbs, GitHub button, Beginner/Advanced toggle and user menu; light content with white cards and Arista-blue primary actions; dark mode kept. Design tokens are CSS variables (`--cv-*`) mapped into Tailwind, so shared buttons, cards, inputs, tables, badges and tabs restyled the whole app.
- **Beginner / Advanced mode** (remembered per browser, default Beginner): Beginner hides NetBox, Devcontainer, Jobs and the advanced lab tabs (MSS, NetBox, ACT, Git) — all still reachable by URL, "Show advanced" at the end of the tab row — and shows one-line hints on Hosts, Images, Labs, New lab and the Build / Topology / Validation tabs.
- **Lean dashboard**: welcome banner with *Start learning*, *New lab* and *GitHub*; the **intro video** (90 s captioned walkthrough, `frontend/public/intro/avdlab-intro.mp4` + `.webm`, falls back to a guided tour when missing); tiles for labs, running labs, hosts online, images; a *Next steps* checklist that follows what exists. Advanced mode adds the hosts, labs and recent-jobs tables.
- **Account requests**: public `/register` page; admins approve (with a role) or reject from a *Pending requests* card on the Users page. Approval creates the account with a one-time temporary password, e-mailed to the applicant when SMTP is configured. 5 requests/hour/IP, audit-logged, can be switched off.
- **Admin settings** page (`/admin/settings`, stored in the database, `AVDLAB_*` env as defaults): app title, GitHub link, support e-mail, registration on/off, intro video URL, SMTP server (password encrypted, never returned), feedback/registration mail recipients, *Send test mail*.
- **Feedback e-mail**: every new feedback entry is mailed as `[avdlab] <kind>: <title>` with a diagnostics summary (default recipient `dietrich@arista.com`); never slows down or fails the request.
- `GET /api/meta` returns `app_title`, `github_url`, `support_email`, `registration_enabled`, `intro_video_url`.

### Changed
- Copyright and licence now read "© 2026 Dietrich De Bruyne — Systems Engineer, Arista Networks. All rights reserved." (About page, `LICENSE`, `AVDLAB_COPYRIGHT` default).

## [0.8.0] - 2026-10-08

### Added
- **Multi-user accounts** with roles `admin`, `operator` and `viewer`, enforced on every `/api/*` request (403 `role viewer may not POST /api/labs`; viewers cannot open web terminals). Upgrade path: the first admin is created from `AVDLAB_ADMIN_USER` / `AVDLAB_ADMIN_PASSWORD`, so existing installs keep their login; a default or weak password forces a change at first sign-in.
- **Password policy**: ≥ 12 characters (`AVDLAB_PASSWORD_MIN_LENGTH`, floor 8), 3 of 4 character classes, must not contain the username, common passwords rejected, no runs of identical characters; argon2id hashes. Account lockout after `AVDLAB_LOGIN_MAX_FAILURES` (5) failures for `AVDLAB_LOGIN_LOCKOUT_MINUTES` (15); unknown users and wrong passwords get the same error.
- **TOTP two-factor authentication**: QR set-up, 10 single-use recovery codes (shown once, downloadable, regenerable), two-step sign-in; admins can reset a user's MFA. Changing or resetting a password signs out that user's other sessions, including open WebSockets.
- **Users page** (admin): create users with a one-time temporary password, change role, disable, reset password/MFA, unlock, delete; audit log of sign-in and account events. **Account page**: change password with a live policy checklist, manage MFA. User menu top-right.
- **Public HTTPS deployment**: compose profile `public` adds Caddy 2 with Let's Encrypt (automatic issue + renewal, certificates in `caddy_data`; `AVDLAB_ACME_STAGING=true` for dry runs). `AVDLAB_BIND_ADDRESS=127.0.0.1` keeps the API reachable only through Caddy. New settings `AVDLAB_PUBLIC_URL` (Secure session cookie, auto-added to CORS), `AVDLAB_SECURE_COOKIES`, `AVDLAB_TRUST_PROXY_HEADERS`. Security headers from the app and from Caddy (HSTS).
- `deploy/azure/setup-azure-vm.sh`: re-runnable Ubuntu 24.04 setup (Docker, `/opt/avdlab`, `.env`, public profile) and a README section on NSG rules, DNS-first, certificate checks, reaching the lab host and backups.
- CloudVision deploy **pre-flight**: a 15 s REST check against the target before the playbook runs turns "ansible failed" into a precise cause (expired/rejected token, unreachable server) and records it on the lab.
- CloudVision tab states the outcome explicitly: *Deploy failed before a workspace was created* (with the job link and typical causes) or *Workspace submitted, nothing to push* (designed config equals running config, so no change control was needed); build warnings are collapsed.

### Fixed
- **The lab page went blank after a failed CloudVision deploy** until the app was restarted: the last-deploy panel assumed a workspace always existed and crashed the whole SPA. The panel is null-safe and every lab tab and routed page is now wrapped in an error boundary that keeps a rendering error local (with *Try again* / *Reload*).

## [0.7.2] - 2026-10-06

### Fixed
- **Test-host deploy and ACT deploy failed on the config push** with `Could not find or access '/proc/1/fd/pipe:[N]'`: the agent container ran `ansible-playbook … -e @/dev/stdin`, and Ansible resolves `/dev/stdin` to the pipe's /proc path, which it cannot open (this also broke inventory parsing). Credentials still arrive on stdin, but are now copied to a 0600 file inside the throw-away agent container (`sh -c 'umask 077; cat > /tmp/avdlab-extra-vars.yml; exec ansible-playbook … -e @/tmp/…'`), so nothing lands on the host or on a command line (`executors.common.ansible_stdin_cmd`, shared by `lab.client_deploy` and `act.deploy`).

## [0.7.1] - 2026-10-06

### Added
- Images page: an **arista.com token** button that is always reachable — replace a mistyped or expired token in place (same secret, no duplicates), show/hide while typing, remove it. When arista.com rejects the saved token the catalogue panel now says so and offers *Replace token* / *Retry* instead of a dead end. New endpoints `GET/PUT/DELETE /api/images/token` (the value is never returned); replacing the token drops the catalogue cache.
- New lab and lab Settings: the cEOS version is now picked from a **dropdown of the images already downloaded** (format, version, architecture, size, which hosts have it) instead of a free-text field; choosing an image also sets the `ceos_arch` knob, warns when the image's architecture does not match the lab host, and shows whether the image is present on the host. *Other…* keeps a free-text version for images that are not in the store.

### Fixed
- Backend tests for `/api/meta` and feedback diagnostics no longer hard-code the release version.

## [0.7.0] - 2026-09-26

### Added
- Update check against a GitHub Releases feed or manifest (`AVDLAB_UPDATE_SOURCE`) and one-click self-update (source or image mode) via a detached helper container; CI and release workflows.
- All Arista AVD examples as templates: campus-fabric, cv-pathfinder, isis-ldp-ipvpn, single-dc-l3ls-ipv6 (with tags, node counts, notes and "what it teaches").
- In-app AVD learning: `/learn` hub with 14 lessons (try/check/quiz, server-side checkpoints and progress), glossary hover terms, schema browser for eos_designs and eos_cli_config_gen, knob help popovers, config-line explainer with the lab's real values, guided tours.
- Interactive test hosts: hot-add a client to any free switch port from the topology (partial build + limited config push + veth), quick ping/iperf/exec from the node inspector, remove.

## [0.6.0] - 2026-09-26

### Added
- Editable topology: `POST /api/topology/{id}/edit` applies ops (`set_uplink`, `add_endpoint`, `update_endpoint`, `remove_endpoint`, `add_leaf_pair`, `remove_last_leaf_pair`, `set_l2leaf_per_pair`, `set_spine_count`) to the AVD inputs, validates them with pyavd, supports `dry_run` (changed files + diff) and re-renders the graph from a rebuilt topology.
- Topology overrides stored in the lab settings (`topology_overrides`) and re-applied after every knob render, so hand-moved uplinks and extra endpoints survive knob changes.
- Free/used switch ports per node (`GET /api/topology/{id}/ports`) derived from eos_designs facts (uplinks, MLAG) and connected-endpoint adapters.
- Node positions saved per lab in `clab/layout.json` (`GET/PUT /api/topology/{id}/layout`) and returned with the topology graph.
- Topology editor in the Topology tab: edit mode, drag nodes, move uplinks to a free spine port, add/remove servers, leaf pair / L2 leaf / spine count toolbar, pending-ops list with dry-run preview.
- Changelog page backed by `CHANGELOG.md` (`GET /api/meta/changelog`) and a version badge with a "what's new" dot.
- `GET /api/meta`: version, git sha and build date baked into the image (build args `AVDLAB_GIT_SHA`, `AVDLAB_BUILD_DATE`), pinned AVD/ANTA versions, Python version, latest changelog entry.
- Feedback form (bug / suggestion / question) with optional server-side diagnostics (version, browser, recent failed jobs with secrets masked, host capabilities), a `/feedback` admin page, a markdown copy per entry and optional GitHub issue creation (`AVDLAB_FEEDBACK_GITHUB_REPO` + secret `github_token`).
- About page with copyright (`AVDLAB_COPYRIGHT`), `LICENSE` (all rights reserved) and third-party notices (`THIRD_PARTY_NOTICES.md`, `GET /api/meta/notices`).
- avdlab logo (mark, full logo, favicon) and a footer with version, Changelog, About and Feedback links.

## [0.5.3] - 2026-09-26

### Fixed
- SSH connect retries transient TCP/handshake failures with backoff (authentication failures are not retried) and reports descriptive connect errors (timeout, connection refused) instead of empty messages.

## [0.5.2] - 2026-09-22

### Fixed
- CloudVision onboarding writes the onboarding token inside the cEOS namespace (`docker exec -i` over stdin): the cEOS tmpfs on `/tmp` hid files copied with `docker cp`. A copy of the token is kept on flash.

## [0.5.1] - 2026-09-22

### Fixed
- CloudVision onboarding restarts TerminAttr in two steps with a PID check, verifies the token file, and logs focused diagnostics from the newest TerminAttr log.

## [0.5.0] - 2026-09-22

### Added
- Per-device CloudVision tags (`device_tags` knob) rendered into each node's structured config, per-device labels win over fabric-wide tags; custom labs get a post-build injection; tags column in the device table.
- Kill a running job (cancels the task and closes its SSH sessions) and stop/kill Docker containers on a host, with a typed confirmation for containers of a deployed lab.
- DHCP + ZTP provisioning (`provisioning` knob): cEOS nodes boot into Zero Touch Provisioning from a `ztp` node running dnsmasq + HTTP, serving intended configs or a CloudVision bootstrap script; ZTP timeline in the Runtime tab.
- NetBox module: NetBox targets with write-only API tokens, idempotent `lab.netbox_sync` from structured configs, fabric CSVs and clients (dry run, delete stale), optional NetBox compose profile, NetBox page and lab tab.
- MSS foundation (`segmentation` knob): `router segment-security` rendered on the VRF carrier leafs, policy matrix, `lab.mss_verify` probes, MSS tab.
- Arista Cloud Test (ACT) target: digital-twin build with pyavd, ACT topology with a tools server and client nodes, deploy/validate from the tools server, ACT tab.

### Changed
- Lab target (`clab` or `act`) can be chosen at creation and changed in the lab settings; switching target resets a built lab to draft.

## [0.4.1] - 2026-09-22

### Fixed
- CloudVision onboarding checks TerminAttr at privilege 15, parses the running config strictly and logs `show daemon` output plus the TerminAttr agent log tail for nodes that are not streaming.

## [0.4.0] - 2026-09-22

### Added
- Linux client containers for connected endpoints (bonds, 802.1Q sub-interfaces, addresses derived from the SVIs) with ping-matrix and iperf traffic runs.
- Running configs, config diff against the intended config, snapshots and web terminals for lab nodes and hosts.
- Per-lab git remote with push / pull (fast-forward, re-validated) and import from git; token and SSH-key credentials never stored in the repository.
- Custom labs imported from an archive (zip / tar.gz) and lab export.
- Host monitoring (CPU, memory, disk, containers) with charts and a 7-day history.
- cEOS arm64 support (`ceos_arch` knob, image formats with architecture, host/image architecture mismatch checks).
- VS Code / devcontainer open links per lab.

## [0.3.0] - 2026-09-22

### Added
- CloudVision integration: `cloudvision` knob (CVaaS or on-prem, TerminAttr via `cv_settings`, topology hints and device tags), CloudVision targets with write-only service-account and onboarding tokens, lab binding, `lab.cv_onboard` and `lab.cv_deploy` jobs, CloudVision tab.
- `dns_servers` knob.

### Changed
- CloudVision mode `none` strips the TerminAttr configuration shipped by the AVD examples.

### Fixed
- Clearing a target's token (null) now removes the stored secret.

## [0.2.0] - 2026-09-22

### Added
- containerlab executor: `lab.deploy`, `lab.destroy`, `lab.status` and `lab.validate` (ANTA) jobs on the lab host, with the cEOS image pushed when missing.
- Runtime and Validation tabs with ANTA reports (JSON, Markdown, CSV).

### Changed
- A validated lab can be redeployed.

## [0.1.1] - 2026-09-22

### Added
- Hosts accept a sudo password (`sudo -S`) when passwordless sudo is not configured; the probe checks sudo and gives a clear hint.

### Changed
- Light database migration for columns added after the first release.

## [0.1.0] - 2026-09-22

### Added
- Initial release: FastAPI backend with single-admin auth, encrypted secret store and a job engine with live log streaming.
- Hosts (Module 1): SSH with trust-on-first-use host keys, probe, `clab-host` bootstrap recipe (Docker, containerlab native or in a container, sysctls, agent image), verify, and a devcontainer generator.
- Lab templates from the AVD examples (single-dc-l3ls, dual-dc-l3ls, single-dc-multipod-l3ls, l2ls-fabric) with knobs; single-dc-l3ls fully generated from counts, pools and ASNs.
- Labs: git repository per lab, pyavd build (validate, facts, structured configs, configs, documentation, ANTA catalogs), history and diff, file editor with validation.
- containerlab topology generation from the AVD build (serial numbers, interface mappings, endpoint containers) and a topology graph.
- cEOS image catalogue and download via eos-downloader, push to hosts, manual upload.
- Docker Compose deployment (single image serving API and UI) and the avdlab-agent image.
- React UI: dashboard, hosts, images, labs, new-lab wizard and lab detail tabs.

### Fixed
- IP pool knobs are validated; image sizes are stored as 64-bit integers.

