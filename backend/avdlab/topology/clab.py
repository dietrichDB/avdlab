"""AVD build outputs → containerlab topology (port of notes/avd2clab.py).

Inputs (from `lab.build`):
  documentation/fabric/<FABRIC>-topology.csv   every physical link, listed from both sides
  intended/structured_configs/<host>.yml       mgmt interface/IP, metadata (serial_number)
  intended/configs/<host>.cfg                  startup-config

Outputs under <lab>/clab/:
  topology.clab.yml, <host>.EosIntfMapping.json, ceos-config/<host> (SERIALNUMBER=...), hosts/<endpoint>.sh,
  clients.json (Module 5 client plans, see topology/clients.py)
"""
from __future__ import annotations

import csv
import ipaddress
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from ..core.config import settings
from ..core.db import Lab
from .models import ClabKind, ClabLink, ClabMgmt, ClabNode, ClabTopology, ClabTopologySection, GraphLink, GraphNode, TopologyGraph

ETH_RE = re.compile(r"^Ethernet(\d+)(?:/(\d+))?$")
DEFAULT_HOST_IMAGE = "ghcr.io/srl-labs/network-multitool:latest"
Log = Callable[[str], None]


def clab_port(intf: str) -> str:
    """Ethernet3 -> eth3 ; Ethernet1/1 -> eth1_1 (containerlab convention)."""
    m = ETH_RE.match(intf)
    if not m:
        raise ValueError(f"unsupported switch interface for containerlab: {intf}")
    return f"eth{m.group(1)}" + (f"_{m.group(2)}" if m.group(2) else "")


def _intf_sort_key(name: str) -> list[int]:
    return [int(n) for n in re.findall(r"\d+", name)]


class TopologyBuild:
    """Result of `build_clab_topology`: the model plus the side files to write."""

    def __init__(self, topology: ClabTopology, mappings: dict[str, dict[str, Any]], ceos_configs: dict[str, str],
                 host_scripts: dict[str, str], graph: TopologyGraph, clients: list[Any] | None = None,
                 ztp: Any = None) -> None:
        self.topology = topology
        self.ztp = ztp  # phase 4 (A): topology.ztp.ZtpPlan in provisioning mode ztp
        self.clients = clients or []
        self.mappings = mappings
        self.ceos_configs = ceos_configs
        self.host_scripts = host_scripts
        self.graph = graph

    def write(self, out: Path) -> Path:
        out.mkdir(parents=True, exist_ok=True)
        (out / "hosts").mkdir(exist_ok=True)
        (out / "ceos-config").mkdir(exist_ok=True)
        for name, mapping in self.mappings.items():
            (out / f"{name}.EosIntfMapping.json").write_text(json.dumps(mapping, indent=2) + "\n")
        for name, content in self.ceos_configs.items():
            (out / "ceos-config" / name).write_text(content)
        for name, script in self.host_scripts.items():
            p = out / "hosts" / f"{name}.sh"
            p.write_text(script)
            p.chmod(0o755)
        from .clients import write_plans

        write_plans(out, self.clients)
        from . import ztp as ztp_mod

        if self.ztp is not None:
            self.ztp.write(out, out.parent)
        else:
            ztp_mod.remove_ztp_dir(out)
        topo_file = out / "topology.clab.yml"
        topo_file.write_text("---\n" + yaml.safe_dump(self.topology.to_yaml_dict(), sort_keys=False, default_flow_style=None))
        (out / "graph.json").write_text(self.graph.model_dump_json(indent=2) + "\n")
        return topo_file


def _parent_intf(intf: str | None) -> str:
    """Ethernet3.100 / "Ethernet1 VLAN 42" -> Ethernet3 / Ethernet1: routed sub-interfaces and their trunk peer
    (e.g. cv-pathfinder `uplink_type: p2p-vrfs` or `lan`) share one cable."""
    return re.sub(r"^(Ethernet[\d/]+)(?:\.\d+| VLAN \d+)$", r"\1", (intf or "").strip())


def read_links(csv_path: Path) -> list[tuple[str, str, str, str, str]]:
    """De-duplicated (node, intf, peer, peer_intf, peer_type) from the AVD topology CSV, in file order."""
    rows = list(csv.DictReader(csv_path.open()))
    links: list[tuple[str, str, str, str, str]] = []
    seen: set[frozenset[tuple[str, str]]] = set()
    for r in rows:
        a, ai, b, bi = r["Node"], _parent_intf(r["Node Interface"]), r["Peer Node"], _parent_intf(r["Peer Interface"])
        if not b or not bi:
            continue
        key = frozenset({(a, ai), (b, bi)})
        if key in seen:
            continue
        seen.add(key)
        links.append((a, ai, b, bi, r.get("Peer Type") or ""))
    return links


def read_node_types(csv_path: Path) -> dict[str, str]:
    """Node -> node type. A node's own rows win; `Peer Type` (which can be e.g. `mlag_peer`) only fills gaps
    such as endpoints that never appear in the `Node` column."""
    rows = list(csv.DictReader(csv_path.open()))
    types: dict[str, str] = {}
    for r in rows:
        types.setdefault(r["Node"], r["Node Type"])
    for r in rows:
        if r.get("Peer Node") and r.get("Peer Type"):
            types.setdefault(r["Peer Node"], r["Peer Type"])
    return types


def build_clab_topology(
    lab_dir: Path,
    fabric_name: str,
    image: str,
    *,
    serials: dict[str, str] | None = None,
    mgmt_subnet: str | None = None,
    lab_name: str | None = None,
    host_image: str = DEFAULT_HOST_IMAGE,
    lab_id: str = "",
    provisioning: dict[str, Any] | None = None,
    mgmt_gateway: str | None = None,
    dns_servers: list[str] | None = None,
    token_file: str | None = None,
) -> TopologyBuild:
    lab_dir = Path(lab_dir)
    serials = dict(serials or {})
    csv_path = lab_dir / "documentation" / "fabric" / f"{fabric_name}-topology.csv"
    if not csv_path.exists():
        raise FileNotFoundError(f"{csv_path} missing — build the lab first (eos_designs_documentation.topology_csv)")

    # --- switches: everything that has a structured config
    switches: dict[str, dict[str, Any]] = {}
    for sc_file in sorted((lab_dir / "intended" / "structured_configs").glob("*.y*ml")):
        sc = yaml.safe_load(sc_file.read_text()) or {}
        mgmt = (sc.get("management_interfaces") or [{}])[0]
        meta = sc.get("metadata") or {}
        v6 = mgmt.get("ipv6_address") or next(iter(mgmt.get("ipv6_addresses") or []), None)  # phase 6: ipv6_mgmt_ip
        switches[sc_file.stem] = {
            "mgmt_ipv6": str(v6).split("/")[0] if v6 else None,
            "mgmt_ipv6_cidr": str(v6) if v6 else None,
            "mgmt_intf": mgmt.get("name", "Management1"),
            "mgmt_ip": (mgmt.get("ip_address") or "").split("/")[0] or None,
            "mgmt_cidr": mgmt.get("ip_address"),
            "serial": serials.get(sc_file.stem) or meta.get("serial_number"),
            "system_mac": sc.get("system_mac_address") or meta.get("system_mac_address"),
        }
        if switches[sc_file.stem]["serial"]:
            serials[sc_file.stem] = switches[sc_file.stem]["serial"]

    links = read_links(csv_path)
    node_types = read_node_types(csv_path)
    sw_ifaces: dict[str, set[str]] = {s: set() for s in switches}
    host_ifaces: dict[str, list[str]] = {}
    host_links: dict[str, list[dict[str, str]]] = {}  # endpoint -> [{eth, switch, switch_port, endpoint_port}]

    def endpoint(node: str, intf: str) -> str:
        if node in switches:
            sw_ifaces[node].add(intf)
            return f"{node}:{clab_port(intf)}"
        ports = host_ifaces.setdefault(node, [])
        if intf not in ports:
            ports.append(intf)
        return f"{node}:eth{ports.index(intf) + 1}"

    clab_links: list[ClabLink] = []
    graph_links: list[GraphLink] = []
    for a, ai, b, bi, ptype in links:
        ea, eb = endpoint(a, ai), endpoint(b, bi)
        clab_links.append(ClabLink(endpoints=[ea, eb]))
        for (h, hi, eh), (sw, swi) in (((a, ai, ea), (b, bi)), ((b, bi, eb), (a, ai))):
            if h not in switches and sw in switches:
                host_links.setdefault(h, []).append({"eth": eh.split(":")[1], "switch": sw, "switch_port": swi,
                                                     "endpoint_port": hi})
        graph_links.append(GraphLink(a=a, a_if=ai, b=b, b_if=bi, a_port=ea.split(":")[1], b_port=eb.split(":")[1], type=ptype))

    # --- mgmt subnet
    if not mgmt_subnet:
        first = next((s["mgmt_cidr"] for s in switches.values() if s["mgmt_cidr"]), None)
        if first:
            mgmt_subnet = str(ipaddress.ip_interface(first).network)

    mgmt_ipv6_subnet = next((str(ipaddress.ip_interface(s["mgmt_ipv6_cidr"]).network) for s in switches.values()
                             if s["mgmt_ipv6_cidr"]), None)

    # --- phase 4 (A): provisioning mode ztp → no startup-config, a dnsmasq/http `ztp` node
    from . import ztp as ztp_mod

    prov = ztp_mod.normalize_provisioning(provisioning)
    ztp_on = prov["mode"] == "ztp"

    # --- nodes
    nodes: dict[str, ClabNode] = {}
    mappings: dict[str, dict[str, Any]] = {}
    ceos_configs: dict[str, str] = {}
    graph_nodes: list[GraphNode] = []
    for name, s in switches.items():
        mappings[name] = {
            "ManagementIntf": {"eth0": s["mgmt_intf"]},
            "EthernetIntf": {clab_port(i): i for i in sorted(sw_ifaces[name], key=_intf_sort_key)},
        }
        binds = [f"{name}.EosIntfMapping.json:/mnt/flash/EosIntfMapping.json:ro"]
        if s["serial"]:
            lines = [f"SERIALNUMBER={s['serial']}"]
            if s["system_mac"]:
                lines.append(f"SYSTEMMACADDR={s['system_mac']}")
            ceos_configs[name] = "\n".join(lines) + "\n"
            binds.append(f"ceos-config/{name}:/mnt/flash/ceos-config:ro")
        nodes[name] = ClabNode(kind="arista_ceos", startup_config=None if ztp_on else f"../intended/configs/{name}.cfg",
                               binds=binds, mgmt_ipv4=s["mgmt_ip"], mgmt_ipv6=s["mgmt_ipv6"], labels={"avdlab.role": node_types.get(name, "switch")},
                               suppress_startup_config=True if ztp_on else None)
        graph_nodes.append(GraphNode(name=name, kind="arista_ceos", role=node_types.get(name), mgmt_ip=s["mgmt_ip"],
                                     mgmt_ipv6=s["mgmt_ipv6"], serial=s["serial"], image=image))

    from .clients import load_hostvars_safe, plan_clients, setup_script

    for name in host_ifaces:
        host_links.setdefault(name, [])
    clients = plan_clients({n: host_links[n] for n in host_ifaces}, load_hostvars_safe(lab_dir), host_image)
    host_scripts: dict[str, str] = {}
    for plan in clients:
        name = plan["name"]
        host_scripts[name] = setup_script(plan)
        nodes[name] = ClabNode(kind="linux", image=host_image, binds=[f"hosts/{name}.sh:/root/setup.sh:ro"],
                               exec=["sh /root/setup.sh"], labels={"avdlab.role": "endpoint"})
        graph_nodes.append(GraphNode(name=name, kind="linux", role="server", image=host_image))

    ztp_plan = None
    if ztp_on:
        if not mgmt_subnet:
            raise ValueError("provisioning mode ztp needs a management subnet (knob mgmt_subnet or switch mgmt IPs)")
        net = ipaddress.IPv4Network(mgmt_subnet, strict=False)
        gateway = mgmt_gateway or str(next(net.hosts()))
        server_ip = ztp_mod.pick_server_ip(mgmt_subnet, {s["mgmt_ip"] for s in switches.values() if s["mgmt_ip"]},
                                           gateway, prov["ztp_server_ip"])
        bootfile = "{}.cfg" if prov["ztp_source"] == "intended" else "bootstrap-{}.sh"
        ztp_plan = ztp_mod.ZtpPlan(
            source=prov["ztp_source"], server_ip=server_ip, subnet=str(net), gateway=gateway, dns=list(dns_servers or []),
            nodes={n: {"ip": s["mgmt_ip"], "bootfile": bootfile.format(n)} for n, s in switches.items() if s["mgmt_ip"]},
            token_file=token_file)
        nodes[ztp_mod.ZTP_NODE] = ClabNode(
            kind="linux", image=settings.agent_image, mgmt_ipv4=server_ip, binds=[f"{ztp_mod.ZTP_DIR}/:/ztp"],
            exec=["sh /ztp/run.sh"], labels={"avdlab.role": "ztp"}, user="0", cap_add=["NET_ADMIN", "NET_RAW"])
        graph_nodes.append(GraphNode(name=ztp_mod.ZTP_NODE, kind="linux", role="ztp", mgmt_ip=server_ip,
                                     image=settings.agent_image))

    topo_name = (lab_name or fabric_name).lower()
    topology = ClabTopology(
        name=topo_name,
        prefix="",
        mgmt=ClabMgmt(network=f"clab-mgmt-{topo_name}", ipv4_subnet=mgmt_subnet, ipv6_subnet=mgmt_ipv6_subnet),
        topology=ClabTopologySection(
            kinds={"arista_ceos": ClabKind(image=image, enforce_startup_config=None if ztp_on else True)},
            nodes=nodes,
            links=clab_links,
        ),
    )
    graph = TopologyGraph(lab_id=lab_id, name=topo_name, mgmt_subnet=mgmt_subnet, mgmt_ipv6_subnet=mgmt_ipv6_subnet,
                          nodes=graph_nodes, links=graph_links)
    return TopologyBuild(topology, mappings, ceos_configs, host_scripts, graph, clients, ztp=ztp_plan)


# ------------------------------------------------------------------------------------------- lab helpers
def lab_fabric_name(lab: Lab, lab_dir: Path) -> str:
    """fabric_name used for the documentation file names: last build summary, then lab settings, then the CSV on disk."""
    summary = lab_dir / "intended" / "build-summary.json"
    if summary.exists():
        try:
            name = json.loads(summary.read_text()).get("fabric_name")
            if name:
                return str(name)
        except ValueError:
            pass
    if (lab.settings or {}).get("fabric_name"):
        return str(lab.settings["fabric_name"])
    csvs = sorted((lab_dir / "documentation" / "fabric").glob("*-topology.csv"))
    return csvs[0].name[: -len("-topology.csv")] if csvs else "FABRIC"


def image_for(lab: Lab) -> str:
    version = lab.ceos_version or (lab.settings or {}).get("knobs", {}).get("ceos_version") or "latest"
    from ..images.service import ceos_image_tag  # phase 3 (backend B): knob ceos_arch → `<version>-arm64` tag

    return ceos_image_tag(version, ((lab.settings or {}).get("knobs") or {}).get("ceos_arch") or "x86_64")


def client_image_for(lab: Lab) -> str:
    """Lab knob `client_image`, else settings.host_image."""
    knob = ((lab.settings or {}).get("knobs") or {}).get("client_image")
    return str(knob).strip() if knob and str(knob).strip() else (settings.host_image or DEFAULT_HOST_IMAGE)


def generate_for_lab(lab: Lab, lab_dir: Path, *, log: Log = print) -> TopologyBuild:
    from ..labs.templates import load_group_vars_dir, serials_from_group_vars

    knobs = (lab.settings or {}).get("knobs", {})
    fabric_name = lab_fabric_name(lab, lab_dir)
    group_vars = load_group_vars_dir(lab_dir)
    serials = serials_from_group_vars(group_vars)
    extra: dict[str, Any] = {}
    from .ztp import provisioning_of

    prov = provisioning_of(knobs)
    if prov["mode"] == "ztp":  # phase 4 (A)
        extra = {"provisioning": prov, **ztp_network_facts(knobs, group_vars)}
    build = build_clab_topology(
        lab_dir, fabric_name, image_for(lab), serials=serials, mgmt_subnet=knobs.get("mgmt_subnet"),
        lab_name=lab.slug, host_image=client_image_for(lab), lab_id=lab.id, **extra,
    )
    out = build.write(lab_dir / "clab")
    if build.ztp is not None:
        log(f"provisioning ztp ({build.ztp.source}): ZTP server {build.ztp.server_ip}, clab/ztp/ written "
            f"({len(build.ztp.nodes)} bootfiles)")
    log(f"wrote {out}: {len(build.mappings)} switches, {len(build.host_scripts)} endpoints, "
        f"{len(build.topology.topology.links)} links, "
        f"{sum(1 for c in build.clients for v in c['vlans'] if v['ip'])} client addresses")
    return build


def ztp_network_facts(knobs: dict[str, Any], group_vars: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Gateway / DNS servers / CV token_file for the ZTP DHCP server: knobs first, then the lab's group_vars."""
    from ..labs.templates import cv_token_file

    gateway = knobs.get("mgmt_gateway")
    dns = knobs.get("dns_servers")
    for data in group_vars.values():
        if not isinstance(data, dict):
            continue
        gateway = gateway or data.get("mgmt_gateway")
        if not dns:
            dns = [s.get("ip_address") for s in ((data.get("dns_settings") or {}).get("servers") or [])
                   if isinstance(s, dict) and s.get("ip_address")]
    if isinstance(dns, str):
        dns = [x.strip() for x in dns.split(",") if x.strip()]
    _, token_file = cv_token_file(group_vars)
    return {"mgmt_gateway": str(gateway) if gateway else None, "dns_servers": [str(d) for d in (dns or [])],
            "token_file": token_file}


def load_graph(lab_dir: Path) -> TopologyGraph | None:
    p = lab_dir / "clab" / "graph.json"
    if not p.exists():
        return None
    return TopologyGraph.model_validate_json(p.read_text())
