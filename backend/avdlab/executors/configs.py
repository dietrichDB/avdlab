"""Running configs, config diff vs intended, and `lab.snapshot` (show-command snapshots committed to the lab repo).

  running config   `docker exec <node> Cli -p 15 -c "show running-config"` over SSH (inline, 25 s budget)
  config diff      normalised intended/configs/<node>.cfg vs running (unified diff)
  snapshot         snapshots/<YYYYmmdd-HHMMSS>/<node>/<cmd slug>.txt (+ meta.json), committed
"""
from __future__ import annotations

import asyncio
import difflib
import json
import re
import shlex
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator

from ..core.db import Host, Lab, SessionLocal
from ..hosts.ssh import SSHClient
from ..jobs.engine import JobContext, job
from ..labs import service
from .base import ExecutorError
from .clab import DEPLOYED_STATES, EOS_KINDS, docker_needs_sudo, load_topology

RUNNING_CONFIG_BUDGET = 25.0
SNAPSHOT_DIR = "snapshots"
DEFAULT_SNAPSHOT_COMMANDS = ["show running-config", "show version", "show lldp neighbors", "show ip bgp summary",
                             "show bgp evpn summary", "show mlag", "show vxlan vni"]
SNAP_ID_RE = re.compile(r"^\d{8}-\d{6}(?:-\d+)?$")
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_BANNERS = ("! Startup-config last modified", "! device:", "! Command:", "! Time:")


class SnapshotParams(BaseModel):
    commands: list[str] = Field(default_factory=lambda: list(DEFAULT_SNAPSHOT_COMMANDS), min_length=1, max_length=50)

    @field_validator("commands")
    @classmethod
    def _show_only(cls, v: list[str]) -> list[str]:
        out = []
        for c in v:
            c = " ".join(str(c).split())
            if not c.lower().startswith("show ") or any(ch in c for ch in "\n\r"):
                raise ValueError(f"only single-line `show …` commands are allowed: {c!r}")
            out.append(c)
        return out


def cmd_slug(cmd: str) -> str:
    return _SLUG_RE.sub("-", cmd.lower()).strip("-")[:120] or "cmd"


def cli_cmd(node: str, command: str) -> str:
    return f"docker exec {shlex.quote(node)} Cli -p 15 -c {shlex.quote(command)}"


def eos_nodes(lab_dir: Path) -> list[str]:
    topo = load_topology(lab_dir)
    nodes = (topo.get("topology") or {}).get("nodes") or {}
    return sorted(n for n, d in nodes.items() if (d or {}).get("kind") in EOS_KINDS)


def normalize_config(text: str) -> list[str]:
    """Lines to compare: no `!` comment/separator lines (incl. banners), no `end`, no trailing whitespace, no blanks."""
    out: list[str] = []
    for raw in (text or "").replace("\r\n", "\n").split("\n"):
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("!") or stripped == "end" or stripped.startswith(_BANNERS):
            continue
        out.append(line)
    return out


def config_diff(node: str, intended: str, running: str) -> dict[str, Any]:
    a, b = normalize_config(intended), normalize_config(running)
    diff = "\n".join(difflib.unified_diff(a, b, fromfile=f"intended/configs/{node}.cfg", tofile=f"{node}:running-config",
                                          lineterm=""))
    return {"node": node, "intended": intended, "running": running, "diff": diff + ("\n" if diff else ""),
            "identical": a == b}


async def _host(lab: Lab) -> Host:
    if not lab.host_id:
        raise ExecutorError("lab has no host")
    async with SessionLocal() as s:
        host = await s.get(Host, lab.host_id)
    if host is None:
        raise ExecutorError("lab host not found")
    return host


async def fetch_running_config(lab: Lab, host: Host, node: str, *, session: Any = None) -> str:
    async def _go(s: Any) -> str:
        async with SSHClient.connect(host, s) as ssh:
            res = await ssh.run(cli_cmd(node, "show running-config"), sudo=docker_needs_sudo(host), timeout=RUNNING_CONFIG_BUDGET)
        if not res.ok:
            tail = " | ".join((res.stderr or res.stdout).strip().splitlines()[-2:])
            raise ExecutorError(f"show running-config on {node} failed (rc={res.returncode}): {tail}")
        return res.stdout

    if session is not None:
        return await _go(session)
    async with SessionLocal() as s:
        return await _go(s)


# ------------------------------------------------------------------------------------------------ snapshots
def new_snapshot_id(root: Path) -> str:
    base = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    sid, n = base, 1
    while (root / SNAPSHOT_DIR / sid).exists():
        n += 1
        sid = f"{base}-{n}"
    return sid


def list_snapshots(lab_dir: Path) -> list[dict[str, Any]]:
    base = lab_dir / SNAPSHOT_DIR
    out: list[dict[str, Any]] = []
    if not base.is_dir():
        return out
    for d in sorted((p for p in base.iterdir() if p.is_dir() and SNAP_ID_RE.match(p.name)), key=lambda p: p.name,
                    reverse=True):
        meta: dict[str, Any] = {}
        try:
            meta = json.loads((d / "meta.json").read_text())
        except (OSError, ValueError):
            pass
        nodes = sorted(p.name for p in d.iterdir() if p.is_dir())
        commands = meta.get("commands") or []
        created = meta.get("created_at") or datetime.strptime(d.name[:15], "%Y%m%d-%H%M%S").replace(
            tzinfo=timezone.utc).isoformat()
        out.append({"id": d.name, "created_at": created, "nodes": len(nodes), "commands": commands,
                    "node_names": nodes, "command_slugs": [cmd_slug(c) for c in commands]})
    return out


def snapshot_file(lab_dir: Path, snap: str, node: str, slug: str) -> Path:
    if not SNAP_ID_RE.match(snap) or not re.match(r"^[A-Za-z0-9_.-]+$", node) or not re.match(r"^[a-z0-9-]+$", slug):
        raise ValueError("invalid snapshot path")
    return service._safe(lab_dir, f"{SNAPSHOT_DIR}/{snap}/{node}/{slug}.txt")


@job("lab.snapshot")
async def lab_snapshot(ctx: JobContext) -> dict[str, Any]:
    params = SnapshotParams(**(ctx.params or {}))
    lab = await ctx.get_lab()
    if lab.state not in DEPLOYED_STATES:
        raise ExecutorError(f"lab is {lab.state.value}: deploy it first")
    host = await _host(lab)
    root = service.lab_dir(lab)
    nodes = eos_nodes(root)
    if not nodes:
        raise ExecutorError("no cEOS nodes in the topology")
    sid = new_snapshot_id(root)
    snap_dir = root / SNAPSHOT_DIR / sid
    sudo = docker_needs_sudo(host)
    errors: list[dict[str, str]] = []
    t0 = time.monotonic()
    ctx.log(f"[snapshot] {sid}: {len(nodes)} nodes × {len(params.commands)} commands")
    sem = asyncio.Semaphore(4)

    async def one_node(ssh: Any, node: str) -> None:
        async with sem:
            ctx.check_cancelled()
            (snap_dir / node).mkdir(parents=True, exist_ok=True)
            for cmd in params.commands:
                try:
                    res = await ssh.run(cli_cmd(node, cmd), sudo=sudo, timeout=60)
                    text = res.stdout if res.ok else (res.stdout + res.stderr)
                    if not res.ok:
                        errors.append({"node": node, "command": cmd, "error": f"rc={res.returncode}"})
                except Exception as exc:  # noqa: BLE001
                    text = f"% avdlab: {exc}\n"
                    errors.append({"node": node, "command": cmd, "error": str(exc)})
                (snap_dir / node / f"{cmd_slug(cmd)}.txt").write_text(text, encoding="utf-8")
            ctx.log(f"[snapshot] {node}: {len(params.commands)} outputs")

    async with SessionLocal() as s:
        async with SSHClient.connect(host, s, log=ctx.log, ctx=ctx) as ssh:
            await asyncio.gather(*(one_node(ssh, n) for n in nodes))
    created = datetime.now(timezone.utc).isoformat()
    (snap_dir / "meta.json").write_text(json.dumps({"id": sid, "created_at": created, "commands": params.commands,
                                                    "nodes": nodes, "job_id": ctx.job_id, "errors": errors}, indent=2))
    sha = await asyncio.to_thread(service.commit_all, root, f"snapshot {sid}: {len(nodes)} nodes")
    ctx.log(f"[snapshot] committed {sha[:8] if sha else '(no changes)'} in {time.monotonic() - t0:.1f}s, "
            f"{len(errors)} error(s)")
    return {"snapshot_id": sid, "nodes": nodes, "commands": params.commands,
            "command_slugs": [cmd_slug(c) for c in params.commands], "errors": errors, "commit": sha}
