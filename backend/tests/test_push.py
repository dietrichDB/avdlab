"""0.10.0: incremental config push — only devices whose intended config changed since the last deploy/push."""
from __future__ import annotations

from avdlab.core.db import LabState
from avdlab.executors.push import config_shas, push_plan, record_pushed
from avdlab.labs import service

from .test_clients_hot import HotFakeSSH, _built_lab
from .test_executors import _CREATED, install, mk_host, ran, run_job, set_state  # noqa: F401


async def test_push_plan_and_job(client, monkeypatch):
    host_id = await mk_host()
    lab = await _built_lab(client, host_id=host_id)
    # not deployed yet → plan says unavailable, POST 409
    plan = (await client.get(f"/api/labs/{lab['id']}/push/plan")).json()
    assert plan["available"] is False and plan["total"] == 8 and len(plan["unknown"]) == 8
    assert (await client.post(f"/api/labs/{lab['id']}/push")).status_code == 409

    await set_state(lab["id"], LabState.deployed)
    async with __import__("avdlab.core.db", fromlist=["SessionLocal"]).SessionLocal() as s:
        row = await s.get(__import__("avdlab.core.db", fromlist=["Lab"]).Lab, lab["id"])
        await record_pushed(lab["id"], None, config_shas(row), job_id="deploy-1")  # what lab.deploy records
    plan = (await client.get(f"/api/labs/{lab['id']}/push/plan")).json()
    assert plan["available"] and plan["changed"] == [] and plan["unknown"] == [] and len(plan["unchanged"]) == 8

    # change a knob → rebuild → only the affected devices differ
    r = await client.patch(f"/api/labs/{lab['id']}", json={"knobs": {"tenants": [{"name": "T1", "mac_vrf_vni_base": 10000, "vrfs": [{"name": "V1", "vrf_vni": 10,
        "svis": [{"id": 11, "name": "A", "enabled": True, "ip_address_virtual": "10.1.11.1/24"}]}]}]}})
    assert r.status_code == 200, r.text
    await run_job(client, (await client.post(f"/api/labs/{lab['id']}/build")).json()["job_id"])
    plan = (await client.get(f"/api/labs/{lab['id']}/push/plan")).json()
    assert plan["changed"] and all(h.startswith("dc1-leaf") for h in plan["changed"]), plan
    assert "dc1-spine1" in plan["unchanged"]

    fake = HotFakeSSH(lab["slug"])
    install(monkeypatch, fake)
    r = await client.post(f"/api/labs/{lab['id']}/push")
    assert r.status_code == 202, r.text
    job = await run_job(client, r.json()["job_id"])
    assert sorted(job["result"]["pushed"]) == plan["changed"]
    cmd = ran(fake, "ansible-playbook")[0][0]
    assert f"--limit {','.join(plan['changed'])}" in cmd and "avdlab-push-config.yml" in cmd
    assert "/dev/stdin" not in cmd
    # recorded → nothing left to push; lab is still deployed (no container was touched)
    plan2 = (await client.get(f"/api/labs/{lab['id']}/push/plan")).json()
    assert plan2["changed"] == [] and plan2["last_job_id"] == job["id"]
    assert (await client.get(f"/api/labs/{lab['id']}")).json()["state"] == "deployed"
    r = await client.post(f"/api/labs/{lab['id']}/push")
    job = await run_job(client, r.json()["job_id"])
    assert job["result"]["pushed"] == []
    # explicit host list
    r = await client.post(f"/api/labs/{lab['id']}/push", json={"hosts": ["dc1-spine1"]})
    job = await run_job(client, r.json()["job_id"])
    assert job["result"]["pushed"] == ["dc1-spine1"]
    assert "--limit dc1-spine1" in ran(fake, "ansible-playbook")[-1][0]


def test_push_plan_pure(tmp_path):
    class L:
        settings = {"pushed_configs": {"at": 1.0, "job_id": "j", "shas": {"a": "1", "b": "2"}}}
    p = push_plan(L(), {"a": "1", "b": "X", "c": "3"})  # type: ignore[arg-type]
    assert p["unchanged"] == ["a"] and p["changed"] == ["b"] and p["unknown"] == ["c"] and p["since"] == 1.0
    assert service is not None
