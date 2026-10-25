# 7. API reference

Everything the SPA does goes through a JSON REST API under `/api` plus a few WebSockets; you can script avdlab with the
same calls. This chapter is derived from the routers registered in `backend/avdlab/main.py` (176 routes) and their
docstrings. FastAPI also serves its generated OpenAPI description at `/docs` and `/openapi.json` (not behind a session).

## 7.1 Conventions

| Topic | Convention |
|---|---|
| Base path | `/api` — the tables below omit it |
| Authentication | session cookie from `POST /api/auth/login` (+ `POST /api/auth/mfa/verify` when MFA is on). A script keeps the cookie jar: `curl -c jar -b jar -H 'content-type: application/json' -d '{"username":"admin","password":"…"}' https://host/api/auth/login` |
| Anonymous | `401 {"detail": "not authenticated"}` except the public routes |
| Role refused | `403 {"detail": "role viewer may not POST /api/labs"}` (RoleGuard) |
| Validation errors | `422 {"detail": {"message": str, "violations": [{"host"?, "path"?, "message"}]}}` — knob coercion, pyavd schema, YAML syntax, topology ops (`op_index`, `hint`) |
| Conflicts | `409` with a message, often `{"message": "lab.deploy is already running for this lab", "job_id": …}` |
| Jobs | long actions answer `202 {"job_id": "<32 hex>"}`; follow with `GET /jobs/{id}` or `WS /jobs/{id}/stream` |
| Inline budgets | status 20 s, running config 25 s, client exec 20 s, CV/NetBox test 15 s, ZTP status 15 s |
| Secrets | write-only everywhere: request bodies may carry `token`, `api_key`, `value`, `smtp_password`; responses only carry `has_*` / `configured` flags. A masked secret knob comes back as `********` and is kept when sent back unchanged |
| Ids | 32-character hex strings |

**Role column.** `public` = no session needed; `viewer` = any signed-in user (read); `viewer (self)` = any signed-in
user, acting on their own account; `operator` = operator or admin; `admin` = admin only. The rules are those of
`core/security.py` `role_denial()` (chapter 2 §(f)).

## 7.2 WebSockets

| Path | Role | Frames |
|---|---|---|
| `/jobs/{job_id}/stream` | viewer | server → client JSON `{"line": "[HH:MM:SS] …"}` for the existing log, then live lines, then `{"done": true}` |
| `/hosts/{host_id}/terminal?cols=&rows=` | operator | `$SHELL -l` on a PTY over SSH |
| `/labs/{lab_id}/nodes/{node}/terminal?shell=cli\|bash&cols=&rows=` | operator | `docker exec -it <node> Cli` or `bash` (linux clients: bash or sh; ACT nodes: SSH from the tools server) |

Terminal frames are binary in both directions (client → stdin, stdout → client); a JSON text frame
`{"resize": {"cols": n, "rows": n}}` resizes the PTY; any other text frame is written to stdin. Close codes: `4401` not
authenticated or stale session, `4403` role not allowed. Session open/close is logged, never the content.

## 7.3 Routes per router

### Auth — `api/auth.py`

| Method | Path | Role | Purpose |
|---|---|---|---|
| POST | `/auth/login` | public | sign in `{username, password}` → user, or `{mfa_required: true}`; lockout after repeated failures |
| POST | `/auth/mfa/verify` | public | second sign-in step `{code}` (TOTP or recovery code) |
| POST | `/auth/logout` | public | clear the session |
| GET | `/auth/me` | viewer | current user (role, MFA, must_change_password, recovery codes left) |
| GET | `/auth/policy` | public | password policy parameters for the live checklist |
| POST | `/auth/password` | viewer (self) | change own password `{current_password, new_password}`; signs out other sessions |
| POST | `/auth/mfa/setup` | viewer (self) | start TOTP enrolment → QR code + key |
| POST | `/auth/mfa/enable` | viewer (self) | confirm `{code}` → 10 recovery codes (shown once) |
| POST | `/auth/mfa/disable` | viewer (self) | `{password, code}` → MFA off |
| POST | `/auth/mfa/recovery-codes` | viewer (self) | `{password}` → new recovery codes (old ones invalid) |

### Jobs — `api/jobs.py`

| Method | Path | Role | Purpose |
|---|---|---|---|
| GET | `/jobs/types` | viewer | registered job types |
| GET | `/jobs` | viewer | list jobs `?lab_id=&host_id=&limit=100`, newest first |
| GET | `/jobs/{job_id}` | viewer | one job: type, status, params, result, artefacts, error, timestamps |
| GET | `/jobs/{job_id}/log` | viewer | full log `{log}` |
| POST | `/jobs/{job_id}/cancel` | operator | kill a queued/running job → `{cancelled, was_running}` |
| WS | `/jobs/{job_id}/stream` | viewer | existing log, then live lines `{line}`, then `{done: true}` |

### Secrets — `api/secrets.py`

| Method | Path | Role | Purpose |
|---|---|---|---|
| GET | `/secrets` | viewer | secret names and kinds (never values) |
| POST | `/secrets` | operator | create `{name, kind, value}` |
| DELETE | `/secrets/{secret_id}` | operator | delete a secret |

### Hosts — `hosts/api.py`, `hosts/monitor.py`

| Method | Path | Role | Purpose |
|---|---|---|---|
| POST | `/hosts/keys/generate` | operator | new ed25519 key pair → `{secret_id, name, public_key}` |
| POST | `/hosts/devcontainer` | operator | devcontainer zip `{mode: local-clab\|remote-host, avd_version?}` |
| GET | `/hosts/recipes` | viewer | bootstrap recipes and their steps |
| GET | `/hosts` | viewer | hosts with a summary (OS, docker, containerlab, recommendations, arch, platform) |
| POST | `/hosts` | operator | create `{name, kind, address, ssh_port, ssh_user, ssh_key_secret_id, password_secret_id, sudo_password_secret_id, jump_host, platform}` |
| GET | `/hosts/{host_id}` | viewer | one host |
| PATCH | `/hosts/{host_id}` | operator | update host fields |
| DELETE | `/hosts/{host_id}` | admin | delete a host |
| GET | `/hosts/{host_id}/capabilities` | viewer | last probe document + `last_probe_at` |
| POST | `/hosts/{host_id}/probe` | operator | 202 job `host.probe` |
| POST | `/hosts/{host_id}/bootstrap` | operator | 202 job `host.bootstrap` `{recipe, options}` |
| POST | `/hosts/{host_id}/verify` | operator | 202 job `host.verify` |
| WS | `/hosts/{host_id}/terminal` | operator | host shell `$SHELL -l` on a PTY (`?cols=&rows=`) |
| GET | `/hosts/{host_id}/metrics` | viewer | points + per-container stats; marks the host watched for 10 min |
| GET | `/hosts/{host_id}/processes` | viewer | process list |
| GET | `/hosts/{host_id}/containers` | viewer | `docker ps -a` with lab labels |
| DELETE | `/hosts/{host_id}/containers/{name}` | admin | stop (`force=false`) or `rm -f` (`force=true`); live lab containers need `{confirm: <name>}` |

### Images — `images/api.py`

| Method | Path | Role | Purpose |
|---|---|---|---|
| GET | `/images/formats` | viewer | `[{fmt, arch, label}]` for cEOS / cEOS64 / cEOSarm |
| GET | `/images/token` | viewer | arista.com token status (never the value) |
| PUT | `/images/token` | operator | create/replace the token `{value}`; drops the catalogue cache |
| DELETE | `/images/token` | operator | remove the token |
| GET | `/images/catalog` | viewer | `?fmt=&refresh=` versions from arista.com (1 h cache; 409 no token, 502 rejected/unreachable) |
| GET | `/images` | viewer | image store |
| GET | `/images/{image_id}` | viewer | one image |
| DELETE | `/images/{image_id}` | operator | delete an image |
| POST | `/images/download` | operator | 202 job `image.download` `{fmt, version, force?}` |
| POST | `/images/{image_id}/push/{host_id}` | operator | 202 job `image.push` `{options: {remote_download?, force?}}` |
| POST | `/images/upload` | operator | multipart `file, fmt, version` |

### Labs — `labs/api.py`

| Method | Path | Role | Purpose |
|---|---|---|---|
| GET | `/labs/templates` | viewer | template descriptors: knobs (with `learn`), tags, node_count, notes |
| GET | `/labs` | viewer | labs |
| POST | `/labs` | operator | create `{name, template, host_id?, ceos_version?, knobs, target?}` → 201 or 422 violations |
| GET | `/labs/{lab_id}` | viewer | one lab (knobs with secrets masked, last build job, host) |
| PATCH | `/labs/{lab_id}` | operator | `{knobs?, host_id?, ceos_version?, name?, cv_target_id?, netbox_target_id?, target?}` — re-render, validate, commit |
| DELETE | `/labs/{lab_id}` | operator | delete a lab (409 while running or while a job runs) |
| GET | `/labs/{lab_id}/files` | viewer | file tree with `editable` / `knob_managed` flags |
| GET | `/labs/{lab_id}/files/{path:path}` | viewer | file content |
| PUT | `/labs/{lab_id}/files/{path:path}` | operator | save `{content, message?}` (YAML + pyavd checks, commit) |
| POST | `/labs/{lab_id}/build` | operator | 202 job `lab.build` |
| GET | `/labs/{lab_id}/history` | viewer | git commits of the lab |
| GET | `/labs/{lab_id}/diff` | viewer | `?a=&b=&path=` unified diff between revisions |
| GET | `/labs/{lab_id}/devices` | viewer | device table from structured configs (type, mgmt IP, serial, loopbacks, AS, tags) |
| POST | `/labs/{lab_id}/deploy` | operator | 202 job `lab.deploy` `{reconfigure, wait_eapi, wait_timeout}` |
| POST | `/labs/{lab_id}/destroy` | operator | 202 job `lab.destroy` |
| GET | `/labs/{lab_id}/status` | viewer | inline status (20 s budget, cached fallback) |
| POST | `/labs/{lab_id}/status/refresh` | operator | 202 job `lab.status` |
| POST | `/labs/{lab_id}/validate` | operator | 202 job `lab.validate` |
| GET | `/labs/{lab_id}/validations` | viewer | validation runs with totals |
| GET | `/labs/{lab_id}/validations/{run_id}/report` | viewer | `?format=json\|md\|csv` report of a run |

### Lab runtime — `executors/lab_routes.py`, `executors/push.py`, `executors/psirt.py`

| Method | Path | Role | Purpose |
|---|---|---|---|
| GET | `/labs/{lab_id}/clients` | viewer | client plans + container state, mgmt IP, hot_added, deployed |
| POST | `/labs/{lab_id}/traffic/ping` | operator | 202 job `lab.traffic_ping` |
| POST | `/labs/{lab_id}/traffic/iperf` | operator | 202 job `lab.traffic_iperf` |
| GET | `/labs/{lab_id}/traffic/runs` | viewer | ping / iperf / mss runs, newest first |
| GET | `/labs/{lab_id}/traffic/runs/{run_id}` | viewer | one run with results |
| GET | `/labs/{lab_id}/nodes/{node}/running-config` | viewer | inline `show running-config` (25 s) |
| GET | `/labs/{lab_id}/nodes/{node}/config-diff` | viewer | intended vs running `{diff, identical}` |
| POST | `/labs/{lab_id}/snapshot` | operator | 202 job `lab.snapshot` `{commands}` |
| GET | `/labs/{lab_id}/snapshots` | viewer | snapshot list |
| GET | `/labs/{lab_id}/snapshots/{snap}/{node}/{cmd_slug}` | viewer | one snapshot output (text/plain) |
| WS | `/labs/{lab_id}/nodes/{node}/terminal` | operator | node terminal `?shell=cli\|bash&cols=&rows=` |
| GET | `/labs/{lab_id}/push/plan` | viewer | `{changed, unchanged, unknown, total, since, available}` |
| POST | `/labs/{lab_id}/push` | operator | 202 job `lab.push_configs` `{hosts?}` |
| POST | `/labs/{lab_id}/psirt` | operator | 202 job `lab.psirt` `{tests?}` |
| GET | `/labs/{lab_id}/psirt` | viewer | last scan summary + rows |
| GET | `/labs/{lab_id}/psirt/report.md` | viewer | Markdown report of the last scan |

### Test hosts — `executors/client_routes.py`

| Method | Path | Role | Purpose |
|---|---|---|---|
| POST | `/labs/{lab_id}/clients` | operator | hot-add a test host `{name, switch, switch_port, vlans, mode, ip, image?, deploy}` → 202 job `lab.client_deploy` |
| DELETE | `/labs/{lab_id}/clients/{name}` | operator | remove → 202 job `lab.client_remove` (200 when not deployed) |
| POST | `/labs/{lab_id}/clients/{name}/exec` | operator | inline `{cmd}` in the client (20 s) |
| POST | `/labs/{lab_id}/clients/{name}/ping` | operator | inline `{dst, count}` |
| POST | `/labs/{lab_id}/clients/{name}/iperf` | operator | inline `{dst, seconds, proto, reverse, bandwidth?}` |

### Topology — `topology/api.py`

| Method | Path | Role | Purpose |
|---|---|---|---|
| GET | `/topology/{lab_id}` | viewer | graph JSON (nodes, links, layout); 404 until built |
| GET | `/topology/{lab_id}/clab` | viewer | raw `clab/topology.clab.yml` |
| POST | `/topology/{lab_id}/regenerate` | operator | 202 job `lab.topology` |
| GET | `/topology/{lab_id}/layout` | viewer | node positions `{positions, locked}` |
| PUT | `/topology/{lab_id}/layout` | operator | save positions (committed) |
| GET | `/topology/{lab_id}/ports` | viewer | used/free ports and peers per node |
| POST | `/topology/{lab_id}/edit` | operator | `{ops, dry_run}` → graph, changed_files, diff, warnings; 422/409 with violations |

### Lab integrations (git, import/export, VS Code) — `labs/integrations_api.py`

| Method | Path | Role | Purpose |
|---|---|---|---|
| GET | `/labs/{lab_id}/git` | viewer | remote config, ahead/behind, last sync |
| PUT | `/labs/{lab_id}/git` | operator | `{url, branch, auth_kind, secret_id\|null, token\|null}` |
| DELETE | `/labs/{lab_id}/git` | operator | remove the remote |
| POST | `/labs/{lab_id}/git/push` | operator | 202 job `lab.git_push` `{force}` |
| POST | `/labs/{lab_id}/git/pull` | operator | 202 job `lab.git_pull` |
| POST | `/labs/import` | operator | multipart archive (zip / tar.gz), name, host_id? → 201 custom lab (+ import_warnings) |
| POST | `/labs/import/git` | operator | 202 job `lab.import_git` |
| GET | `/labs/{lab_id}/export` | viewer | zip of the lab (without .git and ANTA reports) |
| GET | `/labs/{lab_id}/open-links` | viewer | VS Code Remote-SSH / devcontainer links, SSH command, hints |
| POST | `/labs/{lab_id}/sync` | operator | 202 job `lab.sync` |

### CloudVision — `cloudvision/api.py`

| Method | Path | Role | Purpose |
|---|---|---|---|
| GET | `/cv/targets` | viewer | CloudVision targets (token flags only) |
| POST | `/cv/targets` | operator | create `{name, kind: cvaas\|onprem, server, verify_certs, token?, onboarding_token?, onboarding_token_expires_at?, ztp_script?}` |
| GET | `/cv/targets/{target_id}` | viewer | one target |
| PATCH | `/cv/targets/{target_id}` | operator | update; an empty token removes the stored one |
| DELETE | `/cv/targets/{target_id}` | operator | delete |
| POST | `/cv/targets/{target_id}/test` | operator | inline REST inventory call (15 s) → `{ok, devices_seen, error}` |
| POST | `/labs/{lab_id}/cv/onboard` | operator | 202 job `lab.cv_onboard` |
| POST | `/labs/{lab_id}/cv/deploy` | operator | 202 job `lab.cv_deploy` |
| GET | `/labs/{lab_id}/cv/status` | viewer | `{mode, target, onboarding, last_deploy}` |

### NetBox — `netbox/api.py`

| Method | Path | Role | Purpose |
|---|---|---|---|
| GET | `/netbox/targets` | viewer | NetBox targets |
| POST | `/netbox/targets` | operator | create `{name, url, token, verify_certs}` |
| GET | `/netbox/targets/{target_id}` | viewer | one target |
| PATCH | `/netbox/targets/{target_id}` | operator | update |
| DELETE | `/netbox/targets/{target_id}` | operator | delete (409 while bound to labs) |
| POST | `/netbox/targets/{target_id}/test` | operator | inline `status()` (15 s) → `{ok, version, error}` |
| POST | `/labs/{lab_id}/netbox/sync` | operator | 202 job `lab.netbox_sync` |
| GET | `/labs/{lab_id}/netbox/status` | viewer | `{target, last_sync}` |

### ZTP, MSS, ACT — `labs/ztp_api.py`, `labs/mss_api.py`, `labs/act_api.py`

| Method | Path | Role | Purpose |
|---|---|---|---|
| GET | `/labs/{lab_id}/ztp` | viewer | ZTP timeline (15 s): per node MAC, IP, lease, bootfile, eAPI |
| GET | `/labs/{lab_id}/mss` | viewer | segmentation knob, policy matrix, rendered_on, last verify |
| POST | `/labs/{lab_id}/mss/verify` | operator | 202 job `lab.mss_verify` `{cases\|null}` |
| GET | `/labs/{lab_id}/act` | viewer | ACT topology info, nodes, clients, hints |
| GET | `/labs/{lab_id}/act/topology` | viewer | ACT topology YAML download |
| POST | `/labs/{lab_id}/act/prepare` | operator | re-run ACT post-processing |

### Learn — `learn/api.py`

| Method | Path | Role | Purpose |
|---|---|---|---|
| GET | `/learn/schema/search` | viewer | `?q=&schema=&limit=` fuzzy key search |
| GET | `/learn/schema/{name}` | viewer | `?key=` one schema entry |
| GET | `/learn/explain` | viewer | `?lab_id=&node=&line=` explain one config line |
| POST | `/learn/explain` | operator | `{lab_id, node, lines?, all?}` batch explain |
| GET | `/learn/lessons` | viewer | lesson list with progress |
| GET | `/learn/lessons/{lesson_id}` | viewer | parsed lesson |
| POST | `/learn/lessons/{lesson_id}/check` | operator | `{lab_id, checkpoint_id}` → `{passed, detail}` |
| GET | `/learn/progress` | viewer | passed checkpoints |
| GET | `/learn/glossary` | viewer | glossary terms |
| GET | `/learn/tours` | viewer | tours keyed by page |

### Meta and updates — `meta/api.py`

| Method | Path | Role | Purpose |
|---|---|---|---|
| GET | `/meta` | public | version, git sha, build date, AVD/ANTA versions, latest changelog entry, app title, GitHub URL, support/feedback e-mail, registration flag, intro video URL |
| GET | `/meta/changelog` | public | parsed `CHANGELOG.md` |
| GET | `/meta/notices` | public | licence and third-party notices |
| GET | `/meta/update` | viewer | update status: current, latest, available, mode, can_self_update, reason, last_update |
| POST | `/meta/update/check` | operator | force an update check |
| POST | `/meta/update/apply` | operator | 202 job `app.update` `{force?, to?}`; 409 when impossible or jobs run |
| GET | `/meta/update/log` | viewer | `{log, last_update}` |

### Feedback — `feedback/api.py`

| Method | Path | Role | Purpose |
|---|---|---|---|
| POST | `/feedback` | operator | `{kind, title, body, page?, include_diagnostics}` → 201 |
| GET | `/feedback` | viewer | `?status=` list |
| GET | `/feedback/diagnostics` | viewer | preview of the diagnostics |
| GET | `/feedback/{fid}` | viewer | one entry |
| PATCH | `/feedback/{fid}` | operator | `{status: new\|triaged\|done}` |
| DELETE | `/feedback/{fid}` | operator | delete |

### Users — `api/users.py`

| Method | Path | Role | Purpose |
|---|---|---|---|
| GET | `/users` | admin | users |
| GET | `/users/audit` | admin | `?limit=100` audit log |
| POST | `/users` | admin | create `{username, role, password?}` (no password → temporary password returned once) |
| GET | `/users/requests` | admin | account requests `?status=pending\|approved\|rejected\|all` (default pending) |
| POST | `/users/requests/{req_id}/approve` | admin | `{role}` → account + temporary password (once) |
| POST | `/users/requests/{req_id}/reject` | admin | `{reason?, notify}` (mail the applicant when SMTP is configured) |
| PATCH | `/users/{user_id}` | admin | `{role?, disabled?}` |
| DELETE | `/users/{user_id}` | admin | delete (not self, not the last admin) |
| POST | `/users/{user_id}/reset-password` | admin | new temporary password (once) |
| POST | `/users/{user_id}/mfa/reset` | admin | clear MFA |
| POST | `/users/{user_id}/unlock` | admin | clear lockout |

### Registration and admin settings — `api/registration.py`

| Method | Path | Role | Purpose |
|---|---|---|---|
| POST | `/register` | public | public account request → 201; 403 disabled, 409 taken, 429 rate limit |
| GET | `/register/status/{req_id}` | public | `{status}` of a request |
| GET | `/admin/settings` | admin | effective admin settings (`has_smtp_password`) |
| PUT | `/admin/settings` | admin | partial update; `null` resets to env default; `smtp_password: ""` clears |
| POST | `/admin/settings/test-mail` | admin | send a test mail → `{ok, error, to}` |

### AI designer — `ai/api.py`

| Method | Path | Role | Purpose |
|---|---|---|---|
| GET | `/ai/link` | viewer | own link (no key), shared availability, effective source |
| PUT | `/ai/link` | operator | `{provider, base_url?, model, api_key?}` (key write-only) |
| DELETE | `/ai/link` | operator | remove link and key |
| POST | `/ai/link/test` | operator | probe the provider → `{ok, models, model_listed, error}` |
| POST | `/ai/design` | operator | `{lab_id?, template?, prompt, history?, current_knobs?}` → proposal / question / text |
| POST | `/ai/design/apply` | operator | `{lab_id, knobs, topology_ops}` → re-validated, applied, committed |
| GET | `/admin/ai` | admin | shared AI default (no key) |
| PUT | `/admin/ai` | admin | update the shared default |
| POST | `/admin/ai/test` | admin | probe the shared model |
## 7.4 Job types

`GET /jobs/types` lists the registered types. Parameters are the JSON body of the endpoint that starts the job (stored
as the job's `params`); defaults in parentheses.

| Job type | Started by | Parameters | Result (main fields) |
|---|---|---|---|
| `host.probe` | `POST /hosts/{id}/probe` | — | capabilities document without raw outputs, `recommendations` |
| `host.bootstrap` | `POST /hosts/{id}/bootstrap` | `recipe` (`clab-host` \| `act-tools`), `options: {clab_mode: native\|container, native_toolchain: false}` | `family`, `steps` (`skip`/`ok`/`FAIL`), `applied`, `recommendations` |
| `host.verify` | `POST /hosts/{id}/verify` | — | `ok`, `checks`, `recommendations` |
| `image.download` | `POST /images/download` | `fmt` (`cEOS` \| `cEOS64` \| `cEOSarm`), `version`, `force` (false) | image row: path, size, sha256, docker tag |
| `image.push` | `POST /images/{id}/push/{host_id}` | `options: {remote_download: false, force: false}` | method (`import`, `remote_download`, `save_load`), host |
| `lab.build` | `POST /labs/{id}/build` | — | `hosts`, `warnings`, `changed_files`, `anta_tests`, topology summary, commit |
| `lab.topology` | `POST /topology/{id}/regenerate` | — | regenerated `clab/` from the last build |
| `lab.deploy` | `POST /labs/{id}/deploy` | `reconfigure` (true), `wait_eapi` (true), `wait_timeout` (600, 10–7200 s) | `nodes`, `ready`, `pending`, `duration` |
| `lab.destroy` | `POST /labs/{id}/destroy` | — | `destroyed` |
| `lab.status` | `POST /labs/{id}/status/refresh` | — | `deployed`, `nodes` (state, mgmt IP, image, eapi) |
| `lab.validate` | `POST /labs/{id}/validate` | `extra_fabric_validation` (true), `skip_tests` (`["VerifyNTP","VerifyReloadCause"]`), `user_catalogs` (false), `tags` ([]) | `totals`, `by_device`, `report_paths`, `validation_run_id`, `ansible_rc` |
| `lab.sync` | `POST /labs/{id}/sync` | — | `remote_dir`, `files` |
| `lab.push_configs` | `POST /labs/{id}/push` | `hosts` (null = changed + not tracked) | `pushed`, `skipped` |
| `lab.psirt` | `POST /labs/{id}/psirt` | `tests` (e.g. `["SA117","SA140"]`; empty = all) | per device × advisory assessments, summary, report paths |
| `lab.snapshot` | `POST /labs/{id}/snapshot` | `commands` (7 defaults: running-config, version, LLDP, BGP, EVPN, MLAG, VXLAN; 1–50) | snapshot id, nodes, commands |
| `lab.traffic_ping` | `POST /labs/{id}/traffic/ping` | `pairs` (null = full mesh), `count` (3, ≤ 100), `size` (56), `vlan` | loss matrix, run id |
| `lab.traffic_iperf` | `POST /labs/{id}/traffic/iperf` | `flows: [{src, dst, proto tcp\|udp, bandwidth, duration (10), parallel (1), reverse, vlan}]` (1–64), `sequential` (true) | per-flow throughput, retransmits, jitter, loss |
| `lab.client_deploy` | `POST /labs/{id}/clients` | `name`, `switch`, `switch_port` (`EthernetN`), `vlans`, `mode` (access), `ip` (auto), `image`, `deploy` (true) | client, push hosts, `interface_visible`, gateway ping |
| `lab.client_remove` | `DELETE /labs/{id}/clients/{name}` | — | removed client, push hosts, warnings |
| `lab.mss_verify` | `POST /labs/{id}/mss/verify` | `cases: [{src, dst, application, expect allow\|deny, vlan?}]` or null (auto-derive) | passed, failed, per-case observed |
| `lab.cv_onboard` | `POST /labs/{id}/cv/onboard` | — | `nodes` (TerminAttr running, streaming), `complete` |
| `lab.cv_deploy` | `POST /labs/{id}/cv/deploy` | `submit_workspace` (true), `run_change_control` (false), `skip_missing_devices` (true), `strict_tags` (false), `workspace_name` | workspace, change control, devices, errors, warnings |
| `lab.netbox_sync` | `POST /labs/{id}/netbox/sync` | `delete_stale` (false), `dry_run` (false), `include_clients` (true) | `created`, `updated`, `deleted`, `unchanged`, `skipped`, `netbox_url` |
| `lab.git_push` | `POST /labs/{id}/git/push` | `force` (false) | last sync |
| `lab.git_pull` | `POST /labs/{id}/git/pull` | — | fast-forward result, re-validation |
| `lab.import_git` | `POST /labs/import/git` | `name`, `url`, `branch`, `auth_kind`, `secret_id` / `token`, `host_id` | new custom lab |
| `app.update` | `POST /meta/update/apply` | `force` (false), `to` | from, to, mode, helper |

Act labs use the same `lab.deploy` / `lab.destroy` / `lab.status` / `lab.validate` types; the executor is chosen from the
lab's target. Jobs that refuse to run concurrently on one lab answer 409 at the API before a job is created.

## 7.5 Example: build, deploy and validate from a script

```bash
H=https://avdlab.example.com/api; J='-H content-type:application/json'
curl -s -c jar $J -d '{"username":"ops","password":"…"}' $H/auth/login
LAB=$(curl -s -b jar $J -d '{"name":"demo","template":"single-dc-l3ls","host_id":"<host id>","knobs":{"leaf_pairs":3}}' $H/labs | jq -r .id)
JOB=$(curl -s -b jar -X POST $H/labs/$LAB/build | jq -r .job_id)
until [ "$(curl -s -b jar $H/jobs/$JOB | jq -r .status)" != running ]; do sleep 5; done
curl -s -b jar $J -d '{"reconfigure":true,"wait_eapi":true,"wait_timeout":600}' $H/labs/$LAB/deploy
# … wait, then
curl -s -b jar $J -d '{}' $H/labs/$LAB/validate
```

With MFA enabled the login answers `{"mfa_required": true}` and needs `POST /auth/mfa/verify {"code": "123456"}` with the
same cookie jar; for automation use an operator account without MFA on a trusted network, or keep MFA and supply the code.
