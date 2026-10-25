# avdlab lab hosts beyond Linux — macOS and Windows (research, 2026-10-22)

Question: for v1, can a lab *host* (the machine that runs cEOS containers with containerlab) be a Mac or a
Windows PC, so avdlab is not limited to Linux boxes? Short answer: **yes for both, because in both cases the
containers still run in a Linux VM** — containerlab needs Linux kernel APIs (netlink, veth, netns) and does not
run on Darwin or Windows natively. avdlab's host model (SSH + Docker + containerlab) fits that VM unchanged; the
work is in detection, bootstrap recipes, image architecture and the one Windows networking detail below.
Sources: containerlab.dev/macos, containerlab.dev/windows, containerlab install docs (0.79), Arista cEOS-lab
downloads (cEOSarm), Docker Desktop / OrbStack docs.

## 1. macOS

| Option | What it is | Verdict for avdlab |
|---|---|---|
| **OrbStack Linux VM** (maintainer-recommended) | OrbStack provides Docker *and* lightweight Linux VMs (arm64 on Apple Silicon). Create an Ubuntu/Debian VM, install Docker + containerlab in it with the standard script. | **Best fit.** The VM is a normal Linux host: sshd, Docker, clab. avdlab adds it as a host like `sandbox` (SSH to the VM; OrbStack VMs are reachable as `<vm>.orb.local` / `ssh <vm>@orb`). Bootstrap recipe `clab-host` works as-is. |
| Docker Desktop / Rancher Desktop / Colima | Docker daemon in a hidden Linux VM; no shell host to SSH into. | Workable only with "containerlab in a container" (below) driven from the Mac itself; no SSH target → needs a new *local* host kind. |
| containerlab devcontainer (VS Code, docker-outside-of-docker) | clab + tools in a container using the host's Docker. | Good for a developer's own machine; for avdlab it is the same as the previous row. |

Apple Silicon: the VM is arm64. Use **cEOSarm** (arm64 cEOS-lab, already supported in avdlab: image format
`cEOSarm`, knob `ceos_arch: arm64` → `arista/ceos:<ver>-arm64`). x86 cEOS under Rosetta is listed as "works" but
slow enough to be unreliable; avdlab already refuses an arch mismatch at deploy. Intel Macs run x86 cEOS natively.
vrnetlab/VM-based images need nested virt (M3+, macOS 15+) — irrelevant for cEOS. Memory: give the VM ≥ 16 GB for
an 8-switch fabric (OrbStack lets you cap it).

Networking: the management network lives inside the VM. avdlab talks to the switches *through the host* (docker
exec, eAPI from the agent container on the mgmt network), never from the orchestrator directly, so this changes
nothing. Only "open a switch in your browser/SSH client from the Mac" needs a route or port-forward into the VM
(OrbStack routes to VM IPs automatically; Docker Desktop does not).

## 2. Windows

| Option | What it is | Verdict for avdlab |
|---|---|---|
| **"Containerlab" WSL distribution** (prebuilt by the clab team, WSL ≥ 2.4.4) | `wsl --install --from-file clab.wsl`; first launch sets up shell + SSH keys; Docker + clab inside. | **Best fit.** Add sshd + the networking tweak below, then it is a Linux host to avdlab. |
| Debian/Ubuntu WSL2 + `curl -sL https://containerlab.dev/setup \| sudo -E bash -s "all"` | manual equivalent | Same; this is what the `clab-host` bootstrap recipe does already (apt + docker + clab). |
| Docker Desktop with WSL integration | Docker daemon shared into WSL distros | **Must be off** for the clab distro (clab needs its own dockerd in the distro; docs say verify `sudo docker version` fails before enabling it). |

The one real difference: **reaching WSL over SSH from the orchestrator**. WSL2 has NAT networking by default, so
the distro's IP is private to the Windows host. Two fixes:
1. WSL `networkingMode=mirrored` (`%UserProfile%\.wslconfig`, WSL 2.0.5+): the distro shares the Windows IP; add a
   Windows firewall rule for 22 and run sshd in the distro → avdlab adds `windows-pc:22` as a host.
2. Or classic `netsh interface portproxy add v4tov4 listenport=2222 connectaddress=<wsl ip> connectport=22` (+ firewall
   rule); the WSL IP changes on reboot, so a small scheduled task or the mirrored mode is preferable.
systemd is on by default in current WSL, so Docker and sshd start with the distro; the distro itself must be
started (a logon task `wsl -d Containerlab --exec true`, or `wsl --manage Containerlab --set-sparse`… keep it running).
CPU/RAM: `.wslconfig` `memory=20GB processors=8` for an 8-switch fabric. Nested virtualisation not needed for cEOS.

## 3. What avdlab needs to change (estimate: one release, ~2 days)

1. **Host probe**: detect the platform (`uname -a` → `microsoft` in the kernel string = WSL; `orb`/OrbStack hints
   via `/etc/orbstack*` or hostname) and show it as a badge (`Linux`, `WSL2 on Windows`, `OrbStack VM on macOS`).
   Record arch (already done) and whether Docker is *native in the distro* (WSL: warn when `docker context` points at
   Docker Desktop).
2. **Bootstrap recipe `clab-host`**: already apt-based and works in both VMs; add the WSL specifics as a second recipe
   or flags — install `openssh-server`, enable systemd units, print the `.wslconfig` snippet and the firewall/portproxy
   command for the user to run on the Windows side (avdlab cannot do that part over SSH into WSL). For OrbStack:
   nothing extra.
3. **Images**: on an arm64 host default new labs to `ceos_arch: arm64` and the `cEOSarm` catalogue (already
   supported; only the default/auto-pick is missing). Keep the existing refusal on mismatch.
4. **Docs**: a "Lab host on macOS / Windows" page in Getting started + README (the two tables above, commands).
5. **Optional, bigger**: a `local` host kind for the single-machine case (orchestrator and lab on the same
   Mac/PC with no SSH): mount `/var/run/docker.sock` into the api container and run containerlab as a container
   (`ghcr.io/srl-labs/clab`, `--privileged --pid=host --network host -v /var/run/docker.sock:/var/run/docker.sock`).
   This replaces the SSH layer with a local executor; the job code is already abstracted behind `ssh.run`, so a
   `LocalShell` with the same interface is the natural implementation. Defer to after v1 unless the single-laptop
   demo is a priority.

## 4. Recommendation for v1

Ship the Linux + VM story: "the lab host is a Linux machine *or* a Linux VM on your Mac (OrbStack) or Windows PC
(Containerlab WSL distro)". It needs only items 1–4, no new executor, and covers both platforms with the exact
setup the containerlab maintainers recommend. Keep the `local` host kind on the roadmap for the laptop demo.
