"""Host monitoring (phase 3 §5): background sampler + metrics/processes/containers endpoints.

`HostMonitor` samples every `settings.monitor_interval` s (20 s) each host that has a deployed lab or was viewed in the
last 10 min (`GET /hosts/{id}/metrics` marks it watched), over one SSH session per host kept open while it is sampled.
Each sample runs a single shell script (sections separated by `@@<name>` markers) → parsed into a point
`{at, cpu_pct, load1, load5, load15, mem_total, mem_used, mem_available, ncpu, disk: {"/": {size, used}, ...}}` and
per-container stats. Points go to an in-memory ring buffer (360 = 2 h) and to the `host_metrics` table (pruned to 7 d).

The sampler never raises out of its loop (errors are logged per host, the connection is dropped and re-opened on the
next tick) and is not started when `settings.monitor_enabled` is false. Tests build a `HostMonitor(connect=fake)` and
drive `tick()` directly; the module-level `monitor` is only started by the app lifespan.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import deque
from collections.abc import Callable
from contextlib import AsyncExitStack
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import settings
from ..core.db import Host, HostMetric, Lab, LabState, SessionLocal, get_session
from ..core.security import current_user

log = logging.getLogger("avdlab.monitor")

RING_POINTS = 360  # 2 h at 20 s
WATCH_TTL = 600.0  # a viewed host stays sampled for 10 min
RETENTION = timedelta(days=7)
PRUNE_EVERY = 3600.0
INLINE_BUDGET = 20.0
DEPLOYED = (LabState.deployed, LabState.validated)
DISK_PATHS = ("/", "/var/lib/docker")

SAMPLE_SCRIPT = "; ".join([
    "echo @@loadavg", "cat /proc/loadavg",
    "echo @@stat", "head -1 /proc/stat",
    "echo @@free", "free -b",
    *[f"echo @@df:{p}; df -B1 --output=size,used {p} 2>/dev/null | tail -n 1" for p in DISK_PATHS],
    "echo @@nproc", "nproc",
    "echo @@docker", "{DOCKER}docker stats --no-stream --format '{{json .}}' 2>/dev/null",
    "echo @@end",
])
PS_CMD = "ps -eo pid,user,%cpu,%mem,rss,args --sort=-%mem | head -40"
DOCKER_PS = "docker ps --format '{{json .}}'"
DOCKER_PS_ALL = "docker ps -a --format '{{json .}}'"
audit = logging.getLogger("avdlab.audit")


# ------------------------------------------------------------------------------------------------ parsing (pure)
_UNITS = {"b": 1, "kb": 1000, "mb": 1000**2, "gb": 1000**3, "tb": 1000**4, "pb": 1000**5,
          "kib": 1024, "mib": 1024**2, "gib": 1024**3, "tib": 1024**4, "pib": 1024**5}
_SIZE_RE = re.compile(r"^\s*([\d.]+)\s*([a-zA-Z]*)\s*$")


def parse_size(text: str) -> int | None:
    """docker stats sizes ('512MiB', '1.2kB', '0B') → bytes."""
    m = _SIZE_RE.match(text or "")
    if not m:
        return None
    unit = (m.group(2) or "b").lower()
    if unit not in _UNITS:
        return None
    return int(float(m.group(1)) * _UNITS[unit])


def _pct(text: str) -> float | None:
    try:
        return round(float(str(text).strip().rstrip("%")), 2)
    except ValueError:
        return None


def split_sections(text: str) -> dict[str, str]:
    out: dict[str, list[str]] = {}
    cur: str | None = None
    for line in (text or "").splitlines():
        if line.startswith("@@"):
            cur = line[2:].strip()
            out.setdefault(cur, [])
        elif cur is not None:
            out[cur].append(line)
    return {k: "\n".join(v).strip() for k, v in out.items()}


def parse_cpu_counters(line: str) -> tuple[int, int] | None:
    """`cpu  user nice system idle iowait irq softirq steal …` → (total, idle+iowait)."""
    parts = line.split()
    if not parts or parts[0] != "cpu":
        return None
    vals = [int(x) for x in parts[1:9] if x.isdigit()]
    if len(vals) < 4:
        return None
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
    return sum(vals), idle


def cpu_percent(prev: tuple[int, int] | None, cur: tuple[int, int] | None) -> float | None:
    if not prev or not cur:
        return None
    dt, di = cur[0] - prev[0], cur[1] - prev[1]
    if dt <= 0:
        return None
    return round(max(0.0, min(100.0, 100.0 * (dt - di) / dt)), 1)


def parse_free(text: str) -> dict[str, int | None]:
    for line in text.splitlines():
        if line.startswith("Mem:"):
            f = line.split()
            # Mem: total used free shared buff/cache available
            return {"mem_total": int(f[1]), "mem_used": int(f[2]),
                    "mem_available": int(f[6]) if len(f) > 6 else int(f[3])}
    return {"mem_total": None, "mem_used": None, "mem_available": None}


def parse_docker_stats(text: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        name = str(d.get("Name") or d.get("Container") or "")
        if not name:
            continue
        used, _, limit = str(d.get("MemUsage") or "").partition("/")
        rx, _, tx = str(d.get("NetIO") or "").partition("/")
        out[name] = {"cpu_pct": _pct(d.get("CPUPerc", "")), "mem_used": parse_size(used), "mem_limit": parse_size(limit),
                     "net_rx": parse_size(rx), "net_tx": parse_size(tx)}
    return out


def parse_sample(text: str, prev_cpu: tuple[int, int] | None, at: float) -> tuple[dict[str, Any], dict[str, dict[str, Any]], tuple[int, int] | None]:
    """Sample script output → (point, containers {name: stats}, cpu counters for the next delta)."""
    sec = split_sections(text)
    load = (sec.get("loadavg") or "").split()
    counters = parse_cpu_counters(sec.get("stat") or "")
    disk: dict[str, dict[str, int]] = {}
    for p in DISK_PATHS:
        f = (sec.get(f"df:{p}") or "").split()
        if len(f) >= 2 and f[0].isdigit() and f[1].isdigit():
            disk[p] = {"size": int(f[0]), "used": int(f[1])}
    nproc = (sec.get("nproc") or "").strip()
    point: dict[str, Any] = {
        "at": _iso(at),
        "cpu_pct": cpu_percent(prev_cpu, counters),
        "load1": float(load[0]) if len(load) > 2 else None,
        "load5": float(load[1]) if len(load) > 2 else None,
        "load15": float(load[2]) if len(load) > 2 else None,
        **parse_free(sec.get("free") or ""),
        "ncpu": int(nproc) if nproc.isdigit() else None,
        "disk": disk,
    }
    return point, parse_docker_stats(sec.get("docker") or ""), counters


def parse_ps(text: str) -> list[dict[str, Any]]:
    procs: list[dict[str, Any]] = []
    for line in text.splitlines()[1:]:
        f = line.split(None, 5)
        if len(f) < 6 or not f[0].isdigit():
            continue
        procs.append({"pid": int(f[0]), "user": f[1], "cpu_pct": _pct(f[2]), "mem_pct": _pct(f[3]),
                      "rss": int(f[4]) * 1024 if f[4].isdigit() else None, "command": f[5]})
    return procs


def parse_labels(raw: Any) -> dict[str, str]:
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    out: dict[str, str] = {}
    for part in str(raw or "").split(","):
        k, sep, v = part.partition("=")
        if sep and k.strip():
            out[k.strip()] = v
    return out


def parse_docker_ps(text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        labels = parse_labels(d.get("Labels"))
        keep = {k: v for k, v in labels.items() if k.startswith("avdlab.") or k in ("containerlab", "clab-node-name", "clab-node-kind")}
        if "containerlab" in labels and "avdlab.lab" not in keep:
            keep["avdlab.lab"] = labels["containerlab"]
        rows.append({"name": str(d.get("Names") or d.get("Name") or ""), "image": d.get("Image"),
                     "status": d.get("Status"), "state": d.get("State"), "labels": keep})
    return sorted(rows, key=lambda r: r["name"])


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _ts(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp()


def downsample(points: list[dict[str, Any]], step: float | None) -> list[dict[str, Any]]:
    """Keep the first point of each `step`-second bucket."""
    if not step or step <= 0:
        return points
    out: list[dict[str, Any]] = []
    last_bucket: int | None = None
    for p in points:
        b = int(_ts(p["at"]) // step)
        if b != last_bucket:
            out.append(p)
            last_bucket = b
    return out


# ------------------------------------------------------------------------------------------------ sampler
Connect = Callable[[Host, AsyncSession], Any]  # async context manager factory (SSHClient.connect signature)


def _default_connect(host: Host, session: AsyncSession) -> Any:
    from .ssh import SSHClient

    return SSHClient.connect(host, session, connect_timeout=15)


def _docker_prefix(host: Host) -> str:
    docker = (host.capabilities or {}).get("docker") or {}
    return "" if docker.get("user_can_run", True) else "sudo -n "


class HostMonitor:
    def __init__(self, *, interval: float | None = None, connect: Connect | None = None,
                 clock: Callable[[], float] = time.time, persist: bool = True) -> None:
        self.interval = interval if interval is not None else settings.monitor_interval
        self._connect = connect or _default_connect
        self._clock = clock
        self.persist = persist
        self.points: dict[str, deque[dict[str, Any]]] = {}
        self.containers: dict[str, dict[str, deque[dict[str, Any]]]] = {}
        self.watched: dict[str, float] = {}
        self.errors: dict[str, str] = {}
        self._prev_cpu: dict[str, tuple[int, int]] = {}
        self._conns: dict[str, tuple[AsyncExitStack, Any]] = {}
        self._task: asyncio.Task[None] | None = None
        self._last_prune = 0.0
        self._lock = asyncio.Lock()
        self._open_locks: dict[str, asyncio.Lock] = {}

    # ---- lifecycle
    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if not self.running:
            self._task = asyncio.create_task(self._loop(), name="avdlab-host-monitor")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        for hid in list(self._conns):
            await self._close(hid)

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - never let the sampler die
                log.exception("host monitor tick failed")
            await asyncio.sleep(self.interval)

    # ---- watch list
    def watch(self, host_id: str) -> None:
        self.watched[host_id] = self._clock()

    def is_watched(self, host_id: str) -> bool:
        return self._clock() - self.watched.get(host_id, -1e18) < WATCH_TTL

    async def targets(self) -> list[Host]:
        async with SessionLocal() as s:
            deployed = {r[0] for r in (await s.execute(select(Lab.host_id).where(
                Lab.state.in_(DEPLOYED), Lab.host_id.is_not(None)))).all()}
            ids = deployed | {h for h in self.watched if self.is_watched(h)}
            if not ids:
                return []
            return list((await s.execute(select(Host).where(Host.id.in_(ids)))).scalars())

    # ---- sampling
    async def tick(self) -> None:
        async with self._lock:
            hosts = await self.targets()
            active = {h.id for h in hosts}
            for hid in [h for h in self._conns if h not in active]:
                await self._close(hid)
            await asyncio.gather(*(self._sample_safe(h) for h in hosts))
            if self.persist and self._clock() - self._last_prune > PRUNE_EVERY:
                await self.prune()

    async def _sample_safe(self, host: Host) -> None:
        try:
            await asyncio.wait_for(self.sample(host), timeout=max(self.interval, 10.0) * 2)
            self.errors.pop(host.id, None)
        except Exception as exc:  # noqa: BLE001 - one host failing never affects others
            self.errors[host.id] = str(exc) or exc.__class__.__name__
            log.warning("sampling host %s failed: %s", host.name, exc)
            await self._close(host.id)

    async def _ssh(self, host: Host) -> Any:
        async with self._open_locks.setdefault(host.id, asyncio.Lock()):
            return await self._open(host)

    async def _open(self, host: Host) -> Any:
        if host.id in self._conns:
            return self._conns[host.id][1]
        stack = AsyncExitStack()
        try:
            session = await stack.enter_async_context(SessionLocal())
            ssh = await stack.enter_async_context(self._connect(host, session))
        except BaseException:
            await stack.aclose()
            raise
        self._conns[host.id] = (stack, ssh)
        return ssh

    async def _close(self, host_id: str) -> None:
        entry = self._conns.pop(host_id, None)
        if entry is not None:
            try:
                await entry[0].aclose()
            except Exception:  # noqa: BLE001
                pass

    async def run(self, host: Host, cmd: str, *, timeout: float = INLINE_BUDGET) -> str:
        """Run a command over the pooled connection (opened on demand). Raises on SSH failure."""
        ssh = await self._ssh(host)
        try:
            res = await ssh.run(cmd, timeout=timeout)
        except Exception:
            await self._close(host.id)
            raise
        return res.stdout

    async def run_result(self, host: Host, cmd: str, *, timeout: float = INLINE_BUDGET) -> Any:
        """Like `run()` but returns the CompletedProcess (return code + output)."""
        ssh = await self._ssh(host)
        try:
            return await ssh.run(cmd, timeout=timeout)
        except Exception:
            await self._close(host.id)
            raise

    async def sample(self, host: Host) -> dict[str, Any]:
        now = self._clock()
        text = await self.run(host, SAMPLE_SCRIPT.replace("{DOCKER}", _docker_prefix(host)))
        point, containers, counters = parse_sample(text, self._prev_cpu.get(host.id), now)
        if counters:
            self._prev_cpu[host.id] = counters
        self.points.setdefault(host.id, deque(maxlen=RING_POINTS)).append(point)
        cbuf = self.containers.setdefault(host.id, {})
        for name, stats in containers.items():
            cbuf.setdefault(name, deque(maxlen=RING_POINTS)).append({"at": point["at"], **stats})
        cutoff = now - RING_POINTS * max(self.interval, 1.0)
        for name in [n for n, d in cbuf.items() if not d or _ts(d[-1]["at"]) < cutoff]:
            del cbuf[name]
        if self.persist:
            async with SessionLocal() as s:
                s.add(HostMetric(host_id=host.id, at=datetime.fromtimestamp(now, tz=timezone.utc),
                                 data={"point": point, "containers": containers}))
                await s.commit()
        return point

    async def prune(self) -> int:
        self._last_prune = self._clock()
        cutoff = datetime.fromtimestamp(self._clock(), tz=timezone.utc) - RETENTION
        async with SessionLocal() as s:
            res = await s.execute(delete(HostMetric).where(HostMetric.at < cutoff))
            await s.commit()
        return int(res.rowcount or 0)

    def poke(self, host: Host) -> None:
        """Sample a freshly watched host right away (when the sampler runs and has no recent point)."""
        if not self.running:
            return
        pts = self.points.get(host.id)
        if pts and self._clock() - _ts(pts[-1]["at"]) < self.interval:
            return
        asyncio.get_running_loop().create_task(self._sample_safe(host))

    # ---- queries
    async def metrics(self, host_id: str, since: float | None = None, step: float | None = None) -> dict[str, Any]:
        now = self._clock()
        since = since if since is not None else now - RING_POINTS * self.interval
        ring = list(self.points.get(host_id) or [])
        if ring and _ts(ring[0]["at"]) <= since:
            points = [p for p in ring if _ts(p["at"]) >= since]
        else:  # older than the ring buffer (or after a restart) → table
            async with SessionLocal() as s:
                rows = (await s.execute(select(HostMetric).where(
                    HostMetric.host_id == host_id, HostMetric.at >= datetime.fromtimestamp(since, tz=timezone.utc))
                    .order_by(HostMetric.at))).scalars().all()
            points = [r.data.get("point") for r in rows if isinstance(r.data, dict) and r.data.get("point")]
            seen = {p["at"] for p in points}
            points += [p for p in ring if _ts(p["at"]) >= since and p["at"] not in seen]
        ccut = max(since, now - RING_POINTS * self.interval)
        containers = {name: [c for c in buf if _ts(c["at"]) >= ccut]
                      for name, buf in (self.containers.get(host_id) or {}).items()}
        containers = {k: [{"at": c["at"], "cpu_pct": c.get("cpu_pct"), "mem_used": c.get("mem_used"),
                           "mem_limit": c.get("mem_limit")} for c in v] for k, v in containers.items() if v}
        return {"host_id": host_id, "points": downsample(points, step), "containers": containers,
                "sampled_at": points[-1]["at"] if points else None, "watched": self.is_watched(host_id),
                "error": self.errors.get(host_id)}


monitor = HostMonitor()


def set_monitor(m: HostMonitor) -> HostMonitor:
    """Swap the module-level monitor (tests)."""
    global monitor
    monitor = m
    return m


async def start_monitor() -> None:
    if settings.monitor_enabled:
        monitor.start()


async def stop_monitor() -> None:
    try:
        await monitor.stop()
    except Exception:  # noqa: BLE001
        log.exception("stopping host monitor failed")


# ------------------------------------------------------------------------------------------------ API
router = APIRouter(prefix="/hosts", tags=["hosts"])


async def _host(session: AsyncSession, host_id: str) -> Host:
    h = await session.get(Host, host_id)
    if h is None:
        raise HTTPException(404, "host not found")
    return h


@router.get("/{host_id}/metrics")
async def host_metrics(host_id: str, since: str | None = None, step: float | None = None,
                       session: AsyncSession = Depends(get_session), user: str = Depends(current_user)) -> dict[str, Any]:
    """`{host_id, points: [...], containers: {name: [{at, cpu_pct, mem_used, mem_limit}]}, sampled_at, watched, error}`;
    marks the host watched for 10 min (sampled every 20 s)."""
    host = await _host(session, host_id)
    since_ts: float | None = None
    if since:
        try:
            dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(422, "since must be an ISO timestamp") from exc
        since_ts = (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()
    monitor.watch(host.id)
    monitor.poke(host)
    return await monitor.metrics(host.id, since_ts, step)


async def _inline(host: Host, cmd: str) -> str:
    try:
        return await asyncio.wait_for(monitor.run(host, cmd), INLINE_BUDGET)
    except TimeoutError as exc:
        raise HTTPException(504, f"{host.name} did not answer within {INLINE_BUDGET:.0f} s") from exc
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - SSH errors
        raise HTTPException(502, f"ssh to {host.name} failed: {exc}") from exc


@router.get("/{host_id}/processes")
async def host_processes(host_id: str, session: AsyncSession = Depends(get_session),
                         user: str = Depends(current_user)) -> dict[str, Any]:
    host = await _host(session, host_id)
    text = await _inline(host, PS_CMD)
    return {"at": _iso(time.time()), "processes": parse_ps(text)}


@router.get("/{host_id}/containers")
async def host_containers(host_id: str, session: AsyncSession = Depends(get_session),
                          user: str = Depends(current_user)) -> list[dict[str, Any]]:
    host = await _host(session, host_id)
    text = await _inline(host, _docker_prefix(host) + DOCKER_PS)
    return parse_docker_ps(text)


class ContainerKillIn(BaseModel):
    confirm: str | None = None


async def _lab_for_container(session: AsyncSession, labels: dict[str, str]) -> Lab | None:
    """`avdlab.lab` (or containerlab's `containerlab`) label → Lab by slug (the containerlab topology name), name or id."""
    ref = labels.get("avdlab.lab") or labels.get("containerlab")
    if not ref:
        return None
    for col in (Lab.slug, Lab.name, Lab.id):
        lab = (await session.execute(select(Lab).where(col == ref))).scalars().first()
        if lab is not None:
            return lab
    return None


@router.delete("/{host_id}/containers/{name}")
async def kill_container(host_id: str, name: str, force: bool = False, body: ContainerKillIn | None = Body(default=None),
                         session: AsyncSession = Depends(get_session), user: str = Depends(current_user)) -> dict[str, Any]:
    """`force=true` → `docker rm -f <name>`, `force=false` → `docker stop <name>`. 404 when the container is not in
    `docker ps -a`. A container of a lab in state deployed/validated needs body `{"confirm": "<container name>"}`
    (409 otherwise) for either action. Returns `{name, action, removed, returncode, output}` (`removed` = the docker
    command succeeded, i.e. stopped for `force=false`)."""
    import shlex

    host = await _host(session, host_id)
    prefix = _docker_prefix(host)
    rows = parse_docker_ps(await _inline(host, prefix + DOCKER_PS_ALL))
    row = next((r for r in rows if r["name"] == name), None)
    if row is None:
        raise HTTPException(404, f"container {name!r} not found on {host.name}")
    lab = await _lab_for_container(session, row.get("labels") or {})
    confirm = (body.confirm if body else None) or ""
    protected = lab is not None and lab.state in DEPLOYED
    if protected and confirm != name:  # typed confirmation gates both stop and rm -f of a live lab's container
        raise HTTPException(409, {"message": f"{name} belongs to lab {lab.name} ({lab.state.value}): "
                                             "send body {\"confirm\": \"<container name>\"}",
                                  "lab_id": lab.id, "lab": lab.name, "state": lab.state.value})
    action = "rm" if force else "stop"
    cmd = f"{prefix}docker {'rm -f' if force else 'stop'} {shlex.quote(name)} 2>&1"
    try:
        res = await asyncio.wait_for(monitor.run_result(host, cmd, timeout=60), 65)
    except TimeoutError as exc:
        raise HTTPException(504, f"{host.name}: docker {action} {name} did not finish within 60 s") from exc
    except Exception as exc:  # noqa: BLE001 - SSH errors
        raise HTTPException(502, f"ssh to {host.name} failed: {exc}") from exc
    output = ((res.stdout or "") + (res.stderr or "")).strip()
    audit.warning("container %s: user=%s host=%s(%s) container=%s lab=%s state=%s rc=%s output=%r", action, user,
                  host.name, host.id, name, lab.name if lab else None, lab.state.value if lab else None,
                  res.returncode, output[-200:])
    return {"name": name, "action": action, "removed": res.returncode == 0, "returncode": res.returncode,
            "output": output}


__all__ = ["HostMonitor", "monitor", "set_monitor", "start_monitor", "stop_monitor", "router", "parse_sample",
           "parse_ps", "parse_docker_ps"]
