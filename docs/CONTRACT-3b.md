# avdlab — Phase 3b contract: per-device tags, kill job, kill container

## 1. Per-device CloudVision tags

Knob `device_tags` (new, all templates) — `type: yaml`, default `{}`, shape `{<hostname>: {<label>: <value>, ...}}`. Rendered per node
into the node's AVD settings as `structured_config.metadata.cv_tags.device_tags: [{name, value}]` (node level under the node-type key, e.g.
`l3leaf.node_groups[].nodes[].structured_config`), which eos_designs merges last so the tags land in `intended/structured_configs/<host>.yml`
`metadata.cv_tags.device_tags` next to the global `generate_cv_tags` tags — **verify with a real pyavd build in a test** that both global
(`cloudvision.device_tags`) and per-device tags appear and that `cv_deploy` input (`metadata.cv_tags`) is well-formed; if node
`structured_config` does not merge into `metadata` in 6.4, fall back to a post-build step in `labs/build.py` that injects the tags into the
written structured config YAML (before ANTA catalog/topology generation) and document that. Unknown hostnames → 422 listing them.
Also expose `GET /labs/{id}/devices` with a `tags: {label: value}` column (merged global + per-device, per-device wins).
UI: Settings → a "Device tags" editor: table hostname × tag columns (add tag column, edit cells), backed by the knob; Overview devices table shows tags.

## 2. Kill job

- `jobs/engine.py`: handle `asyncio.CancelledError` in `_run` (status → `cancelled`, `ended_at`, log line) — today a cancelled task never
  updates the row. `JobRunner.cancel()` must (a) set the context cancel event, (b) cancel the asyncio task, (c) if the job holds an
  `SSHClient` (register it on the context via `ctx.attach_ssh(client)` in `SSHClient.connect` when a ctx is passed — add an optional `ctx`
  kwarg and use it in every job that opens SSH), close the SSH connection so remote commands die with the session. Return
  `{"cancelled": true, "was_running": bool}`. Cancelling a queued/finished job → `{"cancelled": false}`.
- Tests: a job sleeping in a loop is cancelled → row `cancelled`, log contains "job cancelled"; a job with an attached fake SSH → fake's
  `close()` called.
- UI: "Kill" button in the job drawer and in the Jobs page/list (only for queued/running), with confirm; status badge updates.

## 3. Kill docker containers (host monitor)

- `DELETE /hosts/{id}/containers/{name}?force=true` → inline `docker rm -f <name>` (or `docker stop` when `force=false`) over SSH; 404 if
  not in `docker ps -a`; returns `{name, removed: bool, output}`. Refuse (409) to remove a container whose `avdlab.lab` label maps to a lab
  in state deployed/validated unless `?force=true` **and** body `{"confirm": "<container name>"}`; log an audit line in the app log.
- UI: Host detail → containers table gets a "Stop" and a "Kill" (rm -f) button per row with a confirm dialog that requires typing the
  container name for lab containers; list refreshes afterwards.
