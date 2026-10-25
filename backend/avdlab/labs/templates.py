"""Lab templates: descriptors (`templates/<name>/template.yml`), knob handling and lab rendering.

A template is an AVD example (group_vars + inventory + playbooks) plus a descriptor listing the knobs the UI exposes.
`render_lab()` turns (template, knob values) into the complete set of lab files:

* generator ``l3ls``   – the whole inventory/group_vars for the node types are generated from counts and pools
                         (single-dc-l3ls). Fabric-wide vars come from the template's base FABRIC group.
* generator ``static`` – the example's group_vars are loaded as-is and only targeted edits are applied
                         (mgmt subnet/gateway, password, serials, platform, knobs with an explicit ``path``).

Both generators end with the same *common pass* (passwords, serial numbers, platform, eAPI) so the behaviour is
uniform. Everything here is pure (no DB, no filesystem writes) and therefore unit-testable.
"""
from __future__ import annotations

import copy
import crypt
import hashlib
import ipaddress
import re
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from ..core.config import settings

KnobType = Literal["int", "string", "cidr", "ipv4", "pool", "secret", "object", "yaml", "bool"]

# node type keys we know how to walk for serial numbers / platform (AVD default node_type_keys)
NODE_TYPE_KEYS = ("spine", "l3leaf", "l2leaf", "super_spine", "overlay_controller", "l3spine", "l2spine", "leaf",
                  "wan_router", "wan_rr", "pe", "p", "rr")
BGP_PEER_GROUP_NAMES = {  # eos_designs key -> EOS peer-group name (used as type-7 salt by AVD)
    "ipv4_underlay_peers": "IPv4-UNDERLAY-PEERS",
    "mlag_ipv4_underlay_peer": "MLAG-IPv4-UNDERLAY-PEER",
    "evpn_overlay_peers": "EVPN-OVERLAY-PEERS",
    "evpn_overlay_core": "EVPN-OVERLAY-CORE",
    "mpls_overlay_peers": "MPLS-OVERLAY-PEERS",
    "rr_overlay_peers": "RR-OVERLAY-PEERS",
    "ipvpn_gateway_peers": "IPVPN-GATEWAY-PEERS",
    "wan_overlay_peers": "WAN-OVERLAY-PEERS",
    "wan_rr_overlay_peers": "WAN-RR-OVERLAY-PEERS",  # phase 6: cv-pathfinder
}
# The AVD examples ship this hash for password "arista"; keeping it makes default renders byte-identical.
EXAMPLE_SHA512_ARISTA = (
    "$6$Enl0WfE32FthwyiJ$yTyGaEJ2uPKLU.F7314YtB7J1jrzrMi7ogXIRTEHQfLdLgKWWmr1UvNlZLN6AyuxET7G5aH3AI9OYRzxVTkB1."
)


# ---------------------------------------------------------------------------------------------- descriptors
class Knob(BaseModel):
    key: str
    type: KnobType = "string"
    label: str | None = None
    help: str | None = None
    default: Any = None
    default_from: str | None = Field(default=None, description="'GROUP:dotted.path' into the base group_vars")
    min: int | None = None
    max: int | None = None
    path: str | None = Field(default=None, description="static generator: 'GROUP:dotted.path' to set")
    choices: list[Any] | None = None
    value_type: KnobType | None = Field(default=None, description="object knobs: type every value must satisfy (e.g. pool)")


def _check_pool(raw: str) -> str:
    """AVD ipv4 pool syntax: comma-separated prefixes and/or 'a.b.c.d-e.f.g.h' ranges."""
    parts = [x.strip() for x in str(raw).split(",") if x.strip()]
    if not parts:
        raise ValueError("empty pool")
    for part in parts:
        if "-" in part:
            a, b = (ipaddress.ip_address(x.strip()) for x in part.split("-", 1))
            if b < a:
                raise ValueError(f"range end before start: {part}")
        else:
            ipaddress.ip_network(part, strict=False)
    return ", ".join(parts)


class L3LSLayout(BaseModel):
    """Names used by the l3ls generator (all data, so a second l3ls template can reuse the code)."""

    fabric_group: str = "FABRIC"
    dc_group: str = "{DC}"
    spines_group: str = "{DC}_SPINES"
    l3leaves_group: str = "{DC}_L3_LEAVES"
    l2leaves_group: str = "{DC}_L2_LEAVES"
    services_group: str = "NETWORK_SERVICES"
    endpoints_group: str = "CONNECTED_ENDPOINTS"
    l3_node_group_prefix: str = "{DC}_L3_LEAF"
    l2_node_group_prefix: str = "{DC}_L2_LEAF"
    mgmt_offsets: dict[str, int] = {"spine": 10, "l3leaf": 100, "l2leaf": 150}
    l2leaf_mlag_peer_pool: str = "10.255.2.64/27"


class TemplateDescriptor(BaseModel):
    name: str
    title: str
    description: str = ""
    fabric_name: str = "FABRIC"
    generator: Literal["l3ls", "static", "none"] = "static"  # none = custom lab: files are the source of truth
    base_mgmt_subnet: str | None = Field(default=None, description="static: subnet used by the example's mgmt IPs")
    # phase 6 (A): catalogue metadata
    tags: list[str] = []
    learn: str | None = Field(default=None, description="what the design teaches + the AVD doc URL")
    notes: str | None = Field(default=None, description="lab caveats shown with the template (e.g. cEOS limits)")
    knobs: list[Knob] = []
    l3ls: L3LSLayout = L3LSLayout()
    dir: Path | None = Field(default=None, exclude=True)

    def knob(self, key: str) -> Knob | None:
        return next((k for k in self.knobs if k.key == key), None)

    def public(self) -> dict[str, Any]:
        """Descriptor as served by GET /labs/templates (defaults resolved)."""
        base_gv, _ = load_base(self)
        knobs = []
        for k in self.knobs:
            d = k.model_dump()
            d["default"] = _knob_default(k, base_gv)
            d.pop("default_from", None)
            knobs.append(d)
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "fabric_name": self.fabric_name,
            "generator": self.generator,
            "tags": list(self.tags),
            "node_count": self.node_count(),
            "learn": self.learn,
            "notes": self.notes,
            "knobs": knobs,
        }

    def node_count(self) -> int | None:
        """Switches of a default render (inventory hosts); None for custom labs or when the render fails."""
        if self.generator == "none":
            return None
        try:
            rendered = render_lab(self, None, slug=self.name)
        except Exception:  # noqa: BLE001 - a broken template must not break the catalogue
            return None
        return len(inventory_hosts(rendered.inventory))


def load_templates(templates_dir: Path | None = None) -> dict[str, TemplateDescriptor]:
    root = templates_dir or settings.templates_dir
    out: dict[str, TemplateDescriptor] = {}
    for f in sorted(root.glob("*/template.yml")):
        data = yaml.safe_load(f.read_text()) or {}
        desc = TemplateDescriptor(**data)
        desc.dir = f.parent
        out[desc.name] = desc
    return out


def get_template(name: str, templates_dir: Path | None = None) -> TemplateDescriptor:
    tpls = load_templates(templates_dir)
    if name not in tpls:
        raise KeyError(f"unknown template {name!r}; available: {sorted(tpls)}")
    return tpls[name]


# ---------------------------------------------------------------------------------------------- base files
def load_group_vars_dir(root: Path) -> dict[str, dict[str, Any]]:
    """`group_vars/<G>.yml` or `group_vars/<G>/*.yml` -> {group: merged vars} (files merged in sorted order)."""
    gv_dir = root / "group_vars"
    out: dict[str, dict[str, Any]] = {}
    if not gv_dir.is_dir():
        return out
    for entry in sorted(gv_dir.iterdir()):
        if entry.is_dir():
            data: dict[str, Any] = {}
            for f in sorted(entry.glob("*.y*ml")):
                data.update(yaml.safe_load(f.read_text()) or {})
            out[entry.name] = data
        elif entry.suffix in (".yml", ".yaml"):
            out[entry.stem] = yaml.safe_load(entry.read_text()) or {}
    return out


INVENTORY_NAMES = ("inventory.yml", "inventory.yaml", "hosts.yml")


def find_inventory(root: Path) -> Path | None:
    return next((root / n for n in INVENTORY_NAMES if (root / n).is_file()), None)


def load_inventory(root: Path) -> dict[str, Any]:
    path = find_inventory(root) or root / "inventory.yml"
    inv = yaml.safe_load(path.read_text()) or {}
    if "all" not in inv:  # l2ls-fabric example has no explicit `all:` root
        inv = {"all": {"children": inv}}
    return inv


def load_base(template: TemplateDescriptor) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    assert template.dir is not None
    if template.generator == "none" or find_inventory(template.dir) is None:
        return {}, {"all": {}}
    return load_group_vars_dir(template.dir), load_inventory(template.dir)


def load_playbooks(template: TemplateDescriptor) -> dict[str, str]:
    assert template.dir is not None
    files = {f"playbooks/{f.name}": f.read_text() for f in sorted((template.dir / "playbooks").glob("*.yml"))}
    files.setdefault("playbooks/validate.yml", DEFAULT_VALIDATE_PLAYBOOK)
    return files


DEFAULT_VALIDATE_PLAYBOOK = """---
# validate.yml — ANTA via the arista.avd.anta_runner role (generated by avdlab)
- name: Fabric validation with ANTA
  hosts: FABRIC
  connection: local
  gather_facts: false
  tasks:
    - name: Run ANTA
      ansible.builtin.import_role:
        name: arista.avd.anta_runner
      vars:
        avd_catalogs_extra_fabric_validation: true
        user_catalogs_enabled: true
        anta_report_exclude_statuses: [success, skipped]
        avd_catalogs_filters:
          - skip_tests: [VerifyNTP, VerifyReloadCause]
"""

ANSIBLE_CFG = """[defaults]
inventory=inventory.yml
duplicate_dict_key=error
host_key_checking=False
"""


# ---------------------------------------------------------------------------------------------- knob values
def _dig(data: Any, dotted: str) -> Any:
    cur = data
    for part in dotted.split("."):
        if isinstance(cur, list):
            cur = cur[int(part)]
        elif isinstance(cur, dict):
            cur = cur[part]
        else:
            raise KeyError(dotted)
    return cur


def _set(data: dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    cur: Any = data
    for part in parts[:-1]:
        if isinstance(cur, list):
            cur = cur[int(part)]
        else:
            cur = cur.setdefault(part, {})
    if isinstance(cur, list):
        cur[int(parts[-1])] = value
    else:
        cur[parts[-1]] = value


def _knob_default(knob: Knob, base_gv: dict[str, dict[str, Any]]) -> Any:
    if knob.default_from:
        group, dotted = knob.default_from.split(":", 1)
        try:
            return copy.deepcopy(_dig(base_gv[group], dotted))
        except (KeyError, IndexError):
            return knob.default
    return copy.deepcopy(knob.default)


class KnobError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


def coerce_knobs(template: TemplateDescriptor, values: dict[str, Any] | None) -> dict[str, Any]:
    """Merge user values over defaults, coerce types and range-check. Raises KnobError."""
    base_gv, _ = load_base(template)
    values = values or {}
    out: dict[str, Any] = {}
    errors: list[str] = []
    for knob in template.knobs:
        raw = values.get(knob.key, _knob_default(knob, base_gv))
        try:
            out[knob.key] = _coerce(knob, raw)
        except (ValueError, TypeError) as exc:
            errors.append(f"{knob.key}: {exc}")
    unknown = set(values) - {k.key for k in template.knobs}
    if unknown:
        errors.append(f"unknown knobs: {sorted(unknown)}")
    if errors:
        raise KnobError(errors)
    return out


def _coerce(knob: Knob, raw: Any) -> Any:
    if raw is None:
        return None
    match knob.type:
        case "int":
            v = int(raw)
            if knob.min is not None and v < knob.min:
                raise ValueError(f"must be >= {knob.min}")
            if knob.max is not None and v > knob.max:
                raise ValueError(f"must be <= {knob.max}")
            return v
        case "bool":
            return raw if isinstance(raw, bool) else str(raw).lower() in ("1", "true", "yes", "on")
        case "cidr":
            return str(ipaddress.ip_network(str(raw), strict=False))
        case "ipv4":
            return str(ipaddress.ip_address(str(raw)))
        case "pool":
            return _check_pool(str(raw))
        case "yaml":
            v = yaml.safe_load(raw) if isinstance(raw, str) else raw
            if knob.key == "device_tags":
                return normalize_device_tags(v)
            if knob.key == "segmentation":  # phase 4 (C): MSS foundation
                from .mss import normalize_segmentation

                return normalize_segmentation(v)
            return v
        case "object":
            if isinstance(raw, str):
                raw = yaml.safe_load(raw)
            if not isinstance(raw, dict):
                raise ValueError("must be a mapping")
            merged = copy.deepcopy(knob.default or {})
            merged.update(raw)
            if knob.value_type:
                sub = Knob(key=knob.key, type=knob.value_type)
                for k, v in merged.items():
                    try:
                        merged[k] = _coerce(sub, v)
                    except ValueError as exc:
                        raise ValueError(f"{k}: {exc}") from exc
            if knob.key == "cloudvision":
                return normalize_cloudvision(merged)
            if knob.key == "act":  # phase 4 (backend D): ACT digital twin settings
                return normalize_act(merged)
            if knob.key == "provisioning":  # phase 4 (A): startup-config | ztp
                from ..topology.ztp import normalize_provisioning

                return normalize_provisioning(merged)
            return merged
        case _:
            v = str(raw)
            if knob.choices and v not in [str(c) for c in knob.choices]:
                raise ValueError(f"must be one of {knob.choices}")
            return v


# ---------------------------------------------------------------------------------------------- rendering
@dataclass
class RenderedLab:
    group_vars: dict[str, dict[str, Any]]
    inventory: dict[str, Any]
    extra_files: dict[str, str] = field(default_factory=dict)
    passthrough: bool = False  # custom lab: no files written, no re-validation on knob changes

    def files(self) -> dict[str, str]:
        if self.passthrough:
            return {}
        out = {"inventory.yml": _dump_yaml(self.inventory), "ansible.cfg": ANSIBLE_CFG}
        for group, data in self.group_vars.items():
            out[f"group_vars/{group}.yml"] = _dump_yaml(data)
        out.update(self.extra_files)
        return out

    def hostvars(self) -> dict[str, dict[str, Any]]:
        if self.passthrough:
            return {}
        return resolve_hostvars(self.group_vars, self.inventory)


def _dump_yaml(data: Any) -> str:
    return "---\n" + yaml.safe_dump(data, sort_keys=False, default_flow_style=False, width=120, allow_unicode=True)


def render_lab(template: TemplateDescriptor, knobs: dict[str, Any] | None, *, slug: str = "lab",
               overrides: dict[str, Any] | None = None) -> RenderedLab:
    """Render the full lab from knobs. `slug` feeds the serial-number prefix. `overrides` (phase 5: the lab's
    `settings["topology_overrides"]`, written by the topology editor) is re-applied after every knob render."""
    values = coerce_knobs(template, knobs)
    if template.generator == "none":
        rendered = _passthrough(slug)
        if values.get("device_tags"):  # custom labs: tags are injected after the build (labs/build.py)
            hosts = set(load_lab_hostvars(settings.labs_dir / slug))
            unknown = sorted(set(values["device_tags"]) - hosts)
            if unknown:
                raise KnobError([f"device_tags: unknown hostnames {unknown}"])
        return rendered
    base_gv, base_inv = load_base(template)
    if template.generator == "l3ls":
        group_vars, inventory = _generate_l3ls(template, values, base_gv)
    else:
        group_vars, inventory = copy.deepcopy(base_gv), copy.deepcopy(base_inv)
        _apply_static_edits(template, group_vars, inventory, values)
    _apply_common(template, group_vars, inventory, values, slug)
    apply_device_tags(group_vars, values.get("device_tags"))
    apply_topology_overrides(template, group_vars, overrides)  # phase 5: topology editor
    if (values.get("segmentation") or {}).get("enabled"):  # phase 4 (C): router segment-security on the VRF carriers
        from .mss import SegmentationError, apply_segmentation

        try:
            apply_segmentation(group_vars, inventory, values["segmentation"])
        except SegmentationError as exc:
            raise KnobError([f"segmentation: {exc}"]) from exc
    return RenderedLab(group_vars=group_vars, inventory=inventory, extra_files=load_playbooks(template))


def _passthrough(slug: str) -> RenderedLab:
    """Custom labs: nothing is rendered (knobs only shape the topology). The lab dir must exist (import first)."""
    if not (settings.labs_dir / slug / "group_vars").is_dir():
        raise KnobError(["template: custom labs are created with POST /labs/import (archive) or /labs/import/git"])
    return RenderedLab(group_vars={}, inventory={}, passthrough=True)


# ---- static generator -------------------------------------------------------------------------
def _apply_static_edits(
    template: TemplateDescriptor, gv: dict[str, dict[str, Any]], inv: dict[str, Any], values: dict[str, Any]
) -> None:
    if "mgmt_subnet" in values and template.base_mgmt_subnet:
        old = ipaddress.ip_network(template.base_mgmt_subnet)
        new = ipaddress.ip_network(values["mgmt_subnet"])
        if old != new:
            _remap_mgmt(gv, inv, old, new)
    if values.get("mgmt_gateway"):
        for data in gv.values():
            if "mgmt_gateway" in data:
                data["mgmt_gateway"] = values["mgmt_gateway"]
    if values.get("mgmt_ipv6_subnet"):  # phase 6 (A): optional dual-stack OOB management
        apply_mgmt_ipv6(template, gv, values["mgmt_ipv6_subnet"])
    for knob in template.knobs:
        if knob.path and values.get(knob.key) is not None:
            group, dotted = knob.path.split(":", 1)
            _set(gv.setdefault(group, {}), dotted, values[knob.key])


def apply_mgmt_ipv6(template: TemplateDescriptor, gv: dict[str, dict[str, Any]], subnet: str) -> None:
    """`ipv6_mgmt_ip` on every node = `subnet` + the host bits of its IPv4 `mgmt_ip` (offset within its IPv4 network),
    `ipv6_mgmt_gateway` = the subnet's first host on the fabric group. containerlab picks it up as `mgmt-ipv6`."""
    net = ipaddress.ip_network(subnet, strict=False)
    if net.version != 6:
        raise KnobError([f"mgmt_ipv6_subnet: {subnet} is not an IPv6 prefix"])
    for _g, _t, node in iter_nodes(gv):
        v4 = node.get("mgmt_ip")
        if not v4:
            continue
        iface = ipaddress.ip_interface(str(v4))
        offset = int(iface.ip) - int(iface.network.network_address)
        node["ipv6_mgmt_ip"] = f"{ipaddress.ip_address(int(net.network_address) + offset)}/{net.prefixlen}"
    gv.setdefault(fabric_group_name(template), {})["ipv6_mgmt_gateway"] = str(ipaddress.ip_address(int(net.network_address) + 1))


def _remap_ip(ip: str, old: ipaddress.IPv4Network, new: ipaddress.IPv4Network) -> str:
    addr, _, plen = ip.partition("/")
    a = ipaddress.ip_address(addr)
    if a not in old:
        return ip
    offset = int(a) - int(old.network_address)
    n = ipaddress.ip_address(int(new.network_address) + offset)
    return f"{n}/{new.prefixlen}" if plen else str(n)


def _remap_mgmt(gv: dict[str, dict[str, Any]], inv: dict[str, Any], old: Any, new: Any) -> None:
    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if k in ("mgmt_ip", "ansible_host", "mgmt_gateway") and isinstance(v, str):
                    node[k] = _remap_ip(v, old, new)
                else:
                    walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(gv)
    walk(inv)


# ---- l3ls generator ---------------------------------------------------------------------------
def _fmt(name: str, dc: str) -> str:
    return name.replace("{DC}", dc.upper())


def _generate_l3ls(
    template: TemplateDescriptor, v: dict[str, Any], base_gv: dict[str, dict[str, Any]]
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    lay = template.l3ls
    dc = str(v.get("dc_name", "dc1")).lower()
    spines = int(v["spine_count"])
    pairs = int(v["leaf_pairs"])
    l2_per_pair = int(v.get("l2leaf_per_pair", 1))
    pools: dict[str, str] = v["pools"]
    subnet = ipaddress.ip_network(v["mgmt_subnet"])
    plen = subnet.prefixlen

    def mgmt(offset: int, n: int) -> str:
        return f"{ipaddress.ip_address(int(subnet.network_address) + offset + n)}/{plen}"

    g_fabric, g_dc = lay.fabric_group, _fmt(lay.dc_group, dc)
    g_sp, g_l3, g_l2 = _fmt(lay.spines_group, dc), _fmt(lay.l3leaves_group, dc), _fmt(lay.l2leaves_group, dc)
    g_svc, g_ep = lay.services_group, lay.endpoints_group

    # ---- interfaces layout: uplinks first, then MLAG, downlinks to L2 leaves from Ethernet8 (as in the example)
    n_uplinks = max(2, spines)
    mlag_first = n_uplinks + 1
    leaf_downlink_first = max(8, mlag_first + 2)
    spine_downlinks = max(8, 2 * pairs)
    default_interfaces: list[dict[str, Any]] = [
        {"types": ["spine"], "platforms": ["default"], "uplink_interfaces": ["Ethernet1-2"],
         "downlink_interfaces": [f"Ethernet1-{spine_downlinks}"]},
        {"types": ["l3leaf"], "platforms": ["default"], "uplink_interfaces": [f"Ethernet1-{n_uplinks}"],
         "mlag_interfaces": [f"Ethernet{mlag_first}-{mlag_first + 1}"],
         "downlink_interfaces": [_range("Ethernet", leaf_downlink_first, leaf_downlink_first + max(1, l2_per_pair) - 1)]},
        {"types": ["l2leaf"], "platforms": ["default"], "uplink_interfaces": ["Ethernet1-2"]},
    ]
    if l2_per_pair == 2:
        default_interfaces[2]["mlag_interfaces"] = ["Ethernet3-4"]

    # ---- FABRIC: base vars + regenerated default_interfaces
    fabric = copy.deepcopy(base_gv.get(g_fabric, {}))
    fabric["fabric_name"] = template.fabric_name
    fabric["default_interfaces"] = default_interfaces
    fabric.setdefault("eos_designs_documentation", {})["topology_csv"] = True
    fabric["eos_designs_documentation"]["p2p_links_csv"] = True

    dc_vars = copy.deepcopy(base_gv.get(g_dc, {}))
    dc_vars["mgmt_gateway"] = v["mgmt_gateway"]
    dc_vars["management_eapi"] = {"enabled": True}

    # ---- spines
    spine_names = [f"{dc}-spine{n}" for n in range(1, spines + 1)]
    spine_vars = {
        "type": "spine",
        "spine": {
            "defaults": {"platform": "cEOSLab", "loopback_ipv4_pool": pools["loopback"], "bgp_as": int(v["spine_asn"])},
            "nodes": [
                {"name": name, "id": n, "mgmt_ip": mgmt(lay.mgmt_offsets["spine"], n)}
                for n, name in enumerate(spine_names, start=1)
            ],
        },
    }

    # ---- l3 leaves (MLAG pairs)
    l3_groups: list[dict[str, Any]] = []
    l3_hosts: dict[str, str] = {}
    for p in range(1, pairs + 1):
        nodes = []
        for letter, k in (("a", 1), ("b", 2)):
            node_id = (p - 1) * 2 + k
            name = f"{dc}-leaf{p}{letter}"
            ip = mgmt(lay.mgmt_offsets["l3leaf"], node_id)
            nodes.append({"name": name, "id": node_id, "mgmt_ip": ip})
            l3_hosts[name] = ip.split("/")[0]
        l3_groups.append({"group": f"{_fmt(lay.l3_node_group_prefix, dc)}{p}", "bgp_as": int(v["leaf_asn_base"]) + p - 1,
                          "nodes": nodes})
    l3_vars = {
        "type": "l3leaf",
        "l3leaf": {
            "defaults": {
                "platform": "cEOSLab",
                "loopback_ipv4_pool": pools["loopback"],
                "loopback_ipv4_offset": spines,
                "vtep_loopback_ipv4_pool": pools["vtep"],
                "uplink_switches": spine_names,
                "uplink_ipv4_pool": pools["uplink"],
                "mlag_peer_ipv4_pool": pools["mlag_peer"],
                "mlag_peer_l3_ipv4_pool": pools["mlag_l3"],
                "virtual_router_mac_address": "00:1c:73:00:00:99",
                "spanning_tree_priority": 4096,
                "spanning_tree_mode": "mstp",
            },
            "node_groups": l3_groups,
        },
    }

    # ---- l2 leaves
    l2_groups: list[dict[str, Any]] = []
    l2_hosts: dict[str, str] = {}
    l2_index = 0
    for p in range(1, pairs + 1):
        if l2_per_pair == 0:
            break
        nodes = []
        for k in range(1, l2_per_pair + 1):
            l2_index += 1
            name = f"{dc}-leaf{p}{'cd'[k - 1]}"
            ip = mgmt(lay.mgmt_offsets["l2leaf"], l2_index)
            nodes.append({"name": name, "id": k, "mgmt_ip": ip})
            l2_hosts[name] = ip.split("/")[0]
        l2_groups.append({"group": f"{_fmt(lay.l2_node_group_prefix, dc)}{p}",
                          "uplink_switches": [f"{dc}-leaf{p}a", f"{dc}-leaf{p}b"], "nodes": nodes})
    l2_defaults: dict[str, Any] = {"platform": "cEOSLab", "spanning_tree_mode": "mstp"}
    if l2_per_pair == 2:
        l2_defaults["mlag_peer_ipv4_pool"] = lay.l2leaf_mlag_peer_pool
    l2_vars = {"type": "l2leaf", "l2leaf": {"defaults": l2_defaults, "node_groups": l2_groups}}

    # ---- services / endpoints
    svc = copy.deepcopy(base_gv.get(g_svc, {}))
    svc["tenants"] = v["tenants"] or []
    ep = copy.deepcopy(base_gv.get(g_ep, {}))
    ep["servers"] = shift_endpoint_ports(v["servers"] or [], n_uplinks)

    group_vars: dict[str, dict[str, Any]] = {g_fabric: fabric, g_dc: dc_vars, g_sp: spine_vars, g_l3: l3_vars}
    dc_children: dict[str, Any] = {
        g_sp: {"hosts": {n: {"ansible_host": mgmt(lay.mgmt_offsets["spine"], i).split("/")[0]}
                         for i, n in enumerate(spine_names, start=1)}},
        g_l3: {"hosts": {n: {"ansible_host": ip} for n, ip in l3_hosts.items()}},
    }
    leaf_groups: dict[str, Any] = {g_l3: None}
    if l2_per_pair:
        group_vars[g_l2] = l2_vars
        dc_children[g_l2] = {"hosts": {n: {"ansible_host": ip} for n, ip in l2_hosts.items()}}
        leaf_groups[g_l2] = None
    group_vars[g_svc] = svc
    group_vars[g_ep] = ep
    inventory = {
        "all": {
            "children": {
                g_fabric: {"children": {g_dc: {"children": dc_children}}},
                g_svc: {"children": dict(leaf_groups)},
                g_ep: {"children": dict(leaf_groups)},
            }
        }
    }
    return group_vars, inventory


def shift_endpoint_ports(servers: list[Any], n_uplinks: int) -> list[Any]:
    """0.10.0: with more than 2 spines the leaf uplinks grow to Ethernet1-<n> and the MLAG peer-link slides up with them,
    so endpoint ports that were valid for 2 spines (Ethernet3..6) would collide. Shift those ports by the extra uplinks
    (e.g. 4 spines: Ethernet5 -> Ethernet7). Ports above the peer-link and ports below Ethernet3 are left alone."""
    delta = max(0, int(n_uplinks) - 2)
    if delta == 0:
        return servers
    mlag_last = n_uplinks + 2
    pat = re.compile(r"^(Ethernet)(\d+)$")

    def shift(port: Any) -> Any:
        m = pat.match(str(port))
        if not m:
            return port
        n = int(m.group(2))
        return f"{m.group(1)}{n + delta}" if 3 <= n <= mlag_last else port

    out = copy.deepcopy(servers)
    for srv in out:
        for ad in (srv.get("adapters") or []) if isinstance(srv, dict) else []:
            if isinstance(ad, dict) and isinstance(ad.get("switch_ports"), list):
                ad["switch_ports"] = [shift(p) for p in ad["switch_ports"]]
    return out


def _range(prefix: str, first: int, last: int) -> str:
    return f"{prefix}{first}" if last <= first else f"{prefix}{first}-{last}"


# ---- common pass ------------------------------------------------------------------------------
def sha512_password(password: str) -> str:
    if password == "arista":
        return EXAMPLE_SHA512_ARISTA
    salt = "$6$" + hashlib.sha256(f"avdlab:{password}".encode()).hexdigest()[:16]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return crypt.crypt(password, salt)


def bgp_type7(password: str, peer_group: str) -> str:
    from pyavd._utils.password_utils.password import bgp_encrypt  # deterministic, same as arista.avd.encrypt

    return bgp_encrypt(password, key=peer_group)


def _apply_common(
    template: TemplateDescriptor, gv: dict[str, dict[str, Any]], inv: dict[str, Any], v: dict[str, Any], slug: str
) -> None:
    password = v.get("device_password")
    for data in gv.values():
        # platform / eAPI on every node type default
        for key in NODE_TYPE_KEYS:
            if isinstance(data.get(key), dict):
                data[key].setdefault("defaults", {})["platform"] = "cEOSLab"
        # passwords
        if password:
            if "ansible_password" in data:
                data["ansible_password"] = password
            for user in (data.get("aaa_settings") or {}).get("local_users", []) or []:
                if user.get("name") == data.get("ansible_user", "arista") or "sha512_password" in user:
                    if not user.get("no_password"):
                        user.pop("cleartext_password", None)
                        user["sha512_password"] = sha512_password(password)
            for pg_key, pg in (data.get("bgp_peer_groups") or {}).items():
                if isinstance(pg, dict) and ("password" in pg or "cleartext_password" in pg):
                    pg.pop("cleartext_password", None)
                    pg["password"] = bgp_type7(password, pg.get("name") or BGP_PEER_GROUP_NAMES.get(pg_key, pg_key))
    # serial numbers per node, in inventory order
    scheme = v.get("serial_scheme") or "{PREFIX}{ID:04d}"
    assign_serials(gv, inv, scheme, slug)
    # DNS servers (list of IPs) → dns_settings.servers on the fabric group
    if v.get("dns_servers"):
        ips = v["dns_servers"] if isinstance(v["dns_servers"], list) else [x.strip() for x in str(v["dns_servers"]).split(",") if x.strip()]
        for ip in ips:
            ipaddress.ip_address(str(ip))
        gv.setdefault(fabric_group_name(template), {})["dns_settings"] = {"servers": [{"ip_address": str(ip)} for ip in ips]}
    # CloudVision streaming (TerminAttr) + tags
    apply_cloudvision(template, gv, v.get("cloudvision"))


# ---- phase 5: topology editor overrides ------------------------------------------------------------
# AVD connected_endpoints_keys (defaults) — endpoint list keys the topology editor can add to
CONNECTED_ENDPOINT_KEYS = ("servers", "firewalls", "routers", "load_balancers", "storage_arrays", "cpes", "workstations",
                           "access_points", "phones", "printers", "cameras", "generic_devices")
NODE_OVERRIDE_KEYS = ("uplink_switches", "uplink_switch_interfaces", "uplink_interfaces")


def apply_topology_overrides(template: TemplateDescriptor, gv: dict[str, dict[str, Any]],
                             overrides: dict[str, Any] | None) -> list[str]:
    """Apply `{nodes: {host: {uplink_switches, uplink_switch_interfaces}}, endpoints: {group: {key: [..]}}}` to
    rendered group_vars. Node keys go on the node entry (node level wins over node_group/defaults in eos_designs); a
    node override is skipped (warning returned) when the node or one of its uplink switches no longer exists (e.g.
    after leaf_pairs/spine_count was lowered). Endpoint lists replace the `key` list of that group."""
    warnings_out: list[str] = []
    if not overrides:
        return warnings_out
    nodes = {n.get("name"): n for _g, _t, n in iter_nodes(gv) if n.get("name")}
    for host, keys in (overrides.get("nodes") or {}).items():
        node = nodes.get(host)
        if node is None:
            warnings_out.append(f"override for {host} skipped: node no longer exists")
            continue
        missing = [s for s in (keys.get("uplink_switches") or []) if s not in nodes]
        if missing:
            warnings_out.append(f"override for {host} skipped: uplink switch(es) {missing} no longer exist")
            continue
        for k, v in keys.items():
            if k in NODE_OVERRIDE_KEYS:
                node[k] = copy.deepcopy(v)
    for group, lists in (overrides.get("endpoints") or {}).items():
        for key, items in (lists or {}).items():
            gv.setdefault(group, {})[key] = copy.deepcopy(items)
    return warnings_out


def endpoint_target_group(template: TemplateDescriptor | None, gv: dict[str, dict[str, Any]], key: str,
                          fabric_name: str | None = None) -> str:
    """Group a new `key` endpoint goes to: first group already holding `key`, else the template's endpoints group,
    else any group holding connected endpoints, else the fabric group."""
    holder = next((g for g, d in gv.items() if isinstance(d, dict) and isinstance(d.get(key), list)), None)
    if holder:
        return holder
    if template is not None and template.generator == "l3ls" and template.l3ls.endpoints_group in gv:
        return template.l3ls.endpoints_group
    holder = next((g for g, d in gv.items() if isinstance(d, dict) and any(k in d for k in CONNECTED_ENDPOINT_KEYS)), None)
    if holder:
        return holder
    if template is not None and template.generator != "none":
        return fabric_group_name(template)
    return fabric_name or next(iter(gv), "FABRIC")


# ---- CloudVision knob ---------------------------------------------------------------------------
CV_MODES = ("none", "cvaas", "onprem")
# eos_designs cv_settings.cvaas.clusters[].region (pyavd 6.4 schema)
CV_REGIONS = ("auto", "us-central1-a", "us-central1-b", "us-central1-c", "us-4", "apnortheast-1", "euwest-2", "eu-3",
              "ausoutheast-1", "na-northeast1-b", "uk-1", "india-1", "staging", "dev", "play")
CV_KNOB_KEYS = ("mode", "region", "servers", "disable_aaa", "topology_hints", "device_tags")
CV_ONPREM_PORT = 9910
CV_DEFAULT_DNS = ("1.1.1.1", "8.8.8.8")  # only added when the template has no dns_settings
CV_TOKEN_FILES = {"cvaas": "/tmp/cv-onboarding-token", "onprem": "/tmp/token"}  # AVD defaults


def _as_bool(raw: Any) -> bool:
    return raw if isinstance(raw, bool) else str(raw).lower() in ("1", "true", "yes", "on")


def normalize_cloudvision(raw: dict[str, Any]) -> dict[str, Any]:
    """Validate/canonicalise the `cloudvision` knob:
    `{mode: none|cvaas|onprem, region, servers: [host[:port]], disable_aaa, topology_hints, device_tags: {label: value}}`."""
    unknown = sorted(set(raw) - set(CV_KNOB_KEYS))
    if unknown:
        raise ValueError(f"unknown keys {unknown}; allowed: {list(CV_KNOB_KEYS)}")
    mode = str(raw.get("mode") or "none").lower()
    if mode not in CV_MODES:
        raise ValueError(f"mode must be one of {list(CV_MODES)}")
    region = str(raw.get("region") or "auto")
    if region not in CV_REGIONS:
        raise ValueError(f"region must be one of {list(CV_REGIONS)}")
    servers_raw = raw.get("servers") or []
    if isinstance(servers_raw, str):
        servers_raw = servers_raw.replace("\n", ",").split(",")
    if not isinstance(servers_raw, list):
        raise ValueError("servers must be a list of IPs/hostnames")
    servers = [str(s).strip() for s in servers_raw if str(s).strip()]
    for s in servers:
        host, _, port = s.rpartition(":") if s.count(":") == 1 else (s, "", "")
        if port and not port.isdigit():
            raise ValueError(f"servers: invalid port in {s!r}")
        if not host or " " in host:
            raise ValueError(f"servers: invalid server {s!r}")
    if mode == "onprem" and not servers:
        raise ValueError("servers: at least one CloudVision server is required for mode onprem")
    tags_raw = raw.get("device_tags") or {}
    if isinstance(tags_raw, list):  # tolerate the AVD shape [{name, value}]
        try:
            tags_raw = {str(t["name"]): t.get("value") for t in tags_raw}
        except (TypeError, KeyError) as exc:
            raise ValueError("device_tags must be a {label: value} mapping") from exc
    if not isinstance(tags_raw, dict):
        raise ValueError("device_tags must be a {label: value} mapping")
    tags: dict[str, str] = {}
    for k, val in tags_raw.items():
        if not str(k).strip() or val is None or str(val) == "":
            raise ValueError(f"device_tags: empty label or value ({k!r}: {val!r})")
        tags[str(k).strip()] = str(val)
    return {"mode": mode, "region": region, "servers": servers, "disable_aaa": _as_bool(raw.get("disable_aaa", True)),
            "topology_hints": _as_bool(raw.get("topology_hints", True)), "device_tags": tags}


def _cv_server_entry(server: str) -> dict[str, Any]:
    if server.count(":") == 1:
        host, port = server.split(":")
        return {"name": host, "port": int(port)} if int(port) != CV_ONPREM_PORT else {"name": host}
    return {"name": server}


def fabric_group_name(template: TemplateDescriptor) -> str:
    return template.l3ls.fabric_group if template.generator == "l3ls" else template.fabric_name


def apply_cloudvision(template: TemplateDescriptor, gv: dict[str, dict[str, Any]], cv: dict[str, Any] | None) -> None:
    """Render the knob into the fabric group: `cv_settings` (→ daemon TerminAttr) + `generate_cv_tags`.
    Mode `none` removes any cv_settings/generate_cv_tags the template ships (the AVD examples point TerminAttr at a
    placeholder CVP), so a lab without CloudVision renders no TerminAttr daemon."""
    data = gv.setdefault(fabric_group_name(template), {})
    if not cv or cv.get("mode", "none") == "none":
        data.pop("cv_settings", None)
        data.pop("generate_cv_tags", None)
        return
    old = data.get("cv_settings") if isinstance(data.get("cv_settings"), dict) else {}
    terminattr = {**(old.get("terminattr") or {}), "disable_aaa": bool(cv.get("disable_aaa", True))}
    if cv["mode"] == "cvaas":
        cv_settings: dict[str, Any] = {"cvaas": {"enabled": True, "clusters": [
            {"name": "cvaas", "region": cv.get("region") or "auto", "vrf": "use_mgmt_interface_vrf"}]}}
    else:
        cv_settings = {"onprem_clusters": [{"name": "primary", "servers": [_cv_server_entry(s) for s in cv["servers"]],
                                            "vrf": "use_mgmt_interface_vrf"}]}
    cv_settings["terminattr"] = terminattr
    data["cv_settings"] = cv_settings
    data["generate_cv_tags"] = {"topology_hints": bool(cv.get("topology_hints", True)),
                                "device_tags": [{"name": k, "value": v} for k, v in (cv.get("device_tags") or {}).items()]}
    # eos_designs refuses CloudVision clusters without NTP servers: add one when the template has none
    if not any(((d.get("ntp_settings") or {}).get("servers")) for d in gv.values() if isinstance(d, dict)):
        data["ntp_settings"] = {"server_vrf": "use_mgmt_interface_vrf", "servers": [{"name": "pool.ntp.org"}]}
    # ... and CVaaS (or server names) without DNS servers
    if not any(((d.get("dns_settings") or {}).get("servers")) for d in gv.values() if isinstance(d, dict)):
        data["dns_settings"] = {"servers": [{"ip_address": ip, "vrf": "use_mgmt_interface_vrf"} for ip in CV_DEFAULT_DNS]}


# ---- ACT knob (phase 4, backend D) ---------------------------------------------------------------
ACT_KNOB_KEYS = ("os_version", "username", "password", "internet_access", "tools_server_ip")
ACT_KNOB_DEFAULT = {"os_version": "4.33.1F", "username": "cvpadmin", "password": "cvp123!", "internet_access": True,
                    "tools_server_ip": ""}


def normalize_act(raw: dict[str, Any]) -> dict[str, Any]:
    """Validate/canonicalise the `act` knob (used only when the lab target is `act`):
    `{os_version, username, password, internet_access, tools_server_ip}`; empty tools_server_ip = auto-assign."""
    unknown = sorted(set(raw) - set(ACT_KNOB_KEYS))
    if unknown:
        raise ValueError(f"unknown keys {unknown}; allowed: {list(ACT_KNOB_KEYS)}")
    out = {**ACT_KNOB_DEFAULT, **{k: v for k, v in raw.items() if v is not None}}
    for key in ("os_version", "username", "password"):
        out[key] = str(out[key]).strip()
        if not out[key] or any(ch.isspace() for ch in out[key]):
            raise ValueError(f"{key} must be a non-empty string without spaces")
    out["internet_access"] = _as_bool(out["internet_access"])
    ip = str(out.get("tools_server_ip") or "").strip()
    if ip:
        ip = str(ipaddress.ip_interface(ip).ip)  # tolerate a /len suffix
    out["tools_server_ip"] = ip
    return out


# ---- per-device CloudVision tags (knob `device_tags`) ------------------------------------------------
def normalize_device_tags(raw: Any) -> dict[str, dict[str, str]]:
    """`{hostname: {label: value}}` (values stringified, empty labels/values rejected); hostnames are checked
    against the rendered nodes by `apply_device_tags`."""
    if raw is None or raw == "":
        return {}
    if not isinstance(raw, dict):
        raise ValueError("must be a mapping {hostname: {label: value}}")
    out: dict[str, dict[str, str]] = {}
    for host, tags in raw.items():
        if tags is None:
            continue
        if isinstance(tags, list):  # tolerate the AVD shape [{name, value}]
            try:
                tags = {str(t["name"]): t.get("value") for t in tags}
            except (TypeError, KeyError) as exc:
                raise ValueError(f"{host}: tags must be a {{label: value}} mapping") from exc
        if not isinstance(tags, dict):
            raise ValueError(f"{host}: tags must be a {{label: value}} mapping")
        clean: dict[str, str] = {}
        for label, val in tags.items():
            if not str(label).strip() or val is None or str(val) == "":
                raise ValueError(f"{host}: empty label or value ({label!r}: {val!r})")
            clean[str(label).strip()] = str(val)
        if clean:
            out[str(host)] = clean
    return out


def _cv_tag_list(tags: dict[str, str]) -> list[dict[str, str]]:
    return [{"name": k, "value": v} for k, v in tags.items()]


def apply_device_tags(gv: dict[str, dict[str, Any]], device_tags: dict[str, dict[str, str]] | None) -> None:
    """Render per-device tags into each node's `structured_config.metadata.cv_tags.device_tags` (eos_designs merges
    node structured_config last, so they are appended after the fabric-wide `generate_cv_tags` tags; duplicate
    labels are collapsed — last value wins — by `dedupe_cv_tags` in the build). Unknown hostnames → KnobError."""
    if not device_tags:
        return
    nodes = {n.get("name"): n for _g, _t, n in iter_nodes(gv) if n.get("name")}
    unknown = sorted(set(device_tags) - set(nodes))
    if unknown:
        raise KnobError([f"device_tags: unknown hostnames {unknown}"])
    for host, tags in device_tags.items():
        sc = nodes[host].setdefault("structured_config", {})
        cv_tags = sc.setdefault("metadata", {}).setdefault("cv_tags", {})
        existing = {t["name"]: t.get("value") for t in cv_tags.get("device_tags") or [] if isinstance(t, dict) and "name" in t}
        existing.update(tags)
        cv_tags["device_tags"] = _cv_tag_list(existing)


def dedupe_cv_tags(structured_config: dict[str, Any], extra: dict[str, str] | None = None) -> bool:
    """Collapse duplicate labels in `metadata.cv_tags.device_tags` (last value wins, first position kept) and merge
    `extra` tags over them (custom labs: post-build injection). Returns True when the structure changed."""
    meta = structured_config.get("metadata")
    tags = ((meta or {}).get("cv_tags") or {}).get("device_tags")
    if not tags and not extra:
        return False
    merged: dict[str, Any] = {}
    for t in tags or []:
        if isinstance(t, dict) and "name" in t:
            merged[t["name"]] = t.get("value")
    merged.update(extra or {})
    new = _cv_tag_list(merged)
    if new == tags:
        return False
    structured_config.setdefault("metadata", {}).setdefault("cv_tags", {})["device_tags"] = new
    return True


def cv_token_file(group_vars: dict[str, dict[str, Any]]) -> tuple[str | None, str | None]:
    """(kind, token_file) of the first CloudVision cluster found in (merged) group_vars; (None, None) without cv_settings."""
    for data in group_vars.values():
        cvs = data.get("cv_settings") if isinstance(data, dict) else None
        if not isinstance(cvs, dict):
            continue
        cvaas = cvs.get("cvaas") or {}
        if cvaas.get("enabled", True) and cvaas.get("clusters"):
            return "cvaas", str(cvaas["clusters"][0].get("token_file") or CV_TOKEN_FILES["cvaas"])
        if cvs.get("onprem_clusters"):
            return "onprem", str(cvs["onprem_clusters"][0].get("token_file") or CV_TOKEN_FILES["onprem"])
    return None, None


def serial_for(scheme: str, *, slug: str, index: int, host: str, node_type: str) -> str:
    prefix = re.sub(r"[^A-Z0-9]", "", slug.upper())[:4].ljust(4, "X")
    return scheme.format(PREFIX=prefix, ID=index, HOST=host, TYPE=node_type.upper(), SLUG=slug)


def iter_nodes(gv: dict[str, dict[str, Any]]) -> list[tuple[str, str, dict[str, Any]]]:
    """Yield (group, node_type, node_dict) for every node defined in group_vars."""
    out: list[tuple[str, str, dict[str, Any]]] = []
    for group, data in gv.items():
        for key in NODE_TYPE_KEYS:
            block = data.get(key)
            if not isinstance(block, dict):
                continue
            for node in block.get("nodes") or []:
                out.append((group, key, node))
            for ng in block.get("node_groups") or []:
                for node in ng.get("nodes") or []:
                    out.append((group, key, node))
    return out


def inventory_hosts(inv: dict[str, Any]) -> list[str]:
    hosts: list[str] = []

    def walk(node: dict[str, Any] | None) -> None:
        node = node or {}
        for h in (node.get("hosts") or {}):
            if h not in hosts:
                hosts.append(h)
        for child in (node.get("children") or {}).values():
            walk(child)

    walk(inv.get("all"))
    return hosts


def assign_serials(gv: dict[str, dict[str, Any]], inv: dict[str, Any], scheme: str, slug: str) -> dict[str, str]:
    order = {h: i for i, h in enumerate(inventory_hosts(inv), start=1)}
    serials: dict[str, str] = {}
    for _group, node_type, node in iter_nodes(gv):
        name = node.get("name")
        if not name:
            continue
        idx = order.get(name, len(order) + len(serials) + 1)
        node["serial_number"] = serial_for(scheme, slug=slug, index=idx, host=name, node_type=node_type)
        serials[name] = node["serial_number"]
    return serials


def serials_from_group_vars(gv: dict[str, dict[str, Any]]) -> dict[str, str]:
    return {n["name"]: n["serial_number"] for _g, _t, n in iter_nodes(gv) if n.get("serial_number") and n.get("name")}


# ---------------------------------------------------------------------------------------------- ansible-like resolver
def resolve_hostvars(group_vars: dict[str, dict[str, Any]], inventory: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Merge vars like Ansible: every ancestor group (parents before children, by depth), then host vars."""
    group_hosts: dict[str, dict[str, Any]] = {}
    group_children: dict[str, set[str]] = {}

    def collect(name: str, node: dict[str, Any] | None) -> None:
        node = node or {}
        group_hosts.setdefault(name, {}).update(node.get("hosts") or {})
        group_children.setdefault(name, set()).update((node.get("children") or {}).keys())
        for child, cnode in (node.get("children") or {}).items():
            collect(child, cnode)

    collect("all", inventory.get("all") or {})

    def parents(g: str) -> set[str]:
        return {p for p, ch in group_children.items() if g in ch}

    def depth(g: str) -> int:
        return 0 if not parents(g) else 1 + max(depth(p) for p in parents(g))

    def ancestors(g: str) -> set[str]:
        return parents(g) | {a for p in parents(g) for a in ancestors(p)}

    hostvars: dict[str, dict[str, Any]] = {}
    for g, hosts in group_hosts.items():
        for host, hv in hosts.items():
            merged: dict[str, Any] = {}
            for grp in sorted(ancestors(g) | {g}, key=lambda x: (depth(x), x)):
                merged.update(copy.deepcopy(group_vars.get(grp, {})))
            merged.update(hv or {})
            hostvars.setdefault(host, {}).update(merged)
    return hostvars


def load_lab_hostvars(lab_dir: Path) -> dict[str, dict[str, Any]]:
    return resolve_hostvars(load_group_vars_dir(lab_dir), load_inventory(lab_dir))


# ---------------------------------------------------------------------------------------------- validation
def validate_hostvars(hostvars: dict[str, dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """pyavd.validate_inputs per host. Returns (validated hostvars, violations[{host, message}])."""
    from pyavd import validate_inputs

    validated: dict[str, dict[str, Any]] = {}
    violations: list[dict[str, Any]] = []
    for host, hv in hostvars.items():
        try:
            res = validate_inputs(hv)
        except Exception as exc:  # noqa: BLE001 - pyavd raises AristaAvdError for some structural issues
            violations.append({"host": host, "message": str(exc)})
            continue
        vr = res.validation_result
        if vr.violations or res.validated_data is None:
            for viol in vr.violations:
                violations.append({"host": host, "path": ".".join(map(str, viol.path)), "message": viol.message})
            if not vr.violations:
                violations.append({"host": host, "path": "", "message": "validation failed"})
        else:
            validated[host] = res.validated_data
    return validated, violations
