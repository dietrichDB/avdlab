"""Job `host.probe`: collect host capabilities over SSH and store them on the Host row.

`PROBE_COMMANDS` is a flat map name → shell command; each is run independently and failures are tolerated
(rc != 0 → the raw output is kept and the parsed value is null). `parse_probe()` and `recommendations()` are pure,
so they are unit-tested with canned outputs.
"""
from __future__ import annotations

import re
import shlex
from datetime import datetime, timezone
from typing import Any

from ..core.config import settings
from ..core.db import Host
from ..jobs.engine import JobContext, job
from .ssh import CommandRunner, CompletedProcess, SSHClient

SYSCTLS: dict[str, int] = {
    "fs.inotify.max_user_instances": 64000,
    "fs.inotify.max_user_watches": 1048576,
    "net.ipv4.ip_forward": 1,
    "vm.max_map_count": 262144,
    "fs.file-max": 2097152,
}

MIN_MEM_GB_PER_CEOS = 2.0  # rule of thumb: 2 GB RAM per cEOS-lab node
MIN_DISK_GB = 20


def probe_commands(remote_root: str | None = None) -> dict[str, str]:
    root = shlex.quote(remote_root or settings.remote_root)
    return {
        "os_release": "cat /etc/os-release",
        "kernel": "uname -r",
        "arch": "uname -m",
        "uname_s": "uname -s 2>/dev/null; echo %OS% 2>/dev/null",
        # 1.0: platform hints — WSL2 (Windows) and OrbStack (macOS) Linux VMs are supported as *beta* lab hosts
        "platform": "cat /proc/version 2>/dev/null; ls /etc/orbstack* /opt/orbstack-guest 2>/dev/null; cat /proc/sys/fs/binfmt_misc/WSLInterop 2>/dev/null | head -c 40; "
                    "test -d /run/WSL && echo RUN_WSL; cat /etc/wsl.conf 2>/dev/null; docker context show 2>/dev/null",
        "nproc": "nproc",
        "mem": "grep MemTotal /proc/meminfo",
        "disk_root": "df -Pk / | tail -1",
        "disk_docker": "df -Pk /var/lib/docker 2>/dev/null | tail -1",
        "docker_version": "docker version --format '{{.Server.Version}}' 2>/dev/null",
        "docker_client": "docker version --format '{{.Client.Version}}' 2>/dev/null",
        "docker_user": "docker info >/dev/null 2>&1 && echo yes || echo no",
        "docker_group": "id -nG",
        "containerlab_version": "containerlab version 2>/dev/null | grep -iE '^ *version:' ; true",
        "sysctl": "sysctl -n " + " ".join(SYSCTLS) + " 2>/dev/null",
        "images": "docker images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null | grep -iE 'ceos|arista' ; true",
        "remote_root": f"test -d {root} && echo yes || echo no",
        "python3": "python3 --version 2>&1",
        "rsync": "command -v rsync >/dev/null && echo yes || echo no",
        "sudo": "sudo -n true >/dev/null 2>&1 && echo yes || echo no",
        "pipx": "command -v pipx >/dev/null && echo yes || echo no",
    }


# ------------------------------------------------------------------ parsing helpers
def _os_release(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        if "=" in line and not line.startswith("#"):
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def os_family(os_release: dict[str, str]) -> str | None:
    """'debian' | 'rhel' | None from ID and ID_LIKE."""
    ids = {os_release.get("ID", "").lower(), *os_release.get("ID_LIKE", "").lower().split()}
    if ids & {"debian", "ubuntu"}:
        return "debian"
    if ids & {"rhel", "fedora", "centos", "rocky", "almalinux", "ol"}:
        return "rhel"
    return None


def _int(text: str | None) -> int | None:
    if not text:
        return None
    m = re.search(r"-?\d+", text)
    return int(m.group()) if m else None


def _df_free_kb(text: str) -> int | None:
    parts = text.split()
    # Filesystem 1024-blocks Used Available Capacity Mounted
    return int(parts[3]) if len(parts) >= 4 and parts[3].isdigit() else None


def _first_line(text: str) -> str:
    return text.strip().splitlines()[0].strip() if text.strip() else ""


def parse_probe(outputs: dict[str, CompletedProcess]) -> dict[str, Any]:
    """Turn raw command outputs into the capabilities document stored on the host."""

    def out(name: str) -> str:
        cp = outputs.get(name)
        return cp.stdout if cp and cp.ok else ""

    osr = _os_release(out("os_release"))
    mem_kb = _int(out("mem"))
    sysctl_vals = out("sysctl").split()
    sysctls: dict[str, int | None] = {}
    for key, val in zip(SYSCTLS, sysctl_vals):
        sysctls[key] = _int(val)
    for key in SYSCTLS:
        sysctls.setdefault(key, None)

    clab_line = _first_line(out("containerlab_version"))
    clab_m = re.search(r"(\d+\.\d+\.\d+)", clab_line)
    py_m = re.search(r"(\d+\.\d+\.\d+)", out("python3"))
    docker_server = _first_line(out("docker_version")) or None
    groups = out("docker_group").split()

    caps: dict[str, Any] = {
        "os": {
            "id": osr.get("ID"),
            "name": osr.get("PRETTY_NAME") or osr.get("NAME"),
            "version": osr.get("VERSION_ID"),
            "family": os_family(osr),
            "kernel": _first_line(out("kernel")) or None,
            "arch": _first_line(out("arch")) or None,
            **platform_info(out("uname_s") + "\n" + out("platform"), _first_line(out("kernel"))),
        },
        "cpu": {"nproc": _int(out("nproc"))},
        "mem": {"total_kb": mem_kb, "total_gb": round(mem_kb / 1024 / 1024, 1) if mem_kb else None},
        "disk": {"root_free_kb": _df_free_kb(out("disk_root")), "docker_free_kb": _df_free_kb(out("disk_docker"))},
        "docker": {
            "installed": bool(docker_server or _first_line(out("docker_client"))),
            "server_version": docker_server,
            "client_version": _first_line(out("docker_client")) or None,
            "user_can_run": _first_line(out("docker_user")) == "yes",
            "user_in_group": "docker" in groups,
        },
        "containerlab": {"installed": bool(clab_m), "version": clab_m.group(1) if clab_m else None},
        "sysctl": sysctls,
        "images": [line.strip() for line in out("images").splitlines() if line.strip()],
        "remote_root": {"path": settings.remote_root, "exists": _first_line(out("remote_root")) == "yes"},
        "python3": {"installed": bool(py_m), "version": py_m.group(1) if py_m else None},
        "rsync": _first_line(out("rsync")) == "yes",
        "sudo_nopasswd": _first_line(out("sudo")) == "yes",
        "pipx": _first_line(out("pipx")) == "yes",
        "raw": {k: {"rc": cp.returncode, "stdout": cp.stdout[-2000:], "stderr": cp.stderr[-500:]} for k, cp in outputs.items()},
    }
    caps["recommendations"] = recommendations(caps)
    return caps


def platform_info(text: str, kernel: str | None) -> dict[str, Any]:
    """{platform: linux|wsl|orbstack, platform_label, beta}: a Linux VM on Windows (WSL2) or macOS (OrbStack) is a
    supported lab host, in beta (untested in the field)."""
    t = (text or "").lower()
    k = (kernel or "").lower()
    if t.startswith("darwin") or "\ndarwin" in t:
        return {"platform": "macos", "platform_label": "macOS (not a lab host)", "beta": True, "docker_desktop": False}
    if "windows_nt" in t or t.startswith("windows"):
        return {"platform": "windows", "platform_label": "Windows (not a lab host)", "beta": True, "docker_desktop": False}
    if "microsoft" in t or "microsoft" in k or "wsl" in k or "run_wsl" in t or "wslinterop" in t:
        return {"platform": "wsl", "platform_label": "WSL2 on Windows", "beta": True,
                "docker_desktop": "desktop-linux" in t or "default" not in t and "desktop" in t}
    if "orbstack" in t or "orbstack" in k:
        return {"platform": "orbstack", "platform_label": "OrbStack VM on macOS", "beta": True, "docker_desktop": False}
    return {"platform": "linux", "platform_label": "Linux", "beta": False, "docker_desktop": False}


def platform_mismatch(declared: str | None, detected: str | None) -> str | None:
    """1.0.2: the user picks the platform when adding the host; the probe just double-checks."""
    d = declared or "linux"
    expect = {"linux": {"linux"}, "mac": {"orbstack", "linux"}, "windows": {"wsl"}}.get(d, {"linux"})
    if detected in (None, "") or detected in expect:
        return None
    if detected == "macos":
        return ("this address is the Mac itself, not the Linux VM on it: set Address to the VM (avdlab-vm.orb.local) and Jump host to the Mac — "
                "see the 'Mac (OrbStack VM)' steps in Add host")
    if detected == "windows":
        return ("this address is Windows itself, not the WSL2 distro: enable sshd in the distro and point Address at it (via the Jump host or "
                "WSL mirrored networking) — see the 'Windows (WSL2)' steps in Add host")
    if d == "windows" and detected == "linux":
        return "declared as Windows (WSL2) but the probe sees a plain Linux kernel — fine if it is a VM; otherwise change the platform to Linux"
    if d == "linux" and detected in ("wsl", "orbstack"):
        return f"declared as Linux but this looks like a {detected.upper() if detected == 'wsl' else 'OrbStack'} VM: change the platform so the right hints apply (beta)"
    return None


def recommendations(caps: dict[str, Any]) -> list[str]:
    rec: list[str] = []
    plat = caps["os"].get("platform")
    mismatch = platform_mismatch(caps.get("declared_platform"), plat)
    if mismatch:
        rec.append(mismatch)
    if plat == "macos":
        from .recipes import MACOS_HINT

        rec.append(MACOS_HINT)
        return rec
    if plat == "windows":
        from .recipes import WINDOWS_HINT

        rec.append(WINDOWS_HINT)
        return rec
    if plat == "wsl":
        rec.append("WSL2 host (beta): make sure Docker runs inside this distro (Docker Desktop integration OFF for it), sshd is enabled "
                   "and the orchestrator can reach it — WSL `networkingMode=mirrored` in %UserProfile%\\.wslconfig plus a Windows firewall "
                   "rule for port 22 (or a netsh portproxy). Give it memory in .wslconfig (memory=20GB for an 8-switch fabric)")
        if caps["os"].get("docker_desktop"):
            rec.append("docker context points at Docker Desktop: disable the Docker Desktop WSL integration for this distro and install Docker "
                       "inside it (bootstrap does that)")
    elif plat == "orbstack":
        rec.append("OrbStack VM (beta): give the VM enough memory in OrbStack settings (16 GB+ for an 8-switch fabric); on Apple Silicon use "
                   "cEOSarm images (arch arm64)")
    if caps["os"]["family"] is None:
        rec.append(f"unsupported OS '{caps['os']['id']}' — bootstrap supports Debian/Ubuntu and RHEL-family only")
    if caps["os"]["arch"] and caps["os"]["arch"] not in ("x86_64", "amd64"):
        rec.append(f"arch {caps['os']['arch']}: use cEOSarm images (arista/ceos:<version>-arm64) and set the lab knob "
                   "ceos_arch to arm64")
    if not caps["sudo_nopasswd"] and not caps.get("sudo_with_password"):
        rec.append("sudo needs a password and none is configured — add a sudo password to this host (edit host) or grant NOPASSWD "
                   "(echo \"$USER ALL=(ALL) NOPASSWD:ALL\" | sudo tee /etc/sudoers.d/avdlab); bootstrap will fail otherwise")
    if not caps["docker"]["installed"]:
        rec.append("docker is not installed — run bootstrap")
    elif not caps["docker"]["user_can_run"]:
        rec.append("user cannot run docker — bootstrap adds the user to the docker group (re-login required)")
    if not caps["containerlab"]["installed"]:
        rec.append("containerlab is not installed — run bootstrap")
    for key, wanted in SYSCTLS.items():
        have = caps["sysctl"].get(key)
        if have is None:
            rec.append(f"sysctl {key} unknown — bootstrap sets it to {wanted}")
        elif have < wanted:
            if key.startswith("fs.inotify"):
                rec.append(f"{key}={have} is too low for >10 cEOS nodes (want {wanted})")
            else:
                rec.append(f"{key}={have} is below the recommended {wanted}")
    mem_gb = caps["mem"]["total_gb"]
    if mem_gb:
        nodes = int(mem_gb // MIN_MEM_GB_PER_CEOS)
        rec.append(f"{mem_gb} GB RAM: roughly {nodes} cEOS nodes at {MIN_MEM_GB_PER_CEOS:g} GB each")
    free_kb = caps["disk"]["docker_free_kb"] or caps["disk"]["root_free_kb"]
    if free_kb is not None and free_kb < MIN_DISK_GB * 1024 * 1024:
        rec.append(f"only {free_kb // 1024 // 1024} GB free for docker — cEOS images need ~2 GB each, want >= {MIN_DISK_GB} GB")
    if not caps["images"]:
        rec.append("no cEOS image present — push one from the Images page")
    if not caps["remote_root"]["exists"]:
        rec.append(f"{caps['remote_root']['path']} missing — bootstrap creates it")
    if not caps["rsync"]:
        rec.append("rsync missing (file sync falls back to a tar pipe)")
    return rec


# ------------------------------------------------------------------ job
async def collect(ssh: CommandRunner, *, log: Any = None) -> dict[str, CompletedProcess]:
    outputs: dict[str, CompletedProcess] = {}
    for name, cmd in probe_commands().items():
        try:
            outputs[name] = await ssh.run(cmd, timeout=60)
        except Exception as exc:  # noqa: BLE001 — tolerate per-command failures
            outputs[name] = CompletedProcess(returncode=-1, stderr=str(exc), command=cmd)
        if log:
            cp = outputs[name]
            log(f"[{'ok' if cp.ok else 'FAIL'}] probe {name}: {_first_line(cp.stdout) or _first_line(cp.stderr) or '-'}")
    return outputs


async def probe_host(ctx: JobContext, host: Host) -> dict[str, Any]:
    async with ctx.session() as session:
        async with SSHClient.connect(host, session, log=ctx.log, ctx=ctx) as ssh:
            outputs = await collect(ssh, log=ctx.log)
            # can we become root with the configured sudo password (if any)?
            try:
                cp = await ssh.run("true", sudo=True, timeout=30)
                sudo_ok = cp.ok
            except Exception:  # noqa: BLE001 — SSHError with the sudo hint
                sudo_ok = False
    caps = parse_probe(outputs)
    caps["sudo_with_password"] = sudo_ok and not caps["sudo_nopasswd"]
    caps["sudo_ok"] = sudo_ok
    caps["declared_platform"] = getattr(host, "platform", None) or "linux"
    caps["recommendations"] = recommendations(caps)
    async with ctx.session() as session:
        row = await session.get(Host, host.id)
        if row is not None:
            row.capabilities = caps
            row.last_probe_at = datetime.now(timezone.utc)
            await session.commit()
    for r in caps["recommendations"]:
        ctx.log(f"[hint] {r}")
    return caps


@job("host.probe")
async def probe_job(ctx: JobContext) -> dict[str, Any]:
    host = await ctx.get_host()
    ctx.log(f"probing {host.ssh_user}@{host.address}:{host.ssh_port}")
    caps = await probe_host(ctx, host)
    summary = {k: v for k, v in caps.items() if k != "raw"}
    return summary
