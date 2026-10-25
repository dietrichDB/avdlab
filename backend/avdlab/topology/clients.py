"""Module 5 — client devices: connected endpoints → linux client plans + setup scripts.

Every endpoint that the AVD topology CSV connects to a switch becomes a containerlab `linux` node. Its plan is
derived from the AVD inputs (resolved hostvars of the lab):

* endpoints: every `connected_endpoints_keys` list (default servers, firewalls, routers, load_balancers, storage_arrays,
  cpes, workstations, access_points, phones, printers, cameras, generic_devices, + `custom_connected_endpoints_keys`)
  and the generic `connected_endpoints` list; adapters are merged over their `port_profiles` (parent first);
* adapter with `port_channel` → `bondN` (802.3ad) over the endpoint's ethX for that adapter, else the single ethX;
* `mode: access` → untagged on `vlans`; `mode: trunk` → one 802.1Q sub-interface `<if>.<vid>` per VLAN, native
  VLAN untagged on the parent;
* IP: VLANs with an SVI (`ip_address_virtual` or `ip_virtual_router_addresses`) in the network services get
  `network + 100 + <endpoint index>` (skipping gateway/broadcast), gateway = SVI virtual IP; the first addressed VLAN
  carries the default route. A VLAN is addressed once per client (a second adapter carrying it stays L2-only).

Everything here is pure (no DB, no remote calls) and deterministic, so plans can be snapshot-tested.
"""
from __future__ import annotations

import ipaddress
import json
from pathlib import Path
from typing import Any, TypedDict

CLIENTS_JSON = "clients.json"
DEFAULT_ENDPOINT_KEYS = ("servers", "firewalls", "routers", "load_balancers", "storage_arrays", "cpes", "workstations",
                         "access_points", "phones", "printers", "cameras", "generic_devices")
IP_OFFSET = 100


class ClientLink(TypedDict):
    eth: str
    switch: str
    switch_port: str


class ClientBond(TypedDict):
    name: str
    members: list[str]


class ClientVlan(TypedDict):
    vid: int
    iface: str
    ip: str | None
    gateway: str | None
    default_route: bool
    tenant: str | None
    vrf: str | None


class ClientPlan(TypedDict):
    name: str
    image: str
    links: list[ClientLink]
    bond: ClientBond | None
    bonds: list[ClientBond]
    vlans: list[ClientVlan]


# ------------------------------------------------------------------------------------------------ AVD inputs
def _first_var(hostvars: dict[str, dict[str, Any]], key: str) -> Any:
    for hv in hostvars.values():
        if hv.get(key) is not None:
            return hv[key]
    return None


def endpoint_keys(hostvars: dict[str, dict[str, Any]]) -> list[str]:
    keys = _first_var(hostvars, "connected_endpoints_keys")
    out = [str(k.get("key")) for k in keys if isinstance(k, dict) and k.get("key")] if isinstance(keys, list) else \
        list(DEFAULT_ENDPOINT_KEYS)
    for k in _first_var(hostvars, "custom_connected_endpoints_keys") or []:
        if isinstance(k, dict) and k.get("key") and str(k["key"]) not in out:
            out.append(str(k["key"]))
    out.append("connected_endpoints")
    return out


def _profiles(hostvars: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for hv in hostvars.values():
        for p in hv.get("port_profiles") or []:
            if isinstance(p, dict) and p.get("profile"):
                out.setdefault(str(p["profile"]), p)
    return out


def _apply_profile(adapter: dict[str, Any], profiles: dict[str, dict[str, Any]]) -> dict[str, Any]:
    chain: list[dict[str, Any]] = []
    name, seen = adapter.get("profile"), set()
    while name and name in profiles and name not in seen:
        seen.add(name)
        chain.insert(0, profiles[name])
        name = profiles[name].get("parent_profile")
    merged: dict[str, Any] = {}
    for p in chain:
        merged.update({k: v for k, v in p.items() if k not in ("profile", "parent_profile")})
    merged.update(adapter)
    return merged


def collect_endpoints(hostvars: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """All connected endpoints (deduplicated by name, first host in inventory order wins), adapters profile-merged."""
    profiles = _profiles(hostvars)
    keys = endpoint_keys(hostvars)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for hv in hostvars.values():
        for key in keys:
            for ep in hv.get(key) or []:
                if not isinstance(ep, dict) or not ep.get("name") or str(ep["name"]) in seen:
                    continue
                seen.add(str(ep["name"]))
                adapters = [_apply_profile(a, profiles) for a in ep.get("adapters") or [] if isinstance(a, dict)]
                out.append({**ep, "name": str(ep["name"]), "adapters": adapters, "_key": key})
    return out


class SviInfo(TypedDict):
    network: str | None
    gateway: str | None
    tenant: str
    vrf: str | None


def _svi_subnet(svi: dict[str, Any]) -> tuple[str | None, str | None]:
    """(network, gateway) of an SVI: ip_address_virtual, else ip_virtual_router_addresses (+ ip_address for the mask)."""
    if svi.get("ip_address_virtual"):
        itf = ipaddress.ip_interface(str(svi["ip_address_virtual"]))
        return str(itf.network), str(itf.ip)
    vra = svi.get("ip_virtual_router_addresses") or []
    if vra:
        gw = str(vra[0])
        if "/" in gw:
            itf = ipaddress.ip_interface(gw)
            return str(itf.network), str(itf.ip)
        mask_src = svi.get("ip_address") or next((n.get("ip_address") for n in svi.get("nodes") or []
                                                  if isinstance(n, dict) and n.get("ip_address")), None)
        if mask_src:
            return str(ipaddress.ip_interface(str(mask_src)).network), gw
    return None, None


def collect_vlans(hostvars: dict[str, dict[str, Any]]) -> dict[int, SviInfo]:
    """vid → SVI subnet/gateway/tenant/vrf from every network services key (default `tenants`). L2 VLANs have no subnet."""
    keys = [str(k.get("name")) for k in _first_var(hostvars, "network_services_keys") or [] if isinstance(k, dict)]
    keys = keys or ["tenants"]
    out: dict[int, SviInfo] = {}
    for hv in hostvars.values():
        for key in keys:
            for tenant in hv.get(key) or []:
                if not isinstance(tenant, dict):
                    continue
                tname = str(tenant.get("name", ""))
                for vrf in tenant.get("vrfs") or []:
                    for svi in (vrf or {}).get("svis") or []:
                        if not isinstance(svi, dict) or svi.get("id") is None:
                            continue
                        vid = int(svi["id"])
                        try:
                            net, gw = _svi_subnet(svi)
                        except ValueError:
                            net, gw = None, None
                        if vid not in out or (out[vid]["network"] is None and net):
                            out[vid] = SviInfo(network=net, gateway=gw, tenant=tname, vrf=str(vrf.get("name")))
                for l2 in tenant.get("l2vlans") or []:
                    if isinstance(l2, dict) and l2.get("id") is not None:
                        out.setdefault(int(l2["id"]), SviInfo(network=None, gateway=None, tenant=tname, vrf=None))
    return out


def parse_vlans(spec: Any, known: list[int] | None = None) -> list[int]:
    """"11-12,21-22" → [11, 12, 21, 22]; "all"/None on a trunk → every VLAN we know an SVI/L2 VLAN for."""
    if spec is None:
        return []
    text = str(spec).strip().lower()
    if text in ("all", "defined_vlans"):
        return sorted(known or [])
    out: set[int] = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = (int(x) for x in part.split("-", 1))
            out.update(range(a, b + 1))
        elif part.isdigit():
            out.add(int(part))
    return sorted(v for v in out if 1 <= v <= 4094)


def client_ip(network: str, index: int, gateway: str | None) -> str:
    """network + 100 + index, skipping the gateway, network and broadcast (wraps inside small subnets)."""
    net = ipaddress.ip_network(network)
    first, last = int(net.network_address) + 1, int(net.broadcast_address) - 1
    if net.prefixlen >= 31:
        first, last = int(net.network_address), int(net.broadcast_address)
    size = last - first + 1
    gw = int(ipaddress.ip_address(gateway)) if gateway else None
    if size <= (1 if gw is not None and first <= gw <= last else 0):
        raise ValueError(f"no usable address in {network}")
    cand = int(net.network_address) + IP_OFFSET + index
    if not first <= cand <= last:
        cand = first + (IP_OFFSET + index) % size
    for _ in range(size):
        if cand != gw:
            return f"{ipaddress.ip_address(cand)}/{net.prefixlen}"
        cand = cand + 1 if cand < last else first
    raise ValueError(f"no usable address in {network}")


# ------------------------------------------------------------------------------------------------ plans
def plan_clients(
    host_links: dict[str, list[dict[str, str]]],
    hostvars: dict[str, dict[str, Any]] | None,
    image: str,
) -> list[ClientPlan]:
    """`host_links[name] = [{eth, switch, switch_port, endpoint_port}]` from the topology (eth numbering is the
    containerlab one). Returns one plan per linux node, in `host_links` order."""
    hostvars = hostvars or {}
    endpoints = collect_endpoints(hostvars) if hostvars else []
    index = {ep["name"]: i for i, ep in enumerate(endpoints)}
    by_name = {ep["name"]: ep for ep in endpoints}
    vlan_info = collect_vlans(hostvars) if hostvars else {}
    plans: list[ClientPlan] = []
    for name, links in host_links.items():
        plans.append(_plan_one(name, links, by_name.get(name), index.get(name, len(endpoints)), vlan_info, image))
    return plans


def _plan_one(name: str, links: list[dict[str, str]], ep: dict[str, Any] | None, idx: int,
              vlan_info: dict[int, SviInfo], image: str) -> ClientPlan:
    plan_links = [ClientLink(eth=lk["eth"], switch=lk["switch"], switch_port=lk["switch_port"]) for lk in links]
    by_switch_port = {(lk["switch"], lk["switch_port"]): lk["eth"] for lk in links}
    by_ep_port = {lk.get("endpoint_port"): lk["eth"] for lk in links if lk.get("endpoint_port")}
    bonds: list[ClientBond] = []
    vlans: list[ClientVlan] = []
    addressed: set[int] = set()
    default_set = False

    def add_vlan(vid: int, iface: str) -> None:
        nonlocal default_set
        info = vlan_info.get(vid)
        ip = gw = None
        if info and info["network"] and vid not in addressed:
            try:
                ip = client_ip(info["network"], idx, info["gateway"])
                gw = info["gateway"]
            except ValueError:
                ip = None
        if ip:
            addressed.add(vid)
        is_default = bool(ip) and not default_set
        default_set = default_set or is_default
        vlans.append(ClientVlan(vid=vid, iface=iface, ip=ip, gateway=gw, default_route=is_default,
                                tenant=info["tenant"] if info else None, vrf=info["vrf"] if info else None))

    for adapter in (ep or {}).get("adapters") or []:
        if adapter.get("enabled") is False:
            continue
        switches = [str(s) for s in adapter.get("switches") or []]
        sports = [str(p) for p in adapter.get("switch_ports") or []]
        eports = [str(p) for p in adapter.get("endpoint_ports") or []]
        eths: list[str] = []
        for i, (sw, sp) in enumerate(zip(switches, sports)):
            eth = by_switch_port.get((sw, sp)) or (by_ep_port.get(eports[i]) if i < len(eports) else None)
            if eth and eth not in eths:
                eths.append(eth)
        if not eths:
            continue  # adapter not wired in this topology (switch not deployed)
        if isinstance(adapter.get("port_channel"), dict) and adapter["port_channel"].get("mode"):
            bond = ClientBond(name=f"bond{len(bonds)}", members=eths)
            bonds.append(bond)
            parents = [bond["name"]]
        else:
            parents = eths  # independent NICs: each carries the VLANs, only the first one is addressed
        mode = str(adapter.get("mode") or "access").lower()
        known = sorted(vlan_info)
        for parent in parents:
            if mode.startswith("trunk"):
                native = adapter.get("native_vlan")
                tagged = [v for v in parse_vlans(adapter.get("vlans") if adapter.get("vlans") is not None else "all", known)
                          if v != native]
                if native is not None:
                    add_vlan(int(native), parent)
                for vid in tagged:
                    add_vlan(vid, f"{parent}.{vid}")
            elif mode == "access":
                for vid in parse_vlans(adapter.get("vlans"))[:1]:
                    add_vlan(vid, parent)
    return ClientPlan(name=name, image=image, links=plan_links, bond=bonds[0] if bonds else None, bonds=bonds,
                      vlans=vlans)


# ------------------------------------------------------------------------------------------------ setup script
def setup_script(plan: ClientPlan) -> str:
    """POSIX sh (busybox-compatible) bootstrap run by containerlab `exec` inside the client container."""
    name = plan["name"]
    L: list[str] = [
        "#!/bin/sh",
        f"# {name} — client bootstrap generated by avdlab (Module 5); regenerated on every build, bind-mounted read-only.",
        "# links: " + (", ".join(f"{lk['eth']}={lk['switch']}:{lk['switch_port']}" for lk in plan["links"]) or "none"),
        "set -u",
        f"hostname {name} 2>/dev/null || true",
        "# no IPv6 RA/SLAAC noise; answer ARP only on the interface that owns the address",
        "for k in all default; do",
        "  sysctl -qw net.ipv6.conf.$k.accept_ra=0 net.ipv6.conf.$k.autoconf=0 >/dev/null 2>&1 || true",
        "done",
        "sysctl -qw net.ipv4.conf.all.arp_ignore=1 net.ipv4.conf.all.arp_announce=2 >/dev/null 2>&1 || true",
    ]
    members = {m for b in plan["bonds"] for m in b["members"]}
    for lk in plan["links"]:
        if lk["eth"] not in members:
            L.append(f"ip link set {lk['eth']} up || true")
    for b in plan["bonds"]:
        L.append(f"ip link add {b['name']} type bond mode 802.3ad miimon 100 2>/dev/null || true")
        for m in b["members"]:
            L.append(f"ip link set {m} down; ip link set {m} master {b['name']} || true; ip link set {m} up || true")
        L.append(f"ip link set {b['name']} up || true")
    for v in plan["vlans"]:
        iface = v["iface"]
        if "." in iface:
            parent = iface.split(".", 1)[0]
            L.append(f"ip link add link {parent} name {iface} type vlan id {v['vid']} 2>/dev/null || true")
            L.append(f"ip link set {iface} up || true")
        if v["ip"]:
            L.append(f"ip addr replace {v['ip']} dev {iface}")
        if v["default_route"] and v["gateway"]:
            L.append(f"ip route replace default via {v['gateway']} dev {iface}")
    L += [
        "pgrep iperf3 >/dev/null 2>&1 || iperf3 -s -D >/dev/null 2>&1 || true",
        "cat > /root/avdlab-client.json <<'AVDLAB_EOF'",
        json.dumps(plan, indent=2, sort_keys=True),
        "AVDLAB_EOF",
        f"echo '[avdlab] {name} ready'",
    ]
    return "\n".join(L) + "\n"


# ------------------------------------------------------------------------------------------------ persistence
def write_plans(clab_dir: Path, plans: list[ClientPlan]) -> Path:
    p = Path(clab_dir) / CLIENTS_JSON
    p.write_text(json.dumps(plans, indent=2) + "\n")
    return p


def load_plans(lab_dir: Path) -> list[ClientPlan] | None:
    p = Path(lab_dir) / "clab" / CLIENTS_JSON
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
    except ValueError:
        return None
    return data if isinstance(data, list) else None


def load_hostvars_safe(lab_dir: Path) -> dict[str, dict[str, Any]]:
    """Resolved (unvalidated) hostvars of the lab dir, {} when the inventory cannot be read."""
    from ..labs.templates import load_lab_hostvars

    try:
        return load_lab_hostvars(Path(lab_dir))
    except Exception:  # noqa: BLE001 - plans then carry links only
        return {}


def default_ip(plan: ClientPlan, vlan: int | None = None) -> str | None:
    """Address other clients target: the given VLAN's, else the default-route VLAN's, else the first one."""
    vl = [v for v in plan.get("vlans") or [] if v.get("ip")]
    if vlan is not None:
        vl = [v for v in vl if v["vid"] == vlan]
    if not vl:
        return None
    chosen = next((v for v in vl if v.get("default_route")), vl[0])
    return str(chosen["ip"]).split("/")[0]


# ------------------------------------------------------------------------------------------------ phase 6 (C): hot-added test hosts
def with_ip(plan: ClientPlan, ip: str | None) -> ClientPlan:
    """Explicit address for a client (`"a.b.c.d/nn"`): replaces the default-route VLAN's address (else the first
    VLAN's). `None`/`"auto"` keeps the derived plan."""
    if not ip or str(ip).lower() == "auto" or not plan.get("vlans"):
        return plan
    vlans = [dict(v) for v in plan["vlans"]]
    idx = next((i for i, v in enumerate(vlans) if v.get("default_route")),
               next((i for i, v in enumerate(vlans) if v.get("ip")), 0))
    vlans[idx]["ip"] = str(ipaddress.ip_interface(str(ip)))
    if vlans[idx].get("gateway") and not any(v.get("default_route") for v in vlans):
        vlans[idx]["default_route"] = True
    return ClientPlan(**{**plan, "vlans": vlans})  # type: ignore[typeddict-item]


def apply_client_overrides(lab_dir: Path, hot: dict[str, dict[str, Any]]) -> list[str]:
    """Re-apply the explicit IP / image of hot-added clients (`lab.settings["hot_clients"]`) after the topology was
    regenerated: `clab/clients.json`, `clab/hosts/<name>.sh` and the node image in `clab/topology.clab.yml`.
    Returns the patched client names."""
    clab_dir = Path(lab_dir) / "clab"
    plans = load_plans(Path(lab_dir))
    if not plans or not hot:
        return []
    patched: list[str] = []
    images: dict[str, str] = {}
    for i, plan in enumerate(plans):
        meta = hot.get(plan["name"]) or {}
        if not meta:
            continue
        new = with_ip(plan, meta.get("ip"))
        if meta.get("image"):
            new = ClientPlan(**{**new, "image": str(meta["image"])})  # type: ignore[typeddict-item]
            images[plan["name"]] = str(meta["image"])
        if new != plan:
            plans[i] = new
            script = clab_dir / "hosts" / f"{plan['name']}.sh"
            if script.parent.is_dir():
                script.write_text(setup_script(new))
                script.chmod(0o755)
            patched.append(plan["name"])
    if patched:
        write_plans(clab_dir, plans)
    topo_file = clab_dir / "topology.clab.yml"
    if images and topo_file.is_file():
        import yaml

        topo = yaml.safe_load(topo_file.read_text()) or {}
        nodes = (topo.get("topology") or {}).get("nodes") or {}
        touched = False
        for name, image in images.items():
            if isinstance(nodes.get(name), dict) and nodes[name].get("image") != image:
                nodes[name]["image"] = image
                touched = True
        if touched:
            topo_file.write_text("---\n" + yaml.safe_dump(topo, sort_keys=False, default_flow_style=None))
    return patched
