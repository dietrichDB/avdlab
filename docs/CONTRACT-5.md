# avdlab — Phase 5 contract: editable topology, changelog, feedback, about/copyright, version badge, logo

## 1. Editable topology (backend + frontend)

Principle: the graph is a *view of AVD inputs*. Edits become AVD input changes (knobs / group_vars), are validated with pyavd, and the graph
re-renders from the rebuilt topology. Positions are cosmetic and stored separately.

Backend (`topology/edit.py`, routes in `topology/api.py`):
- `GET /topology/{id}/layout` / `PUT /topology/{id}/layout` body `{positions: {node: {x, y}}, locked: bool}` → stored in `clab/layout.json` (committed).
  `GET /topology/{id}` includes `layout` when present.
- `POST /topology/{id}/edit` body `{ops: [Op...], dry_run: false}` → applies ops in order to a copy of the lab inputs, validates with pyavd
  (`validate_inputs` on resolved hostvars; for generated templates also re-render from knobs), and on success writes files + commits and
  regenerates the topology (job-less; return 200 `{graph, changed_files, warnings}`) or 422 `{message, violations, op_index}`. Ops:
  - `set_uplink {node, index, switch, switch_port}` — rewrites `uplink_switches[index]` / `uplink_switch_interfaces[index]` on the node (node-level override in the node's group_vars entry); for generated templates the node entry is added under the right `node_groups[].nodes[]` (see how `serial_number` per node is written) and marked `custom` so knob re-render keeps it (store overrides in `lab.settings["topology_overrides"]` and re-apply after every knob render).
  - `add_endpoint {name, type: servers|firewalls|..., adapters: [{switches, switch_ports, endpoint_ports, mode, vlans, native_vlan?, port_channel?: {mode}}]}` → appends to the `servers` knob (generated) or the endpoint list in group_vars (custom).
  - `remove_endpoint {name}`; `update_endpoint {name, adapters}`.
  - `add_leaf_pair {}` / `remove_last_leaf_pair {}` / `set_l2leaf_per_pair {n}` / `set_spine_count {n}` — generated l3ls templates only (knob increments); 409 for custom labs with a hint to edit group_vars.
  - `rename_node` is **not** supported (hostnames are keys everywhere) → 409.
- Free ports: `GET /topology/{id}/ports` → `{node: {used: [if], free: [if]}}` derived from `default_interfaces`/`uplink_*`/adapters/mlag so the UI can offer valid targets.
- Tests: each op on the single-dc-l3ls example (set_uplink moves leaf1a Eth1 to spine1 Eth5 and the rebuilt topology reflects it; add_endpoint creates a linux node and links; add_leaf_pair → 3 pairs; invalid port → 422), layout round-trip, overrides survive a knob change.

Frontend (`TopologyTab`): edit mode toggle; drag nodes (positions saved on "Save layout"; auto-layout button resets); drag an uplink edge's spine end to another spine port (port picker popover listing free ports) → `set_uplink`; "Add server" (form: name, leaf pair or single leaf, ports, VLANs, port-channel) → `add_endpoint`; right-click server → remove; toolbar "+ leaf pair", "+ L2 leaf per pair", spine count; pending ops list with Apply (dry-run first showing changed files/violations) and Discard; after apply, offer Build. Node inspector side panel (device facts, ports used/free).

## 2. Changelog page

- `CHANGELOG.md` at repo root (Keep-a-Changelog format; backfill 0.1.0 → 0.6.0 from git history — write real entries), `VERSION` file at repo root (`0.6.0`).
- `GET /api/meta` → `{version, git_sha (if available at build: baked into the image via build arg AVDLAB_GIT_SHA), build_date, avd_version, anta_version, python, changelog_latest: {version, date, sections}}`; `GET /api/meta/changelog` → parsed changelog `[{version, date, sections: {Added: [..], Changed: [..], Fixed: [..]}}]` plus raw markdown.
- Frontend `/changelog` page rendering the parsed list (version anchors, badges per section); a "what's new" dot on the version badge when the latest version differs from `localStorage.lastSeenVersion`.

## 3. Feedback form

- Table `feedback` (id, kind: bug|suggestion|question, title, body, page, diagnostics JSON, created_at, status: new|triaged|done, github_issue_url).
- `POST /api/feedback` body `{kind, title, body, page, include_diagnostics}`; diagnostics collected server-side: version, git sha, browser UA (from header), recent failed jobs (last 5: type, error, first log lines with secrets masked), host capabilities summary — only when `include_diagnostics`. Also appended to `<data_dir>/feedback/<id>.md`.
  `GET /api/feedback` (list), `PATCH /api/feedback/{id}` (status), `DELETE`. Optional GitHub issue creation: app settings `AVDLAB_FEEDBACK_GITHUB_REPO` (`owner/repo`) + secret named `github_token` → creates an issue via REST (label `feedback`, body includes diagnostics) and stores the URL; failures are logged, never block.
- Frontend: a "Feedback" button fixed bottom-right (and in the footer) opening a modal: kind, title, body, current page prefilled, "include diagnostics" checkbox with a preview of what is sent; toast on success; `/feedback` admin page listing entries with status toggles and GitHub links.

## 4. About / copyright page

- `/about`: logo, name, version/build info from `/api/meta`, copyright line `© 2026 Dietrich De Bruyne — Defaultroute BV. All rights reserved.` (configurable via `AVDLAB_COPYRIGHT` env; default as given), license section (app: proprietary/all-rights-reserved by default — `LICENSE` file at repo root with that text and a TODO comment that it can be switched to an OSS licence), third-party notices table (Arista AVD — Apache-2.0; ANTA — Apache-2.0; containerlab — BSD-3-Clause; FastAPI — MIT; React — MIT; Tailwind — MIT; xterm.js — MIT; recharts — MIT; React Flow — MIT; pynetbox — Apache-2.0; eos-downloader — Apache-2.0; network-multitool image — MIT; cEOS-lab — Arista EULA, not redistributed) — generate `THIRD_PARTY_NOTICES.md` and serve it at `GET /api/meta/notices`. Trademark note: Arista, EOS, CloudVision are trademarks of Arista Networks; avdlab is not an Arista product.
- Footer on every page: `avdlab v<version> · Changelog · About · Feedback`.

## 5. Version badge

Top-left under the logo in the sidebar: `v<version>` (from `/api/meta`), tooltip with git sha/build date, click → changelog; small dot when unseen.

## 6. Logo

Provided by the orchestrator as `frontend/public/logo.svg` (full), `frontend/public/logo-mark.svg` (mark only), `frontend/public/favicon.svg`; the frontend uses the mark in the sidebar header (with the wordmark "avdlab"), the favicon in `index.html`, and the full logo on Login and About. Dark-mode: the SVG uses `currentColor` for the wordmark and fixed brand colours for the mark.
