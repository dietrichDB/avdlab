# avdlab — Phase 6 contract: updates, all AVD examples, in-app AVD learning, interactive test hosts

Conventions unchanged. Workstreams: **backend A** (§1 updates + §2 examples), **backend B** (§3 learning), **backend C** (§4 test hosts),
**frontend** (all UI). Routers in their own modules appended to `main.FEATURE_MODULES`.

## 1. Update check + self-update (backend A)

- Settings: `AVDLAB_UPDATE_SOURCE` = `github:<owner>/<repo>` (GitHub Releases API, default empty = checks disabled with a hint) or a
  manifest URL returning `{"latest": "0.7.0", "url": "...", "notes": "...", "published_at": "...", "image": "ghcr.io/…/avdlab:0.7.0"}`;
  `AVDLAB_UPDATE_MODE` = `source` (default: `git pull` + `docker compose up -d --build` in the deploy dir) | `image` (pull the release image
  tag + `docker compose up -d`) | `manual`; `AVDLAB_UPDATE_CHECK_INTERVAL` hours (24).
- `GET /api/meta/update` → `{current, latest, available: bool, url, notes, published_at, checked_at, mode, can_self_update: bool, reason}` — cached
  result of a daily background check (httpx, never blocks startup; failures → `error` field). `POST /api/meta/update/check` forces a check.
- `POST /api/meta/update/apply` → 202 job `app.update`: preconditions `can_self_update` (docker socket mounted at `/var/run/docker.sock`
  and `AVDLAB_DEPLOY_DIR` (the host path of the `deploy/` dir, passed via env) set). Runs a **detached helper container**
  (`docker run -d --rm -v /var/run/docker.sock:/var/run/docker.sock -v <deploy dir>:/deploy -w /deploy docker:cli sh -c '<cmd>'`) so the update
  survives the API restart: source mode `git -C .. pull --ff-only && docker compose up -d --build`; image mode `docker compose pull api && docker compose up -d api`.
  Writes `<data_dir>/update.log` (helper output captured via `docker logs -f` until the API dies; the new API on boot reads `update.log` + the
  previous version from `<data_dir>/update.json` and exposes `last_update: {from, to, at, ok}` in `/api/meta/update`). Refuse (409) while any job is running.
- compose: document the optional `/var/run/docker.sock` mount and `AVDLAB_DEPLOY_DIR=${PWD}` in `.env.example`/README (already commented for images).
- CI: `.github/workflows/release.yml` — on tag `v*`: run backend + frontend tests, build the image, push `ghcr.io/<owner>/avdlab:<version>` + `:latest`,
  create the GitHub Release with the tarball and the changelog section. `.github/workflows/ci.yml` on push/PR: tests + build.
- Tests: manifest/GitHub parsing (fake httpx), semver compare, `can_self_update` logic, apply job composes the right helper command (fake docker), 409s.
- Frontend: version badge shows an "update available" pill; `/about` and Changelog page get an Update panel (latest, notes, mode, Check now, Update now with confirm → job drawer; after the API restarts the UI polls `/api/meta` until it answers and reloads).

## 2. All AVD examples as templates (backend A)

Add `campus-fabric`, `cv-pathfinder`, `isis-ldp-ipvpn`, `single-dc-l3ls-ipv6` from `/home/claude/arista/avd/ansible_collections/arista/avd/examples/`
as `generator: static` templates with the standard knob set (`mgmt_subnet`, `mgmt_gateway`, `ceos_version`, `ceos_arch`, `device_password`,
`serial_scheme`, `dns_servers`, `client_image`, `cloudvision`, `device_tags`, `provisioning`, `act`, `segmentation` where applicable — reuse how
`dual-dc-l3ls` does it) plus `template.yml` `title/description/learn` (a paragraph on what the design teaches + the AVD doc URL). Each must:
render, validate with pyavd, build, and produce a containerlab topology (verify `topology.csv` link handling for L2 uplinks (campus), `core_interfaces`
p2p links (isis-ldp), IPv6 mgmt (ipv6 example: `ipv6_mgmt_ip` → clab `mgmt-ipv6`), WAN routers (cv-pathfinder: mark `notes: "WAN path-selection needs
CloudVision Pathfinder; cEOS supports the control plane only"`). Strip README/intended/documentation/images. Add a `GET /labs/templates` field
`tags: [l3ls, evpn, mlag, campus, wan, mpls, ipv6, …]`, `node_count`, `learn`. Tests: every template renders + builds (parametrised), topology has
≥ 1 link.

## 3. In-app AVD learning (backend B + frontend) — "learn while you build"

Content source for the author: `/home/claude/arista/notes/avd-eos-designs-datamodel.md`, `avd-workflow-roles-labs.md`, `anta-reference.md`,
`04-lab-playbook.md` (accurate, AVD 6.4-verified). Everything must be **accurate to AVD 6.4**; when in doubt, cite the AVD doc path.

Backend package `learn/`:
- **Schema browser**: `GET /api/learn/schema/eos_designs?key=<dotted path>` and `/schema/eos_cli_config_gen?key=` → `{key, type, description, default,
  valid_values, min, max, required, deprecation, keys: [{name, type, short}], items, doc_url}` from pyavd's schema (`pyavd/_eos_designs/schema/eos_designs.schema.pickle`
  or the fragments YAML shipped in the wheel — check what the installed pyavd ships; fall back to a vendored JSON export built from the repo checkout at
  /home/claude/arista/avd if the wheel has only the pickle — either way a `python scripts/export_schema.py` regenerates it). `GET /api/learn/schema/search?q=` → fuzzy matches.
  `doc_url` = `https://avd.arista.com/6.4/ansible_collections/arista/avd/roles/eos_designs/docs/tables/<table>.html#<anchor>` derived from the fragment's `documentation_options.table`.
- **Knob help**: every knob gets `learn: {summary, avd_keys: [dotted eos_designs keys it writes], renders: "what CLI appears", pitfalls: [..], doc_url}`
  in `template.yml` (write them for all knobs of all templates — this is content work, be thorough and correct) and `GET /labs/templates` returns it.
- **Config explainer**: `GET /api/learn/explain?lab_id=&node=&line=<config line>` (and batch `POST /api/learn/explain` `{lab_id, node, lines: []}`) → for a line
  of an intended config returns `{section, avd_input: {key, value, where: "group_vars/…"}, why, doc_url, related_lines}` using a rules table
  (`learn/explain_rules.py`) covering at least: hostname, management interface/vrf/gateway, eAPI, local users, NTP/DNS, `service routing protocols model`,
  `ip routing`, `vlan internal order`, spanning-tree, MLAG vlans/port-channel/`mlag configuration`, loopback0/1 (pools/offset), P2P uplinks (uplink pools,
  `max_uplink_switches` formula), BGP AS/router-id/peer groups/neighbors/maximum-paths/redistribute + route-map/prefix-list, EVPN peer group + address-family,
  VLANs/SVIs/`ip address virtual`/`ip virtual-router mac-address`, VRFs + `vrf_vni`/RD/RT, `interface Vxlan1`, endpoint ports/port-channels/`mlag <id>`,
  TerminAttr (cv_settings), `router segment-security`, `daemon TerminAttr`. Unknown lines → `{section, why: null}`. Values are read from the lab's group_vars/knobs so the
  explanation is specific ("Loopback0 10.255.0.3 = loopback_ipv4_pool 10.255.0.0/27 + id 1 + loopback_ipv4_offset 2").
- **Lessons**: `learn/lessons/*.md` with front-matter `{id, title, level: intro|core|advanced, minutes, requires: [built|deployed|validated], tags}` and
  body markdown with special blocks: `:::try` (an action the UI can execute: `{action: open_file|open_tab|run_job|set_knob, args}`), `:::check` (a
  checkpoint evaluated server-side: `{type: config_contains|knob_equals|job_succeeded|anta_status|file_exists, args}`), `:::quiz` (single-choice with
  explanation). Write **at least 12 lessons**: 1 What AVD is (inputs → structured config → CLI), 2 Inventory & group_vars layering, 3 Node types & ids,
  4 Pools & the IP math (loopbacks, P2P, MLAG), 5 MLAG in AVD, 6 BGP underlay/overlay & route servers, 7 Tenants, VRFs, SVIs, VNIs, RD/RT, 8 Connected
  endpoints & port profiles, 9 Reading the generated documentation, 10 Custom structured configuration & raw CLI escape hatches, 11 ANTA: how validation
  works and reading a report, 12 CloudVision onboarding & cv_deploy, 13 ZTP, 14 Segmentation (MSS foundation). Each has 2–4 `:::try`, ≥ 2 `:::check`, 1–2 `:::quiz`.
  `GET /api/learn/lessons`, `/lessons/{id}` (parsed), `POST /api/learn/lessons/{id}/check` `{lab_id, checkpoint_id}` → `{passed, detail}`; progress table
  `learn_progress` (lesson_id, checkpoint_id, passed_at, lab_id) + `GET /api/learn/progress`.
- **Glossary**: `learn/glossary.yml` (≥ 40 terms: VTEP, VNI, RD, RT, MLAG, ESI, EVPN route types, underlay/overlay, RR/route server, iBGP/eBGP, VARP/anycast,
  SVI, L2VNI/L3VNI, ZTP, TerminAttr, structured config, eos_designs, eos_cli_config_gen, node_type_keys, pool, …) → `GET /api/learn/glossary`.
- **Tours**: `learn/tours.yml` — step lists keyed by page (`first-lab`, `hosts`, `topology-editor`, `validation`, `cloudvision`) with selectors
  (`data-tour="..."` attributes the frontend adds) and text; `GET /api/learn/tours`.
- Tests: schema lookup for `l3leaf.defaults.loopback_ipv4_pool` and `router_bgp.peer_groups`; explain rules on the single-dc-l3ls build (≥ 20 distinct lines
  explained with values); lessons parse; checkpoints evaluate against a built lab; glossary loads.

Frontend:
- `/learn` hub: lessons by level with progress, glossary, schema browser (search + tree), tours launcher; "Learning mode" toggle (persisted) that turns on
  everything below.
- Knob `(i)` popovers everywhere a knob is shown (wizard, settings, topology editor forms): summary, keys it writes, "renders as" CLI, pitfalls, doc link.
- Files/Devices config viewers: click any line → Explain side panel (from `/api/learn/explain`); hover highlights the line's section; "Explain this file"
  batch mode listing sections with counts.
- `<Term>` component: glossary terms rendered with dotted underline + hover card, used in labels/help texts across the app (wire at least the lab detail
  tabs, wizard, validation and CloudVision pages).
- Lessons runner: markdown with `:::try` buttons that perform the action in-app (navigate/open/run), `:::check` buttons calling the checkpoint API with
  ✓/✗ + detail, quizzes with instant feedback; progress bar; "next lesson".
- Tours: lightweight own implementation (spotlight overlay + step card) driven by `tours.yml`; auto-offer the `first-lab` tour on an empty labs page.
- Schema browser also opens from the YAML editor: Ctrl/Cmd-click a key or "?" on the current line → the key's schema entry.

## 4. Interactive test hosts (backend C + frontend)

- Hot-add a client without redeploying the fabric: `POST /labs/{id}/clients` body `{name, switch, switch_port, vlans: "11" (access) | "11-12" (trunk), mode,
  ip: "auto"|"a.b.c.d/nn", image?, deploy: true}` → applies the `add_endpoint` op (so AVD/topology stay truthful), builds only the structured config/config for
  the affected switch(es) in-process, then job `lab.client_deploy`: (1) push the switch's new config with `eos_config` replace via the agent container
  **limited to that switch** (`--limit`), (2) `docker run -d --name <client> --network none <client_image> sleep infinity` on the host with labels
  `avdlab.lab=<slug>` `avdlab.role=client`, (3) `containerlab tools veth create -a <switch container>:eth<N> -b <client>:eth1` (containerlab's veth tool;
  with `prefix: ''` container names are node names), (4) run the generated setup script inside the client (`docker exec`), (5) ping the gateway; result
  `{client, switch, port, ip, gateway, reachable}`; `clab/clients.json` updated so a later full redeploy recreates it. `DELETE /labs/{id}/clients/{name}` →
  job `lab.client_remove` (container rm + `remove_endpoint` op + switch config push). `POST /labs/{id}/clients/{name}/exec` `{cmd}` → inline (20 s) stdout
  for quick checks; quick traffic from the client: `POST /labs/{id}/clients/{name}/ping` `{dst}` / `/iperf` `{dst, seconds}` → inline results (reuse traffic parsers).
- `GET /labs/{id}/clients` gains `hot_added: bool`, `deployed: bool`.
- Tests: fake SSH scripted for docker run / veth / exec / ping; op applied; config for only the affected switch regenerated; remove path; API 409 when lab not deployed.
- Frontend: topology editor gets **"Add test host"** (pick a switch port by clicking a free port on a switch node → small form → Deploy now); node inspector for a
  client shows IP/gateway/state and quick actions: ping (to any other client/IP), iperf (to another client), open terminal, remove; results inline. The Clients tab
  lists hot-added hosts with the same actions.

## Frontend tests
Vitest: update panel states; template cards with tags; learn hub lessons/progress; explain panel rendering; Term hover card; tours step engine; add-test-host
flow from the topology (mock).
