"""Module 3 — DHCP + ZTP provisioning (knob `provisioning`).

Build side (`plan_ztp` / `ZtpPlan.write`): in `ztp` mode the cEOS nodes get no startup-config (they boot into EOS ZTP,
DHCP on Management0), and a linux node `ztp` (settings.agent_image) runs dnsmasq + `python3 -m http.server` from
`clab/ztp/` bound to `/ztp`:

  clab/ztp/run.sh               start (containerlab exec) / `run.sh restart` (dnsmasq only, after deploy wrote the MACs)
  clab/ztp/dnsmasq.conf         skeleton; deploy appends `dhcp-host=<mac>,<ip>,set:<node>` + option 67 per node
  clab/ztp/www/<node>.cfg       intended config (EOS applies a non-`#!` bootfile as startup-config)
  clab/ztp/www/bootstrap-<node>.sh   ztp_source cloudvision: bash bootfile (see `bootstrap_script`)
  clab/ztp/ztp.json             plan metadata read by the executor and GET /labs/{id}/ztp

Secrets never land in the lab repository: the CV target's ZTP bootstrap script / onboarding token are written by the
deploy job straight into the remote `clab/ztp/www/` (`cv-bootstrap.sh`, `cv-token`, mode 0600).

Runtime side: parsers for the dnsmasq lease file and the http.server access log.
"""
from __future__ import annotations

import ipaddress
import json
import re
import shutil
from pathlib import Path
from typing import Any

PROVISIONING_MODES = ("startup-config", "ztp")
ZTP_SOURCES = ("intended", "cloudvision")
PROVISIONING_DEFAULT: dict[str, Any] = {"mode": "startup-config", "ztp_source": "intended", "ztp_server_ip": None}
ZTP_NODE = "ztp"
ZTP_DIR = "ztp"  # relative to clab/
NODES_MARKER = "# --- per-node leases (written by the deploy job) ---"
CV_BOOTSTRAP_FILE = "cv-bootstrap.sh"
CV_TOKEN_FILE = "cv-token"
PERSISTED_TOKEN = "/mnt/flash/avdlab-cv-token"


def normalize_provisioning(raw: Any) -> dict[str, Any]:
    """Validate/canonicalise the knob: `{mode, ztp_source, ztp_server_ip}` (unknown keys rejected)."""
    if raw is None or raw == "":
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("must be a mapping")
    unknown = sorted(set(raw) - set(PROVISIONING_DEFAULT))
    if unknown:
        raise ValueError(f"unknown keys {unknown}; allowed: {list(PROVISIONING_DEFAULT)}")
    mode = str(raw.get("mode") or "startup-config")
    if mode not in PROVISIONING_MODES:
        raise ValueError(f"mode must be one of {list(PROVISIONING_MODES)}")
    source = str(raw.get("ztp_source") or "intended")
    if source not in ZTP_SOURCES:
        raise ValueError(f"ztp_source must be one of {list(ZTP_SOURCES)}")
    ip = raw.get("ztp_server_ip")
    ip = str(ipaddress.IPv4Address(str(ip).strip())) if ip not in (None, "") else None
    return {"mode": mode, "ztp_source": source, "ztp_server_ip": ip}


def provisioning_of(knobs: dict[str, Any] | None) -> dict[str, Any]:
    try:
        return normalize_provisioning((knobs or {}).get("provisioning"))
    except ValueError:
        return dict(PROVISIONING_DEFAULT)


def pick_server_ip(subnet: str, used: set[str], gateway: str | None, requested: str | None = None) -> str:
    """`requested` (must be in the subnet and free), else the highest free host address of the subnet.

    Deviation from "first free after the gateway": containerlab gives the linux client nodes (no mgmt-ipv4) dynamic
    docker-IPAM addresses from the bottom of the subnet, so a low static pick races them; the top of the range does not."""
    net = ipaddress.IPv4Network(subnet, strict=False)
    taken = set(used) | ({gateway} if gateway else set())
    if requested:
        if ipaddress.IPv4Address(requested) not in net:
            raise ValueError(f"ztp_server_ip {requested} is outside the management subnet {net}")
        if requested in taken:
            raise ValueError(f"ztp_server_ip {requested} is already used (gateway or a switch mgmt IP)")
        return requested
    for host in reversed(list(net.hosts())):
        if str(host) not in taken:
            return str(host)
    raise ValueError(f"no free address left in the management subnet {net} for the ZTP server")


RUN_SH = """#!/bin/sh
# avdlab ZTP server (generated at build; edits are overwritten).
#   run.sh          start dnsmasq (DHCP) + python http.server (bootfiles) in the background
#   run.sh restart  restart dnsmasq only (the deploy job rewrote dnsmasq.conf with the cEOS MACs)
cd /ztp || exit 1
if ! command -v dnsmasq >/dev/null 2>&1; then
  { apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends dnsmasq; } \\
    >>/ztp/install.log 2>&1 || apk add --no-cache dnsmasq >>/ztp/install.log 2>&1
fi
touch /ztp/leases /ztp/http.log /ztp/dnsmasq.log
start_dnsmasq() {
  nohup dnsmasq --conf-file=/ztp/dnsmasq.conf --no-daemon >>/ztp/dnsmasq.log 2>&1 &
  echo $! >/ztp/dnsmasq.pid
}
if [ "${1:-}" = "restart" ]; then
  if [ -f /ztp/dnsmasq.pid ]; then kill "$(cat /ztp/dnsmasq.pid)" 2>/dev/null; fi
  pkill -x dnsmasq 2>/dev/null
  sleep 1
  start_dnsmasq
  echo "dnsmasq restarted"
  exit 0
fi
start_dnsmasq
nohup python3 -u -m http.server 80 --directory /ztp/www >>/ztp/http.log 2>&1 &
echo $! >/ztp/http.pid
exit 0
"""


def dnsmasq_skeleton(subnet: str, gateway: str, dns: list[str]) -> str:
    net = ipaddress.IPv4Network(subnet, strict=False)
    lines = [
        "# avdlab ZTP DHCP (dnsmasq). Skeleton written at build; the deploy job appends the per-node lines below the marker.",
        "port=0",
        "interface=eth0",
        "bind-interfaces",
        f"dhcp-range={net.network_address},static,{net.netmask}",
        f"dhcp-option=3,{gateway}",
        f"dhcp-option=6,{','.join(dns or [gateway])}",
        "dhcp-authoritative",
        "log-dhcp",
        "dhcp-leasefile=/ztp/leases",
        NODES_MARKER,
    ]
    return "\n".join(lines) + "\n"


def dnsmasq_node_lines(server_ip: str, nodes: list[dict[str, Any]]) -> str:
    """nodes: [{name, mac, ip, bootfile}] (nodes without a MAC are skipped)."""
    out: list[str] = []
    for n in nodes:
        if not n.get("mac") or not n.get("ip"):
            continue
        out.append(f"dhcp-host={n['mac']},{n['ip']},set:{n['name']}")
        out.append(f'dhcp-option=tag:{n["name"]},67,"http://{server_ip}/{n["bootfile"]}"')
    return "\n".join(out) + ("\n" if out else "")


def with_token_handler(config: str, token_file: str) -> str:
    """Insert an on-boot event-handler that restores the onboarding token (ZTP reloads the switch after the bootfile,
    and AVD's default token files live under /tmp) before the final `end`."""
    block = ("event-handler avdlab-cv-token\n"
             "   trigger on-boot\n"
             f"   action bash mkdir -p {Path(token_file).parent} && cp {PERSISTED_TOKEN} {token_file}\n"
             "!\n")
    lines = config.rstrip("\n").split("\n")
    if lines and lines[-1].strip() == "end":
        return "\n".join(lines[:-1]) + "\n" + block + "end\n"
    return config.rstrip("\n") + "\n" + block


def bootstrap_script(node: str, server_ip: str, token_file: str | None) -> str:
    """ztp_source cloudvision bootfile (bash, `#!` → EOS executes it):
    1. intended config → /mnt/flash/startup-config (the switch comes back with it after the ZTP reload);
    2. if the deploy job published the CV target's ZTP bootstrap script (`cv-bootstrap.sh`), exec it (CV enrolment);
    3. else, if it published the onboarding token (`cv-token`), write it to the cluster's token_file (+ a flash copy
       restored on boot by the event-handler rendered into the served config)."""
    tf = token_file or "/tmp/cv-onboarding-token"
    return f"""#!/bin/bash
# avdlab ZTP bootstrap for {node} (ztp_source: cloudvision). Generated at build.
ZTP=http://{server_ip}
exec >>/mnt/flash/avdlab-ztp.log 2>&1
echo "avdlab ZTP bootstrap {node} $(date)"
curl -fsS -o /mnt/flash/startup-config "$ZTP/{node}.cfg" || {{ echo "cannot fetch {node}.cfg"; exit 1; }}
if curl -fsS -o /tmp/avdlab-cv-bootstrap "$ZTP/{CV_BOOTSTRAP_FILE}"; then
  echo "running the CloudVision ZTP bootstrap script"
  chmod +x /tmp/avdlab-cv-bootstrap
  exec /tmp/avdlab-cv-bootstrap
fi
if curl -fsS -o {PERSISTED_TOKEN} "$ZTP/{CV_TOKEN_FILE}"; then
  chmod 600 {PERSISTED_TOKEN}
  mkdir -p {Path(tf).parent}
  cp {PERSISTED_TOKEN} {tf}
  echo "onboarding token written to {tf}"
else
  echo "no CloudVision bootstrap script or onboarding token published: intended config only"
fi
exit 0
"""


class ZtpPlan:
    def __init__(self, *, source: str, server_ip: str, subnet: str, gateway: str, dns: list[str],
                 nodes: dict[str, dict[str, Any]], token_file: str | None) -> None:
        self.source = source
        self.server_ip = server_ip
        self.subnet = subnet
        self.gateway = gateway
        self.dns = dns
        self.nodes = nodes  # name -> {ip, bootfile}
        self.token_file = token_file

    def meta(self) -> dict[str, Any]:
        return {"mode": "ztp", "source": self.source, "server_ip": self.server_ip, "subnet": self.subnet,
                "gateway": self.gateway, "dns": self.dns, "token_file": self.token_file, "nodes": self.nodes}

    def write(self, clab_dir: Path, lab_dir: Path) -> None:
        out = clab_dir / ZTP_DIR
        if (out / "www").exists():
            shutil.rmtree(out / "www")
        (out / "www").mkdir(parents=True, exist_ok=True)
        run = out / "run.sh"
        run.write_text(RUN_SH)
        run.chmod(0o755)
        (out / "dnsmasq.conf").write_text(dnsmasq_skeleton(self.subnet, self.gateway, self.dns))
        for name in self.nodes:
            cfg = lab_dir / "intended" / "configs" / f"{name}.cfg"
            text = cfg.read_text() if cfg.exists() else ""
            if self.source == "cloudvision" and self.token_file and not self.token_file.startswith("/mnt/flash/"):
                text = with_token_handler(text, self.token_file)
            (out / "www" / f"{name}.cfg").write_text(text)
            if self.source == "cloudvision":
                p = out / "www" / f"bootstrap-{name}.sh"
                p.write_text(bootstrap_script(name, self.server_ip, self.token_file))
                p.chmod(0o755)
        (out / "ztp.json").write_text(json.dumps(self.meta(), indent=2) + "\n")


def remove_ztp_dir(clab_dir: Path) -> None:
    if (clab_dir / ZTP_DIR).exists():
        shutil.rmtree(clab_dir / ZTP_DIR)


def load_meta(lab_dir: Path) -> dict[str, Any] | None:
    p = lab_dir / "clab" / ZTP_DIR / "ztp.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except ValueError:
        return None


# ------------------------------------------------------------------------------------------------ runtime parsers
_MAC_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")


def parse_macs(text: str) -> dict[str, str]:
    """`docker inspect -f '{{.Name}} {{(index .NetworkSettings.Networks "<net>").MacAddress}}' ...` → {name: mac}."""
    out: dict[str, str] = {}
    for line in (text or "").splitlines():
        parts = line.strip().split()
        if len(parts) == 2 and _MAC_RE.match(parts[1].lower()):
            out[parts[0].lstrip("/")] = parts[1].lower()
    return out


def parse_leases(text: str) -> dict[str, str]:
    """dnsmasq lease file (`<expiry> <mac> <ip> <hostname> <client-id>`) → {mac: ip}."""
    out: dict[str, str] = {}
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) >= 3 and _MAC_RE.match(parts[1].lower()):
            out[parts[1].lower()] = parts[2]
    return out


_ACCESS_RE = re.compile(r'^(\d+\.\d+\.\d+\.\d+) - - \[[^\]]*\] "GET /(\S*) HTTP/[\d.]+" (\d{3})')


def parse_http_log(text: str) -> set[tuple[str, str]]:
    """python http.server access log → {(client ip, path)} of successful (200) GETs."""
    out: set[tuple[str, str]] = set()
    for line in (text or "").splitlines():
        m = _ACCESS_RE.match(line.strip())
        if m and m.group(3) == "200":
            out.add((m.group(1), m.group(2)))
    return out


def node_progress(meta: dict[str, Any], macs: dict[str, str], leases: dict[str, str], fetched: set[tuple[str, str]],
                  eapi: dict[str, bool]) -> list[dict[str, Any]]:
    """Per-node `{name, mac, ip, lease, bootfile_fetched, eapi, bootfile}`. A lease counts when the node's MAC holds
    its planned IP; the bootfile counts when that IP fetched it (or, for cloudvision, the served <node>.cfg)."""
    out: list[dict[str, Any]] = []
    for name, n in sorted((meta.get("nodes") or {}).items()):
        mac = macs.get(name)
        ip = n.get("ip")
        lease = bool(mac and leases.get(mac) == ip)
        got = {p for (c, p) in fetched if c == ip}
        out.append({"name": name, "mac": mac, "ip": ip, "lease": lease,
                    "bootfile_fetched": n.get("bootfile") in got, "eapi": bool(eapi.get(name)),
                    "bootfile": n.get("bootfile")})
    return out
