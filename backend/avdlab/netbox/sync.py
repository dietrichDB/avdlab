"""Lab → NetBox: a pure *plan* built from the lab's build outputs, and a *reconciler* that applies it with pynetbox.

Plan inputs (all written by `lab.build`):
  intended/structured_configs/*.yml            devices, interfaces, VLANs, VRFs, IPs, SVI subnets
  documentation/fabric/<fabric>-topology.csv   node types (→ device roles) and cables
  documentation/fabric/<fabric>-p2p-links.csv  P2P /31 prefixes
  clab/clients.json                            client devices (role server), their links, bonds and VLAN IPs
  group_vars/*.yml + knob `pools`              address pools (→ container prefixes)

Reconciler: every object is looked up by its natural key (name/slug, site+vid, prefix+vrf, device+name, …), created
when missing and updated only when a field differs (so a second run is updates-only / unchanged), and tagged `avdlab` +
`avdlab:<slug>` (tag slug `avdlab-<slug>`). Tags are merged, never replaced, so an object shared by two labs (e.g. VRF
VRF10) carries both lab tags; `delete_stale` then only removes this lab's tag from it instead of deleting it.
In `dry_run` nothing is written: missing objects become placeholders (negative ids) and every action is only counted.

Everything here is synchronous (pynetbox is blocking); the job runs it in a thread.
"""
from __future__ import annotations

import csv
import ipaddress
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

Log = Callable[[str], None]

TAG_ALL = {"name": "avdlab", "slug": "avdlab", "color": "2196f3", "description": "managed by avdlab"}
MANUFACTURER = {"name": "Arista", "slug": "arista"}
PLATFORM = {"name": "Arista EOS", "slug": "arista-eos"}
CLIENT_MANUFACTURER = {"name": "Generic", "slug": "generic"}
CLIENT_MODEL = "Linux client"
CLIENT_ROLE = "server"
ROLE_COLORS = {"spine": "3f51b5", "super-spine": "283593", "l3leaf": "009688", "l2leaf": "8bc34a", "l2spine": "673ab7",
               "l3spine": "673ab7", "leaf": "009688", "server": "9e9e9e", "wan_router": "ff9800", "wan_rr": "ff5722"}
# order of creation; deletion runs in STALE_ORDER
OWNED_TYPES = ("site", "location", "rack", "vlan", "vrf", "device", "interface", "prefix", "ip_address", "cable")
STALE_ORDER = ("cable", "ip_address", "interface", "device", "prefix", "vlan", "vrf", "rack", "location", "site")
ENDPOINTS = {
    "tag": ("extras", "tags"), "manufacturer": ("dcim", "manufacturers"), "platform": ("dcim", "platforms"),
    "device_type": ("dcim", "device_types"), "device_role": ("dcim", "device_roles"), "site": ("dcim", "sites"),
    "location": ("dcim", "locations"), "rack": ("dcim", "racks"), "device": ("dcim", "devices"),
    "interface": ("dcim", "interfaces"), "cable": ("dcim", "cables"), "vlan": ("ipam", "vlans"),
    "vrf": ("ipam", "vrfs"), "prefix": ("ipam", "prefixes"), "ip_address": ("ipam", "ip_addresses"),
}


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9_-]+", "-", str(text).strip().lower()).strip("-")
    return s[:100] or "x"


def lab_tag(slug: str) -> dict[str, str]:
    return {"name": f"avdlab:{slug}", "slug": slugify(f"avdlab-{slug}"), "color": "03a9f4",
            "description": f"avdlab lab {slug}"}


# ================================================================================================ plan
@dataclass
class IntfSpec:
    name: str
    type: str
    description: str = ""
    enabled: bool = True
    mgmt_only: bool = False
    mode: str | None = None  # access | tagged | tagged-all
    untagged_vid: int | None = None
    tagged_vids: list[int] = field(default_factory=list)
    lag: str | None = None
    vrf: str | None = None
    parent: str | None = None
    mtu: int | None = None


@dataclass
class DeviceSpec:
    name: str
    role: str
    model: str
    serial: str | None = None
    platform: bool = True
    client: bool = False
    location: str | None = None
    rack: str | None = None
    interfaces: list[IntfSpec] = field(default_factory=list)

    def intf(self, name: str) -> IntfSpec | None:
        return next((i for i in self.interfaces if i.name == name), None)


@dataclass
class IpSpec:
    device: str
    interface: str
    address: str
    vrf: str | None = None
    role: str | None = None  # loopback | anycast
    primary: bool = False


@dataclass
class Plan:
    site: str
    fabric_name: str
    devices: list[DeviceSpec] = field(default_factory=list)
    vlans: dict[int, str] = field(default_factory=dict)
    vrfs: dict[str, str | None] = field(default_factory=dict)  # name → rd
    prefixes: dict[tuple[str, str | None], str] = field(default_factory=dict)  # (prefix, vrf) → description
    ips: list[IpSpec] = field(default_factory=list)
    cables: list[tuple[str, str, str, str]] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def device(self, name: str) -> DeviceSpec | None:
        return next((d for d in self.devices if d.name == name), None)

    def summary(self) -> dict[str, Any]:
        return {"devices": len(self.devices), "clients": sum(1 for d in self.devices if d.client),
                "interfaces": sum(len(d.interfaces) for d in self.devices), "vlans": sorted(self.vlans),
                "vrfs": sorted(self.vrfs), "prefixes": len(self.prefixes), "ip_addresses": len(self.ips),
                "cables": len(self.cables)}


def parse_vlan_list(spec: Any) -> list[int] | None:
    """'11-12,21-22,3401' → [11, 12, 21, 22, 3401]; 'all'/None → None (= all)."""
    if spec is None:
        return None
    if isinstance(spec, int):
        return [spec]
    s = str(spec).strip().lower()
    if s in ("", "all"):
        return None
    if s == "none":
        return []
    out: list[int] = []
    for part in s.replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            if a.isdigit() and b.isdigit():
                out.extend(range(int(a), int(b) + 1))
        elif part.isdigit():
            out.append(int(part))
    return out


def ethernet_type(speed: Any) -> str:
    """EOS `speed` ('100gfull', 'forced 25gfull', '10000full', 'auto') → NetBox interface type (default 1000base-t)."""
    s = str(speed or "").lower()
    for token, t in (("400g", "400gbase-x-qsfpdd"), ("200g", "200gbase-x-qsfp56"), ("100g", "100gbase-x-qsfp28"),
                     ("50g", "50gbase-x-sfp56"), ("40g", "40gbase-x-qsfpp"), ("25g", "25gbase-x-sfp28"),
                     ("10g", "10gbase-x-sfpp"), ("10000", "10gbase-x-sfpp")):
        if re.search(rf"(^|[^0-9]){token}", s):
            return t
    if re.search(r"(^|[^0-9])100(full|half)", s):
        return "100base-tx"
    return "1000base-t"


def device_model(platform: str | None) -> str:
    p = str(platform or "cEOSLab")
    return "cEOS-lab" if p.lower() == "ceoslab" else p


def _switchport(intf: dict[str, Any], known_vids: set[int]) -> tuple[str | None, int | None, list[int]]:
    sp = intf.get("switchport") or {}
    if not sp or sp.get("enabled") is False:
        return None, None, []
    mode = sp.get("mode")
    if mode == "access":
        vid = sp.get("access_vlan")
        return "access", int(vid) if vid and int(vid) in known_vids else None, []
    if mode == "trunk":
        trunk = sp.get("trunk") or {}
        native = trunk.get("native_vlan")
        native = int(native) if native and int(native) in known_vids else None
        allowed = parse_vlan_list(trunk.get("allowed_vlan"))
        if allowed is None:
            return "tagged-all", native, []
        return "tagged", native, sorted(v for v in set(allowed) if v in known_vids and v != native)
    return None, None, []


def _ips_of(intf: dict[str, Any]) -> list[tuple[str, str | None]]:
    """[(address/len, role)] of an EOS interface (structured config)."""
    out: list[tuple[str, str | None]] = []
    ip = intf.get("ip_address")
    if ip and "/" in str(ip):
        out.append((str(ip), None))
    for sec in intf.get("ip_address_secondaries") or []:
        if "/" in str(sec):
            out.append((str(sec), None))
    virt = intf.get("ip_address_virtual")
    if virt and "/" in str(virt):
        out.append((str(virt), "anycast"))
    for v in intf.get("ip_address_virtual_secondaries") or []:
        if "/" in str(v):
            out.append((str(v), "anycast"))
    for v in intf.get("ip_virtual_router_addresses") or []:
        if "/" in str(v):
            out.append((str(v), "anycast"))
    return out


def _network(addr: str) -> str | None:
    try:
        return str(ipaddress.ip_interface(addr).network)
    except ValueError:
        return None


def _cidrs(value: Any) -> list[str]:
    out = []
    for part in str(value).split(","):
        try:
            out.append(str(ipaddress.ip_network(part.strip(), strict=False)))
        except ValueError:
            continue
    return out


def _scan_pools(data: Any, found: dict[str, str], path: str = "") -> None:
    """group_vars keys ending in `_pool`/`_pools`/`ip_range` whose values are CIDRs."""
    if isinstance(data, dict):
        for k, v in data.items():
            key = str(k)
            if isinstance(v, str) and (key.endswith(("_pool", "_pools", "ip_range", "_subnet"))):
                for c in _cidrs(v):
                    found.setdefault(c, key)
            else:
                _scan_pools(v, found, f"{path}.{key}" if path else key)
    elif isinstance(data, list):
        for item in data:
            _scan_pools(item, found, path)


def load_structured(lab_dir: Path) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for f in sorted((Path(lab_dir) / "intended" / "structured_configs").glob("*.yml")):
        sc = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        out[str(sc.get("hostname") or f.stem)] = sc
    return out


def build_plan(lab_dir: Path, *, fabric_name: str, knobs: dict[str, Any] | None = None,
               include_clients: bool = True) -> Plan:
    from ..labs.templates import load_group_vars_dir
    from ..topology.clab import read_links, read_node_types
    from ..topology.clients import load_plans

    lab_dir = Path(lab_dir)
    structured = load_structured(lab_dir)
    if not structured:
        raise FileNotFoundError("no structured configs: build the lab first")
    fab = lab_dir / "documentation" / "fabric"
    topo_csv = fab / f"{fabric_name}-topology.csv"
    if not topo_csv.exists():
        found = sorted(fab.glob("*-topology.csv"))
        topo_csv = found[0] if found else topo_csv
    node_types = read_node_types(topo_csv) if topo_csv.exists() else {}
    first_meta = next(iter(structured.values())).get("metadata") or {}
    plan = Plan(site=str(first_meta.get("dc_name") or fabric_name), fabric_name=fabric_name)

    # ---- VLANs / VRFs (fabric-wide union)
    for sc in structured.values():
        for v in sc.get("vlans") or []:
            if v.get("id") is not None:
                plan.vlans.setdefault(int(v["id"]), str(v.get("name") or f"VLAN{v['id']}"))
    for host in sorted(structured):
        sc = structured[host]
        for v in sc.get("vrfs") or []:
            if v.get("name") and v["name"] != "default":
                plan.vrfs.setdefault(str(v["name"]), None)
        for v in (sc.get("router_bgp") or {}).get("vrfs") or []:
            name = str(v.get("name") or "")
            if name and name != "default":
                if plan.vrfs.get(name) is None and v.get("rd"):
                    plan.vrfs[name] = str(v["rd"])
                plan.vrfs.setdefault(name, None)
    known = set(plan.vlans)

    # ---- switches
    for host, sc in structured.items():
        meta = sc.get("metadata") or {}
        dev = DeviceSpec(name=host, role=str(node_types.get(host) or "switch"), model=device_model(meta.get("platform")),
                         serial=meta.get("serial_number") or None, location=meta.get("pod_name") or None,
                         rack=meta.get("rack") or None)
        pcs = sc.get("port_channel_interfaces") or []
        for pc in pcs:
            mode, untagged, tagged = _switchport(pc, known)
            dev.interfaces.append(IntfSpec(name=pc["name"], type="lag", description=pc.get("description") or "",
                                           enabled=not pc.get("shutdown", False), mode=mode, untagged_vid=untagged,
                                           tagged_vids=tagged, mtu=pc.get("mtu"), vrf=pc.get("vrf"),
                                           parent=pc["name"].split(".")[0] if "." in pc["name"] else None))
        pc_names = {pc["name"] for pc in pcs}
        for eth in sc.get("ethernet_interfaces") or []:
            name = eth["name"]
            mode, untagged, tagged = _switchport(eth, known)
            lag = None
            cg = eth.get("channel_group") or {}
            if cg.get("id") is not None:
                lag = f"Port-Channel{cg['id']}"
                if lag not in pc_names:
                    dev.interfaces.append(IntfSpec(name=lag, type="lag"))
                    pc_names.add(lag)
            sub = "." in name
            dev.interfaces.append(IntfSpec(name=name, type="virtual" if sub else ethernet_type(eth.get("speed")),
                                           description=eth.get("description") or "",
                                           enabled=not eth.get("shutdown", False), mode=mode, untagged_vid=untagged,
                                           tagged_vids=tagged, lag=lag, vrf=eth.get("vrf"), mtu=eth.get("mtu"),
                                           parent=name.split(".")[0] if sub else None))
        for key in ("loopback_interfaces", "vlan_interfaces"):
            for intf in sc.get(key) or []:
                dev.interfaces.append(IntfSpec(name=intf["name"], type="virtual", description=intf.get("description") or "",
                                               enabled=not intf.get("shutdown", False), vrf=intf.get("vrf"),
                                               mtu=intf.get("mtu")))
        for i, mg in enumerate(sc.get("management_interfaces") or []):
            dev.interfaces.append(IntfSpec(name=mg["name"], type="1000base-t", description=mg.get("description") or "",
                                           enabled=not mg.get("shutdown", False), mgmt_only=True, vrf=mg.get("vrf")))
            if mg.get("ip_address") and "/" in str(mg["ip_address"]):
                plan.ips.append(IpSpec(host, mg["name"], str(mg["ip_address"]), mg.get("vrf"), None, primary=i == 0))
                net = _network(str(mg["ip_address"]))
                if net:
                    plan.prefixes.setdefault((net, mg.get("vrf")), "management")
        # IPs + SVI subnets
        for key in ("ethernet_interfaces", "port_channel_interfaces", "loopback_interfaces", "vlan_interfaces"):
            for intf in sc.get(key) or []:
                for addr, role in _ips_of(intf):
                    if intf["name"] == "Loopback0" and role is None:
                        role = "loopback"
                    plan.ips.append(IpSpec(host, intf["name"], addr, intf.get("vrf"), role))
                    if key == "vlan_interfaces":
                        net = _network(addr)
                        if net:
                            plan.prefixes.setdefault((net, intf.get("vrf")), intf.get("description") or intf["name"])
        plan.devices.append(dev)

    # the same address in the same VRF on several devices (VTEP of an MLAG pair, SVI virtual IPs) → anycast
    seen: dict[tuple[str, str | None], set[str]] = defaultdict(set)
    for ip in plan.ips:
        seen[(ip.address, ip.vrf)].add(ip.device)
    for ip in plan.ips:
        if len(seen[(ip.address, ip.vrf)]) > 1 and ip.role in (None, "loopback"):
            ip.role = "anycast"

    # ---- prefixes: pools (knob + group_vars) and P2P /31s
    pools: dict[str, str] = {}
    for k, v in ((knobs or {}).get("pools") or {}).items():
        for c in _cidrs(v):
            pools.setdefault(c, f"pool {k}")
    try:
        _scan_pools(load_group_vars_dir(lab_dir), pools)
    except Exception:  # noqa: BLE001 - group_vars are optional input for pools
        pass
    mgmt_nets = {p for (p, _vrf), d in plan.prefixes.items() if d == "management"}
    for cidr, desc in pools.items():
        if cidr in mgmt_nets or ipaddress.ip_network(cidr).prefixlen == 0:
            continue
        plan.prefixes.setdefault((cidr, None), desc if desc.startswith("pool ") else f"pool {desc}")
    p2p = fab / f"{fabric_name}-p2p-links.csv"
    if p2p.exists():
        for row in csv.DictReader(p2p.open(encoding="utf-8")):
            net = _network(row.get("Leaf IP Address") or row.get("Node IP Address") or "")
            if net:
                plan.prefixes.setdefault((net, None), f"P2P {row.get('Node')} {row.get('Node Interface')} - "
                                                      f"{row.get('Peer Node')} {row.get('Peer Interface')}")

    # ---- clients
    client_ports: dict[tuple[str, str], tuple[str, str]] = {}  # (switch, port) → (client, eth)
    plans = (load_plans(lab_dir) or []) if include_clients else []
    for cp in plans:
        dev = DeviceSpec(name=cp["name"], role=CLIENT_ROLE, model=CLIENT_MODEL, platform=False, client=True)
        bond_of = {m: b["name"] for b in (cp.get("bonds") or []) for m in b.get("members") or []}
        for b in cp.get("bonds") or []:
            dev.interfaces.append(IntfSpec(name=b["name"], type="lag", description="bond"))
        for link in cp.get("links") or []:
            dev.interfaces.append(IntfSpec(name=link["eth"], type="1000base-t",
                                           description=f"{link['switch']} {link['switch_port']}",
                                           lag=bond_of.get(link["eth"])))
            client_ports[(link["switch"], link["switch_port"])] = (cp["name"], link["eth"])
        for v in cp.get("vlans") or []:
            iface = v.get("iface")
            if not iface:
                continue
            if dev.intf(iface) is None:
                parent = iface.split(".")[0] if "." in iface else None
                dev.interfaces.append(IntfSpec(name=iface, type="virtual", description=f"VLAN {v.get('vid')}",
                                               parent=parent if parent and dev.intf(parent) else None,
                                               vrf=v.get("vrf") if v.get("vrf") in plan.vrfs else None))
            if v.get("ip"):
                vrf = v.get("vrf") if v.get("vrf") in plan.vrfs else None
                plan.ips.append(IpSpec(cp["name"], iface, str(v["ip"]), vrf, None, primary=bool(v.get("default_route"))))
        plan.devices.append(dev)

    # ---- cables (topology CSV, de-duplicated; both ends must exist in the plan)
    if topo_csv.exists():
        for a, ai, b, bi, _peer_type in read_links(topo_csv):
            ends = []
            for node, intf, other, other_intf in ((a, ai, b, bi), (b, bi, a, ai)):
                if node not in structured and (other, other_intf) in client_ports:  # AVD 'PCI1' → clab 'eth1'
                    node, intf = client_ports[(other, other_intf)]
                ends.append((node, intf))
            (na, ia), (nb_, ib) = ends
            da, db = plan.device(na), plan.device(nb_)
            if da is None or db is None:
                missing = na if da is None else nb_
                plan.skipped.append(f"cable {a}:{ai} ↔ {b}:{bi}: device {missing} not synced")
                continue
            missing_if = [f"{n}:{i}" for n, i, d in ((na, ia, da), (nb_, ib, db)) if d.intf(i) is None]
            if missing_if:
                plan.skipped.append(f"cable {a}:{ai} ↔ {b}:{bi}: interface {', '.join(missing_if)} not in the configs")
                continue
            plan.cables.append((na, ia, nb_, ib))
    return plan


# ================================================================================================ reconciler
class _Placeholder:
    """Stands in for an object that a dry run would create."""

    def __init__(self, oid: int, data: dict[str, Any]) -> None:
        self.id = oid
        self._data = dict(data)

    def serialize(self) -> dict[str, Any]:
        return dict(self._data)


def _is_placeholder(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value < 0


def _norm(value: Any) -> Any:
    if value == "":
        return None
    if isinstance(value, dict) and "value" in value:
        return value["value"]
    if isinstance(value, dict) and "id" in value:
        return value["id"]
    if isinstance(value, list):
        return sorted((_norm(v) for v in value), key=lambda x: str(x))
    return value


class Cancelled(Exception):
    pass


class Reconciler:
    def __init__(self, nb: Any, *, slug: str, dry_run: bool = False, log: Log = print,
                 check_cancelled: Callable[[], None] | None = None) -> None:
        self.nb = nb
        self.slug = slug
        self.dry_run = dry_run
        self.log = log
        self.check_cancelled = check_cancelled
        self.created: dict[str, int] = defaultdict(int)
        self.updated: dict[str, int] = defaultdict(int)
        self.unchanged: dict[str, int] = defaultdict(int)
        self.deleted: dict[str, int] = defaultdict(int)
        self.skipped: list[str] = []
        self.touched: dict[str, set[int]] = defaultdict(set)
        self._next_placeholder = -1
        self.tag_ids: list[int] = []  # [avdlab, avdlab:<slug>]
        self.lab_tag_slug = lab_tag(slug)["slug"]

    # ---------------------------------------------------------------------------------------- primitives
    def ep(self, kind: str) -> Any:
        app, name = ENDPOINTS[kind]
        return getattr(getattr(self.nb, app), name)

    def _find(self, kind: str, lookup: dict[str, Any]) -> Any:
        if any(_is_placeholder(v) for v in lookup.values()):
            return None
        found = list(self.ep(kind).filter(**lookup))
        return found[0] if found else None

    def _tags(self, owned: bool) -> list[int]:
        return list(self.tag_ids if owned else self.tag_ids[:1])

    def ensure(self, kind: str, lookup: dict[str, Any] | None, payload: dict[str, Any], *, owned: bool = True,
               found: Any = ..., tagged: bool = True) -> Any:
        if self.check_cancelled:
            self.check_cancelled()
        if found is ...:
            found = self._find(kind, lookup or {})
        data = dict(payload)
        if tagged:
            data["tags"] = self._tags(owned)
        if found is None:
            self.created[kind] += 1
            if self.dry_run or any(_is_placeholder(v) for v in data.values()):
                if not self.dry_run:  # cannot happen: placeholders only exist in dry runs
                    raise RuntimeError(f"{kind}: unresolved reference in {data}")
                rec: Any = _Placeholder(self._next_placeholder, data)
                self._next_placeholder -= 1
                return rec
            rec = self.ep(kind).create(data)
            self.touched[kind].add(rec.id)
            return rec
        self.touched[kind].add(found.id)
        self.apply(kind, found, data)
        return found

    def apply(self, kind: str, rec: Any, data: dict[str, Any], *, count: bool = True) -> bool:
        """Update the fields of `rec` that differ from `data` (tags are merged). Returns True when something changed."""
        if isinstance(rec, _Placeholder):
            rec._data.update(data)
            return False
        cur = rec.serialize()
        changes: dict[str, Any] = {}
        for k, v in data.items():
            if k == "tags":
                have = [t for t in (_norm(cur.get("tags")) or [])]
                want = sorted(set(have) | set(v))
                if want != sorted(set(have)):
                    changes["tags"] = want
                continue
            if _norm(cur.get(k)) != _norm(v):
                changes[k] = v
        if not changes:
            if count:
                self.unchanged[kind] += 1
            return False
        if count:
            self.updated[kind] += 1
        if not self.dry_run:
            rec.update(changes)
        return True

    # ---------------------------------------------------------------------------------------- run
    def run(self, plan: Plan, *, delete_stale: bool = False) -> dict[str, Any]:
        log = self.log
        self.skipped.extend(plan.skipped)
        # tags
        t_all = self.ensure("tag", {"slug": TAG_ALL["slug"]}, TAG_ALL, tagged=False)
        lt = lab_tag(self.slug)
        t_lab = self.ensure("tag", {"slug": lt["slug"]}, lt, tagged=False)
        self.tag_ids = [t_all.id, t_lab.id]
        # shared catalog objects (tagged `avdlab` only, never deleted)
        mfr = self.ensure("manufacturer", {"slug": MANUFACTURER["slug"]}, MANUFACTURER, owned=False)
        platform = self.ensure("platform", {"slug": PLATFORM["slug"]}, {**PLATFORM, "manufacturer": mfr.id}, owned=False)
        client_mfr = None
        types: dict[str, Any] = {}
        roles: dict[str, Any] = {}
        for dev in plan.devices:
            if dev.model not in types:
                m = mfr
                if dev.client:
                    client_mfr = client_mfr or self.ensure("manufacturer", {"slug": CLIENT_MANUFACTURER["slug"]},
                                                           CLIENT_MANUFACTURER, owned=False)
                    m = client_mfr
                slug = slugify(dev.model)
                types[dev.model] = self.ensure("device_type", {"manufacturer_id": m.id, "slug": slug},
                                               {"manufacturer": m.id, "model": dev.model, "slug": slug}, owned=False)
            if dev.role not in roles:
                slug = slugify(dev.role)
                roles[dev.role] = self.ensure("device_role", {"slug": slug},
                                              {"name": dev.role, "slug": slug,
                                               "color": ROLE_COLORS.get(dev.role, "607d8b")}, owned=False)
        log(f"[netbox] catalog: {len(types)} device type(s), {len(roles)} role(s)")
        # site / locations / racks
        site_slug = slugify(plan.site)
        site = self.ensure("site", {"slug": site_slug}, {"name": plan.site, "slug": site_slug, "status": "active"})
        locations: dict[str, Any] = {}
        racks: dict[str, Any] = {}
        for dev in plan.devices:
            if dev.location and dev.location not in locations:
                slug = slugify(dev.location)
                locations[dev.location] = self.ensure("location", {"site_id": site.id, "slug": slug},
                                                      {"name": dev.location, "slug": slug, "site": site.id,
                                                       "status": "active"})
            if dev.rack and dev.rack not in racks:
                payload = {"name": dev.rack, "site": site.id, "status": "active"}
                if dev.location:
                    payload["location"] = locations[dev.location].id
                racks[dev.rack] = self.ensure("rack", {"site_id": site.id, "name": dev.rack}, payload)
        # VLANs / VRFs
        vlans = {vid: self.ensure("vlan", {"site_id": site.id, "vid": vid},
                                  {"site": site.id, "vid": vid, "name": name[:64], "status": "active"})
                 for vid, name in sorted(plan.vlans.items())}
        vrfs: dict[str, Any] = {}
        for name, rd in sorted(plan.vrfs.items()):
            payload: dict[str, Any] = {"name": name}
            if rd:
                payload["rd"] = rd
            vrfs[name] = self.ensure("vrf", {"name": name}, payload)
        log(f"[netbox] site {plan.site}: {len(vlans)} VLAN(s), {len(vrfs)} VRF(s)")
        # devices + interfaces
        devices: dict[str, Any] = {}
        intfs: dict[tuple[str, str], Any] = {}
        for dev in plan.devices:
            payload = {"name": dev.name, "device_type": types[dev.model].id, "role": roles[dev.role].id,
                       "site": site.id, "status": "active"}
            if dev.platform:
                payload["platform"] = platform.id
            if dev.serial:
                payload["serial"] = dev.serial
            if dev.location:
                payload["location"] = locations[dev.location].id
            if dev.rack:
                payload["rack"] = racks[dev.rack].id
            found = self._find("device", {"site_id": site.id, "name": dev.name})
            if found is None and dev.serial:
                found = self._find("device", {"serial": dev.serial})
            rec = self.ensure("device", None, payload, found=found)
            devices[dev.name] = rec
            existing = {} if _is_placeholder(rec.id) else {
                str(r.serialize().get("name")): r for r in self.ep("interface").filter(device_id=rec.id)}
            # LAGs first (members reference them), then physical, then virtual (may have a parent)
            order = sorted(dev.interfaces, key=lambda i: (0 if i.type == "lag" else 2 if i.parent else 1))
            for spec in order:
                data: dict[str, Any] = {"device": rec.id, "name": spec.name, "type": spec.type,
                                        "description": spec.description[:200], "enabled": spec.enabled,
                                        "mgmt_only": spec.mgmt_only, "mode": spec.mode,
                                        "untagged_vlan": vlans[spec.untagged_vid].id if spec.untagged_vid else None}
                if spec.mode == "tagged":
                    data["tagged_vlans"] = [vlans[v].id for v in spec.tagged_vids]
                if spec.lag:
                    data["lag"] = intfs[(dev.name, spec.lag)].id
                if spec.parent and (dev.name, spec.parent) in intfs:
                    data["parent"] = intfs[(dev.name, spec.parent)].id
                if spec.vrf and spec.vrf in vrfs:
                    data["vrf"] = vrfs[spec.vrf].id
                if spec.mtu:
                    data["mtu"] = int(spec.mtu)
                intfs[(dev.name, spec.name)] = self.ensure("interface", None, data, found=existing.get(spec.name))
            log(f"[netbox] {dev.name}: {len(dev.interfaces)} interface(s)")
        # prefixes
        for (prefix, vrf), desc in sorted(plan.prefixes.items(), key=lambda kv: (str(kv[0][1] or ""), kv[0][0])):
            vrf_rec = vrfs.get(vrf) if vrf else None
            self.ensure("prefix", {"prefix": prefix, "vrf_id": vrf_rec.id if vrf_rec is not None else "null"},
                        {"prefix": prefix, "vrf": vrf_rec.id if vrf_rec is not None else None, "status": "active",
                         "description": desc[:200]})
        # IP addresses (+ primary_ip4)
        by_device: dict[str, dict[tuple[str, Any], Any]] = {}
        for ip in plan.ips:
            dev_rec = devices.get(ip.device)
            intf = intfs.get((ip.device, ip.interface))
            if dev_rec is None or intf is None:
                self.skipped.append(f"ip {ip.address} on {ip.device}:{ip.interface}: interface not synced")
                continue
            if ip.device not in by_device:
                by_device[ip.device] = {} if _is_placeholder(dev_rec.id) else {
                    (str(r.serialize().get("address")), r.serialize().get("assigned_object_id")): r
                    for r in self.ep("ip_address").filter(device_id=dev_rec.id)}
            vrf_rec = vrfs.get(ip.vrf) if ip.vrf else None
            data = {"address": ip.address, "vrf": vrf_rec.id if vrf_rec is not None else None, "status": "active",
                    "role": ip.role, "assigned_object_type": "dcim.interface", "assigned_object_id": intf.id}
            rec = self.ensure("ip_address", None, data, found=by_device[ip.device].get((ip.address, intf.id)))
            if ip.primary and ip.address.count(".") == 3:
                self.apply("device", dev_rec, {"primary_ip4": rec.id}, count=False)
        # cables
        for a, ai, b, bi in plan.cables:
            self._cable(intfs[(a, ai)], intfs[(b, bi)], f"{a}:{ai} ↔ {b}:{bi}")
        log(f"[netbox] {len(plan.ips)} IP address(es), {len(plan.prefixes)} prefix(es), {len(plan.cables)} cable(s)")
        if delete_stale:
            self.delete_stale()
        return self.result()

    def _cable(self, ia: Any, ib: Any, label: str) -> None:
        if self.check_cancelled:
            self.check_cancelled()
        ca = None if isinstance(ia, _Placeholder) else _norm(ia.serialize().get("cable"))
        cb = None if isinstance(ib, _Placeholder) else _norm(ib.serialize().get("cable"))
        if ca is None and cb is None:
            data = {"a_terminations": [{"object_type": "dcim.interface", "object_id": ia.id}],
                    "b_terminations": [{"object_type": "dcim.interface", "object_id": ib.id}],
                    "status": "connected", "tags": self._tags(True)}
            self.created["cable"] += 1
            if not self.dry_run:
                rec = self.ep("cable").create(data)
                self.touched["cable"].add(rec.id)
            return
        if ca != cb:
            self.skipped.append(f"cable {label}: an end is already cabled elsewhere (cable {ca or cb})")
            return
        self.touched["cable"].add(ca)
        cable = self.ep("cable").get(ca)
        if cable is not None:
            self.apply("cable", cable, {"status": "connected", "tags": self._tags(True)})

    def delete_stale(self) -> None:
        lab_tag_id = self.tag_ids[1]
        other_lab_tags = {int(_norm(t.serialize().get("id"))) for t in self.ep("tag").all()
                          if str(t.serialize().get("slug") or "").startswith("avdlab-")
                          and _norm(t.serialize().get("id")) != lab_tag_id}
        for kind in STALE_ORDER:
            if _is_placeholder(lab_tag_id):
                break  # dry run on a NetBox that never saw this lab: nothing can be stale
            for rec in list(self.ep(kind).filter(tag=self.lab_tag_slug)):
                if self.check_cancelled:
                    self.check_cancelled()
                if rec.id in self.touched[kind]:
                    continue
                tags = set(_norm(rec.serialize().get("tags")) or [])
                try:
                    if tags & other_lab_tags:  # shared with another lab: only drop this lab's tag
                        self.updated[kind] += 1
                        if not self.dry_run:
                            rec.update({"tags": sorted(tags - {lab_tag_id})})
                    else:
                        self.deleted[kind] += 1
                        if not self.dry_run:
                            rec.delete()
                except Exception as exc:  # noqa: BLE001 - e.g. a site still used by devices of another lab
                    self.skipped.append(f"stale {kind} {rec.id}: {exc}")

    def result(self) -> dict[str, Any]:
        def clean(d: dict[str, int]) -> dict[str, int]:
            return {k: v for k, v in sorted(d.items()) if v}

        return {"created": clean(self.created), "updated": clean(self.updated), "deleted": clean(self.deleted),
                "unchanged": clean(self.unchanged), "skipped": list(self.skipped), "dry_run": self.dry_run}
