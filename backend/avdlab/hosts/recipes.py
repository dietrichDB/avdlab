"""Idempotent shell recipes (Step/Recipe engine) + the `clab-host` recipe; jobs `host.bootstrap` and `host.verify`.

Engine semantics per step: run `check` (rc 0 → `[skip]`), otherwise run `apply` then `check` again
(`[ok]` when it now passes, `[FAIL]` otherwise, which aborts the recipe). Steps with a `when` predicate that
returns False for the given options are `[skip]`ped without running anything.
"""
from __future__ import annotations

import shlex
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from ..core.config import settings
from ..jobs.engine import JobContext, job
from .probe import os_family, probe_host, _os_release
from .ssh import CommandRunner, LogFn, SSHClient

OSFamily = Literal["debian", "rhel"]
StepStatus = Literal["skip", "ok", "FAIL"]


@dataclass
class Step:
    name: str
    check: str
    apply: str
    sudo: bool = True
    when: Callable[[dict[str, Any]], bool] | None = None
    timeout: float = 900


@dataclass
class Recipe:
    name: str
    steps: list[Step] = field(default_factory=list)


@dataclass
class StepResult:
    name: str
    status: StepStatus
    detail: str = ""


class RecipeError(RuntimeError):
    def __init__(self, result: StepResult, results: list[StepResult]) -> None:
        self.result = result
        self.results = results
        super().__init__(f"step '{result.name}' failed: {result.detail}")


def _tail(text: str, n: int = 3) -> str:
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    return " | ".join(lines[-n:])


async def run_recipe(recipe: Recipe, ssh: CommandRunner, *, options: dict[str, Any] | None = None, log: LogFn | None = None,
                     check_cancelled: Callable[[], None] | None = None,
                     stop_on_fail: bool = True) -> list[StepResult]:
    options = options or {}
    results: list[StepResult] = []
    emit: LogFn = log or (lambda _line: None)
    emit(f"=== recipe {recipe.name}: {len(recipe.steps)} steps")
    for step in recipe.steps:
        if check_cancelled:
            check_cancelled()
        if step.when is not None and not step.when(options):
            res = StepResult(step.name, "skip", "not selected")
            emit(f"[skip] {step.name} (not selected)")
            results.append(res)
            continue
        pre = await ssh.run(step.check, sudo=step.sudo, timeout=step.timeout)
        if pre.ok:
            res = StepResult(step.name, "skip", "already satisfied")
            emit(f"[skip] {step.name} (already satisfied)")
            results.append(res)
            continue
        emit(f"[apply] {step.name}")
        applied = await ssh.run(step.apply, sudo=step.sudo, timeout=step.timeout, log=log)
        post = await ssh.run(step.check, sudo=step.sudo, timeout=step.timeout)
        if post.ok:
            res = StepResult(step.name, "ok", "applied")
            emit(f"[ok] {step.name}")
        else:
            detail = f"apply rc={applied.returncode}, re-check rc={post.returncode}: {_tail(applied.stderr or applied.stdout or post.stderr)}"
            res = StepResult(step.name, "FAIL", detail)
            emit(f"[FAIL] {step.name}: {detail}")
        results.append(res)
        if res.status == "FAIL" and stop_on_fail:
            raise RecipeError(res, results)
    return results


# ------------------------------------------------------------------ clab-host recipe
BASE_PACKAGES = ["curl", "ca-certificates", "rsync", "git", "python3", "jq"]
SYSCTL_CONF = "/etc/sysctl.d/90-avdlab.conf"
SYSCTL_VALUES: dict[str, int] = {
    "fs.inotify.max_user_instances": 64000,
    "fs.inotify.max_user_watches": 1048576,
    "net.ipv4.ip_forward": 1,
    "vm.max_map_count": 262144,
    "fs.file-max": 2097152,
}
CLAB_WRAPPER = "/usr/local/bin/containerlab"


def containerlab_wrapper_script(clab_image: str) -> str:
    """Wrapper mirroring containerlab's documented container invocation (docs: install → container)."""
    return f"""#!/usr/bin/env bash
# avdlab-wrapper: runs containerlab from the {clab_image} image
set -euo pipefail
tty_flag=""
if [ -t 0 ] && [ -t 1 ]; then tty_flag="-it"; fi
exec docker run --rm $tty_flag --privileged \\
  --network host --pid host \\
  -v /var/run/docker.sock:/var/run/docker.sock \\
  -v /var/run/netns:/var/run/netns \\
  -v /etc/hosts:/etc/hosts \\
  -v /var/lib/docker/containers:/var/lib/docker/containers \\
  -v "$PWD":"$PWD" -w "$PWD" \\
  {clab_image} containerlab "$@"
"""


def _sysctl_check() -> str:
    pairs = " ".join(f"{k}={v}" for k, v in SYSCTL_VALUES.items())
    return (f'for kv in {pairs}; do k="${{kv%=*}}"; v="${{kv#*=}}"; cur="$(sysctl -n "$k" 2>/dev/null || echo 0)"; '
            f'[ "$cur" -ge "$v" ] || exit 1; done; test -f {SYSCTL_CONF}')


def _sysctl_apply() -> str:
    body = "\\n".join(f"{k}={v}" for k, v in SYSCTL_VALUES.items())
    return f"printf '{body}\\n' > {SYSCTL_CONF} && sysctl --system >/dev/null"


def clab_host_recipe(family: OSFamily, ssh_user: str, options: dict[str, Any] | None = None) -> Recipe:
    """Build the `clab-host` recipe for the given OS family.

    options: native_toolchain (bool, default False), clab_mode ("native" | "container", default "native").
    """
    options = dict(options or {})
    clab_mode = options.get("clab_mode", "native")
    if clab_mode not in ("native", "container"):
        raise ValueError(f"clab_mode must be native|container, got {clab_mode!r}")
    user = shlex.quote(ssh_user)
    pkgs = " ".join(BASE_PACKAGES)
    root = shlex.quote(settings.remote_root)
    agent_image = shlex.quote(settings.agent_image)
    clab_image = shlex.quote(settings.clab_image)
    avd = settings.avd_version
    anta = settings.anta_version
    user_path = 'export PATH="$HOME/.local/bin:$PATH"; '

    if family == "debian":
        pkg_check = f"dpkg -s {pkgs} >/dev/null 2>&1"
        pkg_apply = f"export DEBIAN_FRONTEND=noninteractive; apt-get update -qq && apt-get install -y -qq {pkgs}"
        pipx_apply = "export DEBIAN_FRONTEND=noninteractive; apt-get install -y -qq pipx || python3 -m pip install --break-system-packages pipx"
    elif family == "rhel":
        pkg_check = f"rpm -q {pkgs} >/dev/null 2>&1"
        pkg_apply = f"dnf install -y -q {pkgs}"
        pipx_apply = "dnf install -y -q pipx || (dnf install -y -q python3-pip && python3 -m pip install pipx)"
    else:  # pragma: no cover - guarded by typing
        raise ValueError(f"unsupported OS family {family!r}")

    native_tc = lambda o: bool(o.get("native_toolchain"))  # noqa: E731
    is_native = lambda o: o.get("clab_mode", "native") == "native"  # noqa: E731
    is_container = lambda o: o.get("clab_mode", "native") == "container"  # noqa: E731

    steps = [
        Step("base packages", pkg_check, pkg_apply),
        Step("docker engine", "docker info >/dev/null 2>&1",
             "curl -fsSL https://get.docker.com | sh && systemctl enable --now docker"),
        Step("docker group membership", f"id -nG {user} | tr ' ' '\\n' | grep -qx docker", f"usermod -aG docker {user}"),
        Step("containerlab (native)", "command -v containerlab >/dev/null 2>&1 && containerlab version >/dev/null 2>&1",
             'curl -sL https://containerlab.dev/setup | bash -s "all"', when=is_native),
        Step("containerlab image (container mode)", f"docker image inspect {clab_image} >/dev/null 2>&1",
             f"docker pull {clab_image}", when=is_container),
        Step("containerlab wrapper (container mode)",
             f"test -x {CLAB_WRAPPER} && grep -q avdlab-wrapper {CLAB_WRAPPER}",
             f"cat > {CLAB_WRAPPER} <<'AVDLAB_EOF'\n{containerlab_wrapper_script(settings.clab_image)}AVDLAB_EOF\nchmod 0755 {CLAB_WRAPPER}",
             when=is_container),
        Step("sysctls", _sysctl_check(), _sysctl_apply()),
        Step("remote root", f'test -d {root} && [ "$(stat -c %U {root})" = {user} ]',
             f"mkdir -p {root} && chown {user}:{user} {root}"),
        Step("agent image", f"docker image inspect {agent_image} >/dev/null 2>&1", f"docker pull {agent_image}", timeout=1800),
        Step("pipx", "command -v pipx >/dev/null 2>&1 || test -x /usr/bin/pipx", pipx_apply, when=native_tc),
        Step("pyavd (native toolchain)",
             user_path + f"pipx list --short 2>/dev/null | grep -q '^pyavd {avd}'",
             user_path + f'pipx install --include-deps "pyavd[ansible]=={avd}" && pipx ensurepath',
             sudo=False, when=native_tc, timeout=1800),
        Step("anta (native toolchain)",
             user_path + f"pipx list --short 2>/dev/null | grep -q '^anta {anta}'",
             user_path + f'pipx install "anta[cli]=={anta}"', sudo=False, when=native_tc, timeout=1800),
        Step("arista.avd collection (native toolchain)",
             user_path + f"ansible-galaxy collection list arista.avd 2>/dev/null | grep -qE 'arista\\.avd +{avd}'",
             user_path + f"ansible-galaxy collection install arista.avd:=={avd}", sudo=False, when=native_tc, timeout=1800),
    ]
    return Recipe("clab-host", steps)


ACT_TOOLS_STEPS = ("base packages", "docker engine", "docker group membership", "remote root", "agent image")


def act_tools_recipe(family: OSFamily, ssh_user: str, options: dict[str, Any] | None = None) -> Recipe:
    """Phase 4 (backend D): ACT tools server — Docker + the agent image only (no containerlab, no sysctls).
    The steps are the `clab-host` ones of the same name."""
    base = clab_host_recipe(family, ssh_user, {})
    return Recipe("act-tools", [st for st in base.steps if st.name in ACT_TOOLS_STEPS])


RECIPES: dict[str, Callable[[OSFamily, str, dict[str, Any] | None], Recipe]] = {"clab-host": clab_host_recipe,
                                                                                 "act-tools": act_tools_recipe}


MACOS_HINT = ("this host is macOS itself. containerlab needs a Linux kernel, so the lab host must be a Linux VM on the Mac "
              "(beta): install OrbStack (orbstack.dev), run `orb create ubuntu avdlab-vm`, then `orb -m avdlab-vm sudo apt-get install -y "
              "openssh-server` and `orb -m avdlab-vm sudo systemctl enable --now ssh`. In avdlab, edit this host: Address = avdlab-vm.orb.local, "
              "SSH user = your Mac user (OrbStack creates it in the VM with the same name and your SSH key), Jump host = user@<this Mac>. "
              "Then Probe and Bootstrap again — on Apple Silicon use cEOSarm images")
WINDOWS_HINT = ("this host is Windows itself (OpenSSH for Windows). The lab host must be a WSL2 distro (beta): install the Containerlab "
                "WSL distribution (containerlab.dev/windows), enable sshd inside it, and either set WSL networkingMode=mirrored with a firewall "
                "rule for port 22, or use this Windows machine as the Jump host and Address = localhost:<sshd port of the distro>")


async def detect_family(ssh: CommandRunner) -> OSFamily:
    cp = await ssh.run("cat /etc/os-release", timeout=30)
    fam = os_family(_os_release(cp.stdout)) if cp.ok else None
    if fam is None:
        un = await ssh.run("uname -s 2>/dev/null; ver 2>/dev/null; echo %OS% 2>/dev/null", timeout=30)
        probe = ((un.stdout or "") + (un.stderr or "")).lower()
        if "darwin" in probe:
            raise RuntimeError(MACOS_HINT)
        if "windows" in probe or "microsoft windows" in probe or "windows_nt" in probe:
            raise RuntimeError(WINDOWS_HINT)
        raise RuntimeError(f"unsupported or undetectable OS (os-release: {_tail(cp.stdout or cp.stderr) or 'n/a'})")
    return fam  # type: ignore[return-value]


# ------------------------------------------------------------------ jobs
@job("host.bootstrap")
async def bootstrap_job(ctx: JobContext) -> dict[str, Any]:
    host = await ctx.get_host()
    recipe_name = ctx.params.get("recipe", "clab-host")
    options = dict(ctx.params.get("options") or {})
    builder = RECIPES.get(recipe_name)
    if builder is None:
        raise RuntimeError(f"unknown recipe {recipe_name!r}; known: {sorted(RECIPES)}")
    ctx.log(f"bootstrap {host.name} ({host.ssh_user}@{host.address}) recipe={recipe_name} options={options}")
    async with ctx.session() as session:
        async with SSHClient.connect(host, session, log=ctx.log, ctx=ctx) as ssh:
            family = await detect_family(ssh)
            ctx.log(f"os family: {family}")
            recipe = builder(family, host.ssh_user, options)
            results = await run_recipe(recipe, ssh, options=options, log=ctx.log, check_cancelled=ctx.check_cancelled)
    ctx.log("re-probing host")
    caps = await probe_host(ctx, host)
    return {
        "recipe": recipe_name,
        "family": family,
        "steps": [r.__dict__ for r in results],
        "applied": [r.name for r in results if r.status == "ok"],
        "recommendations": caps.get("recommendations", []),
    }


@job("host.verify")
async def verify_job(ctx: JobContext) -> dict[str, Any]:
    host = await ctx.get_host()
    checks: list[dict[str, Any]] = []
    cmds = {} if host.kind.value == "act" else {"containerlab": "containerlab version"}  # phase 4 (D): ACT tools server
    cmds |= {
        "agent image": f"docker run --rm {shlex.quote(settings.agent_image)} python3 -c 'import pyavd, anta; print(pyavd.__version__, anta.__version__)'",
    }
    async with ctx.session() as session:
        async with SSHClient.connect(host, session, log=ctx.log, ctx=ctx) as ssh:
            for name, cmd in cmds.items():
                ctx.check_cancelled()
                cp = await ssh.run(cmd, timeout=600, log=ctx.log)
                ctx.log(f"[{'ok' if cp.ok else 'FAIL'}] verify {name}")
                checks.append({"name": name, "ok": cp.ok, "output": _tail(cp.stdout or cp.stderr, 5)})
    caps = await probe_host(ctx, host)
    ok = all(c["ok"] for c in checks)
    return {"ok": ok, "checks": checks, "recommendations": caps.get("recommendations", [])}

