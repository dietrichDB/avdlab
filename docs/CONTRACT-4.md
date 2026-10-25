# avdlab — Phase 4 contract: Module 3 (DHCP/ZTP), Module 6 (NetBox), Module 4 (MSS foundation), ACT executor

Conventions as before. Each backend workstream ships its own router module (`<pkg>/api.py` or `labs/<name>_api.py`) registered in
`main.FEATURE_MODULES` (append your own line; re-read `main.py` before editing) and its own tests; never edit another workstream's files.
Frontend is built to the shapes below.

## A. Module 3 — DHCP + ZTP provisioning (backend A)

Knob `provisioning` (all templates, `type: object`): `{mode: "startup-config"|"ztp", ztp_source: "intended"|"cloudvision", ztp_server_ip: null|"a.b.c.d"}`.
Default `startup-config` (current behaviour). In `ztp` mode:
- **Topology** (`topology/clab.py`): cEOS nodes get **no** `startup-config` (they boot into ZTP; containerlab already sets the ZTP
  barrier env). Add node `ztp` (`kind: linux`, image `settings.agent_image`, `mgmt-ipv4` = `ztp_server_ip` or the first free IP after
  the gateway in the mgmt subnet, `binds: [ztp/:/ztp]`, `exec` starting `/ztp/run.sh` which runs `dnsmasq --conf-file=/ztp/dnsmasq.conf
  --no-daemon &` and `python3 -m http.server 80 --directory /ztp/www`), labels `avdlab.role=ztp`. `clab/ztp/` is generated at build:
  `www/<node>.cfg` (copies of `intended/configs`), `www/bootstrap-<node>.sh` for `ztp_source: cloudvision` (a shell script that fetches the
  CVaaS/on-prem bootstrap script — the target's stored **ZTP bootstrap script** if present, see below — else writes the onboarding token to
  the cluster's `token_file` and applies the intended config), `dnsmasq.conf` skeleton (interface eth0, `dhcp-range=<subnet>,static`,
  `dhcp-option=3,<gw>`, `dhcp-option=6,<dns>`, `log-dhcp`, `dhcp-leasefile=/ztp/leases`), and `run.sh`.
- **Deploy** (`executors/clab.py`, `ztp` branch): after `containerlab deploy` + inspect, read each cEOS container's eth0 MAC
  (`docker inspect -f '{{.NetworkSettings.Networks.<mgmt net>.MacAddress}}'`), write `dhcp-host=<mac>,<mgmt-ip>,set:<node>` and
  `dhcp-option=tag:<node>,67,"http://<ztp ip>/<node>.cfg"` (or `bootstrap-<node>.sh`) lines into `dnsmasq.conf` on the host, restart
  dnsmasq inside the ztp container (`docker exec ztp sh /ztp/run.sh restart`), then poll: leases file (DHCP done), http.server access
  log (bootfile fetched), eAPI up (as today). Timeouts: ZTP on cEOS retries DHCP every ~10 s; overall wait = `wait_timeout`.
- CV target gets an optional **ZTP bootstrap script** (write-only secret, kind generic, `has_ztp_script` in the target response, field
  `ztp_script` on POST/PATCH) — the script CVaaS offers under Devices → Onboard → ZTP.
- `GET /labs/{id}/ztp` → `{mode, server_ip, nodes: [{name, mac, ip, lease: bool, bootfile_fetched: bool, eapi: bool, bootfile}]}` (inline,
  from the last deploy's poll + a fresh read of leases/log with a 15 s budget). `VerifyZeroTouch` stays in the ANTA catalog (ZTP is
  disabled once the config is applied).
- Tests: topology in ztp mode (no startup-config, ztp node present, dnsmasq skeleton), deploy branch with fake SSH (MAC read → dhcp-host lines,
  restart, lease/bootfile/eAPI progression), `GET /ztp`, CV target ztp_script write-only.
- Frontend: Settings → provisioning sub-form; Runtime tab → "ZTP" panel (per-node DHCP ✓ → bootfile ✓ → eAPI ✓ timeline) when mode = ztp;
  CloudVision target modal gets the ZTP script textarea.

## B. Module 6 — NetBox (backend B)

- Table `netbox_targets` (id, name, url, token_secret_id, verify_certs, created_at); `GET/POST /netbox/targets`, `GET/PATCH/DELETE
  /netbox/targets/{id}` (token write-only, `has_token`), `POST /netbox/targets/{id}/test` → `{ok, version, error}` (pynetbox `status()`;
  isolate in `netbox/service.py` for fakes). `labs.netbox_target_id` column (light migration), settable via `PATCH /labs/{id}`.
- Job `lab.netbox_sync` (`POST /labs/{id}/netbox/sync` body `{delete_stale: false, dry_run: false, include_clients: true}` → 202), precondition
  built. Idempotent sync (lookup-by-name/serial, create or update) with pynetbox, everything tagged `avdlab` + `avdlab:<slug>`:
  manufacturer Arista; platform "Arista EOS"; device types per `platform` (`cEOSLab` → model `cEOS-lab`, slug `ceos-lab`; other AVD platform
  strings → model = platform); device roles from node type (spine/l3leaf/l2leaf/…, clients → `server`); site = `dc_name` or fabric name;
  location = `pod_name` if set; rack if `rack`; devices (name, serial from `metadata.serial_number`, role, type, site, status active,
  custom field-free); interfaces from structured configs (`ethernet_interfaces` → type `1000base-t` unless a speed says otherwise,
  `port_channel_interfaces` → `lag` with members, `loopback_interfaces`/`vlan_interfaces` → `virtual`, `management_interfaces` →
  `1000base-t` mgmt_only, descriptions, enabled = not shutdown, mode/untagged/tagged VLANs from `switchport`); VLANs (site-scoped, from
  `vlans`); VRFs (name, RD from `router_bgp.vrfs[].rd`); prefixes (pools from knobs + SVI subnets + P2P /31s from `p2p-links.csv`);
  IP addresses (interface IPs, `ip_address_virtual` marked role `anycast`, Loopback0 = `loopback`, management IP set as `primary_ip4`);
  cables from `topology.csv` (dedup; endpoints must exist); clients (from `clab/clients.json`) as devices with role server and their
  VLAN IPs when `include_clients`. `delete_stale` removes objects tagged `avdlab:<slug>` that were not touched in this run. Result
  `{created: {type: n}, updated: {..}, deleted: {..}, skipped: [str], netbox_url}` stored in `lab.settings["netbox_last_sync"]`;
  `GET /labs/{id}/netbox/status` → `{target, last_sync}`.
- Compose: `deploy/docker-compose.yml` gains profile `netbox` (netbox + postgres + redis + redis-cache, `netboxcommunity/netbox:v4.x`,
  env with a generated superuser token from `.env`), documented in README.
- Tests: a fake pynetbox (record calls; return objects with ids) — assert the object set produced for the single-dc-l3ls build (device
  count 8 + 2 clients, cable count 22, VLANs 11/12/21/22/3401/3402, VRFs VRF10/VRF11, prefixes incl. 10.10.11.0/24), idempotency (second run
  = updates only), dry_run touches nothing, API/tokens.
- Frontend: NetBox page (targets + test), lab NetBox tab (target select, sync form, result counts, "open in NetBox" link).

## C. Module 4 — MSS foundation (backend C)

Scope for this iteration (Dietrich will detail MSS later): make the lab able to express and verify **segmentation** on Arista EOS with
`router segment-security` (eos_cli_config_gen key `router_segment_security`: `enabled`, `policies[]{name, sequence_numbers[]{sequence,
application, action: forward|drop|redirect, log, stateless, next_hop}}`, `vrfs[]{name, segments[]{name, definition{interfaces[],
match_lists[]{address_family, prefix|covered_prefix_list}}, policies[]{from, policy}, fallback_policy}}`) plus supporting
`ip_access_lists`/`application_traffic_recognition` where needed.
- Knob `segmentation` (all templates, `type: yaml`, default `{}`): `{enabled: bool, applications: [{name, protocol: tcp|udp|icmp|any, ports: "80,443"|null}],
  policies: [{name, rules: [{application, action: forward|drop, log: bool}]}], vrfs: [{name, segments: [{name, prefixes: [cidr], interfaces: [str], policies: [{from, policy}], fallback: forward|drop}]}]}`.
  Rendering (`labs/mss.py`): into the fabric group's `custom_structured_configuration_router_segment_security` (+ `custom_structured_configuration_application_traffic_recognition`
  for the applications), but **only on nodes that carry the VRF** — use node-level `structured_config` on l3leaf nodes (like device tags) so spines
  stay untouched. Validate with pyavd in tests (the generated CLI must contain `router segment-security`).
- Verification job `lab.mss_verify` (`POST /labs/{id}/mss/verify` body `{cases: [{src, dst, application, expect: allow|deny}]|null}` → 202): for
  each case run the matching probe between clients (icmp → ping; tcp/udp → `nc -z -w2` / iperf3 for udp) and compare with `expect`; null cases =
  auto-derived from the policy matrix (every client pair × every application whose segments are known from the clients' VLAN prefixes); result
  `{cases: [{src, dst, application, expect, observed: allow|deny|error, pass}], passed, failed}` stored as a `traffic_runs` row kind `mss`.
- `GET /labs/{id}/mss` → `{enabled, applications, policies, vrfs, matrix: {vrf: {segment: {segment: policy}}}, rendered_on: [hostnames]}`.
- Tests: knob → CLI, matrix derivation, verify job with fake SSH, API.
- Frontend: Lab → MSS tab: application/policy/segment editors (form-based, writing the knob), a policy matrix view (segment × segment → policy/action),
  "Verify" → job → cases table pass/fail. Mark the tab "foundation — MSS design to be refined".

## D. ACT executor (backend D)

Stage-2 target: run the same lab on **Arista Cloud Test**. Model: a `Host` of kind `act` = the ACT lab's **tools server** (generic Ubuntu
node with public SSH) — bootstrap recipe `act-tools` (Docker + agent image only; no containerlab, no sysctls) — from which all switch mgmt
IPs are reachable. Everything else reuses the clab executor pieces (sync, agent container, ansible, ANTA, curl eAPI wait).
- Lab `target: act` (settable on create/PATCH; requires host kind act when deploying). Knob `act` (all templates, `type: object`):
  `{os_version: "4.33.1F", username: "cvpadmin", password: "cvp123!", internet_access: true, tools_server_ip: "<mgmt ip>"}`.
- Build in act mode runs pyavd with `digital_twin=True` (`get_avd_facts/get_device_structured_config/get_fabric_documentation` all take it)
  with `digital_twin: {environment: act, fabric: {act_os_version, act_username, act_password, act_internet_access, act_ensure_eapi_access: true}}`
  injected into the fabric group vars (see /home/claude/arista/notes/avd-workflow-roles-labs.md §8.2 — in twin mode platforms map to vEOS-lab, mgmt
  interface forced to Management1, `metadata.digital_twin` appears); the ACT topology YAML is written to `documentation/fabric/<fabric>-topology.yml`
  and **post-processed** (`executors/act.py`): append a tools-server node `{avdlab-tools: {node_type: tools-server, ip_addr: <tools_server_ip>/<len>, version: ubuntu-2204-lts}}`
  and, for each client from `clab/clients.json`, a generic node (`node_type: generic`, `version: ubuntu-2204-lts`) with links mirroring the client's
  `links` (ACT link format `connection: ["<node>:Ethernet1", "<switch>:EthernetN"]`); client interface names become `eth1..N` on ubuntu — document
  that the client setup script is reused via the tools server (`ssh` from tools server to the generic nodes is out of scope: generate the script and
  show it). Endpoint `GET /labs/{id}/act/topology` → the YAML (download) and `GET /labs/{id}/act` → `{prepared, topology_path, nodes: [...], hints}`.
- `ActExecutor`: `deploy` = precondition host kind act; sync lab to the tools server; **skip containerlab**; wait for eAPI on every switch mgmt IP
  from the tools server (`curl`), then run `ansible-playbook playbooks/deploy.yml` in the agent container (eAPI config replace — ACT boots vEOS
  with mgmt IPs only), then state deployed. `status` = eAPI probe; `validate` = same as clab; `destroy` = mark destroyed + log "destroy the ACT lab in the
  ACT UI"; `cv_onboard`/traffic/terminal: terminal for act nodes = `ssh <user>@<mgmt ip>` from the tools server (PTY), traffic = not supported (409 with
  reason) in this iteration.
- Tests: twin build produces the ACT YAML with tools server + clients appended; executor deploy with fake SSH (no containerlab commands issued);
  status; API.
- Frontend: lab target selector (clab/act) in wizard + settings; when act: ACT tab with the topology download, hints (upload in act.arista.com, then set
  the tools-server host), and the same Deploy/Validate buttons.

## E. Frontend (all of A–D)

Coded to the shapes above; MSW mocks + vitest tests (ZTP timeline states, NetBox sync result counts, MSS matrix rendering, ACT tab). Keep
existing pages working; run build/lint/vitest.
