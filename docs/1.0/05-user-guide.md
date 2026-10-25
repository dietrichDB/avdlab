# 5. User guide

This chapter walks through the UI page by page and, for a lab, tab by tab. Roles matter: **viewers** see everything but
every button that changes something is refused by the server (403); **operators** can do everything on this page except
user administration, admin settings and deleting hosts; **admins** can do everything.

## 5.0 The typical workflow

| # | Where | Action | Result |
|---|---|---|---|
| 1 | Hosts | *Generate SSH key*, put the public key on the Linux box, *Add host*, *Probe*, *Bootstrap* (`clab-host`), *Verify* | host with Docker + containerlab, recommendations cleared |
| 2 | Images | *arista.com token…* → paste token; pick a version in the catalogue → *Download*; optionally *Push* to the host | `arista/ceos:<version>` in the image store |
| 3 | Labs → New lab | template `single-dc-l3ls`, keep or change the knobs, name it, pick the host and the image, *Create lab* | lab in state `draft` |
| 4 | Lab header | *Build* | configs, docs, ANTA catalogs, topology; state `built` |
| 5 | Lab header | *Deploy* | cEOS containers running, eAPI up; state `deployed` |
| 6 | Lab header | *Validate* | ANTA report; state `validated` when nothing failed |
| 7 | Topology / Devices / Clients | inspect, open terminals, ping, add test hosts | |
| 8 | Settings → change a knob → *Save* → *Build* → Runtime *Push config changes* | only changed switches get the new config, no restart | |
| 9 | CloudVision page + lab CloudVision tab (optional) | add a target, set the `cloudvision` knob, rebuild, *Onboard*, *Deploy to CloudVision* | devices streaming, workspace submitted |
| 10 | Lab header | *Destroy* (and *Delete* when no longer needed) | containers removed; state `destroyed` |

Every long action opens the **job drawer** with the live log; the drawer can be reopened from any job link.

## 5.1 Shell: navigation, top bar, footer

The dark sidebar groups the pages (from `Layout.tsx`):

| Group | Items (B = hidden in Beginner mode, A = admins only) |
|---|---|
| Overview | Dashboard |
| Provisioning | Hosts, Images, Labs, CloudVision, NetBox (B), Devcontainer (B) |
| Learn | Getting started, Learn AVD |
| Admin | Users (A), Admin settings (A), Jobs (B), Feedback, Changelog, About |

Under the logo: the **version badge** (a dot when the changelog has news, an *update available* pill when a newer release
is known). Bottom of the sidebar: the **learning mode** switch. Top bar: breadcrumbs, **GitHub** button (when
`github_url` is set), **Beginner/Advanced** toggle, **user menu** (Account, Admin settings for admins, Sign out). Footer:
version, Changelog, About, Feedback. A floating **Feedback** button sits bottom-right on every page. Every page and
every lab tab is wrapped in an error boundary: a rendering error stays local with *Try again* / *Reload*.

## 5.2 Sign-in, account requests

- **Sign in** with username and password; with MFA enabled a second step asks for the 6-digit code or a recovery code
  (`xxxxx-xxxxx`). A user flagged *must change password* (first sign-in with a temporary password, or a weak env
  password for the bootstrap admin) must choose a new one before anything else.
- **Request an account** (link on the sign-in page, when registration is enabled): username (3–32 characters: lowercase
  letters, digits, `.`, `_`, `-`), e-mail, full name, reason. The page then shows the status of the request (pending →
  approved / rejected). Limited to 5 requests per hour per IP. Disabled registration shows a contact address instead.

## 5.3 Dashboard

Welcome banner with *Start learning*, *New lab* and *GitHub*; the **intro video** (90-second captioned walkthrough; falls
back to a guided tour when the file is missing; hidden when `intro_video_url` is empty); tiles for Labs, Running labs,
Hosts online, Images; **Next steps** — a checklist that follows what exists (add a lab host → download a cEOS image →
create your first lab → deploy it → validate it with ANTA), each linking to the right page; the **Lab hosts** panel with
CPU, memory and disk bars and the container count per host (refreshed every 30 s). Advanced mode adds tables of hosts,
labs and recent jobs.

## 5.4 Getting started

`/getting-started` presents the seven stations of a lab — host, image, lab, build, deploy, validate, CloudVision — as a
journey: for each, *what it is*, *why it matters* and *how, in three moves*, a done-state taken from this server, and a
*Guided tour* button. Below: a card per menu item (with *advanced* and *admin* badges) and "five words you will meet
everywhere". Step 1 carries the beta note for Mac/Windows lab hosts.

## 5.5 Hosts

Table columns: Name (with a violet *platform · beta* badge for OrbStack/WSL hosts), Kind, SSH (`user@address:port`),
Auth (key / password / none), OS, Docker, containerlab, Recs (number of recommendations, or *ok*), Last probe, Actions.

| Control | Does |
|---|---|
| Generate SSH key | creates an ed25519 key pair (private key encrypted in the secret store), shows the public key and an install command |
| Add host / Edit | the host dialog: platform card (Linux / Mac (OrbStack VM) beta / Windows (WSL2) beta, with the setup steps), name, kind (*containerlab host* or *act*), address, SSH user and port, jump host (Mac/Windows only), SSH key or password, sudo password |
| Monitor | the host page: metrics charts, processes, containers (Stop/Kill), terminal |
| Probe | job `host.probe` |
| Bootstrap | dialog: recipe (`clab-host` or `act-tools`), containerlab mode (native / container wrapper), native toolchain checkbox → job `host.bootstrap` |
| Verify | job `host.verify` |
| Delete | removes the host (admins only) |
| click the name | Capabilities panel: System, Toolchain, sysctl, Recommendations |

Details, recipe steps and probe output: chapter 4.

## 5.6 Images

| Area | Controls |
|---|---|
| Header | **arista.com token…** — always reachable: paste (show/hide while typing), *Replace* (same secret, no duplicates), *Remove*. Stored as secret `arista_token`, never shown again; replacing it drops the cached catalogue. |
| Local images | Format, Arch, Version, Docker tag, Size, Added, hosts that have it; per host *Push* (or *Fetch* = download on the host with `ardl`); delete |
| Arista catalogue | format selector (`cEOS`, `cEOS64`, `cEOSarm`), version filter (e.g. `4.34`), *Download* per version (job `image.download`), *downloaded* marker. When arista.com rejects the token: *Replace token* / *Retry*. Without a token: 409 and a prompt to add one. Answers are cached for an hour. |
| Manual upload | image file (`.tar` / `.tar.xz`), format, version → stored and `docker import`ed locally when Docker is available |

## 5.7 Labs

The list shows Name, Slug, Template, State, Host, Target, cEOS, Last build, Updated, and *Export zip* per lab.

- **New lab** opens the wizard (5.8).
- **Import ▾ → Upload archive (.zip / .tar.gz)** — the archive must contain an Ansible inventory (`inventory.yml`,
  `inventory.yaml` or `hosts.yml`) and `group_vars/`; one top-level directory is stripped; `intended/`, `documentation/`,
  `clab/`, `anta/reports/` and `.git/` are ignored (they are rebuilt). The lab uses template `custom`. Invalid AVD inputs
  still create the lab (`draft`) with *import warnings* shown on the lab page.
- **Import ▾ → Clone from git** — repository URL (no credentials in the URL), branch, authentication (none / access token /
  SSH key secret), optional host → job `lab.import_git`.

## 5.8 New lab

Three steps: **1. Template**, **2. Knobs & target**, **3. Host & image**. Nothing is created until *Create lab*.

| Step | Contents |
|---|---|
| 1. Template | **Start from a sentence** card (AI: picks the template and fills the knobs, see chapter 6), or *…or pick a template yourself*: cards with title, tags, node count, notes and "What it teaches" with the AVD doc link |
| 2. Knobs & target | lab name (used for the slug, the containerlab topology name and serial numbers), target (*containerlab (clab)* / *Arista Cloud Test (act)*), the typed knob form with inline validation; *Design with AI* to refine |
| 3. Host & image | lab host (optional now, required to deploy; for target act only hosts of kind act), cEOS image picker (lists downloaded images; *Other…* for free text). Picking an arm64 host switches `ceos_arch` to `arm64`. Server-side violations (422) are listed. |

### Knobs of `single-dc-l3ls` (from `templates/single-dc-l3ls/template.yml`)

| Knob | Type | Default | Meaning |
|---|---|---|---|
| `dc_name` | string | `dc1` | DC name / hostname prefix: `<dc>-spine<n>`, `<dc>-leaf<n>a/b` (L3 MLAG pair), `<dc>-leaf<n>c/d` (L2 leafs); groups use `upper(dc)` |
| `mgmt_subnet` | cidr | `172.16.1.0/24` | management subnet, also the containerlab management network; spines get .11+, L3 leafs .101+, L2 leafs .151+ |
| `mgmt_gateway` | ipv4 | `172.16.1.1` | management gateway |
| `ceos_version` | string | `4.34.2F` | Docker image `arista/ceos:<version>` used in the containerlab topology |
| `ceos_arch` | string, choices `x86_64` / `arm64` | `x86_64` | `x86_64` → `arista/ceos:<version>` (cEOS/cEOS64), `arm64` → `arista/ceos:<version>-arm64` (cEOSarm); must match the host CPU |
| `device_password` | secret | `arista` | password of the `arista` local user (sha512) and of all BGP peer groups (type-7); `ansible_password` follows |
| `spine_count` | int 1–4 | `2` | with more than 2 spines the leaf uplinks use Ethernet1-4 and the MLAG peer-link Ethernet5-6; server ports on Ethernet3-6 shift up automatically (Ethernet5 → Ethernet7) |
| `leaf_pairs` | int 1–8 | `2` | L3 leaf MLAG pairs; spine downlinks extend beyond Ethernet1-8 automatically above 4 pairs |
| `l2leaf_per_pair` | int 0–2 | `1` | 0 = none, 1 = single L2 leaf (`<dc>-leaf<n>c`), 2 = an MLAG pair of L2 leafs (c/d) sharing a node group |
| `pools` | object of pools | loopback `10.255.0.0/27`, vtep `10.255.1.0/27`, uplink `10.255.255.0/26`, mlag_peer `10.255.1.64/27`, mlag_l3 `10.255.1.96/27` | loopback (Loopback0, spines then leafs), vtep (Loopback1), uplink (/31 P2P), mlag_peer (Vlan4094), mlag_l3 (Vlan4093); AVD pool syntax is validated |
| `spine_asn` | int | `65100` | spine ASN |
| `leaf_asn_base` | int | `65101` | first leaf-pair ASN; pair n gets `leaf_asn_base + n - 1` |
| `serial_scheme` | string | `{PREFIX}{ID:04d}` | Python format: PREFIX = upper(lab slug[:4]), ID = position in the inventory, also HOST, TYPE, SLUG; written to each node's `serial_number` and to the cEOS `ceos-config` file |
| `tenants` | yaml | from the example's `NETWORK_SERVICES` `tenants` | `tenants:` list of the AVD network services model, validated with pyavd |
| `servers` | yaml | from the example's `CONNECTED_ENDPOINTS` `servers` | `servers:` list; endpoints become linux containers in the topology |
| `dns_servers` | yaml | `["192.168.1.1"]` | resolvers of the switches (management VRF); use public resolvers (1.1.1.1, 8.8.8.8) when the lab must reach CVaaS |
| `cloudvision` | object | `{mode: none, region: auto, servers: [], disable_aaa: true, topology_hints: true, device_tags: {}}` | mode `none` / `cvaas` / `onprem`; cvaas → region (the AVD 6.4 list, e.g. `euwest-2`, `us-4`, `apnortheast-1`); onprem → servers (port 9910); `device_tags` applied to every device. Mode `none` strips the example's TerminAttr config. |
| `client_image` | string | empty | Docker image of the linux client containers; needs iperf3, iproute2 (bond/vlan) and ping; empty = `ghcr.io/srl-labs/network-multitool:latest` |
| `device_tags` | yaml | `{}` | CloudVision tags per device `{hostname: {label: value}}`, merged after the global tags (per-device wins); unknown hostnames are rejected |
| `provisioning` | object | `{mode: startup-config, ztp_source: intended, ztp_server_ip: null}` | `startup-config` (containerlab mounts intended configs) or `ztp` (cEOS boots into ZTP; a `ztp` node runs dnsmasq + HTTP; option 67 serves `<node>.cfg`, or with `ztp_source: cloudvision` a bootstrap script running the CloudVision target's ZTP script / writing the onboarding token); empty `ztp_server_ip` = highest free IP of the management subnet |
| `segmentation` | yaml | `{}` | MSS foundation: `router segment-security` with applications, policies and VRF segments; rendered only on the L3 leafs that carry each VRF |
| `act` | object | `{os_version: 4.33.1F, username: cvpadmin, password: cvp123!, internet_access: true, tools_server_ip: ""}` | used for target act: digital-twin build (vEOS-lab, Management1), ACT topology with a tools server and one ubuntu node per client; the ACT credentials are also added as a local user |

The static templates expose a subset (`mgmt_subnet`, `mgmt_gateway`, `ceos_version`, `ceos_arch`, `device_password`,
`serial_scheme`, mostly `dns_servers`, `cloudvision`, `client_image`, `device_tags`, `provisioning`, `segmentation`, `act`;
`single-dc-l3ls-ipv6` adds `mgmt_ipv6_subnet`); `custom` only the topology-shaping knobs.

## 5.9 A lab: header and tabs

The **header** shows name, state badge, slug, template and target, with *Build*, *Deploy*, *Validate*, *Destroy*
(confirmation), *Export zip* and *Delete* (only when not running). Which buttons are enabled depends on the state
(chapter 1 §1.4). The header *Deploy* uses `reconfigure=true, wait_eapi=true, wait_timeout=600`; the header *Validate*
uses extra fabric validation and skips `VerifyNTP` and `VerifyReloadCause`.

Tabs: Overview, Settings, Files, Build, Topology, Runtime, Devices, Clients, Validation, CloudVision, MSS (A), NetBox (A),
ACT (A, only for target act), Git & VS Code (A), Jobs. (A) = hidden in Beginner mode; *Show advanced* at the end of the
tab row switches mode.

### Overview

Lab facts (state, template, cEOS, host, created/updated), **Last build** (status, job link, host count, warnings, error)
and the **Devices** table from the structured configs: hostname, type, platform, mgmt IP, serial, Loopback0, VTEP, BGP AS,
CloudVision tags.

### Settings

A sticky bar at the top with **Save** (validates, re-renders, commits), **Reset**, an unsaved-changes counter and
**Design with AI**. Fields: **Target** (locked while the lab runs), **Host**, **cEOS image** (image picker; also sets
`ceos_arch`), **CloudVision target** (enabled only when the `cloudvision` knob mode is not `none`). Then the typed
**knob form**: structured sub-forms for `cloudvision`, `provisioning`, `act`, a device-tags editor, YAML editors for
`tenants`/`servers`, (i) help popovers in learning mode, and *custom* markers for knobs whose files were edited by hand.
Violations (422) are shown next to the knob. Saving a knob change of a built lab resets it to `draft`. Saving replaces
hand edits of knob-managed files.

### Files

A file tree of the lab directory and an editor. Editable: `inventory.yml`, `ansible.cfg`, `group_vars/`, `host_vars/`,
`playbooks/`, `anta/user_catalogs/`; read-only (generated): `intended/`, `documentation/`, `clab/`, `anta/`. Files marked
**knob-managed** (`inventory.yml`, `group_vars/`) are validated with pyavd before saving and mark the related knobs as
custom; YAML syntax is checked for every `.yml`. Each save is a commit; *Revert* discards unsaved edits. In learning mode
the config viewer offers click-to-explain (which AVD input produced a line, with the lab's real values).

### Build

**Push config changes** card (when the lab runs), **Last build** (status, job, duration, hosts, ANTA test count,
warnings, error, changed files linking to Files, previous builds) with *Build now*, a **Diff** between any two revisions
(from / to / swap) and the **History** of commits (rev, message, author, date, files, *diff* to the previous one).

### Topology

The fabric graph as AVD sees it (React Flow): spines, leafs, L2 leafs, endpoints, with a links table (P2P uplinks, MLAG
peer-links, endpoints — each a veth pair; A/B interfaces and the containerlab port names). Controls: filter, *Regenerate*
(job `lab.topology`, rebuilds `clab/` from the last build), **Add test host**, **Design with AI**, *Auto-layout*, *lock
layout* (positions are saved per lab in `clab/layout.json`).

**Edit mode** turns the graph into an editor of the AVD inputs: drag the switch end of a blue uplink onto another switch
to move it to a free port (`set_uplink`), *Add server* (name, type, attach to a leaf pair or single leaf, NIC, mode
access/trunk, VLANs, native VLAN, LACP port-channel for MLAG), right-click a server to remove it, *+ leaf pair*, *− last
leaf pair*, L2 leafs per pair and spine count (l3ls templates only). Changes queue as **pending operations**; a dry run
shows the changed files and the diff; *Apply* validates with pyavd, commits, sets a built lab back to `draft` and offers
*Build now* / *Later*. Click a node to inspect it (role, config, uplinks with *move…*, free ports — click a free port to
plug in a test host).

### Runtime

**Status** table from `containerlab inspect` plus an eAPI probe per node: node, kind, state, mgmt IP, image, eAPI ✓/✗,
with a summary (nodes, running, eAPI n/m). *Refresh* runs the inline status (≤ 20 s; a cached result is used when the
host does not answer); *Refresh (job)* runs `lab.status` as a job. **Deploy** panel: host, target, deployed-at, options
*reconfigure* (`--reconfigure`), *wait for eAPI on all cEOS nodes*, *timeout* (60–3600 s), and *Deploy* / *Redeploy*. In
ZTP mode a **ZTP panel** shows per node MAC, IP, DHCP lease, bootfile fetched, eAPI (polled during the deploy, refreshed
from the dnsmasq leases and HTTP log).

**Push config changes** (containerlab labs that are running): after a *Build*, the card lists the switches whose
intended config changed since they last received it (*changed*), and switches with no record (*not tracked yet* —
deployed before 0.10.1, or new). *Push N changed* sends only those, *Push all N* every switch; both run
`lab.push_configs`: an `eos_config` **config replace over eAPI** from the agent container — seconds, no container
restart, the fabric keeps running. Validate afterwards.

### Devices

The node list (switches and linux clients). Per switch: **Running** config (`show running-config`, fetched inline),
**Diff vs intended** (normalised unified diff; "Running config matches the intended config" or a *drift* badge), the
config explainer, and a **Terminal** (EOS `Cli` or `bash` through `docker exec`, over the SSH connection; operators and
admins). Linux clients open a bash terminal. **Snapshots**: pick nodes and commands (default `show running-config`,
`show version`, `show lldp neighbors`, `show ip bgp summary`, `show bgp evpn summary`, `show mlag`, `show vxlan vni`) →
job `lab.snapshot` stores the outputs under `snapshots/<timestamp>/<node>/` in the lab repo (committed) and lists them.

### Clients & test hosts

**Clients** are derived from AVD connected endpoints: each becomes a linux container whose plan shows links, bond
(802.3ad for port-channels), VLAN sub-interfaces for trunks, addresses (`.100 + index` in each SVI subnet), gateway (the
anycast SVI address) and state. Traffic tests (lab must be running):

| Test | Options | Result |
|---|---|---|
| Ping mesh | count (default 3), size (56), VLAN, all pairs or selected | loss matrix src ↓ / dst →, 0 % / partial / 100 % |
| iperf3 | flows (src, dst, proto tcp/udp, bandwidth, seconds, …), sequential | per flow sent / received / retransmits / jitter / loss |
| Runs | history of all ping, iperf and MSS runs with parameters and job | detail view |

**Test hosts** are hot-added without redeploying: *Add test host* (name, leaf, switch port, mode access/trunk, VLANs, IP
*auto* or manual, image) adds the endpoint to the AVD inputs, rebuilds only the affected switch(es), pushes their new
config, starts the container and connects it with `containerlab tools veth create`. Per test host: *ping*, *iperf*,
*exec*, *Open terminal*, remove. If a switch is not actually running (e.g. after a host reboot) the add is refused with
the name of the switch and a hint to Redeploy; a remove still completes with a warning. A hot-added port may not appear
as `EthernetN` on the switch until a full redeploy (the job result says `interface_visible: false`).

### Validation

*Run ANTA* with options: extra fabric validation, skip tests (default `VerifyNTP`, `VerifyReloadCause`; type
`VerifyXxx` + Enter), tags (e.g. `leaf`, `spine`), and *anta/user_catalogs* (include your own catalogs from the Files tab).
**Runs** lists every validation with totals; a run opens the **results table** (device, test, category, result, messages)
with status, device and text filters, and downloads (JSON, CSV, Markdown report).

**Security advisories (ANTA PSIRT, preview)** card: *Run advisory scan* (optionally only some advisories, e.g.
`SA117 SA140`; empty = all built-in) runs `anta psirt` with the dedicated ANTA 1.10 image against the running switches
(job `lab.psirt`) and shows device × advisory: **affected**, **mitigated** (by a configured control), **not affected**
(fixed release, feature not used) or **inconclusive**, with severity, vulnerability details, remediation plan and the
Markdown report. cEOS-lab reports its EOS version like hardware, so the lab is a faithful demo. Reports are kept under
`anta/psirt/` in the lab repo.

### CloudVision

Prerequisites: a CloudVision target (5.10), the `cloudvision` knob set to `cvaas` or `onprem` (rebuild after changing
it), the target bound in Settings, and public DNS for CVaaS (`dns_servers`).

| Card | Contents |
|---|---|
| Target | name, kind, server, verify-certs, token and onboarding-token status, *Test connection* |
| Onboarding | *Onboard* (job `lab.cv_onboard`, lab must be running): writes the onboarding token into every cEOS node at the token file the build rendered (via `docker exec -i`, plus a copy on flash), restarts TerminAttr, then polls TerminAttr and (with a service-account token) the CloudVision inventory. Table: node, serial, TerminAttr ✓/✗, streaming ✓/✗; TerminAttr log tails for nodes that do not stream. |
| Deploy to CloudVision | `arista.avd.cv_deploy` (job `lab.cv_deploy`, lab built or running) pushes the built configs and tags into a CloudVision workspace. Options below. |
| Last deploy | workspace (id, state, *open in CloudVision ↗*), change control, devices (exists on CloudVision, streaming), errors, collapsed CloudVision lint warnings |

Deploy options and their semantics:

| Option | Default | Meaning |
|---|---|---|
| submit workspace | on | unchecked: the workspace is only built, nothing is pushed |
| approve & execute the change control | off | a submitted workspace with changes always creates a change control; unchecked → it waits in **pending approval** (you approve and run it in CloudVision), checked → AVD approves and executes it (**completed**) |
| skip devices missing on CloudVision | on | devices not (yet) in the inventory are skipped instead of failing |
| strict tags (remove other tags) | off | tags not set by AVD are removed |
| Workspace name | empty | optional name |

Outcomes explained on the tab: **Deploy failed before a workspace was created** (job link, typical causes — the 15 s
pre-flight from the orchestrator turns an expired/rejected token or an unreachable server into a precise message before
the playbook runs), or **Workspace submitted, nothing to push** — no change control is created when the designed config
equals the running config, which is the normal answer right after a deploy with the same intended configs.

### MSS (advanced)

Segmentation (MSS foundation — design to be refined): edit **applications** (name, protocol tcp/udp/icmp/any, ports),
**policies** (rules: application → forward/drop, log) and **VRFs & segments** (prefixes, interfaces, "from → policy",
fallback); the **policy matrix** per VRF (preview of unsaved changes), **Rendered on** (which L3 leafs carry the VRF and
get `router segment-security`). **Verify** (job `lab.mss_verify`): auto-derived or explicit cases (src, dst,
application, expect allow/deny) probed between clients with ping / `nc` / iperf3; results pass/fail per case.

### NetBox (advanced)

Pick a **NetBox target** (bound to the lab), options *dry run — nothing written*, *delete stale objects tagged
avdlab:<slug>*, include clients; **Sync** (job `lab.netbox_sync`, lab must be built). Last sync: created / updated /
deleted / unchanged / skipped per object type and *Open in NetBox ↗*. The sync creates sites/locations/racks, devices with
their AVD serials (plus clients as role `server`), interfaces (LAGs with members, switchport mode and VLANs, VRFs,
management `mgmt_only`), VLANs, VRFs (with RD), prefixes (pools, SVI subnets, P2P /31s), IPs (anycast for SVI virtual
IPs and shared VTEPs, `loopback` role for Loopback0, management IP as primary IPv4) and cables, tagged `avdlab` and
`avdlab:<slug>`; it is idempotent. The site is the AVD `dc_name` (else the fabric name), so two labs with the same name
share a site — give each lab its own `dc_name` or NetBox.

### ACT (advanced, target act only)

The ACT topology (prepared / not prepared, node table with role, node type, version, IP), *Download topology YAML* to
upload in act.arista.com, client setup scripts with an "apply all from the tools server" command, and next steps (add the
tools server as a host of kind act, Bootstrap with `act-tools`, then Deploy and Validate as usual).

### Git & VS Code (advanced)

**Git remote**: URL (no credentials in the URL), branch, authentication (none / access token over HTTPS — write-only,
sent as `x-access-token` for GitHub or `oauth2` for GitLab / SSH key secret); *Push* (optional force; job `lab.git_push`),
*Pull* (`git pull --ff-only`, then re-validated; job `lab.git_pull`), ahead/behind from the last fetch, last sync.
**Open in VS Code**: a Remote-SSH `vscode://` link to `/opt/avdlab/labs/<slug>` on the host, the topology path for the
containerlab extension, a devcontainer clone link, the SSH command; *sync* (job `lab.sync`) copies the lab to the host
without deploying.

### Jobs

All jobs of this lab with status, type, duration and log links (same as the global Jobs page, filtered).

## 5.10 CloudVision page (targets)

Table: name, kind, server, certs, token, onboarding token, ZTP script, created, *Test connection* (inline REST
inventory call, 15 s → ok / devices seen / error), Edit, Delete. **Add target**: name; kind *CVaaS* (server = the regional
API endpoint of your tenant, e.g. `www.cv-prod-….arista.io`, or *custom…*) or *on-prem* (CVP cluster address); verify TLS
certificates; **service-account token** (for cv_deploy and the inventory checks), **onboarding token** with optional expiry
(for TerminAttr), optional **ZTP bootstrap script** — all write-only; each can be removed. Generate the service-account
token in CloudVision under Settings → Access Control → Service Accounts.

## 5.11 NetBox page (targets)

Table: name, URL, certs, token, labs, created, *Test connection* (pynetbox `status()`, 15 s → ok / version / error), Edit,
Delete. **Add target**: name, base URL (`http(s)://host[:port]`), verify TLS certificates, API token (write-only). Both
token formats work: legacy 40-hex tokens and NetBox 4.5+ `nbt_<id>.<secret>` tokens (sent as `Bearer`); a pasted
`Token ` / `Bearer ` prefix is stripped. The compose profile `netbox` starts a local NetBox (chapter 3).

## 5.12 Devcontainer (advanced)

Downloads a zip for VS Code Dev Containers: mode **local-clab** (containerlab inside the devcontainer, Docker-in-Docker;
needs a strong x86_64 machine or a Codespace) or **remote-host** (tooling only; labs run on the SSH lab host); optional AVD
version (blank = server default). The page lists the bundle contents.

## 5.13 Learn

`/learn` has four sections: **Lessons** (14 lessons, from "What is AVD" to segmentation; each mixes text with *try*
actions the UI performs, *checks* evaluated against your practice lab on the server, and quizzes; progress is stored per
user), **Glossary** (54 terms, the same cards that appear on hover), **Schema browser** (search and look up keys of the AVD
6.4 `eos_designs` and `eos_cli_config_gen` schemas) and **Tours** (guided tours per page). The learning-mode switch adds
(i) help to every knob, click-to-explain to config viewers and hover cards to glossary terms across the app.

## 5.14 Account

Signed in as / role. **Change password** with a live policy checklist (length, character classes, no username, not
common — checked by the server); success signs out your other sessions. **Two-factor authentication**: *Set up* shows a
QR code and the key for manual entry → enter the 6-digit code → *Enable* → 10 **recovery codes**, shown once (*Copy
codes*, *Download*); *Disable MFA* needs your password and a current code; *Regenerate recovery codes* needs your password (regenerating
invalidates the old codes). **AI assistant**: link your own model (provider, base URL, model, API key — write-only), *Test connection* lists
the provider's models, *Remove*; viewers cannot use the designer (chapter 6).

## 5.15 Users (admin)

**Pending requests** (from the public form): username, name / e-mail, reason, requested; *Approve* with a role (creates
the account with a 20-character temporary password, e-mailed to the applicant when SMTP is configured and always shown
**once** to the admin) or *Reject* with an optional reason. **Users**: *New user* (username, role, optional password —
empty generates a temporary one), and per user role, disabled, MFA, locked, last login, *must change pw*; actions *Reset
password*, *Reset MFA*, *Unlock*, *Delete* (not yourself, not the last admin). **Audit log**: the last 100 sign-in and
account events (when, user, event, IP, detail). The header links to Admin settings.

## 5.16 Admin settings (admin)

A sticky Save/Reset bar at the top. Sections: **Project** (app title, GitHub URL, support e-mail, intro video URL,
registration enabled), **E-mail** (SMTP host, port — 587 STARTTLS / 465 implicit TLS, user, password (encrypted, never
returned), from address, use STARTTLS, feedback e-mail to, account-requests e-mail to, mail every new feedback entry,
*Send test mail* which shows the SMTP error if it fails) and **AI assistant** (its own Save: shared default provider,
base URL, model, API key, *Allow users without their own link to use this model*, *Test connection*). Stored values win
over the `AVDLAB_*` defaults; a reset returns a key to its env default.

## 5.17 Feedback

The **Feedback** button (every page) opens a form: kind (bug / suggestion / question), title, description, page, *Include
diagnostics* (version, browser, recent failed jobs with secrets masked, host capabilities — previewable). Sending stores
the entry, writes `/data/feedback/<id>.md`, optionally opens a GitHub issue (`AVDLAB_FEEDBACK_GITHUB_REPO` + secret
`github_token`) and mails `feedback_mail_to` when SMTP is configured. The **E-mail** button opens your mail client with
the feedback address, a subject and the page context prefilled — it works without SMTP. The `/feedback` page lists entries
(kind, title, page, received, status new/triaged/done, GitHub issue link, delete).

## 5.18 Changelog and About

**Changelog** renders `CHANGELOG.md` per version (Added / Changed / Fixed) with the running version marked, plus the
**Updates** panel (current/latest, *Check now*, *Update now* when self-update is possible, otherwise the reason).
**About**: version, git sha and build date, pinned AVD/ANTA versions, author, GitHub link, copyright and licence (all
rights reserved), third-party notices.

## 5.19 Jobs (advanced)

`/jobs` lists the latest jobs of all labs and hosts (type, status, lab/host, created, duration); `/jobs/<id>` shows
parameters, result, error and the live log with a *Kill* button while it runs.
