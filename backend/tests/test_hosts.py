"""Hosts module tests: probe parsing, recipe engine, host CRUD/keys API, bootstrap job with fake SSH, devcontainer zip."""
from __future__ import annotations

import io
import json
import re
import zipfile
from contextlib import asynccontextmanager
from typing import Any

import pytest

from avdlab.core.config import settings
from avdlab.hosts import devcontainer, probe, recipes
from avdlab.hosts.ssh import CompletedProcess, SSHClient, sudo_wrap
from avdlab.jobs.engine import read_log, runner


# ------------------------------------------------------------------ fake SSH
class FakeSSH:
    """Scripted command runner: `responses` maps a substring of the command to (rc, stdout) or a callable.

    Commands are matched in insertion order by substring; unmatched commands return `default_rc`.
    `state` lets a step's apply flip its own check (idempotency simulation).
    """

    def __init__(self, responses: dict[str, Any] | None = None, *, default_rc: int = 1) -> None:
        self.responses: dict[str, Any] = dict(responses or {})
        self.default_rc = default_rc
        self.calls: list[tuple[str, bool]] = []
        self.state: set[str] = set()

    async def run(self, cmd: str, *, sudo: bool = False, timeout: float | None = 600, log: Any = None,
                  input: str | None = None) -> CompletedProcess:
        self.calls.append((cmd, sudo))
        for needle, resp in self.responses.items():
            if needle in cmd:
                if callable(resp):
                    resp = resp(self, cmd)
                rc, out = resp if isinstance(resp, tuple) else (0, str(resp))
                if log and out:
                    log(out)
                return CompletedProcess(rc, out, "", command=cmd)
        return CompletedProcess(self.default_rc, "", f"no fake response for: {cmd[:60]}", command=cmd)


UBUNTU_OS_RELEASE = 'PRETTY_NAME="Ubuntu 24.04.1 LTS"\nNAME="Ubuntu"\nVERSION_ID="24.04"\nID=ubuntu\nID_LIKE=debian\n'
ROCKY_OS_RELEASE = 'NAME="Rocky Linux"\nVERSION_ID="9.4"\nID="rocky"\nID_LIKE="rhel centos fedora"\nPRETTY_NAME="Rocky Linux 9.4"\n'


def probe_responses(*, docker: bool = True, clab: bool = True, low_inotify: bool = False) -> dict[str, Any]:
    sysctl_vals = ["128" if low_inotify else "64000", "1048576", "1", "262144", "2097152"]
    r: dict[str, Any] = {
        "cat /etc/os-release": (0, UBUNTU_OS_RELEASE),
        "uname -r": (0, "6.8.0-45-generic\n"),
        "uname -m": (0, "x86_64\n"),
        "nproc": (0, "16\n"),
        "MemTotal": (0, "MemTotal:       65807792 kB\n"),
        "df -Pk /var/lib/docker": (0, "/dev/sda1 500000000 100000000 380000000 21% /\n"),
        "df -Pk /": (0, "/dev/sda1 500000000 100000000 380000000 21% /\n"),
        "{{.Server.Version}}": (0, "27.3.1\n") if docker else (1, ""),
        "{{.Client.Version}}": (0, "27.3.1\n") if docker else (1, ""),
        "docker info": (0, "yes\n" if docker else "no\n"),
        "id -nG": (0, "ubuntu adm docker\n"),
        "containerlab version": (0, "    version: 0.68.0\n" if clab else ""),
        "sysctl -n": (0, "\n".join(sysctl_vals) + "\n"),
        "docker images": (0, "arista/ceos:4.34.2F\n" if docker else ""),
        "test -d": (0, "yes\n"),
        "python3 --version": (0, "Python 3.12.3\n"),
        "command -v rsync": (0, "yes\n"),
        "sudo -n true": (0, "yes\n"),
        "command -v pipx": (0, "no\n"),
    }
    return r


def install_fake(monkeypatch: pytest.MonkeyPatch, fake: FakeSSH) -> None:
    @asynccontextmanager
    async def fake_connect(cls, host, session, **kw):  # noqa: ANN001
        yield fake

    monkeypatch.setattr(SSHClient, "connect", classmethod(fake_connect))


# ------------------------------------------------------------------ probe parsing
async def test_probe_parse_and_recommendations():
    fake = FakeSSH(probe_responses())
    caps = probe.parse_probe(await probe.collect(fake))
    assert caps["os"]["family"] == "debian" and caps["os"]["version"] == "24.04" and caps["os"]["arch"] == "x86_64"
    assert caps["cpu"]["nproc"] == 16 and caps["mem"]["total_gb"] == 62.8
    assert caps["docker"] == {"installed": True, "server_version": "27.3.1", "client_version": "27.3.1", "user_can_run": True, "user_in_group": True}
    assert caps["containerlab"] == {"installed": True, "version": "0.68.0"}
    assert caps["sysctl"]["fs.inotify.max_user_instances"] == 64000 and caps["sysctl"]["fs.file-max"] == 2097152
    assert caps["images"] == ["arista/ceos:4.34.2F"] and caps["remote_root"]["exists"] and caps["rsync"] and caps["sudo_nopasswd"]
    assert caps["python3"]["version"] == "3.12.3"
    # healthy host → only the capacity hint
    assert len(caps["recommendations"]) == 1 and "cEOS nodes" in caps["recommendations"][0]


async def test_probe_recommendations_missing_bits():
    fake = FakeSSH(probe_responses(docker=False, clab=False, low_inotify=True))
    caps = probe.parse_probe(await probe.collect(fake))
    recs = "\n".join(caps["recommendations"])
    assert not caps["docker"]["installed"] and not caps["containerlab"]["installed"]
    assert "docker is not installed" in recs and "containerlab is not installed" in recs
    assert "fs.inotify.max_user_instances=128 is too low for >10 cEOS" in recs
    assert "no cEOS image present" in recs


def test_probe_tolerates_failures():
    outputs = {name: CompletedProcess(127, "", "not found", command=cmd) for name, cmd in probe.probe_commands().items()}
    outputs["os_release"] = CompletedProcess(0, ROCKY_OS_RELEASE)
    caps = probe.parse_probe(outputs)
    assert caps["os"]["family"] == "rhel" and caps["cpu"]["nproc"] is None and caps["mem"]["total_gb"] is None
    assert all(v is None for v in caps["sysctl"].values())
    assert any("unknown" in r for r in caps["recommendations"])


def test_os_family_detection():
    assert probe.os_family({"ID": "ubuntu", "ID_LIKE": "debian"}) == "debian"
    assert probe.os_family({"ID": "debian"}) == "debian"
    assert probe.os_family({"ID": "rocky", "ID_LIKE": "rhel centos fedora"}) == "rhel"
    assert probe.os_family({"ID": "almalinux", "ID_LIKE": "rhel centos fedora"}) == "rhel"
    assert probe.os_family({"ID": "alpine"}) is None


# ------------------------------------------------------------------ recipe engine
def _stateful(fake: FakeSSH, check_key: str, apply_key: str, *, apply_works: bool = True) -> None:
    """check passes only after apply ran (entries are put first so they win over generic probe responses)."""
    check = lambda f, c: (0 if check_key in f.state else 1, "")  # noqa: E731
    apply = (lambda f, c: (f.state.add(check_key) or 0, "applied")) if apply_works else (lambda f, c: (1, "boom"))  # noqa: E731
    fake.responses = {check_key: check, apply_key: apply, **{k: v for k, v in fake.responses.items() if k not in (check_key, apply_key)}}


async def test_recipe_engine_skip_apply_fail():
    logs: list[str] = []
    fake = FakeSSH()
    _stateful(fake, "check-two", "apply-two")
    _stateful(fake, "check-three", "apply-three", apply_works=False)
    fake.responses["check-one"] = (0, "")
    recipe = recipes.Recipe("t", [
        recipes.Step("one", "check-one", "apply-one"),
        recipes.Step("two", "check-two", "apply-two", sudo=False),
        recipes.Step("gated", "check-gated", "apply-gated", when=lambda o: bool(o.get("x"))),
        recipes.Step("three", "check-three", "apply-three"),
        recipes.Step("never", "check-never", "apply-never"),
    ])
    with pytest.raises(recipes.RecipeError) as ei:
        await recipes.run_recipe(recipe, fake, options={}, log=logs.append)
    res = ei.value.results
    assert [(r.name, r.status) for r in res] == [("one", "skip"), ("two", "ok"), ("gated", "skip"), ("three", "FAIL")]
    assert "[skip] one" in "\n".join(logs) and "[apply] two" in "\n".join(logs) and "[ok] two" in "\n".join(logs)
    assert any(line.startswith("[FAIL] three") for line in logs)
    # sudo flag is passed through; gated step never touched ssh; "never" not reached
    assert ("apply-two", False) in fake.calls and not any("gated" in c for c, _ in fake.calls) and not any("never" in c for c, _ in fake.calls)
    # stop_on_fail=False continues
    fake2 = FakeSSH({"check-one": (0, "")})
    res2 = await recipes.run_recipe(recipes.Recipe("t", [recipes.Step("bad", "c-bad", "a-bad"), recipes.Step("one", "check-one", "a")]),
                                    fake2, stop_on_fail=False)
    assert [r.status for r in res2] == ["FAIL", "skip"]


def test_clab_host_recipe_families_and_modes():
    deb = recipes.clab_host_recipe("debian", "ubuntu", {"clab_mode": "native"})
    rhel = recipes.clab_host_recipe("rhel", "rocky", {"clab_mode": "container", "native_toolchain": True})
    names = [s.name for s in deb.steps]
    assert names[0] == "base packages" and "docker engine" in names and "sysctls" in names and "agent image" in names
    assert "apt-get" in deb.steps[0].apply and "dpkg -s" in deb.steps[0].check
    assert "dnf install" in rhel.steps[0].apply and "rpm -q" in rhel.steps[0].check
    by = {s.name: s for s in rhel.steps}
    assert by["containerlab wrapper (container mode)"].when({"clab_mode": "container"}) and not by["containerlab (native)"].when({"clab_mode": "container"})
    assert settings.clab_image in by["containerlab wrapper (container mode)"].apply
    assert "--privileged" in by["containerlab wrapper (container mode)"].apply and "/var/run/docker.sock" in by["containerlab wrapper (container mode)"].apply
    assert "usermod -aG docker rocky" == by["docker group membership"].apply
    assert settings.agent_image in by["agent image"].apply
    assert f"pyavd[ansible]=={settings.avd_version}" in by["pyavd (native toolchain)"].apply and not by["pyavd (native toolchain)"].sudo
    assert f"arista.avd:=={settings.avd_version}" in by["arista.avd collection (native toolchain)"].apply
    assert "fs.inotify.max_user_instances=64000" in by["sysctls"].apply and "/etc/sysctl.d/90-avdlab.conf" in by["sysctls"].apply
    with pytest.raises(ValueError):
        recipes.clab_host_recipe("debian", "u", {"clab_mode": "weird"})


def test_sudo_wrap_quotes():
    assert sudo_wrap("echo 'a b' && ls") == "sudo -n bash -c 'echo '\"'\"'a b'\"'\"' && ls'"


async def test_detect_family_unsupported():
    with pytest.raises(RuntimeError):
        await recipes.detect_family(FakeSSH({"cat /etc/os-release": (0, 'ID=alpine\n')}))
    assert await recipes.detect_family(FakeSSH({"cat /etc/os-release": (0, ROCKY_OS_RELEASE)})) == "rhel"


# ------------------------------------------------------------------ API: keys + CRUD
async def test_generate_key_and_host_crud(client):
    r = await client.post("/api/hosts/keys/generate", json={"name": "lab-key"})
    assert r.status_code == 201, r.text
    key = r.json()
    assert key["public_key"].startswith("ssh-ed25519 ") and key["public_key"].endswith(" avdlab")
    assert "private" not in json.dumps(key).lower()
    # stored encrypted, revealable, with public key in meta
    from avdlab.core.db import Secret, SessionLocal
    from avdlab.core.security import secret_store
    async with SessionLocal() as s:
        sec = await s.get(Secret, key["secret_id"])
        assert sec.kind.value == "ssh_key" and sec.meta["public_key"] == key["public_key"]
        assert "BEGIN OPENSSH PRIVATE KEY" not in sec.ciphertext
        assert "BEGIN OPENSSH PRIVATE KEY" in await secret_store.reveal(s, sec.id)
    listed = (await client.get("/api/secrets")).json()
    assert all("ciphertext" not in x and "value" not in x for x in listed)
    assert (await client.post("/api/hosts/keys/generate", json={"name": "lab-key"})).status_code == 409

    # create
    body = {"name": "mini", "address": "10.0.0.5", "ssh_user": "ubuntu", "ssh_key_secret_id": key["secret_id"]}
    r = await client.post("/api/hosts", json=body)
    assert r.status_code == 201, r.text
    host = r.json()
    assert host["name"] == "mini" and host["ssh_port"] == 22 and host["kind"] == "linux" and host["last_probe_at"] is None
    assert (await client.post("/api/hosts", json=body)).status_code == 409
    assert (await client.post("/api/hosts", json={**body, "name": "x", "ssh_key_secret_id": "nope"})).status_code == 422
    # list / get / patch
    assert [h["id"] for h in (await client.get("/api/hosts")).json()] == [host["id"]]
    assert (await client.get(f"/api/hosts/{host['id']}")).json()["address"] == "10.0.0.5"
    r = await client.patch(f"/api/hosts/{host['id']}", json={"ssh_port": 2222, "address": "10.0.0.6"})
    assert r.status_code == 200 and r.json()["ssh_port"] == 2222 and r.json()["address"] == "10.0.0.6"
    assert (await client.get(f"/api/hosts/{host['id']}/capabilities")).json()["capabilities"] == {}
    assert (await client.get("/api/hosts/missing")).status_code == 404
    # delete
    assert (await client.delete(f"/api/hosts/{host['id']}")).status_code == 204
    assert (await client.get(f"/api/hosts/{host['id']}")).status_code == 404
    assert {"clab-host", "act-tools"} <= {r["name"] for r in (await client.get("/api/hosts/recipes")).json()}  # phase 4 (D): act-tools


# ------------------------------------------------------------------ jobs end-to-end with fake SSH
async def _mk_host(client, name: str = "h1") -> dict:
    key = (await client.post("/api/hosts/keys/generate", json={"name": f"key-{name}"})).json()
    r = await client.post("/api/hosts", json={"name": name, "address": "192.0.2.10", "ssh_key_secret_id": key["secret_id"]})
    assert r.status_code == 201, r.text
    return r.json()


async def test_probe_job(client, monkeypatch):
    fake = FakeSSH(probe_responses(clab=False))
    install_fake(monkeypatch, fake)
    host = await _mk_host(client, "probe-host")
    r = await client.post(f"/api/hosts/{host['id']}/probe")
    assert r.status_code == 202
    jid = r.json()["job_id"]
    await runner.wait(jid)
    j = (await client.get(f"/api/jobs/{jid}")).json()
    assert j["status"] == "succeeded", read_log(jid)
    assert j["result"]["containerlab"]["installed"] is False and "raw" not in j["result"]
    caps = (await client.get(f"/api/hosts/{host['id']}/capabilities")).json()
    assert caps["last_probe_at"] and caps["capabilities"]["docker"]["server_version"] == "27.3.1"
    assert any("containerlab is not installed" in r for r in caps["capabilities"]["recommendations"])
    assert (await client.get(f"/api/hosts/{host['id']}")).json()["summary"]["docker"] == "27.3.1"


async def test_bootstrap_job_end_to_end(client, monkeypatch):
    fake = FakeSSH(probe_responses())
    # base packages + docker already present; containerlab, sysctls, remote root, agent image get applied
    fake.responses["dpkg -s"] = (0, "")
    fake.responses["id -nG ubuntu"] = (0, "")
    _stateful(fake, "command -v containerlab", "containerlab.dev/setup")
    _stateful(fake, "for kv in fs.inotify", "sysctl --system")
    _stateful(fake, "stat -c %U", "mkdir -p /opt/avdlab")
    _stateful(fake, f"docker image inspect {settings.agent_image}", f"docker pull {settings.agent_image}")
    install_fake(monkeypatch, fake)
    host = await _mk_host(client, "boot-host")

    r = await client.post(f"/api/hosts/{host['id']}/bootstrap", json={"recipe": "clab-host", "options": {"clab_mode": "native"}})
    assert r.status_code == 202
    jid = r.json()["job_id"]
    await runner.wait(jid)
    j = (await client.get(f"/api/jobs/{jid}")).json()
    log = read_log(jid)
    assert j["status"] == "succeeded", log
    assert j["result"]["family"] == "debian"
    status = {s["name"]: s["status"] for s in j["result"]["steps"]}
    assert status["base packages"] == "skip" and status["docker engine"] == "skip"
    assert status["containerlab (native)"] == "ok" and status["sysctls"] == "ok" and status["remote root"] == "ok" and status["agent image"] == "ok"
    assert status["containerlab wrapper (container mode)"] == "skip" and status["pyavd (native toolchain)"] == "skip"
    assert set(j["result"]["applied"]) == {"containerlab (native)", "sysctls", "remote root", "agent image"}
    assert "[skip] base packages" in log and "[apply] sysctls" in log and "[ok] sysctls" in log
    # apply commands ran with sudo, and the host got re-probed afterwards
    assert any("containerlab.dev/setup" in c and sudo for c, sudo in fake.calls)
    assert (await client.get(f"/api/hosts/{host['id']}/capabilities")).json()["last_probe_at"]
    assert (await client.post(f"/api/hosts/{host['id']}/bootstrap", json={"recipe": "nope"})).status_code == 422


async def test_bootstrap_job_fails_when_recheck_fails(client, monkeypatch):
    fake = FakeSSH(probe_responses())
    fake.responses["dpkg -s"] = (1, "")  # base packages missing, and apt-get "succeeds" without fixing it
    fake.responses["apt-get"] = (0, "Reading package lists...")
    install_fake(monkeypatch, fake)
    host = await _mk_host(client, "fail-host")
    jid = (await client.post(f"/api/hosts/{host['id']}/bootstrap")).json()["job_id"]
    await runner.wait(jid)
    j = (await client.get(f"/api/jobs/{jid}")).json()
    assert j["status"] == "failed" and "base packages" in j["error"]
    assert "[FAIL] base packages" in read_log(jid)


async def test_verify_job(client, monkeypatch):
    fake = FakeSSH(probe_responses())
    fake.responses["import pyavd, anta"] = (0, "6.4.0 1.10.0\n")
    install_fake(monkeypatch, fake)
    host = await _mk_host(client, "verify-host")
    jid = (await client.post(f"/api/hosts/{host['id']}/verify")).json()["job_id"]
    await runner.wait(jid)
    j = (await client.get(f"/api/jobs/{jid}")).json()
    assert j["status"] == "succeeded" and j["result"]["ok"] is True
    assert {c["name"] for c in j["result"]["checks"]} == {"containerlab", "agent image"}


# ------------------------------------------------------------------ devcontainer
@pytest.mark.parametrize("mode", ["local-clab", "remote-host"])
def test_devcontainer_files(mode: str):
    files = devcontainer.generate_files(mode, avd_version="6.4.0", include_vscode_extensions=True)
    assert set(files) == {".devcontainer/devcontainer.json", ".devcontainer/Dockerfile", "ansible.cfg", "requirements.txt", "Makefile", "README.md"}
    dc = json.loads(files[".devcontainer/devcontainer.json"])
    assert dc["build"]["dockerfile"] == "Dockerfile" and dc["build"]["args"]["AVD_VERSION"] == "6.4.0"
    assert "redhat.ansible" in dc["customizations"]["vscode"]["extensions"]
    has_dind = any("docker-in-docker" in k for k in dc["features"])
    assert has_dind == (mode == "local-clab")
    df = files[".devcontainer/Dockerfile"]
    assert df.startswith(f"FROM {settings.agent_image}") and "containerlab.dev/setup" in df and "eos-downloader" in df and "docker-ce-cli" in df
    assert "pyavd[ansible]==6.4.0" in files["requirements.txt"]
    for target in ("build:", "deploy:", "validate:", "clab-up:", "clab-down:"):
        assert re.search(rf"^{re.escape(target)}", files["Makefile"], re.M), target
    assert "[defaults]" in files["ansible.cfg"] and mode in files["README.md"]
    if mode == "remote-host":
        assert "LAB_HOST" in files["Makefile"] and "rsync" in files["Makefile"]
    no_ext = json.loads(devcontainer.generate_files(mode, include_vscode_extensions=False)[".devcontainer/devcontainer.json"])
    assert no_ext["customizations"] == {}
    with pytest.raises(ValueError):
        devcontainer.generate_files("nope")


async def test_devcontainer_endpoint_zip(client):
    r = await client.post("/api/hosts/devcontainer", json={"mode": "local-clab", "avd_version": "6.4.0", "include_vscode_extensions": True})
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert "avdlab-devcontainer-local-clab.zip" in r.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        names = set(zf.namelist())
        assert {".devcontainer/devcontainer.json", ".devcontainer/Dockerfile", "Makefile", "README.md", "ansible.cfg", "requirements.txt"} <= names
        dc = json.loads(zf.read(".devcontainer/devcontainer.json"))
        assert dc["name"].startswith("avdlab") and dc["privileged"] is True
    assert (await client.post("/api/hosts/devcontainer", json={"mode": "bogus"})).status_code == 422


# ------------------------------------------------------------------ real asyncssh client against an in-process server
async def test_ssh_client_against_local_server(client, tmp_path):
    import asyncssh
    import subprocess
    import uuid
    from avdlab.core.db import Host, SecretKind, SessionLocal
    from avdlab.core.security import secret_store
    from avdlab.hosts import ssh as sshmod

    ckey = asyncssh.generate_private_key("ssh-ed25519")
    hkey = asyncssh.generate_private_key("ssh-ed25519")

    class Srv(asyncssh.SSHServer):
        def begin_auth(self, username: str) -> bool:
            return True

        def public_key_auth_supported(self) -> bool:
            return True

        def validate_public_key(self, username: str, key: asyncssh.SSHKey) -> bool:
            return key == ckey.convert_to_public()

    async def handler(proc: asyncssh.SSHServerProcess) -> None:
        data = await proc.stdin.read() if "tar -C" in (proc.command or "") else b""
        r = subprocess.run(["bash", "-c", proc.command or "true"], capture_output=True, input=data)
        proc.stdout.write(r.stdout)
        proc.stderr.write(r.stderr)
        proc.exit(r.returncode)

    server = await asyncssh.listen("127.0.0.1", 0, server_host_keys=[hkey], server_factory=Srv, process_factory=handler,
                                   sftp_factory=True, encoding=None)
    port = server.sockets[0].getsockname()[1]
    kh = sshmod.known_hosts_path()
    if kh.exists():
        kh.unlink()
    try:
        async with SessionLocal() as s:
            sec = await secret_store.create(s, "k-" + uuid.uuid4().hex, SecretKind.ssh_key, ckey.export_private_key("openssh").decode())
            host = Host(name="local-ssh", address="127.0.0.1", ssh_port=port, ssh_user="tester", ssh_key_secret_id=sec.id)
            lines: list[str] = []
            async with SSHClient.connect(host, s, log=lines.append) as ssh:
                cp = await ssh.run("echo hi; echo err >&2; exit 3", log=lines.append)
                assert (cp.returncode, cp.stdout, cp.stderr) == (3, "hi\n", "err\n")
                assert "hi" in lines and "! err" in lines and any(l.startswith("[tofu] pinned") for l in lines)
                (tmp_path / "src").mkdir()
                (tmp_path / "src" / "a.txt").write_text("x")
                (tmp_path / "src" / ".git").mkdir()
                (tmp_path / "src" / ".git" / "z").write_text("")
                await ssh.put(tmp_path / "src" / "a.txt", str(tmp_path / "b.txt"))
                await ssh.get(str(tmp_path / "b.txt"), tmp_path / "c.txt")
                assert (tmp_path / "c.txt").read_text() == "x"
                await ssh.rsync_dir(tmp_path / "src", str(tmp_path / "sync"), exclude=[".git"])
                assert sorted(p.name for p in (tmp_path / "sync").iterdir()) == ["a.txt"]
            # second connection reuses the pinned key (no second [tofu] line)
            lines.clear()
            async with SSHClient.connect(host, s, log=lines.append) as ssh:
                assert (await ssh.run("echo again")).stdout == "again\n"
            assert not any("[tofu]" in l for l in lines)
            assert f"[127.0.0.1]:{port} ssh-ed25519" in kh.read_text()
    finally:
        server.close()


def test_sudo_wrap_modes():
    from avdlab.hosts.ssh import sudo_wrap, is_sudo_auth_error
    assert sudo_wrap("apt-get update").startswith("sudo -n bash -c ")
    assert sudo_wrap("apt-get update", with_password=True).startswith("sudo -S -p '' bash -c ")
    assert is_sudo_auth_error("sudo: a password is required")
    assert is_sudo_auth_error("sudo: interactive authentication is required")
    assert not is_sudo_auth_error("E: Unable to locate package")


async def test_host_sudo_password_secret_roundtrip(client):
    r = await client.post("/api/secrets", json={"name": "sudo-pw", "kind": "password", "value": "hunter2"})
    sid = r.json()["id"]
    r = await client.post("/api/hosts", json={"name": "sudo-host", "address": "10.9.9.9", "ssh_user": "adm", "sudo_password_secret_id": sid})
    assert r.status_code == 201 and r.json()["sudo_password_secret_id"] == sid
    r = await client.patch(f"/api/hosts/{r.json()['id']}", json={"sudo_password_secret_id": None})
    assert r.status_code == 200 and r.json()["sudo_password_secret_id"] is None


def test_light_migration_adds_column(tmp_path):
    import asyncio, sqlite3
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from avdlab.core.db import Base, _apply_light_migrations
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE hosts (id VARCHAR(32) PRIMARY KEY, name VARCHAR(120))")
    con.commit(); con.close()
    eng = create_async_engine(f"sqlite+aiosqlite:///{db}")

    async def go():
        async with eng.begin() as conn:
            await conn.run_sync(_apply_light_migrations)
            cols = [r[1] for r in (await conn.execute(text("PRAGMA table_info(hosts)"))).fetchall()]
        return cols
    assert "sudo_password_secret_id" in asyncio.run(go())


async def test_connect_retry_and_message(monkeypatch):
    import asyncio as _a
    from avdlab.hosts import ssh as sshmod
    calls = {"n": 0}

    async def fake_connect(*a, **k):
        calls["n"] += 1
        raise _a.TimeoutError()

    monkeypatch.setattr(sshmod.asyncssh, "connect", fake_connect)
    monkeypatch.setattr(sshmod, "CONNECT_BACKOFF", 0)
    from avdlab.core.db import Host
    host = Host(name="h", address="10.1.1.1", ssh_port=22, ssh_user="u")
    with pytest.raises(sshmod.SSHError) as ei:
        await sshmod._connect_with_retry(host, {}, None)
    assert calls["n"] == sshmod.CONNECT_ATTEMPTS and "TCP connect timed out" in str(ei.value) and "3 attempts" in str(ei.value)


def test_platform_detection_and_recommendations():
    """1.0: WSL2 / OrbStack Linux VMs are recognised (beta lab hosts) and get platform-specific advice."""
    from avdlab.hosts.probe import platform_info, recommendations

    assert platform_info("Linux version 5.15.167-microsoft-standard-WSL2", "5.15.167-microsoft-standard-WSL2") == {
        "platform": "wsl", "platform_label": "WSL2 on Windows", "beta": True, "docker_desktop": False}
    assert platform_info("Linux version 6.8\n/etc/orbstack-release\ndefault\n", "6.8.0-orbstack")["platform"] == "orbstack"
    assert platform_info("Linux version 6.8.0-45-generic\ndefault\n", "6.8.0-45-generic") == {
        "platform": "linux", "platform_label": "Linux", "beta": False, "docker_desktop": False}
    base = {"os": {"id": "ubuntu", "family": "debian", "arch": "x86_64", "platform": "wsl", "docker_desktop": True}, "sudo_nopasswd": True,
            "docker": {"installed": True, "user_can_run": True, "user_in_group": True}, "containerlab": {"installed": True, "version": "0.79.0"},
            "sysctl": {}, "remote_root": {"exists": True}, "python3": {"installed": True}, "rsync": True, "pipx": True, "images": [],
            "mem": {"total_gb": 32}, "disk": {"docker_free_kb": 100_000_000, "root_free_kb": 100_000_000}}
    rec = recommendations(base)
    assert any("WSL2 host (beta)" in r and "mirrored" in r for r in rec) and any("Docker Desktop" in r for r in rec)
    base["os"].update(platform="orbstack", docker_desktop=False, arch="aarch64")
    rec = recommendations(base)
    assert any("OrbStack VM (beta)" in r for r in rec) and any("cEOSarm" in r for r in rec)


async def test_bootstrap_on_macos_gives_orbstack_instructions(client, monkeypatch):
    """1.0.1: SSH-ing into the Mac itself is not a lab host; bootstrap says how to set up the OrbStack VM + jump host."""
    from avdlab.hosts import recipes
    from avdlab.hosts.ssh import parse_jump

    class Mac:
        async def run(self, cmd, **kw):
            class R:
                def __init__(self, ok, out):
                    self.ok, self.stdout, self.stderr, self.returncode = ok, out, "" if ok else out, 0 if ok else 1
            if "os-release" in cmd:
                return R(False, "cat: /etc/os-release: No such file or directory")
            if "uname -s" in cmd:
                return R(True, "Darwin\n")
            return R(True, "")

    import pytest as _pt
    with _pt.raises(RuntimeError) as exc:
        await recipes.detect_family(Mac())
    assert "OrbStack" in str(exc.value) and "orb create ubuntu" in str(exc.value) and "Jump host" in str(exc.value)
    assert parse_jump("dietrichd@100.67.127.55", "x") == ("dietrichd", "100.67.127.55", 22)


async def test_host_jump_host_roundtrip(client):
    r = await client.post("/api/hosts", json={"name": "vm-behind-mac", "address": "avdlab-vm.orb.local", "ssh_user": "dietrich",
                                              "jump_host": "dietrich@mac-studio.local"})
    assert r.status_code == 201, r.text
    assert r.json()["jump_host"] == "dietrich@mac-studio.local"
    r = await client.patch(f"/api/hosts/{r.json()['id']}", json={"jump_host": None})
    assert r.status_code == 200 and r.json()["jump_host"] is None


def test_platform_mismatch_messages():
    from avdlab.hosts.probe import platform_mismatch

    assert platform_mismatch("linux", "linux") is None and platform_mismatch("mac", "orbstack") is None and platform_mismatch("windows", "wsl") is None
    assert platform_mismatch(None, None) is None
    assert "Mac itself" in platform_mismatch("mac", "macos")
    assert "Windows itself" in platform_mismatch("windows", "windows")
    assert "change the platform" in platform_mismatch("linux", "wsl")

