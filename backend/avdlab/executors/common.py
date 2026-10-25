"""Helpers shared by the containerlab and ACT executors: remote layout, lab sync, eAPI probing over the host's shell
(`curl` with credentials on stdin), lab row updates and the ANTA validation run inside the agent container.

`clab.py` re-exports these names so existing `from .clab import ...` imports keep working.
"""
from __future__ import annotations

import asyncio
import fnmatch
import json
import shlex
import shutil
import time
from pathlib import Path
from typing import Any

import yaml

from ..core.config import settings
from ..core.db import Host, Lab, LabState, SessionLocal, ValidationRun, new_id
from ..core.security import secret_store
from ..hosts.ssh import SSHClient
from ..labs import service
from .base import ExecContext, ExecutorError, ValidateParams, ValidationSummary, parse_anta_results

VALIDATE_PLAYBOOK = "playbooks/avdlab-validate.yml"
RSYNC_EXCLUDES = [".git", "documentation", "anta/reports", "anta/runs", "**/__pycache__"]
MIN_ANTA = (1, 8)
REPORT_FORMATS = ("json", "md", "csv")
DEPLOYED_STATES = (LabState.deployed, LabState.validated)
DEPLOYABLE_STATES = (LabState.built, LabState.deployed, LabState.validated, LabState.destroyed)
EAPI_PAYLOAD = json.dumps({"jsonrpc": "2.0", "method": "runCmds",
                           "params": {"version": 1, "cmds": ["show version"], "format": "json"}, "id": 1})


def remote_lab_dir(lab: Lab) -> str:
    return f"{settings.remote_root.rstrip('/')}/labs/{lab.slug}"


def docker_needs_sudo(host: Host) -> bool:
    docker = (host.capabilities or {}).get("docker") or {}
    return not docker.get("user_can_run", True)


def count_files(root: Path, excludes: list[str] = RSYNC_EXCLUDES) -> int:
    n = 0
    for f in root.rglob("*"):
        if not f.is_file():
            continue
        rel = f.relative_to(root).as_posix()
        if any(rel == p or rel.startswith(p.rstrip("/") + "/") or fnmatch.fnmatch(rel, p) or p.strip("*/") in f.parts
               for p in excludes):
            continue
        n += 1
    return n


def eapi_credentials(lab: Lab) -> tuple[str, str]:
    """(user, password) for eAPI: `ansible_user` from group_vars (default arista), password from the secret knob
    `device_password`, else group_vars `ansible_password`, else `arista`."""
    from ..labs.templates import load_group_vars_dir

    user, gv_password = "arista", None
    try:
        for data in load_group_vars_dir(service.lab_dir(lab)).values():
            if data.get("ansible_user"):
                user = str(data["ansible_user"])
            if data.get("ansible_password"):
                gv_password = str(data["ansible_password"])
    except Exception:  # noqa: BLE001 - unreadable group_vars → defaults
        pass
    st = lab.settings or {}
    token = (st.get("secret_knobs") or {}).get("device_password")
    password = secret_store.decrypt(token) if token else gv_password
    return user, password or "arista"


def curl_config_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def eapi_curl(ip: str, payload: str = EAPI_PAYLOAD, *, max_time: int = 5) -> str:
    """curl command for an eAPI call; the `user = "u:p"` config line is passed on stdin (`-K -`)."""
    return (f"curl -sk --max-time {max_time} -K - -H 'Content-Type: application/json' "
            f"-d {shlex.quote(payload)} https://{ip}/command-api")


def curl_auth(user: str, password: str) -> str:
    return f"user = {curl_config_quote(f'{user}:{password}')}\n"


async def eapi_ok(ssh: Any, ip: str, user: str, password: str) -> bool:
    try:
        res = await ssh.run(eapi_curl(ip), timeout=15, input=curl_auth(user, password))
    except Exception:  # noqa: BLE001 - a single probe failing just means "not ready"
        return False
    if not res.ok:
        return False
    try:
        return "result" in json.loads(res.stdout)
    except ValueError:
        return False


async def wait_eapi(ctx: ExecContext, ssh: Any, targets: dict[str, str | None], user: str, password: str, *,
                    timeout: float, interval: float, on_ready: Any = None) -> tuple[list[str], list[str]]:
    """Poll `targets` ({name: mgmt ip}) until every node answers eAPI; raises after `timeout` s.
    Returns (ready, pending) — pending is empty on success."""
    ready: list[str] = []
    pending = list(targets)
    deadline = time.monotonic() + timeout
    ctx.log(f"[eapi] waiting for {len(pending)} EOS nodes (user {user}, timeout {timeout}s)")
    while True:
        ctx.check_cancelled()
        for name in list(pending):
            ip = targets[name]
            if ip and await eapi_ok(ssh, ip, user, password):
                pending.remove(name)
                ready.append(name)
                if on_ready is not None:
                    on_ready(name)
                ctx.log(f"[eapi] {name} ({ip}) ready")
        if not pending:
            return ready, pending
        if time.monotonic() >= deadline:
            raise ExecutorError(f"eAPI not reachable after {timeout}s on: {', '.join(pending)}")
        ctx.log(f"[eapi] pending: {', '.join(pending)}")
        await asyncio.sleep(interval)


def validate_playbook(fabric_group: str, params: ValidateParams) -> str:
    """Playbook running arista.avd.anta_runner with the validate params mapped to role vars (play vars win over group_vars)."""
    play_vars: dict[str, Any] = {
        "avd_catalogs_extra_fabric_validation": params.extra_fabric_validation,
        "user_catalogs_enabled": params.user_catalogs,
        "anta_report_exclude_statuses": [],
    }
    if params.skip_tests:
        play_vars["avd_catalogs_filters"] = [{"skip_tests": list(params.skip_tests)}]
    if params.tags:
        play_vars["anta_runner_tags"] = list(params.tags)
    play = [{
        "name": "Fabric validation (generated by avdlab)",
        "hosts": fabric_group,
        "gather_facts": False,
        "vars": play_vars,
        "tasks": [{"name": "Validate devices with ANTA", "ansible.builtin.import_role": {"name": "arista.avd.anta_runner"}}],
    }]
    return "---\n# Generated by avdlab for each validation run; edits are overwritten.\n" + yaml.safe_dump(play, sort_keys=False)


def version_tuple(text: str) -> tuple[int, ...]:
    parts: list[int] = []
    for p in text.strip().split("."):
        digits = "".join(ch for ch in p if ch.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


async def update_lab(lab_id: str, *, state: LabState | None = None, **settings_kv: Any) -> None:
    async with SessionLocal() as s:
        row = await s.get(Lab, lab_id)
        if row is None:
            return
        if state is not None:
            row.state = state
        if settings_kv:
            row.settings = {**(row.settings or {}), **settings_kv}
        await s.commit()


async def sync_lab(ctx: ExecContext, ssh: Any, lab: Lab, host: Host, *, tag: str = "deploy") -> str:
    root = service.lab_dir(lab)
    rdir = remote_lab_dir(lab)
    n_files = await asyncio.to_thread(count_files, root)
    ctx.log(f"[{tag}] syncing {n_files} files to {host.name}:{rdir}")
    await ssh.rsync_dir(root, rdir, exclude=RSYNC_EXCLUDES, log=ctx.log)
    return rdir


async def agent_uid_gid(ssh: Any) -> str:
    ids = await ssh.run("id -u; id -g")
    return ":".join(ids.stdout.split()[:2]) if ids.ok and len(ids.stdout.split()) >= 2 else ""


async def chown_back(ssh: Any, rdir: str, sub: str, uid_gid: str, *, sudo: bool, log: Any) -> None:
    """Files written by the agent container user → give them back to the SSH user."""
    if uid_gid:
        await ssh.run(f"docker run --rm --user 0 -v {shlex.quote(rdir)}:/lab --entrypoint chown "
                      f"{shlex.quote(settings.agent_image)} -R {uid_gid} /lab/{sub}", sudo=sudo, timeout=300, log=log)


async def run_validation(ctx: ExecContext, lab: Lab, host: Host, params: ValidateParams, *,
                         network: str) -> ValidationSummary:
    """ANTA via `arista.avd.anta_runner` in the agent container attached to `network` (clab mgmt network, or `host`
    on an ACT tools server), reports pulled back, ValidationRun row, state deployed/validated."""
    if lab.state not in DEPLOYED_STATES:
        raise ExecutorError(f"lab is {lab.state.value}: deploy it before validating")
    root = service.lab_dir(lab)
    sc_dir = root / "intended" / "structured_configs"
    if not sc_dir.is_dir() or not any(sc_dir.iterdir()):
        raise ExecutorError("intended/structured_configs is empty: build the lab first")
    fabric_group = str((lab.settings or {}).get("fabric_name") or "FABRIC")
    sha = await asyncio.to_thread(service.write_files, lab, {VALIDATE_PLAYBOOK: validate_playbook(fabric_group, params)},
                                  "validate: generated playbook")
    ctx.log(f"[validate] {VALIDATE_PLAYBOOK} {'updated' if sha else 'unchanged'}: extra_fabric={params.extra_fabric_validation} "
            f"skip_tests={params.skip_tests} user_catalogs={params.user_catalogs} tags={params.tags}")
    rdir = remote_lab_dir(lab)
    q = shlex.quote
    dsudo = docker_needs_sudo(host)
    agent = settings.agent_image
    reports_local = root / "anta" / "reports"
    reports_local.mkdir(parents=True, exist_ok=True)
    async with SessionLocal() as s:
        async with SSHClient.connect(host, s, log=ctx.log, ctx=ctx) as ssh:
            # ANTA version in the agent image
            res = await ssh.run(f"docker run --rm --entrypoint python3 {q(agent)} -c "
                                f"{q('import anta; print(anta.__version__)')}", sudo=dsudo, timeout=300, log=ctx.log)
            version = res.stdout.strip().splitlines()[-1] if res.ok and res.stdout.strip() else ""
            if not version or version_tuple(version) < MIN_ANTA:
                raise ExecutorError(f"agent image {agent} lacks ANTA >= {'.'.join(map(str, MIN_ANTA))} "
                                    f"(found {version or 'none'}): update settings.agent_image or re-run host verify")
            ctx.log(f"[validate] agent image ANTA {version}")
            await sync_lab(ctx, ssh, lab, host, tag="validate")
            uid_gid = await agent_uid_gid(ssh)
            await ssh.run(f"cd {q(rdir)} && mkdir -p anta/reports anta/avd_catalogs anta/user_catalogs && "
                          f"chmod -R a+rwX anta && rm -f anta/reports/anta_report.*", log=ctx.log)
            ctx.check_cancelled()
            cmd = (f"docker run --rm --network {q(network)} -v {q(rdir)}:/lab -w /lab -e ANSIBLE_FORCE_COLOR=0 "
                   f"{q(agent)} ansible-playbook -i inventory.yml {VALIDATE_PLAYBOOK}")
            ctx.log(f"[validate] {cmd}")
            run = await ssh.run(cmd, sudo=dsudo, timeout=3600, log=ctx.log)
            ctx.log(f"[validate] ansible-playbook rc={run.returncode} (informational; the report decides)")
            await chown_back(ssh, rdir, "anta", uid_gid, sudo=dsudo, log=ctx.log)
            pulled: dict[str, Path] = {}
            for fmt in REPORT_FORMATS:
                local = reports_local / f"anta_report.{fmt}"
                local.unlink(missing_ok=True)
                try:
                    await ssh.get(f"{rdir}/anta/reports/anta_report.{fmt}", local)
                except Exception as exc:  # noqa: BLE001
                    ctx.log(f"[validate] could not fetch anta_report.{fmt}: {exc}")
                    continue
                if local.exists():
                    pulled[fmt] = local
            if "json" not in pulled:
                raise ExecutorError(f"no ANTA JSON report produced (ansible-playbook rc={run.returncode}); see log above")
    try:
        results = json.loads(pulled["json"].read_text())
    except ValueError as exc:
        raise ExecutorError(f"invalid ANTA JSON report: {exc}") from exc
    totals, by_device = parse_anta_results(results)
    run_id = new_id()
    run_dir = root / "anta" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    report_paths: dict[str, str] = {}
    for fmt, path in pulled.items():
        shutil.copy2(path, run_dir / path.name)
        report_paths[fmt] = (run_dir / path.name).relative_to(root).as_posix()
    await asyncio.to_thread(service.commit_all, root, f"validate: {totals.get('success', 0)}/{totals['total']} passed")
    async with SessionLocal() as s:
        s.add(ValidationRun(id=run_id, lab_id=lab.id, job_id=ctx.job_id, totals=totals, report_path=report_paths["json"]))
        await s.commit()
    failed = totals.get("failure", 0) + totals.get("error", 0)
    await update_lab(lab.id, state=LabState.deployed if failed else LabState.validated, last_validation_run=run_id)
    ctx.log("[validate] totals: " + ", ".join(f"{k}={v}" for k, v in totals.items()))
    return ValidationSummary(totals=totals, by_device=by_device, report_paths=report_paths, validation_run_id=run_id,
                             ansible_rc=run.returncode)


# ------------------------------------------------------------------------------------------------ ansible with secrets
STDIN_EXTRA_VARS = "/tmp/avdlab-extra-vars.yml"


def ansible_stdin_cmd(docker_opts: str, ansible_args: str, *, image: str | None = None) -> str:
    """`docker run --rm -i <docker_opts> <agent> sh -c '...'`: extra vars (credentials) arrive on stdin and are copied
    to a 0600 file INSIDE the throw-away container before ansible-playbook starts.

    `-e @/dev/stdin` does not work: Ansible resolves /dev/stdin to /proc/1/fd/0 -> "pipe:[N]", which is not a
    readable path (0.7.2 fix for lab.client_deploy / act deploy). The file lives only in the container's own
    filesystem, which is removed with it (--rm); nothing lands on the host or on a command line.
    """
    inner = f"umask 077; cat > {STDIN_EXTRA_VARS}; exec ansible-playbook {ansible_args} -e @{STDIN_EXTRA_VARS}"
    # the image's own entrypoint stays in place (it `exec "$@"`s) — only the command changes
    return f"docker run --rm -i {docker_opts} {shlex.quote(image or settings.agent_image)} sh -c {shlex.quote(inner)}"

