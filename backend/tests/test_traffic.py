"""Module 5: ping / iperf traffic jobs with a scripted fake SSH, traffic_runs table and endpoints."""
from __future__ import annotations

import json
from pathlib import Path

from avdlab.core.db import LabState
from avdlab.executors import traffic as tr
from avdlab.topology.clients import write_plans

from .test_clients import _server_plan
from .test_executors import ClabFakeSSH, _cleanup, install, mk_host, mk_lab, ran, run_job  # noqa: F401

IPUTILS_OK = """PING 10.10.11.101 (10.10.11.101) 56(84) bytes of data.
64 bytes from 10.10.11.101: icmp_seq=1 ttl=64 time=1.52 ms

--- 10.10.11.101 ping statistics ---
3 packets transmitted, 3 received, 0% packet loss, time 402ms
rtt min/avg/max/mdev = 0.911/1.204/1.520/0.249 ms
"""
BUSYBOX_PARTIAL = """--- 10.10.11.100 ping statistics ---
4 packets transmitted, 3 packets received, 25% packet loss
round-trip min/avg/max = 0.412/0.530/0.700 ms
"""
UNREACHABLE = """--- 10.10.11.9 ping statistics ---
3 packets transmitted, 0 received, +3 errors, 100% packet loss, time 2051ms
"""
IPERF_TCP = {"start": {}, "intervals": [], "end": {
    "sum_sent": {"start": 0, "end": 10.0, "seconds": 10.000123, "bytes": 1, "bits_per_second": 9.41e8, "retransmits": 12},
    "sum_received": {"start": 0, "end": 10.0, "seconds": 10.0421, "bytes": 1, "bits_per_second": 9.38e8}}}
IPERF_UDP = {"start": {}, "end": {"sum": {"start": 0, "end": 5.0, "seconds": 5.0, "bits_per_second": 1.0e8,
                                          "jitter_ms": 0.042, "lost_packets": 3, "packets": 43000, "lost_percent": 0.00697}}}
IPERF_ERR = {"start": {}, "end": {}, "error": "unable to connect to server: Connection refused"}


def test_parse_ping():
    assert tr.parse_ping(IPUTILS_OK) == {"sent": 3, "received": 3, "loss_pct": 0.0, "rtt_avg_ms": 1.204}
    assert tr.parse_ping(BUSYBOX_PARTIAL) == {"sent": 4, "received": 3, "loss_pct": 25.0, "rtt_avg_ms": 0.53}
    assert tr.parse_ping(UNREACHABLE) == {"sent": 3, "received": 0, "loss_pct": 100.0, "rtt_avg_ms": None}
    assert tr.parse_ping("ping: bad address")["loss_pct"] == 100.0


def test_parse_iperf():
    tcp = tr.parse_iperf(json.dumps(IPERF_TCP))
    assert tcp == {"seconds": 10.0, "bits_per_second_sent": 9.41e8, "bits_per_second_received": 9.38e8, "retransmits": 12,
                   "jitter_ms": None, "lost_percent": None, "error": None}
    udp = tr.parse_iperf("warning: something\n" + json.dumps(IPERF_UDP))
    assert udp["bits_per_second_sent"] == 1.0e8 and udp["jitter_ms"] == 0.042 and udp["lost_percent"] == 0.00697
    assert udp["retransmits"] is None and udp["error"] is None
    assert tr.parse_iperf(json.dumps(IPERF_ERR))["error"].startswith("unable to connect")
    assert tr.parse_iperf("OCI runtime exec failed")["error"] == "OCI runtime exec failed"


def test_iperf_cmd():
    f = tr.IperfFlow(src="a", dst="b", proto="udp", bandwidth="200M", duration=5, parallel=4, reverse=True)
    assert tr.iperf_client_cmd(f, "10.0.0.1") == "iperf3 -c 10.0.0.1 -J -t 5 -u -b 200M -P 4 -R"
    assert tr.iperf_client_cmd(tr.IperfFlow(src="a", dst="b"), "10.0.0.1") == "iperf3 -c 10.0.0.1 -J -t 10"


class TrafficFakeSSH(ClabFakeSSH):
    def __init__(self, lab_name: str) -> None:
        super().__init__(lab_name)
        self.responses = {
            "ping -c": self._ping,
            "pgrep iperf3": (0, ""),
            "iperf3 -c": self._iperf,
            **self.responses,
        }

    def _ping(self, fake, cmd: str) -> tuple[int, str]:
        if cmd.startswith("docker exec dc1-leaf2-server1"):
            return 1, UNREACHABLE  # leaf2 → leaf1 is broken
        return 0, IPUTILS_OK

    def _iperf(self, fake, cmd: str) -> tuple[int, str]:
        if " -u " in cmd:
            return 0, json.dumps(IPERF_UDP)
        if "dc1-leaf2-server1 iperf3" in cmd:
            return 1, json.dumps(IPERF_ERR)
        return 0, json.dumps(IPERF_TCP)


async def _deployed_lab(client, *, with_clients: bool = True):
    host_id = await mk_host()
    lab = await mk_lab(client, host_id=host_id, state=LabState.deployed)
    if with_clients:
        write_plans(Path(lab["dir"]) / "clab", [_server_plan("dc1-leaf1-server1", 100, 1),
                                                 _server_plan("dc1-leaf2-server1", 101, 2)])
    return lab


async def test_ping_mesh_job_and_runs(client, monkeypatch):
    lab = await _deployed_lab(client)
    fake = TrafficFakeSSH(lab["slug"])
    install(monkeypatch, fake)
    r = await client.post(f"/api/labs/{lab['id']}/traffic/ping", json={"pairs": None, "count": 3, "size": 56})
    assert r.status_code == 202, r.text
    job = await run_job(client, r.json()["job_id"])
    res = job["result"]
    assert [(x["src"], x["dst"], x["target_ip"], x["ok"]) for x in res["results"]] == [
        ("dc1-leaf1-server1", "dc1-leaf2-server1", "10.10.11.101", True),
        ("dc1-leaf2-server1", "dc1-leaf1-server1", "10.10.11.100", False)]
    assert res["results"][0]["rtt_avg_ms"] == 1.204 and res["results"][1]["rtt_avg_ms"] is None
    assert res["matrix"] == {"dc1-leaf1-server1": {"dc1-leaf2-server1": 0.0}, "dc1-leaf2-server1": {"dc1-leaf1-server1": 100.0}}
    pings = ran(fake, "ping -c")
    assert pings[0][0] == "docker exec dc1-leaf1-server1 ping -c 3 -s 56 -W 2 -i 0.2 10.10.11.101" and pings[0][1] is False

    # explicit pair in another VLAN
    r = await client.post(f"/api/labs/{lab['id']}/traffic/ping",
                          json={"pairs": [{"src": "dc1-leaf1-server1", "dst": "dc1-leaf2-server1"}], "count": 1, "vlan": 22})
    job2 = await run_job(client, r.json()["job_id"])
    assert job2["result"]["results"][0]["target_ip"] == "10.10.22.101"

    runs = (await client.get(f"/api/labs/{lab['id']}/traffic/runs")).json()
    assert [x["kind"] for x in runs] == ["ping", "ping"] and runs[0]["id"] == job2["result"]["run_id"]
    assert runs[1]["summary"] == {"total": 2, "ok": 1} and "results" not in runs[1]
    full = (await client.get(f"/api/labs/{lab['id']}/traffic/runs/{runs[1]['id']}")).json()
    assert full["job_id"] == job["id"] and full["params"]["count"] == 3 and len(full["results"]) == 2
    assert full["matrix"] == res["matrix"]
    assert (await client.get(f"/api/labs/{lab['id']}/traffic/runs/nope")).status_code == 404


async def test_iperf_job(client, monkeypatch):
    lab = await _deployed_lab(client)
    fake = TrafficFakeSSH(lab["slug"])
    install(monkeypatch, fake)
    flows = [{"src": "dc1-leaf1-server1", "dst": "dc1-leaf2-server1", "proto": "tcp", "duration": 10, "parallel": 2},
             {"src": "dc1-leaf1-server1", "dst": "dc1-leaf2-server1", "proto": "udp", "bandwidth": "100M", "duration": 5},
             {"src": "dc1-leaf2-server1", "dst": "dc1-leaf1-server1"}]
    r = await client.post(f"/api/labs/{lab['id']}/traffic/iperf", json={"flows": flows, "sequential": True})
    assert r.status_code == 202, r.text
    res = (await run_job(client, r.json()["job_id"]))["result"]["results"]
    assert res[0] == {"src": "dc1-leaf1-server1", "dst": "dc1-leaf2-server1", "proto": "tcp", "seconds": 10.0,
                      "bits_per_second_sent": 9.41e8, "bits_per_second_received": 9.38e8, "retransmits": 12,
                      "jitter_ms": None, "lost_percent": None, "ok": True, "error": None}
    assert res[1]["proto"] == "udp" and res[1]["jitter_ms"] == 0.042 and res[1]["ok"] is True
    assert res[2]["ok"] is False and "Connection refused" in res[2]["error"]
    servers = ran(fake, "pgrep iperf3")
    assert servers[0][0] == "docker exec dc1-leaf2-server1 sh -c 'pgrep iperf3 >/dev/null || iperf3 -s -D'"
    clients = [c for c, _ in ran(fake, "iperf3 -c")]
    assert clients[0] == "docker exec dc1-leaf1-server1 iperf3 -c 10.10.11.101 -J -t 10 -P 2"
    assert clients[1] == "docker exec dc1-leaf1-server1 iperf3 -c 10.10.11.101 -J -t 5 -u -b 100M"
    runs = (await client.get(f"/api/labs/{lab['id']}/traffic/runs")).json()
    assert runs[0]["kind"] == "iperf" and runs[0]["summary"] == {"total": 3, "ok": 2}


async def test_traffic_preconditions(client, monkeypatch):
    install(monkeypatch, TrafficFakeSSH("x"))
    host_id = await mk_host()
    built = await mk_lab(client, host_id=host_id, state=LabState.built)
    assert (await client.post(f"/api/labs/{built['id']}/traffic/ping", json={})).status_code == 409
    no_clients = await _deployed_lab(client, with_clients=False)
    r = await client.post(f"/api/labs/{no_clients['id']}/traffic/ping", json={})
    assert r.status_code == 409 and "clients" in r.text
    lab = await _deployed_lab(client)
    r = await client.post(f"/api/labs/{lab['id']}/traffic/iperf", json={"flows": [{"src": "ghost", "dst": "dc1-leaf1-server1"}]})
    assert r.status_code == 422 and "ghost" in r.text
    r = await client.post(f"/api/labs/{lab['id']}/traffic/ping", json={"vlan": 999})
    assert r.status_code == 409  # nobody has an address in VLAN 999
    r = await client.post(f"/api/labs/{lab['id']}/traffic/iperf", json={"flows": []})
    assert r.status_code == 422
    r = await client.post(f"/api/labs/{lab['id']}/traffic/iperf",
                          json={"flows": [{"src": "dc1-leaf1-server1", "dst": "dc1-leaf2-server1", "bandwidth": "1G; rm -rf /"}]})
    assert r.status_code == 422
