# avdlab — Phase 3 contract: clients & traffic, git coupling, running configs, custom labs, host monitoring, cEOS arm, web terminal, VS Code

Builds on CONTRACT*.md and the current code. Work is split: **backend A** (§1, §3, §7), **backend B** (§2, §4, §5, §6, §8),
**frontend** (all UI, coded to the exact shapes below). Conventions unchanged (jobs via `@job`, 202 `{job_id}`, secrets never
returned, fake SSH/docker in tests, light migrations via `core.db._ADDED_COLUMNS`, `settings.*` for images).

## 1. Module 5 — client devices & traffic generation (backend A)

**Client image**: `settings.host_image` default becomes `ghcr.io/srl-labs/network-multitool:latest` (has iperf3, iproute2, vlan, tcpdump, curl). Configurable per lab via knob `client_image` (string) on all templates.

**Client derivation** (`topology/clients.py`, used by `topology/clab.py`): every endpoint from `connected_endpoints`
(servers/firewalls/… in group_vars) becomes a `linux` node whose setup script is generated from its adapters and the tenants:
- adapter `port_channel` → `bond0` (802.3ad, members = the endpoint's ethN for that adapter; `lacp_rate fast` off), else the single ethN;
- `mode: access` → untagged on `vlans`; `mode: trunk` → one `802.1Q` sub-interface per VLAN in `vlans` (`<if>.<vid>`), native VLAN untagged;
- IP: for each VLAN that has an SVI with `ip_address_virtual` (or `ip_virtual_router_addresses`) in `tenants`, assign the endpoint an address in that subnet: `base = network.network_address + 100 + <endpoint index>` (index = position in the endpoint list, 0-based; skip broadcast/gateway); gateway = the SVI virtual IP; first assigned VLAN gets the default route. L2-only VLANs get no IP.
- script also sets `hostname`, disables IPv6 RA noise, starts `iperf3 -s -D` on every client, and writes `/root/avdlab-client.json` (the plan) for debugging.
- **Plan** is also returned structured: `ClientPlan = {name, image, links: [{eth, switch, switch_port}], bond: {name, members}|null, vlans: [{vid, iface, ip: "a.b.c.d/nn"|null, gateway: str|null, default_route: bool, tenant, vrf}]}`; `GET /labs/{id}/clients` → `[ClientPlan + {container_state, mgmt_ip}]` (runtime fields from `last_inspect` when deployed). Plans are persisted in `clab/clients.json` at build/topology time.

**Traffic jobs** (executor helpers in `executors/traffic.py`; all commands run on the host via SSH `docker exec <client> …`):
- `POST /labs/{id}/traffic/ping` body `{pairs: [{src, dst}]|null (null = full mesh over clients that have an IP), count: 3, size: 56, vlan?: int}` → 202 job `lab.traffic_ping`; result `{results: [{src, dst, target_ip, sent, received, loss_pct, rtt_avg_ms|null, ok}], matrix: {src: {dst: loss_pct}}}`.
- `POST /labs/{id}/traffic/iperf` body `{flows: [{src, dst, proto: tcp|udp, bandwidth: "100M"|null, duration: 10, parallel: 1, reverse: false}], sequential: true}` → 202 job `lab.traffic_iperf`; per flow: ensure `iperf3 -s -D` on dst (`pgrep iperf3 || iperf3 -s -D`), run `iperf3 -c <dst ip> -J -t <duration> [-u -b <bw>] [-P n] [-R]` on src, parse JSON: `{src, dst, proto, seconds, bits_per_second_sent, bits_per_second_received, retransmits|null, jitter_ms|null, lost_percent|null, ok, error|null}`. Store each run in a new table `traffic_runs` (id, lab_id, job_id, kind: ping|iperf, params, results JSON, created_at); `GET /labs/{id}/traffic/runs` → list newest first; `GET /labs/{id}/traffic/runs/{run_id}` → full.
- Preconditions: lab deployed/validated, clients present; 409 otherwise. Streams progress to the job log.

## 2. Git coupling (backend B)

Per-lab remote in `lab.settings["git"] = {url, branch: "main", auth: {kind: "none"|"token"|"ssh_key", secret_id}}`.
- `PUT /labs/{id}/git` body `{url, branch, auth_kind, secret_id|null, token|null (creates a Secret kind generic)}` → configures `origin` (GitPython `remote.set_url`, no credentials in the stored URL) and returns `{url, branch, auth_kind, has_secret}`; `DELETE /labs/{id}/git` removes it; `GET /labs/{id}/git` → `{configured, url, branch, auth_kind, has_secret, ahead, behind, last_sync: {at, direction, ok, message}|null}` (ahead/behind computed from the last fetch, not live).
- Jobs `lab.git_push` (`POST /labs/{id}/git/push` body `{force: false}`) and `lab.git_pull` (`POST /labs/{id}/git/pull`, refuses on local uncommitted changes — there should be none since every write commits; pulls with `--ff-only`, re-validates the lab with pyavd, resets state to `draft` if group_vars changed). Credentials: `token` → `https://x-access-token:<token>@…` injected only at run time via `GIT_ASKPASS` helper script (never stored in `.git/config`, never logged); `ssh_key` → temp key file 0600 + `GIT_SSH_COMMAND="ssh -i <f> -o StrictHostKeyChecking=accept-new"`; both cleaned in `finally`. GitHub and GitLab both work with token auth (`x-access-token` for GitHub, `oauth2` for GitLab — detect by host, default `x-access-token`).
- App-level import from git: `POST /labs/import/git` body `{name, url, branch, auth_kind, secret_id|null, token|null, host_id?}` → 202 job `lab.import_git`: clone into a new lab dir (template `custom`, see §4), validate, keep the remote configured.
- A generated `.gitignore` in every lab dir already excludes `anta/reports`; make sure `intended/` and `documentation/` are **committed** (they are useful in the remote) but `clab/ceos-config/` and any token files are ignored.

## 4. Custom labs (backend B)

- New template `custom` (`templates/custom/template.yml`, `generator: none`, no knobs except `ceos_version`, `client_image`, `cloudvision`, `dns_servers` are **not** applied — the user's group_vars rule). `POST /labs/import` multipart `file` (zip or tar.gz) + `name` (+ optional `host_id`): extract into a new lab dir; the archive must contain an Ansible inventory (`inventory.yml|.yaml|hosts.yml`) and `group_vars/`; if it has one top-level directory, strip it. Detect `fabric_name` from group_vars (first `fabric_name` found) and store in `lab.settings["fabric_name"]`; run `pyavd.validate_inputs` on the resolved hostvars → 422 with violations if invalid (but still create the lab as `draft` with `settings["import_warnings"]`, so the user can fix files in the editor — decide: create + warn). `intended/`/`documentation/` in the archive are ignored (we rebuild). Build/topology/deploy work unchanged; `labs/build.py` must take `fabric_name` from settings when the template is `custom`.
- `GET /labs/{id}/export` → zip download of the lab dir (excluding `.git`, `anta/reports`).

## 3. Running configs & snapshots (backend A)

- `GET /labs/{id}/nodes/{node}/running-config` → inline (25 s budget) `{node, config: str, fetched_at}` via SSH `docker exec <node> Cli -p 15 -c "show running-config"`; 409 if not deployed.
- `GET /labs/{id}/nodes/{node}/config-diff` → `{node, intended: str, running: str, diff: str (unified), identical: bool}`; normalise both sides before diffing: drop lines starting with `!` and the `end` line, strip trailing whitespace, ignore `! Startup-config last modified…` and `! device: …` banners.
- Job `lab.snapshot` (`POST /labs/{id}/snapshot` body `{commands: ["show running-config", "show version", "show lldp neighbors", "show ip bgp summary", "show bgp evpn summary", "show mlag", "show vxlan vni"]}`) → for every cEOS node run each command (`Cli -p 15 -c`) and write `snapshots/<YYYYmmdd-HHMMSS>/<node>/<cmd slug>.txt`; commit; `GET /labs/{id}/snapshots` → `[{id (dir name), created_at, nodes: n, commands: [..]}]`, `GET /labs/{id}/snapshots/{snap}/{node}/{cmd_slug}` → text.

## 7. Web terminal (backend A)

`WS /api/labs/{id}/nodes/{node}/terminal?shell=cli|bash&cols=&rows=` and `WS /api/hosts/{id}/terminal?cols=&rows=` (host shell).
Auth: session cookie (same check as the jobs stream; close 4401 otherwise). Backend: asyncssh connection to the host; open a PTY process:
node cli → `docker exec -it <node> Cli` (`bash` → `docker exec -it <node> bash`); host → `$SHELL -l`; `term_type="xterm-256color"`.
Bridge: binary frames both ways (client → stdin, stdout → client); JSON text frame `{"resize": {cols, rows}}` → `process.change_terminal_size`.
Close the process on disconnect; log session open/close (no content). `executors/terminal.py`. Sudo is not used; the SSH user must be in the `docker` group (bootstrap does that).

## 5. Host monitoring (backend B)

- `hosts/monitor.py`: a sampler task started with the app (`lifespan`) that every 20 s samples each host that has a deployed lab or was viewed in the last 10 min (`GET /hosts/{id}/metrics` marks it "watched"): one SSH session per host kept open while watched. Sample commands: `cat /proc/loadavg`, `head -1 /proc/stat` (compute CPU % between samples), `free -b`, `df -B1 --output=target,size,used /  /var/lib/docker`, `docker stats --no-stream --format '{{json .}}'` (per container CPU %, mem used/limit, net I/O), `nproc`.
  Keep an in-memory ring buffer per host (last 2 h at 20 s = 360 points) + persist every sample to a new table `host_metrics` (host_id, at, data JSON) pruned to 7 days.
- `GET /hosts/{id}/metrics?since=<iso>&step=<s>` → `{host_id, points: [{at, cpu_pct, load1, load5, load15, mem_total, mem_used, mem_available, disk: {"/": {size, used}, "/var/lib/docker": {...}}}], containers: {name: [{at, cpu_pct, mem_used, mem_limit}]} (last 2 h only), sampled_at, watched: bool}`.
- `GET /hosts/{id}/processes` → inline `{at, processes: [{pid, user, cpu_pct, mem_pct, rss, command}]}` from `ps -eo pid,user,%cpu,%mem,rss,args --sort=-%mem | head -40`.
- `GET /hosts/{id}/containers` → inline `docker ps --format json` → `[{name, image, status, state, labels: {avdlab.lab?}}]`.

## 6. cEOS arm + image formats (backend B)

- `Image` gets `arch` (`x86_64|arm64`, migration) derived from format (`cEOSarm` → arm64; `cEOS`, `cEOS64` → x86_64) and `docker_tag` `arista/ceos:<version>` for x86_64 and `arista/ceos:<version>-arm64` for arm.
- `GET /images/formats` returns `[{fmt, arch, label}]` for `cEOS`, `cEOS64`, `cEOSarm` (others filtered out); catalog endpoint accepts any of them.
- Deploy/push check host arch (`capabilities.os.arch`) vs image arch → clear 409/job error ("host is x86_64, image is arm64"); the lab knob `ceos_version` plus a new knob `ceos_arch` (`x86_64|arm64`, default x86_64) pick the tag written into the clab topology.

## 8. VS Code / containerlab extension (backend B)

`GET /labs/{id}/open-links` → `{remote_ssh: "vscode://vscode-remote/ssh-remote+<user>@<host>[:port]/opt/avdlab/labs/<slug>", clab_topology_path: "/opt/avdlab/labs/<slug>/clab/topology.clab.yml", remote_ssh_topology: "<remote_ssh link>/clab/topology.clab.yml", devcontainer: "vscode://ms-vscode-remote.remote-containers/cloneInVolume?url=<git url>"|null, ssh_command: "ssh <user>@<host>", hints: [str]}` plus `POST /labs/{id}/sync` → 202 job `lab.sync` that rsyncs the lab to the host without deploying (so "Open in VS Code" always shows current files). The containerlab VS Code extension (`srl-labs.vscode-containerlab`) discovers `*.clab.yml` in the opened workspace, so opening the remote folder is enough; include that in `hints`, plus "install extensions ms-vscode-remote.remote-ssh and srl-labs.vscode-containerlab".

## Frontend (coded to the shapes above)

- **Lab → Clients tab**: plan table (client, image, links, bond, VLAN/IP/gateway rows, container state); **Ping mesh** button → job → matrix heat table (green/amber/red by loss); **iperf** form (flow rows src/dst/proto/bandwidth/duration/parallel/reverse; add/remove) → job → results table with Mbps sent/received, retransmits/jitter/loss; runs history list.
- **Lab → Devices tab** (new): node list; select node → Running config viewer with "Diff vs intended" toggle (unified diff rendered with +/- colouring); **Terminal** button opens an xterm.js panel (WebSocket binary; resize on fit; CLI/bash switch); **Snapshot** button + snapshots list drill-down.
- **Lab → Git panel** (in Settings or its own tab): remote form (url, branch, auth kind, token input write-only or key secret select), Push/Pull buttons → jobs, ahead/behind, last sync; **Open in VS Code** section with the links (buttons that open the `vscode://` URLs, copy buttons, the hints) and a "Sync files to host" button.
- **Labs list**: "Import" menu → upload archive (multipart) or clone from git (form) → creates lab / job; lab row "Export zip".
- **Hosts → host detail page** (`/hosts/:id`): live charts (recharts, 2 h window, poll every 20 s): CPU %, load, memory used/available, disk per mount, per-container memory (top 10) — dark-mode aware; processes table (auto-refresh 30 s); containers table; Terminal button (host shell).
- **Images**: format select (`cEOS` / `cEOS64` / `cEOSarm`) with arch badge; host presence shows arch mismatch warning; lab knob `ceos_arch`.
- MSW mocks for all of it (including a fake terminal WS echoing input), vitest tests: client plan table, ping matrix colouring, diff rendering, metrics chart data mapping, git panel state.

## Tests (backends)

Fake SSH scripted per feature (client setup scripts generated deterministically — snapshot test on the single-dc-l3ls example: two clients, bond0, VLAN 11/12/21/22 sub-interfaces with 10.10.11.100/24 etc.; ping/iperf parsing from canned outputs; running-config diff normalisation; terminal bridge unit-tested with a fake PTY process; git push/pull with a local bare repo as remote (real GitPython, no network); import zip → lab; host metrics parsing from canned `/proc`/`free`/`docker stats` output; formats/arch mapping; open-links). Keep all existing tests green.
