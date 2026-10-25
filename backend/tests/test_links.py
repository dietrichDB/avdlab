"""VS Code open-links and the lab.sync job (phase 3 §8) with a fake SSH."""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from typing import Any

from avdlab.core.config import settings
from avdlab.core.db import Host, SessionLocal
from avdlab.hosts.ssh import CompletedProcess, SSHClient
from avdlab.jobs.engine import read_log, runner


class SyncSSH:
    def __init__(self) -> None:
        self.syncs: list[tuple[str, str, list[str]]] = []

    async def run(self, cmd: str, **kw: Any) -> CompletedProcess:
        return CompletedProcess(0, "", "", command=cmd)

    async def rsync_dir(self, local_dir, remote_dir, *, exclude=(), delete=False, log=None) -> None:  # noqa: ANN001
        self.syncs.append((str(local_dir), remote_dir, list(exclude)))


async def test_open_links_and_sync(client, monkeypatch):
    fake = SyncSSH()

    @asynccontextmanager
    async def connect(cls, host, session, **kw):  # noqa: ANN001
        yield fake

    monkeypatch.setattr(SSHClient, "connect", classmethod(connect))
    async with SessionLocal() as s:
        h = Host(name=f"vs-{uuid.uuid4().hex[:6]}", address="lab1.example.net", ssh_port=2222, ssh_user="netops")
        s.add(h)
        await s.commit()
        host_id = h.id
    r = await client.post("/api/labs", json={"name": f"links-{uuid.uuid4().hex[:6]}", "template": "single-dc-l3ls"})
    lab = r.json()
    try:
        assert (await client.get(f"/api/labs/{lab['id']}/open-links")).status_code == 409
        assert (await client.post(f"/api/labs/{lab['id']}/sync")).status_code == 409
        assert (await client.patch(f"/api/labs/{lab['id']}", json={"host_id": host_id})).status_code == 200

        links = (await client.get(f"/api/labs/{lab['id']}/open-links")).json()
        rdir = f"{settings.remote_root}/labs/{lab['slug']}"
        assert links["remote_ssh"] == f"vscode://vscode-remote/ssh-remote+netops@lab1.example.net:2222{rdir}"
        assert links["clab_topology_path"] == f"{rdir}/clab/topology.clab.yml"
        assert links["remote_ssh_topology"] == links["remote_ssh"] + "/clab/topology.clab.yml"
        assert links["devcontainer"] is None and links["ssh_command"] == "ssh netops@lab1.example.net -p 2222"
        hints = " ".join(links["hints"])
        assert "ms-vscode-remote.remote-ssh" in hints and "srl-labs.vscode-containerlab" in hints and "*.clab.yml" in hints

        assert (await client.put(f"/api/labs/{lab['id']}/git", json={"url": "https://github.com/acme/fabric.git"})).status_code == 200
        links = (await client.get(f"/api/labs/{lab['id']}/open-links")).json()
        assert links["devcontainer"] == ("vscode://ms-vscode-remote.remote-containers/cloneInVolume?url="
                                         "https%3A%2F%2Fgithub.com%2Facme%2Ffabric.git")

        r = await client.post(f"/api/labs/{lab['id']}/sync")
        assert r.status_code == 202
        await runner.wait(r.json()["job_id"])
        job = (await client.get(f"/api/jobs/{r.json()['job_id']}")).json()
        assert job["status"] == "succeeded", (job["error"], read_log(job["id"]))
        assert job["result"]["remote_dir"] == rdir and job["result"]["files"] > 0
        assert fake.syncs == [(lab["dir"], rdir, [".git", "anta/reports", "anta/runs", "**/__pycache__"])]
        lab_now = (await client.get(f"/api/labs/{lab['id']}")).json()
        assert lab_now["state"] == "draft" and lab_now["settings"]["remote_dir"] == rdir  # synced, not deployed
    finally:
        assert (await client.delete(f"/api/labs/{lab['id']}")).status_code == 204
        async with SessionLocal() as s:
            await s.delete(await s.get(Host, host_id))
            await s.commit()
