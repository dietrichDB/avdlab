# avdlab — Module 2b contract: deploy, destroy, status, validate (containerlab executor)

Builds on `docs/CONTRACT.md` and the existing code (read `backend/avdlab/hosts/ssh.py`, `hosts/recipes.py`,
`labs/build.py`, `labs/service.py`, `topology/clab.py`, `core/config.py`, `core/db.py`). All work goes into a new
package `backend/avdlab/executors/` plus small edits in `labs/api.py` and the frontend lab pages.

## Remote layout on the lab host

`<settings.remote_root>/labs/<slug>/` (default `/opt/avdlab/labs/<slug>/`) — a copy of the lab directory made with
`SSHClient.rsync_dir(local, remote, exclude=[".git", "documentation", "anta/reports", "**/__pycache__"])`. Ownership: the SSH user
(no sudo). The containerlab topology stays at `clab/topology.clab.yml` with `startup-config: ../intended/configs/<host>.cfg`
paths relative to the topo file (already how `topology/clab.py` writes it), so it works unchanged after the copy.

containerlab needs root: run `sudo containerlab ...` via `ssh.run(cmd, sudo=True)` (works with NOPASSWD and the new sudo
password). `containerlab` resolves to either the native binary or the `/usr/local/bin/containerlab` wrapper installed
by the bootstrap (container mode) — do not care which.

## Executor interface `executors/base.py`

```python
class Executor(Protocol):
    async def deploy(self, ctx: JobContext, lab: Lab, host: Host) -> DeployResult
    async def destroy(self, ctx, lab, host) -> None
    async def status(self, ctx, lab, host) -> LabStatus          # nodes with container state + mgmt ip + eAPI reachable
    async def validate(self, ctx, lab, host, *, tags, skip_tests, extra_fabric) -> ValidationSummary
def get_executor(lab) -> Executor   # "clab" → ClabExecutor; "act" → raise NotImplementedError("ACT executor: stage 2")
```

## `executors/clab.py` — `ClabExecutor`

**deploy** (job `lab.deploy`, params `{reconfigure: bool=true, wait_eapi: bool=true, wait_timeout: int=600}`):
1. Preconditions: lab state ∈ {built, deployed, destroyed}, lab has a host, `clab/topology.clab.yml` exists; host capabilities show docker+containerlab (else clear error pointing at bootstrap).
2. Ensure the cEOS image exists on the host: read `topology.kinds.arista_ceos.image`; `docker image inspect <image>` over SSH; if missing and an `Image` row with `docker_tag == image` has `local_path`, upload + `docker import` (reuse `images/service.py` push logic — refactor into a callable if needed); else fail with "image <tag> not on host — push it from Images".
3. `rsync_dir` lab → remote; log file counts.
4. `sudo containerlab deploy -t clab/topology.clab.yml --reconfigure` (cwd = remote lab dir; `cd <dir> && ...`), streamed to the job log, timeout 900 s.
5. `sudo containerlab inspect -t clab/topology.clab.yml --format json` → parse containers (name, state, ipv4 address, image, kind); save to `lab.settings["last_inspect"]` and return as `nodes`.
6. If `wait_eapi`: poll every 10 s until every cEOS node answers eAPI: run inside the agent container on the host
   `docker run --rm --network <clab mgmt network name from topology.mgmt.network> <agent_image> python3 -c "<httpx POST /command-api show version>"` — or simpler and dependency-free: from the host itself `curl -sk -u <user>:<pw> https://<mgmt-ip>/command-api -d '{"jsonrpc":"2.0","method":"runCmds","params":{"version":1,"cmds":["show version"]},"id":1}'` (mgmt IPs are reachable from the host). Credentials: the lab's `device_password` knob (secret; `labs/api.py` shows how secrets knobs are stored) and user `arista` (make the user a knob default if not already present; fall back to `arista/arista`). Log per node ready/pending; fail after `wait_timeout` with the list of pending nodes.
7. Set `lab.state = deployed`, `lab.settings["deployed_at"]`, return `{nodes, ready, pending, duration}`.

**destroy** (job `lab.destroy`): `sudo containerlab destroy -t clab/topology.clab.yml --cleanup` in the remote dir; tolerate "lab not found"; state → `destroyed`.

**status** (job `lab.status`, quick, also exposed synchronously as `GET /labs/{id}/status` running it inline with a 20 s budget): `containerlab inspect --format json` + eAPI probe per node (as above but single pass); returns `{nodes: [{name, kind, state, mgmt_ip, image, eapi: bool}], deployed: bool}`; updates `lab.settings["last_status"]`.

**validate** (job `lab.validate`, params `{extra_fabric_validation: bool=true, skip_tests: [str]=["VerifyNTP","VerifyReloadCause"], user_catalogs: bool=false, tags: [str]=[]}`):
1. Preconditions: deployed.
2. Write a temporary `playbooks/validate.yml` into the lab dir (commit it via `labs/service.py` if absent) using `arista.avd.anta_runner` with the params mapped to role vars (`avd_catalogs_extra_fabric_validation`, `avd_catalogs_filters: [{skip_tests: [...]}]`, `user_catalogs_enabled`, `anta_runner_tags`, `anta_report_exclude_statuses: []`). The inventory must carry the eAPI connection vars — the templates already put `ansible_connection: ansible.netcommon.httpapi` etc. in `group_vars/FABRIC.yml` (verify; add if missing).
3. rsync lab → remote (so intended/ and anta/ are current), then run on the host: `docker run --rm --network <clab mgmt network> -v <remote lab dir>:/lab -w /lab <settings.agent_image> ansible-playbook playbooks/validate.yml` (the agent image has ansible + arista.avd + anta; the mgmt network makes clab IPs reachable). Stream output. `ignore_errors` is not used: treat the ansible rc as informational, the report decides.
4. Pull back `anta/reports/anta_report.{json,md,csv}` with `ssh.get` into the local lab dir; parse the JSON (list of ANTA results: name, test, categories, result, messages, description, custom_field) into totals per status and per device; insert a `ValidationRun` row (`lab_id, job_id, totals, report_path`); commit the reports in the lab repo; state → `validated` if no failure/error else stay `deployed`.
5. Return `{totals, by_device, report_paths, validation_run_id}`.

Important details: the `anta_report_*` paths are relative to the lab root inside the container (`/lab/anta/reports/...`); `intended/structured_configs` must exist (build first). If the agent image lacks ANTA ≥1.8 the run fails clearly.

## Labs API additions (`labs/api.py`)

- `POST /labs/{id}/deploy` (body = deploy params) → 202 `{job_id}`; 409 if not built / no host.
- `POST /labs/{id}/destroy` → 202; `GET /labs/{id}/status` → inline status (returns `{deployed: false, nodes: []}` fast if the lab was never deployed); `POST /labs/{id}/status/refresh` → 202 job.
- `POST /labs/{id}/validate` (body = validate params) → 202; `GET /labs/{id}/validations` → list of `ValidationRun` (id, job_id, created_at, totals, report_path); `GET /labs/{id}/validations/{run_id}/report?format=json|md|csv` → file content.
- `DELETE /labs/{id}` must refuse (409) while state == deployed.

## Frontend

- Lab detail header: state badge + action buttons **Build**, **Deploy**, **Validate**, **Destroy** (confirm), each opening the job drawer; disabled according to state.
- New tab **Runtime** (`RuntimeTab.tsx`): status table (node, kind, container state, mgmt IP, image, eAPI ✓/✗) with a Refresh button (GET status, and "Refresh (job)" for the slow path); deploy options (reconfigure, wait for eAPI).
- New tab **Validation** (`ValidationTab.tsx`): run form (extra fabric validation, skip tests chips, user catalogs, tags) → job; list of runs with totals as coloured pills; selecting a run shows the parsed results table (device, test, category, result, messages) with status filters and the raw Markdown report toggle.
- Types + API client + MSW mocks updated; vitest tests for the validation results table filtering and the runtime status table.

## Tests (backend `tests/test_executors.py`)

Fake SSH (same style as `test_hosts.py`) scripted for: `docker image inspect`, `containerlab deploy`, `containerlab inspect --format json` (return a realistic JSON for 2 spines + 2 leaves), `curl … /command-api` (ready after N polls), `containerlab destroy`, the `docker run … ansible-playbook` call, and `ssh.get` producing a canned `anta_report.json` (write it locally). Cover: deploy happy path (state → deployed, nodes parsed, eAPI wait), deploy refused when not built, image missing → clear error, destroy, status parsing, validate → ValidationRun totals + state, API endpoints. Keep the existing 43 tests green.
