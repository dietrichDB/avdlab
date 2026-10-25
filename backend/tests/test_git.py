"""Git coupling (phase 3 §2): remote config, push/pull jobs and import from git against a local bare repo (no network)."""
from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import uuid
from pathlib import Path

import pytest
from git import Repo

from avdlab.core.db import Lab, LabState, SessionLocal
from avdlab.jobs.engine import read_log, runner
from avdlab.labs import git as labgit
from avdlab.labs import service

TOKEN = "ghp_SuperSecretToken123"


async def wait(client, job_id: str, ok: bool = True) -> dict:
    await runner.wait(job_id)
    job = (await client.get(f"/api/jobs/{job_id}")).json()
    if ok:
        assert job["status"] == "succeeded", (job["error"], read_log(job_id)[-3000:])
    return job


def _tmp_git_dirs() -> set[str]:
    return {p for p in os.listdir(tempfile.gettempdir()) if p.startswith("avdlab-git-")}


# ------------------------------------------------------------------ pure helpers
def test_url_checks_and_token_user():
    assert labgit.check_url("https://github.com/org/repo.git")
    assert labgit.check_url("git@gitlab.com:org/repo.git")
    assert labgit.check_url("/srv/git/lab.git")
    for bad in ("https://user:pw@github.com/x.git", "https://tok@github.com/x.git", "ext::sh -c id", "ftp://x/y"):
        with pytest.raises(ValueError):
            labgit.check_url(bad)
    assert labgit.token_username("https://github.com/o/r.git") == "x-access-token"
    assert labgit.token_username("https://gitlab.example.com/o/r.git") == "oauth2"
    assert labgit.token_username("https://git.example.com/o/r.git") == "x-access-token"


def test_auth_env_askpass_and_ssh_cleanup():
    before = _tmp_git_dirs()
    with labgit.auth_env("https://gitlab.com/o/r.git", "token", TOKEN) as env:
        askpass = env["GIT_ASKPASS"]
        assert TOKEN not in askpass and TOKEN not in Path(askpass).read_text()
        tmpdir = Path(askpass).parent
        assert stat.S_IMODE((tmpdir / "token").stat().st_mode) == 0o600
        user = subprocess.run([askpass, "Username for 'https://gitlab.com': "], capture_output=True, text=True).stdout
        pw = subprocess.run([askpass, "Password for 'https://oauth2@gitlab.com': "], capture_output=True, text=True).stdout
        assert user.strip() == "oauth2" and pw.strip() == TOKEN
        assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert not tmpdir.exists()
    with labgit.auth_env("git@github.com:o/r.git", "ssh_key", "-----BEGIN KEY-----\nabc\n-----END KEY-----") as env:
        cmd = env["GIT_SSH_COMMAND"]
        key = Path(cmd.split("-i ")[1].split()[0])
        assert "StrictHostKeyChecking=accept-new" in cmd and stat.S_IMODE(key.stat().st_mode) == 0o600
    assert not key.exists()
    with pytest.raises(RuntimeError):  # cleaned even when the body fails
        with labgit.auth_env("https://github.com/o/r.git", "token", TOKEN):
            raise RuntimeError("boom")
    assert _tmp_git_dirs() == before


# ------------------------------------------------------------------ API + jobs with a local bare repo
async def _mk_lab(client) -> dict:
    r = await client.post("/api/labs", json={"name": f"git-{uuid.uuid4().hex[:6]}", "template": "single-dc-l3ls"})
    assert r.status_code == 201, r.text
    return r.json()


async def _set_state(lab_id: str, state: LabState) -> None:
    async with SessionLocal() as s:
        row = await s.get(Lab, lab_id)
        row.state = state
        await s.commit()


async def test_git_remote_push_pull_import(client, tmp_path):
    bare = tmp_path / "remote.git"
    Repo.init(bare, bare=True, initial_branch="main")
    lab = await _mk_lab(client)
    lab_id, root = lab["id"], Path(lab["dir"])

    g = (await client.get(f"/api/labs/{lab_id}/git")).json()
    assert g["configured"] is False and g["last_sync"] is None
    assert (await client.post(f"/api/labs/{lab_id}/git/push", json={})).status_code == 409
    bad = await client.put(f"/api/labs/{lab_id}/git", json={"url": "https://u:p@github.com/x.git"})
    assert bad.status_code == 422
    assert (await client.put(f"/api/labs/{lab_id}/git", json={"url": str(bare), "auth_kind": "token"})).status_code == 422

    # token auth: a local remote never asks for it, but the token must land nowhere but the secret store
    r = await client.put(f"/api/labs/{lab_id}/git", json={"url": str(bare), "branch": "main", "auth_kind": "token",
                                                          "token": TOKEN})
    assert r.status_code == 200, r.text
    assert r.json() == {"url": str(bare), "branch": "main", "auth_kind": "token", "has_secret": True}
    assert TOKEN not in (root / ".git" / "config").read_text()
    assert Repo(root).remote("origin").url == str(bare)
    gi = (root / ".gitignore").read_text()
    assert "clab/ceos-config/" in gi and "intended" not in gi and "documentation" not in gi
    g = (await client.get(f"/api/labs/{lab_id}/git")).json()
    assert g["configured"] and g["auth_kind"] == "token" and g["has_secret"] and g["ahead"] >= 1 and g["behind"] == 0
    assert TOKEN not in str(g) and TOKEN not in str((await client.get(f"/api/labs/{lab_id}")).json())
    # re-PUT without token keeps the stored one (write-only)
    r = await client.put(f"/api/labs/{lab_id}/git", json={"url": str(bare), "auth_kind": "token"})
    assert r.status_code == 200 and r.json()["has_secret"]

    before_tmp = _tmp_git_dirs()
    r = await client.post(f"/api/labs/{lab_id}/git/push", json={"force": False})
    assert r.status_code == 202
    job = await wait(client, r.json()["job_id"])
    head = Repo(root).head.commit.hexsha
    assert job["result"]["commit"] == head and job["result"]["ahead"] == 0
    assert Repo(bare).heads.main.commit.hexsha == head
    assert TOKEN not in read_log(r.json()["job_id"]) and TOKEN not in (root / ".git" / "config").read_text()
    assert _tmp_git_dirs() == before_tmp
    g = (await client.get(f"/api/labs/{lab_id}/git")).json()
    assert g["last_sync"]["direction"] == "push" and g["last_sync"]["ok"] and g["ahead"] == 0

    # someone else edits group_vars in the remote → pull fast-forwards, re-validates, built → draft
    other = tmp_path / "other"
    Repo.clone_from(str(bare), other)
    f = other / "group_vars" / "NETWORK_SERVICES.yml"
    f.write_text(f.read_text().replace("name: VRF10_VLAN11", "name: VRF10_VLAN11_REMOTE", 1))
    orepo = Repo(other)
    orepo.index.add(["group_vars/NETWORK_SERVICES.yml"])
    orepo.index.commit("remote edit", author=service.GIT_AUTHOR, committer=service.GIT_AUTHOR)
    orepo.remote("origin").push("main")
    await _set_state(lab_id, LabState.built)
    job = await wait(client, (await client.post(f"/api/labs/{lab_id}/git/pull")).json()["job_id"])
    assert job["result"]["inputs_changed"] and job["result"]["changed_files"] == ["group_vars/NETWORK_SERVICES.yml"]
    assert job["result"]["violations"] == [] and job["result"]["behind"] == 0
    assert "VRF10_VLAN11_REMOTE" in (root / "group_vars" / "NETWORK_SERVICES.yml").read_text()
    lab_now = (await client.get(f"/api/labs/{lab_id}")).json()
    assert lab_now["state"] == "draft" and lab_now["settings"]["git"]["last_sync"]["direction"] == "pull"

    # uncommitted local changes → pull refused (job fails, last_sync records it)
    (root / "group_vars" / "stray.yml").write_text("x: 1\n")
    job = await wait(client, (await client.post(f"/api/labs/{lab_id}/git/pull")).json()["job_id"], ok=False)
    assert job["status"] == "failed" and "uncommitted" in job["error"]
    assert (await client.get(f"/api/labs/{lab_id}/git")).json()["last_sync"]["ok"] is False
    (root / "group_vars" / "stray.yml").unlink()

    # import from git → new custom lab with the remote kept
    r = await client.post("/api/labs/import/git", json={"name": f"cloned-{uuid.uuid4().hex[:6]}", "url": str(bare),
                                                        "branch": "main", "auth_kind": "none"})
    assert r.status_code == 202, r.text
    job = await wait(client, r.json()["job_id"])
    new_id = job["result"]["lab_id"]
    assert job["lab_id"] == new_id and job["result"]["import_warnings"] == []
    cloned = (await client.get(f"/api/labs/{new_id}")).json()
    assert cloned["template"] == "custom" and cloned["state"] == "draft" and cloned["settings"]["fabric_name"] == "FABRIC"
    cg = (await client.get(f"/api/labs/{new_id}/git")).json()
    assert cg["configured"] and cg["url"] == str(bare) and cg["branch"] == "main"
    assert "VRF10_VLAN11_REMOTE" in (Path(cloned["dir"]) / "group_vars" / "NETWORK_SERVICES.yml").read_text()
    assert (await client.post("/api/labs/import/git", json={"name": cloned["name"], "url": str(bare)})).status_code == 409
    job = await wait(client, (await client.post("/api/labs/import/git", json={
        "name": f"nope-{uuid.uuid4().hex[:6]}", "url": str(tmp_path / "missing.git")})).json()["job_id"], ok=False)
    assert job["status"] == "failed" and "clone failed" in job["error"]

    # remove the remote: origin gone, the token secret created for the lab deleted
    assert (await client.delete(f"/api/labs/{lab_id}/git")).status_code == 204
    assert (await client.get(f"/api/labs/{lab_id}/git")).json()["configured"] is False
    assert "origin" not in [x.name for x in Repo(root).remotes]
    assert not [s for s in (await client.get("/api/secrets")).json() if s["name"] == f"git-token-{lab['slug']}"]
    for lid in (lab_id, new_id):
        assert (await client.delete(f"/api/labs/{lid}")).status_code == 204
