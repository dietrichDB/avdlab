"""Host monitoring (phase 3 §5): parsing of canned /proc, free, df, docker stats/ps, ps output; sampler ring buffer,
persistence, pruning and failure isolation with an injected fake SSH; metrics/processes/containers endpoints.
No real SSH: every HostMonitor here gets `connect=` a fake, and the app lifespan (which would start the real one)
does not run under httpx's ASGITransport."""
from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import delete, func, select

from avdlab.core.config import settings
from avdlab.core.db import Host, HostMetric, SessionLocal
from avdlab.hosts import monitor as mon
from avdlab.hosts.ssh import CompletedProcess

STAT1 = "cpu  1000 0 500 8000 500 0 0 0 0 0"
STAT2 = "cpu  1600 0 700 8600 600 0 0 0 0 0"  # +800 busy, +700 idle/iowait → 53.3 %
FREE = ("               total        used        free      shared  buff/cache   available\n"
        "Mem:     16651386880  4294967296  8000000000   100000000  4356419584 11811160064\n"
        "Swap:              0           0           0\n")
STATS = "\n".join(json.dumps(d) for d in [
    {"Name": "clab-lab1-dc1-spine1", "CPUPerc": "3.50%", "MemUsage": "1.2GiB / 15.5GiB", "NetIO": "1.5kB / 648B"},
    {"Name": "clab-lab1-client1", "CPUPerc": "0.00%", "MemUsage": "12MiB / 15.5GiB", "NetIO": "0B / 0B"},
])


def sample_output(stat: str = STAT1) -> str:
    return (f"@@loadavg\n0.52 0.61 0.70 2/1234 5678\n@@stat\n{stat}\n@@free\n{FREE}"
            f"@@df:/\n100000000000 40000000000\n@@df:/var/lib/docker\n200000000000 50000000000\n"
            f"@@nproc\n8\n@@docker\n{STATS}\n@@end\n")


PS = ("    PID USER     %CPU %MEM   RSS COMMAND\n"
      "   4242 root     12.5 10.1 1048576 /usr/bin/dockerd -H fd:// --containerd=/run/containerd/containerd.sock\n"
      "      1 root      0.0  0.1 12000 /sbin/init splash\n")
DOCKER_PS = "\n".join(json.dumps(d) for d in [
    {"Names": "clab-lab1-dc1-spine1", "Image": "arista/ceos:4.34.2F", "Status": "Up 5 minutes", "State": "running",
     "Labels": "clab-node-kind=arista_ceos,containerlab=lab1,avdlab.role=spine,clab-node-name=dc1-spine1"},
    {"Names": "portainer", "Image": "portainer/portainer-ce", "Status": "Exited (0)", "State": "exited", "Labels": ""},
])


class FakeSSH:
    def __init__(self) -> None:
        self.stat = STAT1
        self.cmds: list[str] = []
        self.fail = False

    async def run(self, cmd: str, **kw: Any) -> CompletedProcess:
        self.cmds.append(cmd)
        if self.fail:
            raise RuntimeError("connection lost")
        if cmd.startswith("ps -eo"):
            return CompletedProcess(0, PS, "", command=cmd)
        if "docker ps" in cmd:
            return CompletedProcess(0, DOCKER_PS, "", command=cmd)
        return CompletedProcess(0, sample_output(self.stat), "", command=cmd)


def fake_connector(fake: FakeSSH, counter: dict[str, int] | None = None, fail: bool = False):
    @asynccontextmanager
    async def connect(host, session):  # noqa: ANN001
        if counter is not None:
            counter["open"] = counter.get("open", 0) + 1
        if fail:
            raise OSError("no route to host")
        try:
            yield fake
        finally:
            if counter is not None:
                counter["closed"] = counter.get("closed", 0) + 1
    return connect


async def mk_host(caps: dict[str, Any] | None = None) -> str:
    async with SessionLocal() as s:
        h = Host(name=f"mon-{uuid.uuid4().hex[:6]}", address="192.0.2.77", ssh_user="ubuntu", capabilities=caps or {})
        s.add(h)
        await s.commit()
        return h.id


async def rm_host(host_id: str) -> None:
    async with SessionLocal() as s:
        await s.execute(delete(HostMetric).where(HostMetric.host_id == host_id))
        await s.delete(await s.get(Host, host_id))
        await s.commit()


# ------------------------------------------------------------------ parsing
def test_parse_sample_and_cpu_delta():
    point, containers, counters = mon.parse_sample(sample_output(STAT1), None, 1_700_000_000)
    assert point["cpu_pct"] is None and point["load1"] == 0.52 and point["load15"] == 0.70 and point["ncpu"] == 8
    assert point["mem_total"] == 16651386880 and point["mem_used"] == 4294967296 and point["mem_available"] == 11811160064
    assert point["disk"] == {"/": {"size": 100000000000, "used": 40000000000},
                             "/var/lib/docker": {"size": 200000000000, "used": 50000000000}}
    assert point["at"].startswith("2023-11-14T")
    spine = containers["clab-lab1-dc1-spine1"]
    assert spine["cpu_pct"] == 3.5 and spine["mem_used"] == int(1.2 * 1024**3) and spine["mem_limit"] == int(15.5 * 1024**3)
    assert spine["net_rx"] == 1500 and spine["net_tx"] == 648
    point2, _, _ = mon.parse_sample(sample_output(STAT2), counters, 1_700_000_020)
    assert point2["cpu_pct"] == 53.3
    assert mon.parse_size("0B") == 0 and mon.parse_size("2kB") == 2000 and mon.parse_size("bogus") is None
    empty, c, _ = mon.parse_sample("", None, 0)
    assert empty["load1"] is None and empty["disk"] == {} and c == {}


def test_parse_ps_and_docker_ps():
    procs = mon.parse_ps(PS)
    assert procs[0] == {"pid": 4242, "user": "root", "cpu_pct": 12.5, "mem_pct": 10.1, "rss": 1048576 * 1024,
                        "command": "/usr/bin/dockerd -H fd:// --containerd=/run/containerd/containerd.sock"}
    assert len(procs) == 2
    rows = mon.parse_docker_ps(DOCKER_PS)
    spine = next(r for r in rows if r["name"] == "clab-lab1-dc1-spine1")
    assert spine["state"] == "running" and spine["labels"]["avdlab.lab"] == "lab1" and spine["labels"]["avdlab.role"] == "spine"
    assert next(r for r in rows if r["name"] == "portainer")["labels"] == {}


# ------------------------------------------------------------------ sampler
async def test_sampler_ring_persist_prune_and_failures(client):
    host_id = await mk_host()
    fake, counter = FakeSSH(), {}
    t = [1_800_000_000.0]
    m = mon.HostMonitor(interval=20, connect=fake_connector(fake, counter), clock=lambda: t[0])
    try:
        await m.tick()  # nothing watched, no deployed lab → no SSH
        assert counter == {} and not m.points
        m.watch(host_id)
        await m.tick()
        t[0] += 20
        fake.stat = STAT2
        await m.tick()
        assert counter == {"open": 1}  # one session kept open across ticks
        pts = list(m.points[host_id])
        assert len(pts) == 2 and pts[0]["cpu_pct"] is None and pts[1]["cpu_pct"] == 53.3
        assert "sudo" not in fake.cmds[0]
        async with SessionLocal() as s:
            n = (await s.execute(select(func.count()).select_from(HostMetric).where(HostMetric.host_id == host_id))).scalar()
            assert n == 2
            s.add(HostMetric(host_id=host_id, at=datetime.fromtimestamp(t[0], tz=timezone.utc) - timedelta(days=8), data={}))
            await s.commit()
        assert await m.prune() == 1

        out = await m.metrics(host_id)
        assert [p["cpu_pct"] for p in out["points"]] == [None, 53.3] and out["watched"] is True
        assert set(out["containers"]) == {"clab-lab1-dc1-spine1", "clab-lab1-client1"}
        assert set(out["containers"]["clab-lab1-dc1-spine1"][0]) == {"at", "cpu_pct", "mem_used", "mem_limit"}
        assert out["sampled_at"] == out["points"][-1]["at"]
        assert len((await m.metrics(host_id, step=3600))["points"]) == 1
        # after a restart the ring is empty → points come from the table
        m2 = mon.HostMonitor(interval=20, connect=fake_connector(fake), clock=lambda: t[0])
        assert len((await m2.metrics(host_id, since=t[0] - 3600))["points"]) == 2

        # a failing host never breaks the tick; the connection is dropped and re-opened later
        fake.fail = True
        await m.tick()
        assert "connection lost" in m.errors[host_id] and counter["closed"] == 1
        fake.fail = False
        t[0] += 20
        await m.tick()
        assert host_id not in m.errors and counter["open"] == 2
        # watch expiry → session closed
        t[0] += mon.WATCH_TTL + 1
        await m.tick()
        assert counter["closed"] == 2 and host_id not in m._conns

        bad = mon.HostMonitor(interval=20, connect=fake_connector(fake, fail=True), clock=lambda: t[0])
        bad.watch(host_id)
        await bad.tick()
        assert "no route" in bad.errors[host_id]
    finally:
        await m.stop()
        await rm_host(host_id)


async def test_loop_start_stop_and_disabled(monkeypatch):
    fake = FakeSSH()
    m = mon.HostMonitor(interval=0.01, connect=fake_connector(fake), persist=False)

    async def boom() -> None:
        raise RuntimeError("db down")

    monkeypatch.setattr(m, "targets", boom)
    m.start()
    await asyncio.sleep(0.05)
    assert m.running  # exceptions inside the loop never kill it
    await m.stop()
    assert not m.running

    old = mon.monitor
    try:
        mon.set_monitor(mon.HostMonitor(connect=fake_connector(fake)))
        monkeypatch.setattr(settings, "monitor_enabled", False)
        await mon.start_monitor()
        assert not mon.monitor.running
        await mon.stop_monitor()
    finally:
        mon.set_monitor(old)


# ------------------------------------------------------------------ API
async def test_metrics_processes_containers_api(client):
    host_id = await mk_host({"docker": {"installed": True, "user_can_run": False}})
    fake = FakeSSH()
    old = mon.monitor
    m = mon.set_monitor(mon.HostMonitor(interval=20, connect=fake_connector(fake)))
    try:
        r = await client.get(f"/api/hosts/{host_id}/metrics")
        assert r.status_code == 200
        body = r.json()
        assert body == {"host_id": host_id, "points": [], "containers": {}, "sampled_at": None, "watched": True,
                        "error": None}
        await m.tick()  # the lifespan task would do this every 20 s
        body = (await client.get(f"/api/hosts/{host_id}/metrics",
                                 params={"since": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()})).json()
        assert len(body["points"]) == 1 and body["points"][0]["mem_total"] == 16651386880
        assert "clab-lab1-dc1-spine1" in body["containers"] and body["sampled_at"]
        assert any(c.startswith("echo @@loadavg") and "sudo -n docker stats" in c for c in fake.cmds)
        assert (await client.get(f"/api/hosts/{host_id}/metrics", params={"since": "yesterday"})).status_code == 422

        procs = (await client.get(f"/api/hosts/{host_id}/processes")).json()
        assert procs["at"] and procs["processes"][0]["pid"] == 4242
        conts = (await client.get(f"/api/hosts/{host_id}/containers")).json()
        assert [c["name"] for c in conts] == ["clab-lab1-dc1-spine1", "portainer"]
        assert any(c.startswith("sudo -n docker ps") for c in fake.cmds)
        fake.fail = True
        assert (await client.get(f"/api/hosts/{host_id}/processes")).status_code == 502
        assert (await client.get("/api/hosts/nope/metrics")).status_code == 404
    finally:
        await m.stop()
        mon.set_monitor(old)
        await rm_host(host_id)
