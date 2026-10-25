"""0.10.0: ANTA security advisories (anta psirt) demo job."""
from __future__ import annotations

from avdlab.core.db import LabState
from avdlab.executors.psirt import anta_inventory, parse_psirt_csv, psirt_cmd

from .test_clients_hot import HotFakeSSH, _built_lab
from .test_executors import _CREATED, install, mk_host, ran, read_log, run_job, set_state  # noqa: F401

CSV = '''Device,Test Name,Advisory Result,Advisory Result Messages,Vulnerability Result,Vulnerability Result Messages,Vulnerability Remediation,Advisory Remediation,Advisory ID,Advisory Title,Advisory Severity,Advisory URL,Advisory Description,Vulnerability ID,Vulnerability Description,Vulnerability Severity
dc1-spine1,SA117,affected,,affected,EOS 4.34.2F is affected\\nupgrade,Upgrade to 4.34.3F,Upgrade to 4.34.3F,SA0117,Arista EOS BGP issue,high,https://www.arista.com/en/support/advisories-notices/security-advisory/117,desc,CVE-2025-0001,BGP crash,high
dc1-spine1,SA140,not affected,,not affected,,,,SA0140,Something else,medium,https://x/140,desc,CVE-2025-0002,x,medium
dc1-leaf1a,SA117,mitigated,,mitigated,ACL in place,Keep the ACL,Keep the ACL,SA0117,Arista EOS BGP issue,high,https://www.arista.com/en/support/advisories-notices/security-advisory/117,desc,CVE-2025-0001,BGP crash,high
dc1-leaf1a,SA140,error,collection failed,error,collection failed,,,SA0140,Something else,medium,https://x/140,desc,,,
'''


def test_parse_and_inventory():
    out = parse_psirt_csv(CSV)
    assert out["summary"] == {"affected": 1, "inconclusive": 0, "mitigated": 1, "not affected": 1, "error": 1, "skipped": 0}
    assert out["devices"] == ["dc1-leaf1a", "dc1-spine1"]
    assert [a["id"] for a in out["advisories"]] == ["SA0117", "SA0140"] and out["advisories"][0]["severity"] == "high"
    assert out["assessments"][0] == {"device": "dc1-spine1", "advisory_id": "SA0117", "test": "SA117", "title": "Arista EOS BGP issue",
                                     "severity": "high", "url": "https://www.arista.com/en/support/advisories-notices/security-advisory/117", "result": "affected"}
    assert out["rows"][0]["vulnerability_messages"] == "EOS 4.34.2F is affected\nupgrade"
    inv, names = anta_inventory({"dc1-spine1": {"kind": "arista_ceos", "mgmt_ip": "172.16.1.11", "state": "running"},
                                 "dc1-host1": {"kind": "linux", "mgmt_ip": "172.16.1.200", "state": "running"},
                                 "dc1-leaf1a": {"kind": "arista_ceos", "mgmt_ip": "172.16.1.101", "state": "exited"}})
    assert names == ["dc1-spine1"] and "host: 172.16.1.11" in inv and "dc1-host1" not in inv
    cmd = psirt_cmd("/r", "clab-mgmt-x", "/home/u/.env", "img", "csv --csv-output anta/psirt/psirt.csv", ["SA117", "SA140"])
    assert "--env-file /home/u/.env" in cmd and "--test SA117 --test SA140" in cmd and "--insecure --enable" in cmd and " img psirt -i anta/psirt/inventory.yml" in cmd
    assert " img anta psirt -i " in psirt_cmd("/r", "n", "/e", "img", "csv", [], entrypoint_is_anta=False)


async def test_psirt_job(client, monkeypatch):
    host_id = await mk_host()
    lab = await _built_lab(client, host_id=host_id)
    assert (await client.post(f"/api/labs/{lab['id']}/psirt")).status_code == 409  # not deployed
    await set_state(lab["id"], LabState.deployed)
    # runtime nodes known from an inspect
    from avdlab.executors.common import update_lab
    await update_lab(lab["id"], last_inspect={"at": 1.0, "nodes": [
        {"name": "dc1-spine1", "kind": "arista_ceos", "mgmt_ip": "172.16.1.11", "state": "running"},
        {"name": "dc1-leaf1a", "kind": "arista_ceos", "mgmt_ip": "172.16.1.101", "state": "running"}]})
    fake = HotFakeSSH(lab["slug"])
    fake.responses = {
        "docker image inspect ghcr.io/aristanetworks/anta": (1, ""),
        "docker pull ghcr.io/aristanetworks/anta": (0, "Status: Downloaded newer image\n"),
        "psirt --help": (0, "Usage: anta psirt ... Run ANTA tests for Arista security advisories\n"),
        "mktemp": (0, "/home/ubuntu/.avdlab-psirt-env.Ab12\n"),
        "cat > /home/ubuntu/.avdlab-psirt-env.Ab12": (0, ""),
        "anta psirt": (1, "Security Advisory tests are in preview\n"),
        **fake.responses,
    }
    install(monkeypatch, fake)

    async def fake_get(remote, local):  # the "reports" written by anta in the container
        from pathlib import Path
        if remote.endswith("psirt.csv"):
            Path(local).write_text(CSV)
        elif remote.endswith("psirt.md"):
            Path(local).write_text("# Advisory Assessment Summary\n")
    monkeypatch.setattr(fake, "get", fake_get)

    r = await client.post(f"/api/labs/{lab['id']}/psirt", json={"tests": ["SA117", "SA140"]})
    assert r.status_code == 202, r.text
    job = await run_job(client, r.json()["job_id"])
    res = job["result"]
    assert res["summary"]["affected"] == 1 and res["devices"] == ["dc1-leaf1a", "dc1-spine1"] and res["has_md"]
    cmds = [c for c, _ in fake.calls if "ghcr.io/aristanetworks/anta:1.10.0 psirt -i" in c]
    assert ran(fake, "docker pull ghcr.io/aristanetworks/anta:1.10.0")
    assert len(cmds) == 2 and "--test SA117 --test SA140" in cmds[0] and "--network clab-mgmt-" in cmds[0]
    assert "csv --csv-output anta/psirt/psirt.csv" in cmds[0] and "md-report --md-output anta/psirt/psirt.md" in cmds[1]
    assert any("ANTA_PASSWORD=arista123" in i for i in fake.inputs) and "arista123" not in read_log(job["id"])
    assert ran(fake, "rm -f /home/ubuntu/.avdlab-psirt-env.Ab12")
    st = (await client.get(f"/api/labs/{lab['id']}/psirt")).json()
    assert st["available"] and st["last"]["summary"]["mitigated"] == 1 and len(st["last"]["assessments"]) == 4
    assert (await client.get(f"/api/labs/{lab['id']}/psirt/report.md")).text.startswith("# Advisory")
