# avdlab — Module 2c contract: CloudVision (targets, onboarding token, TerminAttr, cv_deploy, tags)

Builds on CONTRACT.md / CONTRACT-2b.md and the existing code (`executors/clab.py`, `labs/api.py`, `labs/templates.py`,
`hosts/ssh.py`, `core/db.py` — `CVTarget` table already exists). AVD 6.4 references: `cv_settings` model
(`cvaas.clusters[].{region: auto, vrf, token_file: /tmp/cv-onboarding-token}` / `onprem_clusters[].{servers[].{name,port:9910}, token_file: /tmp/token}` /
`terminattr.{disable_aaa, ingestexclude, smashexcludes}`), `generate_cv_tags.{topology_hints, device_tags[], interface_tags[]}`,
role `arista.avd.cv_deploy` vars (`cv_server`, `cv_token`, `cv_verify_certs`, `cv_submit_workspace`, `cv_run_change_control`,
`cv_skip_missing_devices`, `cv_strict_tags`, `cv_workspace_name`, `cv_change_control_name`). See
`/home/claude/arista/notes/avd-workflow-roles-labs.md` §2.4 and §4.4 for the exact semantics.

## Design

- **CV target** = where to deploy: `kind: cvaas|onprem`, `server` (CVaaS: regional `www.<region>.arista.io` URL; on-prem: host/IP),
  service-account **token** (secret, kind `cv_token`), `verify_certs`. Optional per-target **onboarding token** (secret, kind
  `cv_token`, meta `{purpose: onboarding, expires_at}`) — the token generated in CV under *Devices → Onboard → Generate token*, used by
  TerminAttr for the first connection. Both are entered interactively in the UI and stored encrypted.
- A lab is **bound** to a target through the `cloudvision` knob (below); the build renders `cv_settings` → `daemon TerminAttr` with the
  right cluster/token_file, plus `generate_cv_tags.topology_hints: true` and any device tags; `serial_number` per node is already rendered
  (Module 2a) so `cv_deploy` matches devices deterministically.
- Everything that talks to CloudVision runs **in the agent container on the lab host** (same as validate): the host has the lab files,
  the collection, and internet/LAN access. The Mac mini never needs the CV token on a command line: tokens go into a `0600` env-file on
  the host created for the run and removed afterwards.

## Template knob (`templates/single-dc-l3ls/template.yml` + `labs/templates.py`)

```yaml
- key: cloudvision
  type: object
  label: CloudVision
  default: {mode: none, region: auto, servers: [], disable_aaa: true, topology_hints: true, device_tags: {}}
  help: "mode: none | cvaas | onprem. cvaas → region (auto|us-central1-a|...); onprem → servers (IPs/hostnames, port 9910). device_tags: {label: value} applied to every device."
```
Rendering (generator + static templates via `path` edits are both fine):
- `mode: cvaas` → `cv_settings: {cvaas: {enabled: true, clusters: [{name: cvaas, region: <region>, vrf: use_mgmt_interface_vrf}]}, terminattr: {disable_aaa: <bool>}}`
- `mode: onprem` → `cv_settings: {onprem_clusters: [{name: primary, servers: [{name: <s>} ...], vrf: use_mgmt_interface_vrf}], terminattr: {...}}`
- `mode: none` → no `cv_settings` key (no TerminAttr rendered).
- Always when mode ≠ none: `generate_cv_tags: {topology_hints: <bool>, device_tags: [{name, value} ...]}`; keep `ntp_settings.servers` (AVD requires NTP when CV is configured — already in the template).
- Validate with `pyavd.validate_inputs` as today. The template-render byte-identical test must keep passing for `mode: none`.

## Backend

### `cloudvision/` package (`api.py`, `service.py`, jobs registered on import; add to `FEATURE_MODULES`)

- `GET/POST /cv/targets`, `GET/PATCH/DELETE /cv/targets/{id}`; body `{name, kind, server, verify_certs, token: str|None (write-only → secret), onboarding_token: str|None (write-only → secret, meta.expires_at optional)}`; responses never include token values, only `has_token`, `has_onboarding_token`, `onboarding_token_meta`.
- `POST /cv/targets/{id}/test` → inline (15 s budget): runs in the agent container on any bootstrapped host? No — simpler and host-independent: from the API process do an HTTPS `GET https://<server>/api/resources/inventory/v1/Device/all` (CVaaS/on-prem REST, bearer token, `verify=verify_certs`) with httpx; return `{ok, devices_seen: n, error}`. Isolate the HTTP call in `service.py` (fakeable).
- Lab binding: `PATCH /labs/{id}` already accepts knobs; add `cv_target_id` to the lab (new nullable column `labs.cv_target_id`, via `_ADDED_COLUMNS`) exposed in `GET /labs/{id}` and settable via `PATCH /labs/{id}` body `{cv_target_id}`. 409 if the lab's `cloudvision.mode` is `none`.

### Jobs

- `lab.cv_onboard` (`POST /labs/{id}/cv/onboard` → 202): preconditions deployed/validated + target with onboarding token. On the host, for every cEOS node in `last_inspect`: write the token into the container at the `token_file` path the build used (`/tmp/cv-onboarding-token` for cvaas, `/tmp/token` for onprem — read from `cv_settings` in the lab's group_vars) using `docker cp` from a temp file (0600, removed after) and then `docker exec <node> Cli -p 15 -c $'configure\ndaemon TerminAttr\nshutdown\nno shutdown'`. Then poll up to 120 s: `docker exec <node> Cli -c "show daemon TerminAttr | json"` is not always available on cEOS → use `Cli -c "show daemon TerminAttr"` text and treat "running" as up; additionally, if the target token is present, query CV inventory (`service.py` helper) and mark `streaming: true` when the serial appears with `streamingStatus == STREAMING_STATUS_ACTIVE`. Result `{nodes: [{name, serial, terminattr_running, streaming}]}` saved in `lab.settings["cv_onboarding"]`.
- `lab.cv_deploy` (`POST /labs/{id}/cv/deploy` body `{submit_workspace: true, run_change_control: false, skip_missing_devices: true, strict_tags: false, workspace_name?: str}` → 202): preconditions built + target with token. Writes `playbooks/avdlab-cv-deploy.yml` (committed; hosts FABRIC, connection local, `import_role: arista.avd.cv_deploy` with `cv_server: "{{ lookup('env','AVDLAB_CV_SERVER') }}"`, `cv_token: "{{ lookup('env','AVDLAB_CV_TOKEN') }}"`, `cv_verify_certs`, and the body params mapped to `cv_submit_workspace`, `cv_run_change_control`, `cv_skip_missing_devices`, `cv_strict_tags`, `cv_workspace_name`, `cv_register_detailed_results: true`), syncs the lab, creates the env-file on the host, runs `docker run --rm --env-file <f> -v <remote lab>:/lab -w /lab <agent_image> ansible-playbook playbooks/avdlab-cv-deploy.yml -v`, removes the env-file (also on failure), streams output. Parse the play output for the workspace id/state and change-control id (`cv_deploy` prints them; also register results by adding a `debug` task printing `cv_deploy_results | to_json` between markers `AVDLAB_CV_RESULT_BEGIN/END` and parse that JSON). Result `{workspace: {id, state, url?}, change_control: {id, state}, devices: [{hostname, serial, exists_on_cv, streaming}], errors, warnings}` → `lab.settings["cv_last_deploy"]`; state stays as is.
- `GET /labs/{id}/cv/status` → `{target, onboarding: lab.settings.cv_onboarding, last_deploy: lab.settings.cv_last_deploy}`.

### Tests (`tests/test_cloudvision.py`)
Fake SSH scripted for docker cp/exec/run; fake httpx for target test + inventory; template render with `cloudvision.mode: cvaas` produces `daemon TerminAttr` with `-cvaddr=apiserver.arista.io:443`-style config and `-cvauth=token-secure,/tmp/cv-onboarding-token` (check via pyavd build in the test); `mode: onprem` renders `-cvaddr=<ip>:9910` and `/tmp/token`; targets CRUD hides tokens; onboard job result; cv_deploy job parses the result block; API guards (409s). Keep all existing tests green.

## Frontend

- **CloudVision page** (`/cloudvision`): targets table; add/edit modal (kind, server with CVaaS region presets `www.arista.io`, `www.cv-prod-euwest-2.arista.io`, `www.cv-prod-na-northeast1-b.arista.io`, … as a select + free text; service-account token input; onboarding token input with optional expiry; verify certs) → tokens are write-only; "Test connection" button showing devices_seen.
- **Lab → Settings**: `cloudvision` knob rendered as a structured sub-form (mode select, region select / servers list, disable_aaa, topology hints, device tags key/value editor) instead of a raw object editor; a "CloudVision target" select (PATCH `cv_target_id`).
- **Lab → CloudVision tab**: target summary + Test; **Onboard** button (job) with per-node TerminAttr/streaming table; **Deploy to CloudVision** form (submit workspace, run change control, skip missing, strict tags, workspace name) → job; last deploy result panel (workspace id/state, change control, per-device table, warnings/errors). Buttons disabled per state (onboard needs deployed; deploy needs built + target).
- MSW mocks + vitest tests (sub-form → knob object; cv status panel rendering).
