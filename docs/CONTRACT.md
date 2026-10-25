# avdlab — implementation contract (Phase 0 + Module 1 + Module 2a)

Read `docs/10-app-architecture.md` (copied from the Project) for the why. This file is the *what* for the first build.

## Repo

```
avdlab/
├── backend/            FastAPI app `avdlab` (python 3.11), tests in backend/tests (pytest, asyncio_mode=auto)
│   └── avdlab/
│       ├── core/       config.py (Settings, env prefix AVDLAB_), db.py (models + engine), security.py (auth, SecretStore)
│       ├── jobs/       engine.py (job registry/runner/context)  ← DONE, do not redesign
│       ├── api/        auth.py, jobs.py, secrets.py            ← DONE
│       ├── hosts/      api.py (router), ssh.py, probe.py, recipes.py, devcontainer.py       (agent B)
│       ├── labs/       api.py, service.py (lab dirs + git), build.py (pyavd), templates.py    (agent C)
│       ├── topology/   api.py, clab.py (avd outputs → containerlab), models.py                (agent C)
│       └── images/     api.py, service.py (eos-downloader wrapper, catalogue)                 (agent C)
├── frontend/           Vite + React 18 + TypeScript + Tailwind; dev proxy /api → :8000        (agent D)
├── templates/          lab templates: <name>/template.yml + group_vars/ + inventory.yml + playbooks (agent C)
├── agent/              Dockerfile for avdlab-agent (ansible+pyavd+anta+dnsmasq)                (agent B)
├── deploy/             docker-compose.yml, Dockerfile.backend, .env.example                    (agent B)
└── cli/                (later)
```

Conventions: every feature package exposes `router = APIRouter(prefix="/<feature>", tags=[...])` in `<feature>/api.py`;
`avdlab.main` auto-includes `avdlab.hosts.api`, `avdlab.labs.api`, `avdlab.images.api`, `avdlab.topology.api` under `/api`.
Job types are registered at import time with `@job("<feature>.<verb>")` — put the `import` of the job module in `api.py` so
including the router registers the jobs. All endpoints require `Depends(current_user)`. Use `get_session` for DB.
Long work → submit a job and return `{"job_id": ...}` (HTTP 202). Never return secret plaintext from the API.

## Job engine (given)

`from avdlab.jobs.engine import job, runner, JobContext`. Inside a job: `ctx.log(line)`, `ctx.artefact(name, path)`,
`ctx.check_cancelled()`, `ctx.params`, `await ctx.get_host()`, `await ctx.get_lab()`, `ctx.session()` (AsyncSession ctx-manager),
`ctx.dir` (job scratch dir). Return a JSON-able dict as result. Submit: `await runner.submit("host.probe", host_id=..., params={})`.
Stream: `WS /api/jobs/{id}/stream` sends `{"line": str}` frames then `{"done": true}`.

## Settings (given) — `avdlab.core.config.settings`

`data_dir` (labs/, images/, jobs/ under it), `templates_dir`, `agent_image`, `clab_image`, `remote_root` (`/opt/avdlab`),
`avd_version`, `anta_version`.

## Agent B — hosts + Module 1

### SSH layer `hosts/ssh.py`
`class SSHClient` wrapping asyncssh: `async with SSHClient.connect(host_row, session) as ssh:`; resolves key/password from the
secret store; `await ssh.run(cmd, *, sudo=False, timeout=600, log=ctx.log) -> CompletedProcess(returncode, stdout, stderr)`;
streams stdout/stderr lines to `log` while running; `await ssh.put(local, remote)`, `await ssh.get(remote, local)`,
`await ssh.rsync_dir(local_dir, remote_dir, exclude=[...])` (use `tar | ssh` pipe if rsync is absent), `sudo` uses `sudo -n`.
Host key policy: TOFU stored under `<data_dir>/known_hosts`.

### API `hosts/api.py`
- `GET/POST /hosts`, `GET/PATCH/DELETE /hosts/{id}`; body: name, kind, address, ssh_port, ssh_user, `ssh_key_secret_id` | `password_secret_id`.
- `POST /hosts/keys/generate` → creates an ed25519 keypair, stores private key as Secret(kind=ssh_key, meta.public_key), returns `{secret_id, public_key}` so the user can paste it into `authorized_keys`.
- `POST /hosts/{id}/probe` → 202 job `host.probe`; `POST /hosts/{id}/bootstrap` (body: `{recipe: "clab-host", options: {native_toolchain: bool, clab_mode: "native"|"container"}}`) → job `host.bootstrap`; `POST /hosts/{id}/verify` → job `host.verify`.
- `GET /hosts/{id}/capabilities` → the JSON stored by probe.

### Probe `hosts/probe.py` — job `host.probe`
Collect (each as a shell command, tolerate failures): os-release, kernel, arch, nproc, mem (kB), disk free on `/` and `/var/lib/docker`,
docker version + whether user can run docker, containerlab version, sysctl values (`fs.inotify.max_user_instances`, `fs.inotify.max_user_watches`,
`net.ipv4.ip_forward`, `vm.max_map_count`, `fs.file-max`), present docker images matching `ceos|arista`, `remote_root` exists, python3 version, rsync present.
Store in `Host.capabilities` + `last_probe_at`. Result includes `recommendations: [str]` (e.g. "inotify too low for >10 cEOS").

### Recipes `hosts/recipes.py` — job `host.bootstrap`
`@dataclass Step(name, check: str, apply: str, sudo: bool=True, when: Callable|None)`; `Recipe(name, steps)`; runner: for each step
`check` (rc 0 = skip) → `apply` → re-check (fail if still not OK). Recipe `clab-host`:
1. apt/dnf base packages (curl, ca-certificates, rsync, git, python3, jq)
2. Docker CE via get.docker.com if missing; add user to docker group
3. containerlab: `native` → `curl -sL https://containerlab.dev/setup | sudo -E bash -s "all"`; `container` → pull `settings.clab_image` and install a wrapper `/usr/local/bin/containerlab` that runs the image with `--privileged --pid=host -v /var/run/docker.sock ... -v /var/run/netns -v /etc/hosts -v $PWD:$PWD -w $PWD` (mirror containerlab's documented container invocation)
4. sysctls in `/etc/sysctl.d/90-avdlab.conf` (`fs.inotify.max_user_instances=64000`, `fs.inotify.max_user_watches=1048576`, `net.ipv4.ip_forward=1`, `vm.max_map_count=262144`, `fs.file-max=2097152`) + `sysctl --system`
5. `mkdir -p /opt/avdlab && chown $USER`
6. pull `settings.agent_image`
7. optional native toolchain: pipx + `pipx install "pyavd[ansible]==<avd_version>"`, `pipx inject`/`pip` anta, `ansible-galaxy collection install arista.avd:==<avd_version>`
Support Debian/Ubuntu (apt) and RHEL-family (dnf). Every step logs `[skip]/[apply]/[ok]/[FAIL]`. Job `host.verify`: `containerlab version`, `docker run --rm <agent_image> python3 -c "import pyavd, anta"`, and re-probe.

### Devcontainer generator `hosts/devcontainer.py`
`POST /hosts/devcontainer` body `{mode: "local-clab"|"remote-host", avd_version?, include_vscode_extensions: bool}` → returns a zip (`application/zip`) containing
`.devcontainer/devcontainer.json`, `.devcontainer/Dockerfile` (FROM `settings.agent_image`, adds containerlab CLI + eos-downloader + docker CLI; `local-clab` adds the docker-in-docker feature), `ansible.cfg`, `requirements.txt`, `Makefile` (build/deploy/validate/clab-up/clab-down targets), `README.md`. Pure functions → unit-testable without SSH.

### Deploy `deploy/`
`docker-compose.yml`: `api` (build ../backend, mounts `avdlab-data:/data`, env from `.env`, port 8080 serving API + built SPA),
`db` (postgres 16, optional profile; default sqlite), `netbox` profile placeholder (comment). `Dockerfile.backend`: multi-stage — build frontend (node 22) then python 3.11 image with backend + `frontend/dist`. `.env.example` with all `AVDLAB_*`.
`agent/Dockerfile`: FROM `ghcr.io/aristanetworks/avd/universal:python3.11-avd-v6.4.0`, add `anta==1.10.0` if missing, dnsmasq, iproute2, curl, jq, eos-downloader.

### Tests (agent B)
Unit tests for recipes (step ordering/OS detection with a fake SSH runner), devcontainer generator (files present, JSON valid), probe parser
(feed canned outputs), API CRUD for hosts with the fake SSH injected (monkeypatch `SSHClient.connect`).

## Agent C — labs, templates, build, topology, images

### Templates `templates/<name>/`
Copy AVD examples from `/home/claude/arista/avd/ansible_collections/arista/avd/examples/{single-dc-l3ls,dual-dc-l3ls,single-dc-multipod-l3ls,l2ls-fabric}`
(group_vars, inventory.yml, build/deploy playbooks; strip `intended/`, `documentation/`, `clab/`, images, README). Add `template.yml`:
```yaml
name: single-dc-l3ls
title: Single DC L3LS (EVPN/VXLAN, 2 spines, 2 MLAG leaf pairs, 2 L2 leafs)
description: ...
fabric_name: FABRIC
knobs:                                   # rendered as a form; each knob maps to a JSON-pointer-ish path in a group_vars file
  - key: mgmt_subnet      ; type: cidr   ; default: 172.16.1.0/24 ; label: Management subnet ; help: "..."
  - key: mgmt_gateway     ; type: ipv4   ; default: 172.16.1.1
  - key: ceos_version     ; type: string ; default: 4.34.2F
  - key: device_password  ; type: secret ; default: arista
  - key: spine_count      ; type: int    ; default: 2 ; min: 1 ; max: 4
  - key: leaf_pairs       ; type: int    ; default: 2 ; min: 1 ; max: 8
  - key: l2leaf_per_pair  ; type: int    ; default: 1 ; min: 0 ; max: 2
  - key: pools            ; type: object ; default: {loopback: 10.255.0.0/27, vtep: 10.255.1.0/27, uplink: 10.255.255.0/26, mlag_peer: 10.255.1.64/27, mlag_l3: 10.255.1.96/27}
  - key: spine_asn        ; type: int    ; default: 65100
  - key: leaf_asn_base    ; type: int    ; default: 65101
  - key: serial_scheme    ; type: string ; default: "{PREFIX}{ID:04d}" ; help: PREFIX = upper(lab slug[:4])
  - key: tenants          ; type: yaml   ; default: (the example's tenants list)   # edited as YAML with pyavd validation
  - key: servers          ; type: yaml   ; default: (the example's servers list)
```
Knob application is done by a *generator* in `labs/templates.py`: `render_lab(template, knobs) -> dict[str, dict]` returning the full set of
group_vars files (`FABRIC.yml`, `DC1.yml`, `DC1_SPINES.yml`, ...) and `inventory.yml`. For the l3ls templates generate nodes/ids/mgmt IPs/ASNs
from the counts (host naming `<dc>-spine<n>`, `<dc>-leaf<n>a/b`, `<dc>-leaf<n>c`), set `platform: cEOSLab`, `management_eapi.enabled: true`,
`serial_number` per node from the scheme, `default_interfaces` consistent with the example (spine downlinks Ethernet1-8 → cap leaf count accordingly),
`eos_designs_documentation.topology_csv: true`. Keep it data-driven so other templates can add knobs without code where possible.

### Lab service `labs/service.py`
Lab dir = `settings.labs_dir/<slug>/` in the standard AVD layout (`inventory.yml`, `group_vars/`, `playbooks/`, `ansible.cfg`); `git init` on create, commit on every write
(`labs/service.py: write_files(lab, files, message)`); `read_files(lab)`; `history(lab)` from git log; `diff(lab, rev_a, rev_b)`.

### Build `labs/build.py` — job `lab.build`
Load inventory + group_vars into per-host vars exactly like Ansible (group inheritance parents→children, host vars last; reuse the resolver in
`/home/claude/arista/notes/02-avd-workflow-roles-labs.md` §6.3), then pyavd: `validate_inputs` (fail with violations listed) → `get_avd_facts` →
per host `get_device_structured_config` → `get_device_config`, `get_device_doc` → `get_fabric_documentation(topology_csv=True, p2p_links_csv=True)`
→ ANTA catalogs via `AVDFabricData` + `get_device_test_catalog(output_dir=lab/anta/avd_catalogs)`. Write to `intended/structured_configs/*.yml`,
`intended/configs/*.cfg`, `documentation/devices/*.md`, `documentation/fabric/*`. Commit. Result: `{hosts: [...], warnings: [...], changed_files: [...]}`.
Then call topology generation (below) as the last build step. Set `Lab.state = built`.
Run pyavd in a thread (`asyncio.to_thread`) — it is CPU-bound.

### Topology `topology/clab.py`
Port `/home/claude/arista/notes/avd2clab.py` into a module: `build_clab_topology(lab_dir, fabric_name, image, *, serials: dict[host,str], mgmt_subnet) -> ClabTopology`
(pydantic model) + `write(lab_dir/clab)`. Additions vs the script: per-node `ceos-config` file (`SERIALNUMBER=<serial>` from AVD `metadata.serial_number` /
node var, `SYSTEMMACADDR` optional) bound at `/mnt/flash/ceos-config:ro`; endpoint (`linux`) nodes get `image` from settings and a generated setup script;
`GET /topology/{lab_id}` returns nodes/links JSON for the UI graph (nodes: name, kind, mgmt_ip, role from `metadata`/node type; links: a, a_if, b, b_if).
Job `lab.topology` regenerates only the topology.

### Labs API `labs/api.py`
- `GET /labs/templates` → list of template descriptors (name, title, description, knobs with defaults).
- `POST /labs` body `{name, template, host_id?, ceos_version?, knobs: {...}}` → creates dir + files (validated with `pyavd.validate_inputs` on the rendered vars; return 422 with violations) → returns lab.
- `GET /labs`, `GET /labs/{id}` (includes state, host, last build job, knob values), `PATCH /labs/{id}` (knobs / host / ceos_version → re-render + validate + commit), `DELETE /labs/{id}` (only when not deployed).
- `GET /labs/{id}/files` (tree) and `GET /labs/{id}/files/{path}` (content), `PUT /labs/{id}/files/{path}` (raw edit of group_vars with validation; marks knobs as "custom" if a knob-managed file is edited by hand).
- `POST /labs/{id}/build` → 202 job `lab.build`; `GET /labs/{id}/history`; `GET /labs/{id}/diff?a=&b=`.
- `GET /labs/{id}/devices` → from structured configs: hostname, type, platform, mgmt_ip, serial, loopback0, vtep, bgp_as (for the UI table).

### Images `images/`
`GET /images/catalog?fmt=cEOS` → versions via eos-downloader (`eos_downloader` python API; cache 1 h; if no arista token → 409 with hint);
`POST /images/download` `{fmt, version}` → job `image.download` (runs `ardl get eos --format cEOS --version X --import-docker` with `ARISTA_TOKEN` from
secret named `arista_token`; stores file under `settings.images_dir`, docker tag `arista/ceos:<version>`); `GET /images`; `POST /images/{id}/push/{host_id}` →
job `image.push` (`docker save | ssh docker load`, or `ardl` on the host when `options.remote_download`); manual upload `POST /images/upload` (multipart .tar.xz).
Since Docker is not available in this build environment, isolate docker calls behind `images/docker.py` so tests can fake them.

### Tests (agent C)
Template render → pyavd build for `single-dc-l3ls` with default knobs must produce configs equal (modulo serials/passwords) to the AVD example's
`intended/configs`; knob changes (3 leaf pairs) validate; topology equality test as in `avd2clab.py`; API flow create → build (job) → devices → topology.

## Agent D — frontend

Vite + React 18 + TS + Tailwind + TanStack Query + react-router; `@xyflow/react` for the topology graph; `xterm.js` (or a simple monospace pane) for logs.
Pages: Login; Dashboard (hosts, labs, recent jobs); Hosts (list, add/edit, generate key modal showing public key, Probe/Bootstrap/Verify buttons → job drawer,
capabilities panel with recommendations); Devcontainer (form → downloads zip); Images (catalog, download button, presence per host); Labs (list; New-lab wizard:
template → knobs form generated from `knobs[]` types → host/image → create); Lab detail tabs: Overview (state, devices table), Settings (knobs), Files
(tree + Monaco/CodeMirror editor with save), Build (button, last build summary, diff), Topology (React Flow graph from `/topology/{id}` + link table), Jobs.
Global job drawer: any 202 opens a live log panel via WebSocket `/api/jobs/{id}/stream`. Auth: cookie session; 401 → redirect to login.
`npm run build` must produce `frontend/dist`. Keep it clean and fast; no design-system dependency beyond Tailwind.
Mock the API in dev via MSW or a tiny json-server so the UI can be built before the backend endpoints exist; the contract above is the schema.
