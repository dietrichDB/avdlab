# AVD Lab Orchestrator — architecture & module roadmap

Working name: **avdlab** (rename freely). Status: design v0.1, 2026-09-22. Companion to `00`–`04` in this folder.

## 1. Vision

A single web app, running in Docker on the Mac mini, that turns "I want an AVD lab" into a few clicks:
prepare a lab host → pick and customise a topology → get the right cEOS image → boot containerlab → build/deploy
with AVD → validate with ANTA → (optionally) onboard the switches to CloudVision, ZTP them, add clients and traffic,
and sync everything to NetBox. Later: ACT as a second execution target and MSS labs.

Decisions taken (2026-09-22):

| Topic | Decision |
|---|---|
| Stack | Python 3.12 **FastAPI** backend (pyavd/ANTA in-process) + **React/TypeScript** SPA; PostgreSQL; docker-compose on the Mac mini |
| Reaching lab hosts | **SSH + remote agent**: the app SSHes to the lab host, drives containerlab there, and runs deploy/ANTA in a helper container *on the lab host* (cEOS mgmt IPs are local there) |
| Execution targets | Linux host over SSH (local LAN or cloud VM) — 90 %; **ACT** in stage 2 (same pipeline, different "executor") |
| CloudVision | CVaaS **and** on-prem, selectable per lab |
| Users | single admin, LAN only, encrypted secret store; multi-user later |
| Versions | pin AVD 6.4.x / ANTA 1.10.x per app release; cEOS version chosen per lab |

## 2. Principles

1. **AVD is the source of truth for the lab.** The app never invents its own network model; it edits AVD group_vars (schema-validated with `pyavd.validate_inputs`) and derives everything else (clab topology, ANTA inventory, NetBox objects, CV tags) from AVD's outputs (`structured_configs`, `topology.csv`, `metadata`).
2. **Every action is a job** with streamed logs, idempotent re-runs and artefacts (files) — the UI is a job runner with forms on top.
3. **Thin remote footprint.** A lab host needs only Docker + SSH. containerlab and our helper image are pulled on demand; nothing is installed system-wide unless the user chooses "native install".
4. **Files first.** Each lab is a directory in the AVD layout (§0 of `04-lab-playbook.md`), git-initialised, so anything the UI does is also usable from a terminal or a devcontainer.
5. **Secrets never leave the Mac mini in plain text.** SSH keys, arista.com token, CV tokens, NetBox token in an encrypted store (Fernet, key from env/keychain); remote hosts only receive short-lived material (CV onboarding token file, per-run env).

## 3. System architecture

```
Mac mini (docker-compose)                                   Lab host (Linux, Docker, SSH)   [or ACT]
┌─────────────────────────────────────────────┐             ┌────────────────────────────────────────┐
│ web (React SPA, nginx)                      │             │ containerlab  (installed or run as      │
│ api (FastAPI)  ──── jobs ────► worker (arq) │──SSH/SFTP──►│   `ghcr.io/srl-labs/clab` container)    │
│   pyavd 6.4 · anta 1.10 · ansible-core      │             │ cEOS containers  (clab mgmt bridge)     │
│   asyncssh · eos-downloader · pynetbox      │             │ avdlab-agent container (our image):     │
│ postgres  (labs, jobs, hosts, secrets)      │◄─rsync/log──│   ansible+pyavd+anta, runs deploy/NRFU  │
│ volume: /labs/<lab>/  (AVD dirs, git)       │             │   against local eAPI; dnsmasq for ZTP;  │
│ optional: netbox, cvp-mock                  │             │   clients (alpine/netshoot), trafgen    │
└─────────────────────────────────────────────┘             └────────────────────────────────────────┘
```

**Backend packages** (`backend/avdlab/`):

| Package | Responsibility |
|---|---|
| `core/` | settings, DB (SQLAlchemy 2 + Alembic), secret store, auth (single admin, session cookie) |
| `hosts/` | lab-host registry, SSH connection pool (asyncssh), capability probe (docker, clab, kernel, CPU/RAM, images present), bootstrap (Module 1) |
| `labs/` | lab directory lifecycle, AVD inventory editor model, `pyavd` build, git commits, artefact index |
| `topology/` | AVD outputs → containerlab YAML (`avd2clab.py` grown into a module), ACT YAML, clients & traffic nodes, DHCP/ZTP overlay |
| `images/` | eos-downloader wrapper, image catalogue, transfer/import on remote hosts |
| `executors/` | `ClabExecutor` (SSH), `ActExecutor` (stage 2) — same interface: `deploy`, `destroy`, `status`, `exec`, `push_files`, `pull_files` |
| `validate/` | ANTA runs via agent, report ingestion (JSON → DB), diff between runs |
| `cloudvision/` | cv_deploy wrapper (pyavd `_cv` workflow), token handling, device onboarding status |
| `netbox/` | pynetbox sync (Module 6) |
| `jobs/` | arq worker, job model, log streaming (WebSocket), artefacts |

**Job engine:** `arq` (Redis) or a Postgres-backed queue; each job = `(lab, action, params)`, runs a Python coroutine
that yields log lines; the API stores lines and pushes them over a WebSocket; UI shows a live terminal per job.
Jobs are re-runnable and carry an artefact list (paths under `/labs/<lab>/…`).

**Remote agent:** *not a daemon.* It is a container image `ghcr.io/<us>/avdlab-agent:<avd-ver>` containing
ansible-core + arista.avd + pyavd + anta + dnsmasq + small tools. The worker `docker run --rm --network <clab-mgmt-net>
-v /opt/avdlab/<lab>:/lab avdlab-agent <command>` over SSH, so eAPI/ANTA traffic never has to leave the lab host.
Files are synced with rsync over SSH (`/labs/<lab>` ⇄ `/opt/avdlab/<lab>`); only `intended/`, `anta/`, `clab/`
go over; secrets are injected as env for the single run.

**Why not run Ansible on the Mac mini and reach eAPI remotely?** The clab management bridge is host-local; port-forwards per node are brittle and ANTA identity is `(host, port)`. Running the agent on the host also makes "cloud VM" and "local VM" identical.

## 4. Data model (Postgres)

- `host` — name, address, ssh_user, key_ref, kind (`linux`|`act`), capabilities JSON (docker/clab versions, images), last_probe.
- `image` — vendor/format/version (cEOS-lab 4.34.2F), sha, local path on Mac mini, `host_image` rows for presence per host.
- `lab` — name, slug, path, template (`single-dc-l3ls`…), avd_version, ceos_version, host_id, target (`clab`|`act`), state (`draft`|`built`|`deployed`|`validated`|`destroyed`), settings JSON (mgmt subnet, credentials, CV target, serial scheme, tags).
- `lab_revision` — git commit + summary + who/when (every UI edit commits).
- `job` — lab_id, type, status, started/ended, params JSON, log path, artefacts JSON, exit info.
- `validation_run` — job_id, totals by status, JSON report; `validation_result` rows for filtering/diffing.
- `secret` — name, kind (`ssh_key`, `arista_token`, `cv_token`, `netbox_token`, `device_password`), encrypted blob.
- `cv_target` — name, kind (`cvaas`|`onprem`), server URL, token secret_ref, verify_certs.

## 5. Module specifications

### Module 1 — Host & toolchain provisioning ("few clicks to a ready host")

**A. Remote host bootstrap** (target: Ubuntu 22.04/24.04, Debian 12, Rocky 9; cloud VM or local box)

1. Add host (address, user, SSH key from store or generated) → *Probe* job: OS, kernel, CPU/RAM/disk, Docker present?, containerlab present?, sysctls (`fs.inotify.max_user_instances`, `net.ipv4.ip_forward`, `net.bridge.bridge-nf-call-iptables`), user in `docker` group, `vm.max_map_count`.
2. *Bootstrap* job (idempotent shell steps, each logged): install Docker (get.docker.com), install containerlab (`curl -sL https://containerlab.dev/setup | sudo -E bash -s "all"` or the `.deb`/`.rpm`), set sysctls for many cEOS (`fs.inotify.max_user_instances=64000`, `fs.file-max`), create `/opt/avdlab`, pull `avdlab-agent` image, optional native toolchain (`pipx install "pyavd[ansible]==6.4.0" anta==1.10.0`, `ansible-galaxy collection install arista.avd:==6.4.0`) for people who want a shell there too.
3. *Verify* job: `containerlab version`, `docker run --rm avdlab-agent anta --version`, a 2-node smoke lab (optional).

Implementation: a `Recipe` = ordered list of `Step(name, check_cmd, apply_cmd, needs_sudo)`; runner executes `check` → skip if OK → `apply` → re-check. Same recipe engine reused for ZTP/DHCP setup in Module 3.

**B. Devcontainer generator** — for the Mac mini itself or any laptop:
- Writes `.devcontainer/devcontainer.json` + `Dockerfile` (base `ghcr.io/aristanetworks/avd/universal:python3.11-avd-v6.4.0`, plus containerlab CLI, eos-downloader, our CLI), `requirements.txt`, `ansible.cfg`, VS Code extensions (Ansible, YAML with AVD schemas), and a `Makefile` (`build/deploy/validate/clab-up/clab-down`).
- Option "lab host = this devcontainer" (Docker-in-Docker for local containerlab) vs "lab host = remote" (only SSH client needed).
- Output: a downloadable zip or written into a chosen lab directory; one click "open in VS Code" link (`vscode://vscode-remote/...`).

Acceptance: fresh Ubuntu VM → green checkmarks in < 5 min; devcontainer builds and `ansible-playbook build.yml` runs on the example.

### Module 2 — Topology builder, image management, deploy, CloudVision

**Templates:** ship the AVD examples (`single-dc-l3ls`, `dual-dc-l3ls`, `single-dc-multipod-l3ls`, `l2ls-fabric`, `campus-fabric`, `isis-ldp-ipvpn`, `single-dc-l3ls-ipv6`) as *lab templates* = AVD group_vars + a template descriptor (`template.yml`: knobs exposed to the UI).

**Pre-deployment knobs (UI form → group_vars edits):** lab name/prefix; number of spines, leaf pairs, L2 leafs per pair; node type per group; pools (loopback, VTEP, uplink, MLAG, mgmt subnet/gateway); AS numbering scheme; cEOS version; credentials; tenants/VRFs/VLANs (table editor); connected endpoints (table editor with `port_profiles`); device tags & CV topology hints (`generate_cv_tags`); serial-number scheme; extra `custom_structured_configuration_*` (YAML editor with schema validation); `avd_design_future` toggles. Each save = `pyavd.validate_inputs` → errors shown inline → git commit.

**Build:** in-process pyavd (`get_avd_facts` → `get_device_structured_config` → `get_device_config`/`get_device_doc` → `get_fabric_documentation` → `get_device_test_catalog`), writing the standard AVD directories so the lab is Ansible-compatible. Show diffs vs previous build (configs & structured configs).

**Topology:** `topology/clab.py` (from `avd2clab.py`): dedup links from `topology.csv`, per-node `EosIntfMapping.json`, `mgmt-ipv4` from `mgmt_ip`, startup-config = intended config, per-node `binds` for `ceos-config` (see serials), endpoints as `linux` nodes (Module 5 upgrades them), optional extra "external" nodes (DHCP, CVP-mock, traffic gen) from a side-car descriptor. Render the graph in the UI (React Flow) with editable link table before deploy. The name/prefix policy makes container names == hostnames.

**Images:** `images/` uses `eos-downloader` (`ardl get eos --format cEOS --version <v> --import-docker`) with the arista.com token from the secret store; catalogue of versions (`ardl info versions --format cEOS`); "ensure image on host" = check `docker image inspect` over SSH → if missing, `docker save | ssh docker load` from the Mac mini cache (or run `ardl` on the host with a one-shot token env). Images cached under `/images/` on the Mac mini.

**Deploy:** `ClabExecutor.deploy` = rsync lab → `containerlab deploy -t clab/topology.clab.yml --reconfigure` (via `docker run ... ghcr.io/srl-labs/clab` if not installed natively) → wait for eAPI readiness (agent polls `show version` per node) → optional `deploy.yml` (only needed after edits; first boot uses startup-config) → `validate.yml` (ANTA via agent) → ingest report. Destroy, restart, "redeploy changed nodes only" (`containerlab deploy --reconfigure` with `enforce-startup-config`).

**Unique serial numbers:** cEOS reads `/mnt/flash/ceos-config` (`SERIALNUMBER=…`, and `SYSTEMMACADDR=…` — verify on the chosen EOS version); we generate one file per node (scheme: `<LABPREFIX><4-digit node id>`, e.g. `AVDL0001`, or user-editable) and bind it (`ceos-config:/mnt/flash/ceos-config:ro`, or containerlab `extras.ceos-copy-to-flash`). The same serial is written into AVD `serial_number` per node so `cv_deploy` matches devices by serial and docs/NetBox agree.

**CloudVision:** per-lab `cv_target` (CVaaS regional URL or on-prem host). AVD side: `cv_settings` (onprem cluster or `cvaas.clusters[].region`) + `cv_tags`. Flow: (1) build renders `daemon TerminAttr` with `-cvauth=token,/tmp/token` (or `token-secure` for CVaaS); (2) UI asks for the onboarding token (generated in CV UI — interactive entry, stored encrypted, expiry tracked) and the service-account token for `cv_deploy`; (3) agent copies the onboarding token to each container's `/tmp/token` (`docker cp`) and restarts TerminAttr; (4) `cv_deploy` job with `cv_server`, `cv_token`, `cv_verify_certs`, `cv_submit_workspace`, `cv_run_change_control` toggles, streaming workspace/change-control state; (5) status panel shows per-device streaming/onboarded/provisioned. Serial uniqueness is what makes this repeatable across lab rebuilds.

**Metadata:** device tags (`generate_cv_tags.device_tags/interface_tags`, `cv_tags_topology_type`), rack, `pod_name`/`dc_name`, IP ranges (pools table), `validation_profiles`, tenant tags — all real AVD keys so the metadata survives into `structured_configs.metadata` and is reusable by ANTA/CV/NetBox.

### Module 3 — DHCP + ZTP variant

- Topology overlay: nodes boot **without** `startup-config` (cEOS then runs ZTP: DHCP on Ma0, downloads bootfile); a `dnsmasq` node (our agent image) on the clab mgmt network hands out fixed leases by MAC (containerlab lets us pin `mac-address` per node or we read them post-deploy) with option 67 `bootfile-name` pointing to an HTTP server (same container) that serves `intended/configs/<host>.cfg` (or a CV onboarding script that installs the TerminAttr token → CV ZTP-as-a-Service). The clab `mgmt` network is set to `external-access: false`/DHCP-free so only our dnsmasq answers.
- AVD side: unchanged; for CV-driven ZTP the bootstrap script from CVaaS ("Onboard devices → ZTP script") is served instead of the full config; `cv_deploy` then pushes the config as a workspace.
- Verify: cEOS honours ZTP when `/mnt/flash/startup-config` is absent (containerlab sets `SKIP_ZEROTOUCH_BARRIER_IN_SYSDBINIT=1`, not "skip ZTP" — to be tested with the chosen EOS version; fallback is `zerotouch` re-enable in a minimal startup config).
- UI: lease table, ZTP progress per device (DHCP → bootfile fetched → config applied → eAPI up), ANTA `VerifyZeroTouch`.

### Module 4 — MSS (to be specified)

Placeholder: MSS (Multi-Domain Segmentation Service) labs need CloudVision + `router_segment_security` / `mac_security`-style keys (all in the EOS Config schema: `router_segment_security`, `traffic_policies`, `ip_access_lists`, `mac_access_lists`, `address_locking`) plus clients that generate east-west traffic (Module 5). Design later; keep the topology builder's "extra structured config per node" and "client nodes" generic enough.

### Module 5 — Clients & traffic generation

- Client node kinds: `linux` (netshoot/alpine) with a generated setup script (VLAN sub-interfaces, bond/LACP, IP from `network_services` SVI subnets, default gateway = `ip_address_virtual`), `iperf3` servers/clients, optional `ostinato`/`trex` node for line-rate, and a cEOS "server" node when we need LACP behaviour that matches Arista's examples.
- The topology builder derives client placement from `connected_endpoints` (endpoint name → node, `endpoint_ports` → ethN, `vlans`/`native_vlan` → sub-interfaces). Traffic profiles (pairs, bandwidth, duration) are jobs run through the agent; results (iperf JSON) stored per run and correlated with ANTA (`VerifyInterfaceUtilization`, EVPN type-2 routes for client MACs/IPs → auto-generated `VerifyEVPNType2Route` inputs).

### Module 6 — NetBox

- Optional NetBox container in the compose stack (or external URL + token). Sync job (pynetbox): sites/locations from `dc_name`/`pod_name`/`rack`; device roles from node types; device types from `platform` (cEOSLab → a "cEOS-lab" type; real platforms via the community devicetype library); devices with serials, interfaces from `structured_configs` (`ethernet_interfaces`, `port_channel_interfaces`, `loopback_interfaces`, `vlan_interfaces`, `management_interfaces`), cables from `topology.csv`, IPs/prefixes from `p2p-links.csv` + pools + SVIs, VLANs/VRFs from `network_services`, tags from CV tags. Direction: AVD → NetBox first; later a NetBox → AVD import for `connected_endpoints`/`network_services` (NetBox as design source), which AVD does not ship natively.

## 6. Cross-cutting

- **CLI parity:** `avdlab` CLI (Typer) exposing the same commands as the UI — useful in the devcontainer and for CI.
- **Observability:** every job writes `jobs/<id>/log.txt` + artefacts; a lab "timeline" view.
- **Testing:** unit tests on pure functions (topology derivation, serial scheme, validators) with the AVD example outputs as fixtures (we already proved `avd2clab.py` reproduces the shipped clab topology); integration tests need a real lab host (a Proxmox VM on your home lab is ideal as the CI runner).
- **Upgrades:** app release pins AVD/ANTA; a "migrate lab" job runs `pyavd.validate_inputs` on the new version and shows porting-guide hits.

## 7. Risks / things to verify early

| Item | Why it matters | How we test |
|---|---|---|
| `ceos-config` `SERIALNUMBER`/`SYSTEMMACADDR` honoured on cEOS-lab 4.34 | CV onboarding & NetBox rely on unique serials | boot one node with a bind, `show version` |
| ZTP with cEOS in containerlab (env `SKIP_ZEROTOUCH_BARRIER_IN_SYSDBINIT`) | Module 3 | node without startup-config + dnsmasq |
| TerminAttr from cEOS to CVaaS through the lab host's NAT (port 443, `token-secure`) | Module 2 | one node, `show daemon TerminAttr` |
| Running containerlab from the `ghcr.io/srl-labs/clab` container vs native install | thin-footprint principle | both paths in Module 1 recipe |
| cEOS density: ~50 nodes/host, RAM ≈ 1–1.5 GB each, inotify limits | sizing cloud VMs | probe + recommend |
| eos-downloader API changes (arista.com download API) | image module | pin version; fallback = manual upload of the `.tar.xz` |
| ACT topology upload API (login-gated docs) | stage 2 | check ACT API/CLI availability with Arista |

## 8. Repo layout & delivery plan

```
avdlab/
├── backend/   (FastAPI app, arq worker, alembic, tests)      ├── agent/  (Dockerfile for avdlab-agent)
├── frontend/  (Vite + React + TS + Tailwind, React Flow)     ├── templates/ (lab templates = AVD examples + template.yml)
├── cli/       (Typer)                                        ├── deploy/ (docker-compose.yml for the Mac mini, .env.example)
└── docs/      (this file, ADRs)
```

| Phase | Scope | Exit criterion |
|---|---|---|
| 0 (week 1) | repo scaffold, compose stack, auth, secret store, host registry, job engine with live logs | add a host, run a "probe" job from the UI |
| 1 | Module 1 A+B | fresh VM bootstrapped; devcontainer generated & builds |
| 2a | Module 2: templates, knob forms, pyavd build, diff view | build `single-dc-l3ls` from the UI |
| 2b | images + clab topology + deploy/destroy + ANTA ingest | green ANTA report on a remote host from one click |
| 2c | serials + CloudVision (CVaaS & on-prem), tags/metadata | devices visible & provisioned in CV |
| 3 | DHCP/ZTP overlay | ZTP progress panel green |
| 5 | clients & traffic (before 4, because MSS needs it) | iperf between two tenants' clients, EVPN type-2 verified |
| 6 | NetBox sync | lab fully mirrored in NetBox |
| 4 | MSS | spec to be written with you |
| S2 | ACT executor (`avd_digital_twin_mode`, ACT topology upload) | same lab deployed to ACT from the same UI |
