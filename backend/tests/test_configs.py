"""Running configs, config diff normalisation and `lab.snapshot` (fake SSH)."""
from __future__ import annotations

import json
from pathlib import Path

from git import Repo

from avdlab.core.db import LabState
from avdlab.executors import configs

from .test_executors import ClabFakeSSH, _cleanup, install, mk_host, mk_lab, ran, run_job  # noqa: F401

INTENDED = """!RANCID-CONTENT-TYPE: arista
!
vlan internal order ascending range 1006 1199
!
transceiver qsfp default-mode 4x10G
!
hostname dc1-spine1
!
interface Ethernet1
   description P2P_dc1-leaf1a_Ethernet1
   mtu 1500
   no switchport
   ip address 10.255.255.0/31
!
router bgp 65100
   router-id 10.255.0.1
   !
   address-family evpn
      neighbor EVPN-OVERLAY-PEERS activate
!
end
"""
RUNNING = """! Command: show running-config
! device: dc1-spine1 (cEOSLab, EOS-4.34.2F-44141813.4342F (engineering build))
!
! boot system flash:/cEOS-lab.swi
!
vlan internal order ascending range 1006 1199
!
transceiver qsfp default-mode 4x10G
!
hostname dc1-spine1
!
interface Ethernet1
   description P2P_dc1-leaf1a_Ethernet1
   mtu 1500
   no switchport
   ip address 10.255.255.0/31
!
router bgp 65100
   router-id 10.255.0.1
   !
   address-family evpn
      neighbor EVPN-OVERLAY-PEERS activate
!
end
"""


def test_normalize_and_diff():
    assert configs.normalize_config(INTENDED) == configs.normalize_config(RUNNING)
    same = configs.config_diff("dc1-spine1", INTENDED, RUNNING)
    assert same["identical"] is True and same["diff"] == "" and same["intended"] == INTENDED and same["running"] == RUNNING
    padded = RUNNING.replace("hostname dc1-spine1\n", "hostname dc1-spine1   \r\n")  # trailing whitespace / CRLF
    assert configs.config_diff("dc1-spine1", INTENDED, padded)["identical"] is True
    drift = RUNNING.replace("   mtu 1500\n", "   mtu 9214\n").replace("hostname dc1-spine1\n",
                                                                         "hostname dc1-spine1\nip routing\n")
    d = configs.config_diff("dc1-spine1", INTENDED, drift)
    assert d["identical"] is False
    lines = d["diff"].splitlines()
    assert lines[0] == "--- intended/configs/dc1-spine1.cfg" and lines[1] == "+++ dc1-spine1:running-config"
    assert "-   mtu 1500" in lines and "+   mtu 9214" in lines and "+ip routing" in lines
    assert not any(ln.startswith(("+!", "-!", "+end", "-end")) for ln in lines)


def test_slugs_and_params():
    assert configs.cmd_slug("show ip bgp summary") == "show-ip-bgp-summary"
    assert configs.cmd_slug("show interfaces | json") == "show-interfaces-json"
    assert configs.SnapshotParams().commands == configs.DEFAULT_SNAPSHOT_COMMANDS
    assert configs.cli_cmd("dc1-spine1", "show version") == "docker exec dc1-spine1 Cli -p 15 -c 'show version'"


class CfgFakeSSH(ClabFakeSSH):
    def __init__(self, lab_name: str) -> None:
        super().__init__(lab_name)
        self.responses = {"show running-config": (0, RUNNING), "Cli -p 15 -c": self._cli, **self.responses}

    def _cli(self, fake, cmd: str) -> tuple[int, str]:
        if "show mlag" in cmd and "spine" in cmd:
            return 1, "% MLAG is not configured\n"
        return 0, f"output of {cmd.split(' -c ', 1)[1]}\n"


async def test_running_config_and_diff(client, monkeypatch):
    host_id = await mk_host()
    lab = await mk_lab(client, host_id=host_id, state=LabState.deployed)
    fake = CfgFakeSSH(lab["slug"])
    install(monkeypatch, fake)
    (Path(lab["dir"]) / "intended" / "configs").mkdir(parents=True, exist_ok=True)
    (Path(lab["dir"]) / "intended" / "configs" / "dc1-spine1.cfg").write_text(INTENDED)

    r = await client.get(f"/api/labs/{lab['id']}/nodes/dc1-spine1/running-config")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["node"] == "dc1-spine1" and body["config"] == RUNNING and body["fetched_at"]
    assert ran(fake, "show running-config")[0] == ("docker exec dc1-spine1 Cli -p 15 -c 'show running-config'", False)

    d = (await client.get(f"/api/labs/{lab['id']}/nodes/dc1-spine1/config-diff")).json()
    assert d["identical"] is True and d["diff"] == "" and d["intended"] == INTENDED

    assert (await client.get(f"/api/labs/{lab['id']}/nodes/ghost/running-config")).status_code == 404
    assert (await client.get(f"/api/labs/{lab['id']}/nodes/dc1-spine2/config-diff")).status_code == 404  # no intended cfg
    built = await mk_lab(client, host_id=host_id, state=LabState.built)
    assert (await client.get(f"/api/labs/{built['id']}/nodes/dc1-spine1/running-config")).status_code == 409


async def test_snapshot_job(client, monkeypatch):
    host_id = await mk_host()
    lab = await mk_lab(client, host_id=host_id, state=LabState.validated)
    fake = CfgFakeSSH(lab["slug"])
    install(monkeypatch, fake)
    assert (await client.get(f"/api/labs/{lab['id']}/snapshots")).json() == []
    r = await client.post(f"/api/labs/{lab['id']}/snapshot", json={"commands": ["show version", "show mlag"]})
    assert r.status_code == 202, r.text
    job = await run_job(client, r.json()["job_id"])
    res = job["result"]
    sid = res["snapshot_id"]
    assert res["nodes"] == ["dc1-leaf1a", "dc1-leaf1b", "dc1-spine1", "dc1-spine2"]
    assert res["command_slugs"] == ["show-version", "show-mlag"] and res["commit"]
    assert {(e["node"], e["command"]) for e in res["errors"]} == {("dc1-spine1", "show mlag"), ("dc1-spine2", "show mlag")}
    snap = Path(lab["dir"]) / "snapshots" / sid
    assert (snap / "dc1-leaf1a" / "show-version.txt").read_text() == "output of 'show version'\n"
    assert "MLAG is not configured" in (snap / "dc1-spine1" / "show-mlag.txt").read_text()
    assert json.loads((snap / "meta.json").read_text())["commands"] == ["show version", "show mlag"]
    repo = Repo(lab["dir"])
    assert repo.head.commit.message.startswith(f"snapshot {sid}") and not repo.is_dirty(untracked_files=True)
    assert len(ran(fake, "Cli -p 15 -c")) == 8

    snaps = (await client.get(f"/api/labs/{lab['id']}/snapshots")).json()
    assert snaps == [{"id": sid, "created_at": snaps[0]["created_at"], "nodes": 4, "commands": ["show version", "show mlag"],
                      "node_names": res["nodes"], "command_slugs": ["show-version", "show-mlag"]}]
    t = await client.get(f"/api/labs/{lab['id']}/snapshots/{sid}/dc1-leaf1b/show-version")
    assert t.status_code == 200 and t.text == "output of 'show version'\n" and t.headers["content-type"].startswith("text/plain")
    assert (await client.get(f"/api/labs/{lab['id']}/snapshots/{sid}/dc1-leaf1b/show-nothing")).status_code == 404
    assert (await client.get(f"/api/labs/{lab['id']}/snapshots/..%2F..%2Fetc/dc1-leaf1b/show-version")).status_code in (400, 404)

    # only show commands; not deployed → 409
    r = await client.post(f"/api/labs/{lab['id']}/snapshot", json={"commands": ["reload now"]})
    assert r.status_code == 422
    built = await mk_lab(client, host_id=host_id, state=LabState.built)
    assert (await client.post(f"/api/labs/{built['id']}/snapshot", json={})).status_code == 409
