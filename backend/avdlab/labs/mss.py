"""Module 4 — MSS foundation: knob `segmentation` → EOS `router segment-security` (+ application traffic recognition,
prefix match-lists), rendered per node, plus the policy matrix used by `GET /labs/{id}/mss` and `lab.mss_verify`.

Knob shape (normalized by `normalize_segmentation`)::

    enabled: bool
    applications: [{name, protocol: tcp|udp|icmp|any, ports: "80,443"|"8000-8080"|null}]
    policies: [{name, rules: [{application, action: forward|drop, log: bool}]}]
    vrfs: [{name, segments: [{name, prefixes: [cidr], interfaces: [str], policies: [{from, policy}], fallback: forward|drop|null}]}]

Rendering (templated labs): the structured config is merged into the node-level `structured_config` of every node
that carries one of the VRFs (L3 network-service nodes whose tenants put an SVI / L3 interface of that VRF on them —
same approach as the per-device CloudVision tags), so spines and L2 leafs stay untouched. Only the VRFs a node carries
are rendered on it. Custom labs (no rendered group_vars) get the same structured config injected after
`get_device_structured_config` in `labs/build.py`, on hosts whose structured config has the VRF.

EOS mapping per node:
* application (tcp/udp with ports) → `application_traffic_recognition.field_sets.l4_ports[MSS-<APP>]` +
  `applications.l4_applications[{name, protocols: [proto], <proto>_dest_port_set_name}]`; icmp / portless tcp/udp →
  an l4 application with just the protocol; protocol `any` → the built-in `app-match-all` (no ATR entry).
* policy → `router_segment_security.policies[{name, sequence_numbers: [{sequence: 10*i, application, action, log,
  stateless: true}]}]` (only the policies referenced by the node's segments, plus rules' applications).
* segment prefixes → `match_list_input.prefix_ipv4[MSS-<VRF>-<SEGMENT>]` referenced as `match_lists[{ipv4, prefix}]`;
  interfaces → `definition.interfaces`; `from`/`policy` → `policies`; fallback forward|drop →
  `fallback_policy: policy-forward-all|policy-drop-all`.

Matrix semantics (`policy_matrix`): `matrix[vrf][src_segment][dst_segment]` = the policy that governs traffic from
`src` into `dst` (the dst segment's explicit `from src`, else its `from forwarding-segments`, else its fallback, else
null = unspecified). `decide()` evaluates a policy for an application: first rule whose application matches (or is
`app-match-all`/protocol any) wins; a policy with no matching rule → drop (assumption: EOS implicit deny); the
built-ins `policy-forward-all` / `policy-drop-all` are honoured. Only the forward direction is modelled (segment
security is stateless on cEOS: return traffic is governed by the reverse segment pair).
"""
from __future__ import annotations

import copy
import ipaddress
import re
from typing import Any

BUILTIN_POLICIES = {"policy-forward-all": "forward", "policy-drop-all": "drop"}
BUILTIN_APP = "app-match-all"
FORWARDING_SEGMENTS = "forwarding-segments"
PROTOCOLS = ("tcp", "udp", "icmp", "any")
ACTIONS = ("forward", "drop")
# node types (AVD defaults) that realise L3 network services
L3_NODE_TYPES = ("l3leaf", "l3spine", "wan_router", "wan_rr", "pe", "leaf")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
PORTS_RE = re.compile(r"^\d{1,5}(-\d{1,5})?$")
EMPTY: dict[str, Any] = {"enabled": False, "applications": [], "policies": [], "vrfs": []}


class SegmentationError(ValueError):
    pass


# ------------------------------------------------------------------------------------------------ knob
def _name(raw: Any, what: str) -> str:
    v = str(raw or "").strip()
    if not NAME_RE.match(v):
        raise SegmentationError(f"{what}: invalid name {raw!r} (letters, digits, _ . : -; max 64)")
    return v


def _bool(raw: Any) -> bool:
    return raw if isinstance(raw, bool) else str(raw).strip().lower() in ("1", "true", "yes", "on")


def _ports(raw: Any, app: str) -> str | None:
    if raw is None or str(raw).strip() == "":
        return None
    parts = [p.strip() for p in str(raw).split(",") if p.strip()]
    for p in parts:
        if not PORTS_RE.match(p) or any(not 0 <= int(x) <= 65535 for x in p.split("-")):
            raise SegmentationError(f"applications.{app}.ports: invalid port or range {p!r}")
        if "-" in p and int(p.split("-")[0]) > int(p.split("-")[1]):
            raise SegmentationError(f"applications.{app}.ports: range end before start {p!r}")
    return ",".join(parts)


def _list(raw: Any, what: str) -> list[Any]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise SegmentationError(f"{what}: must be a list")
    return raw


def normalize_segmentation(raw: Any) -> dict[str, Any]:
    """Validate and normalize the knob; raises SegmentationError (a ValueError → KnobError on coercion)."""
    if raw is None or raw == "" or raw == {}:
        return copy.deepcopy(EMPTY)
    if not isinstance(raw, dict):
        raise SegmentationError("must be a mapping {enabled, applications, policies, vrfs}")
    unknown = set(raw) - set(EMPTY)
    if unknown:
        raise SegmentationError(f"unknown keys {sorted(unknown)}")

    apps: list[dict[str, Any]] = []
    for a in _list(raw.get("applications"), "applications"):
        if not isinstance(a, dict):
            raise SegmentationError("applications: items must be mappings")
        name = _name(a.get("name"), "applications")
        if name == BUILTIN_APP:
            raise SegmentationError(f"applications: {BUILTIN_APP} is built in")
        proto = str(a.get("protocol") or "any").lower()
        if proto not in PROTOCOLS:
            raise SegmentationError(f"applications.{name}.protocol: must be one of {list(PROTOCOLS)}")
        ports = _ports(a.get("ports"), name)
        if ports and proto not in ("tcp", "udp"):
            raise SegmentationError(f"applications.{name}.ports: only tcp/udp applications take ports")
        apps.append({"name": name, "protocol": proto, "ports": ports})
    _unique([a["name"] for a in apps], "applications")
    app_names = {a["name"] for a in apps} | {BUILTIN_APP}

    policies: list[dict[str, Any]] = []
    for p in _list(raw.get("policies"), "policies"):
        if not isinstance(p, dict):
            raise SegmentationError("policies: items must be mappings")
        name = _name(p.get("name"), "policies")
        if name in BUILTIN_POLICIES:
            raise SegmentationError(f"policies: {name} is built in")
        rules = []
        for r in _list(p.get("rules"), f"policies.{name}.rules"):
            if not isinstance(r, dict):
                raise SegmentationError(f"policies.{name}.rules: items must be mappings")
            app = str(r.get("application") or "")
            if app not in app_names:
                raise SegmentationError(f"policies.{name}: unknown application {app!r}")
            action = str(r.get("action") or "forward").lower()
            if action not in ACTIONS:
                raise SegmentationError(f"policies.{name}: action must be forward|drop")
            rules.append({"application": app, "action": action, "log": _bool(r.get("log", False))})
        if not rules:
            raise SegmentationError(f"policies.{name}: needs at least one rule")
        if len(rules) > 102:
            raise SegmentationError(f"policies.{name}: too many rules (sequence numbers 10..1020)")
        policies.append({"name": name, "rules": rules})
    _unique([p["name"] for p in policies], "policies")
    policy_names = {p["name"] for p in policies} | set(BUILTIN_POLICIES)

    vrfs: list[dict[str, Any]] = []
    for v in _list(raw.get("vrfs"), "vrfs"):
        if not isinstance(v, dict):
            raise SegmentationError("vrfs: items must be mappings")
        vrf = _name(v.get("name"), "vrfs")
        segs: list[dict[str, Any]] = []
        for s in _list(v.get("segments"), f"vrfs.{vrf}.segments"):
            if not isinstance(s, dict):
                raise SegmentationError(f"vrfs.{vrf}.segments: items must be mappings")
            seg = _name(s.get("name"), f"vrfs.{vrf}.segments")
            where = f"vrfs.{vrf}.segments.{seg}"
            prefixes = []
            for pfx in _list(s.get("prefixes"), f"{where}.prefixes"):
                try:
                    net = ipaddress.ip_network(str(pfx), strict=False)
                except ValueError as exc:
                    raise SegmentationError(f"{where}.prefixes: {exc}") from exc
                if net.version != 4:
                    raise SegmentationError(f"{where}.prefixes: only IPv4 prefixes are supported ({pfx})")
                prefixes.append(str(net))
            interfaces = [str(i).strip() for i in _list(s.get("interfaces"), f"{where}.interfaces") if str(i).strip()]
            if not prefixes and not interfaces:
                raise SegmentationError(f"{where}: needs prefixes and/or interfaces")
            seg_pols = []
            for sp in _list(s.get("policies"), f"{where}.policies"):
                if not isinstance(sp, dict):
                    raise SegmentationError(f"{where}.policies: items must be mappings {{from, policy}}")
                pol = str(sp.get("policy") or "")
                if pol not in policy_names:
                    raise SegmentationError(f"{where}.policies: unknown policy {pol!r}")
                seg_pols.append({"from": str(sp.get("from") or ""), "policy": pol})
            _unique([sp["from"] for sp in seg_pols], f"{where}.policies.from")
            fallback = s.get("fallback")
            fallback = None if fallback in (None, "") else str(fallback).lower()
            if fallback is not None and fallback not in ACTIONS:
                raise SegmentationError(f"{where}.fallback: must be forward|drop|null")
            segs.append({"name": seg, "prefixes": prefixes, "interfaces": interfaces, "policies": seg_pols,
                         "fallback": fallback})
        _unique([s["name"] for s in segs], f"vrfs.{vrf}.segments")
        seg_names = {s["name"] for s in segs} | {FORWARDING_SEGMENTS}
        for s in segs:
            for sp in s["policies"]:
                if sp["from"] not in seg_names:
                    raise SegmentationError(f"vrfs.{vrf}.segments.{s['name']}.policies: unknown source segment {sp['from']!r}")
        if not segs:
            raise SegmentationError(f"vrfs.{vrf}: needs at least one segment")
        vrfs.append({"name": vrf, "segments": segs})
    _unique([v["name"] for v in vrfs], "vrfs")
    out = {"enabled": _bool(raw.get("enabled", False)), "applications": apps, "policies": policies, "vrfs": vrfs}
    if out["enabled"] and not vrfs:
        raise SegmentationError("enabled without any vrfs/segments")
    return out


def _unique(names: list[str], what: str) -> None:
    dup = sorted({n for n in names if names.count(n) > 1})
    if dup:
        raise SegmentationError(f"{what}: duplicate names {dup}")


# ------------------------------------------------------------------------------------------------ structured config
def match_list_name(vrf: str, segment: str) -> str:
    return f"MSS-{vrf}-{segment}"


def _eos_app(seg: dict[str, Any], app: str) -> str:
    a = next((x for x in seg["applications"] if x["name"] == app), None)
    return BUILTIN_APP if a is None or a["protocol"] == "any" else app


def structured_config_for(seg: dict[str, Any], vrf_names: list[str] | set[str]) -> dict[str, Any]:
    """eos_cli_config_gen structured config for a node carrying `vrf_names` ({} when disabled / nothing to render)."""
    seg = normalize_segmentation(seg)
    vrfs = [v for v in seg["vrfs"] if v["name"] in set(vrf_names)]
    if not seg["enabled"] or not vrfs:
        return {}
    used_policies: list[str] = []
    rss_vrfs = []
    match_lists = []
    for v in vrfs:
        segments = []
        for s in v["segments"]:
            definition: dict[str, Any] = {}
            if s["interfaces"]:
                definition["interfaces"] = list(s["interfaces"])
            if s["prefixes"]:
                name = match_list_name(v["name"], s["name"])
                match_lists.append({"name": name, "prefixes": list(s["prefixes"])})
                definition["match_lists"] = [{"address_family": "ipv4", "prefix": name}]
            entry: dict[str, Any] = {"name": s["name"], "definition": definition}
            if s["policies"] or s["fallback"]:
                entry["policies"] = [{"from": p["from"], "policy": p["policy"]} for p in s["policies"]]
            if s["fallback"]:
                entry["fallback_policy"] = f"policy-{s['fallback']}-all"
            for p in s["policies"]:
                if p["policy"] not in BUILTIN_POLICIES and p["policy"] not in used_policies:
                    used_policies.append(p["policy"])
            segments.append(entry)
        rss_vrfs.append({"name": v["name"], "segments": segments})

    pol_by_name = {p["name"]: p for p in seg["policies"]}
    policies = []
    used_apps: list[str] = []
    for name in used_policies:
        seqs = []
        for i, r in enumerate(pol_by_name[name]["rules"], start=1):
            app = _eos_app(seg, r["application"])
            if app != BUILTIN_APP and app not in used_apps:
                used_apps.append(app)
            item: dict[str, Any] = {"sequence": 10 * i, "application": app, "action": r["action"], "stateless": True}
            if r["log"]:
                item["log"] = True
            seqs.append(item)
        policies.append({"name": name, "sequence_numbers": seqs})

    out: dict[str, Any] = {"router_segment_security": {"enabled": True, **({"policies": policies} if policies else {}),
                                                       "vrfs": rss_vrfs}}
    if match_lists:
        out["match_list_input"] = {"prefix_ipv4": match_lists}
    l4_apps, port_sets = [], []
    for a in seg["applications"]:
        if a["name"] not in used_apps:
            continue
        item = {"name": a["name"], "protocols": [a["protocol"]]}
        if a["ports"]:
            ps = f"MSS-{a['name']}"
            port_sets.append({"name": ps, "port_values": a["ports"].split(",")})
            item[f"{a['protocol']}_dest_port_set_name"] = ps
        l4_apps.append(item)
    if l4_apps:
        atr: dict[str, Any] = {"applications": {"l4_applications": l4_apps}}
        if port_sets:
            atr["field_sets"] = {"l4_ports": port_sets}
        out["application_traffic_recognition"] = atr
    return out


# ------------------------------------------------------------------------------------------------ VRF carriers
def _tags_match(item_tags: list[str] | None, node_tags: list[str]) -> bool:
    """eos_designs: `"all" in filter_tags or svi.tags ∩ filter_tags` (svi tags default ["all"])."""
    return "all" in node_tags or bool(set(item_tags or ["all"]) & set(node_tags))


def _type_from_blocks(hv: dict[str, Any], host: str) -> str:
    """No `type` var (e.g. `default_node_types` by hostname regex): the node-type block that defines the host."""
    from .templates import NODE_TYPE_KEYS

    for key in NODE_TYPE_KEYS:
        block = hv.get(key)
        if not isinstance(block, dict):
            continue
        names = [n.get("name") for n in block.get("nodes") or []]
        names += [n.get("name") for ng in block.get("node_groups") or [] for n in ng.get("nodes") or []]
        if host in names:
            return key
    return ""


def _node_filter(hv: dict[str, Any], host: str) -> tuple[str, dict[str, Any]] | None:
    """(node type, merged `filter`) for `host` from its resolved vars, or None when it is not an L3 service node."""
    ntype = str(hv.get("type") or "") or _type_from_blocks(hv, host)
    if ntype not in L3_NODE_TYPES:
        return None
    block = hv.get(ntype) or {}
    flt = dict((block.get("defaults") or {}).get("filter") or {})
    for n in block.get("nodes") or []:
        if n.get("name") == host:
            flt.update(n.get("filter") or {})
    for ng in block.get("node_groups") or []:
        for n in ng.get("nodes") or []:
            if n.get("name") == host:
                flt.update(ng.get("filter") or {})
                flt.update(n.get("filter") or {})
                flt["_group"] = ng.get("group")
    return ntype, flt


def vrf_carriers(hostvars: dict[str, dict[str, Any]], vrf_names: list[str]) -> dict[str, list[str]]:
    """{host: [vrf, ...]} — which of `vrf_names` each L3 network-service node will carry, from the AVD inputs
    (filter.tenants / allow_vrfs / deny_vrfs / always_include_vrfs_in_tenants, svi `tags` vs filter.tags + node group,
    `l3_interfaces[].nodes`, `loopbacks[].node`, `l3_port_channels[].node`). VRF `default` is carried by every L3
    service node. `only_vlans_in_use` is not modelled."""
    out: dict[str, list[str]] = {}
    for host, hv in hostvars.items():
        nf = _node_filter(hv, host)
        if nf is None:
            continue
        _t, flt = nf
        f_tenants = [str(t) for t in flt.get("tenants") or ["all"]]
        f_tags = [str(t) for t in flt.get("tags") or ["all"]] + ([str(flt["_group"])] if flt.get("_group") else [])
        always = [str(t) for t in flt.get("always_include_vrfs_in_tenants") or []]
        allow = [str(t) for t in flt.get("allow_vrfs") or ["all"]]
        deny = [str(t) for t in flt.get("deny_vrfs") or []]
        keys = [k.get("name") for k in hv.get("network_services_keys") or [] if isinstance(k, dict)] or ["tenants"]
        carried: list[str] = []
        for vname in vrf_names:
            if vname == "default":
                carried.append(vname)
                continue
            for key in keys:
                for tenant in hv.get(key) or []:
                    tname = str(tenant.get("name"))
                    if "all" not in f_tenants and tname not in f_tenants:
                        continue
                    for vrf in tenant.get("vrfs") or []:
                        if str(vrf.get("name")) != vname or ("all" not in allow and vname not in allow) or vname in deny:
                            continue
                        if "all" in always or tname in always:
                            carried.append(vname)
                        if any(_tags_match(svi.get("tags"), f_tags) for svi in vrf.get("svis") or []):
                            carried.append(vname)
                        if any(host in (x.get("nodes") or []) for x in vrf.get("l3_interfaces") or []):
                            carried.append(vname)
                        if any(x.get("node") == host for x in (vrf.get("loopbacks") or []) + (vrf.get("l3_port_channels") or [])):
                            carried.append(vname)
        if carried:
            out[host] = sorted(set(carried), key=vrf_names.index)
    return out


def carriers_from_structured(structured: dict[str, dict[str, Any]], vrf_names: list[str]) -> dict[str, list[str]]:
    """Custom labs / post-build view: {host: [vrf]} from the structured configs (`vrfs` list, VRF `default` = any
    host with an SVI in the default VRF)."""
    out: dict[str, list[str]] = {}
    for host, sc in structured.items():
        have = {str(v.get("name")) for v in sc.get("vrfs") or []}
        if any(not vi.get("vrf") or vi.get("vrf") == "default" for vi in sc.get("vlan_interfaces") or []
               if vi.get("ip_address") or vi.get("ip_address_virtual") or vi.get("ip_virtual_router_addresses")):
            have.add("default")
        carried = [v for v in vrf_names if v in have]
        if carried:
            out[host] = carried
    return out


def _deep_merge(dst: dict[str, Any], src: dict[str, Any]) -> None:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = copy.deepcopy(v)


def apply_segmentation(gv: dict[str, dict[str, Any]], inventory: dict[str, Any], seg: dict[str, Any] | None) -> list[str]:
    """Render the knob into node-level `structured_config` of the VRF carriers (templated labs). Returns the hosts.
    Raises SegmentationError when an enabled VRF is carried by no node."""
    from .templates import iter_nodes, resolve_hostvars

    seg = normalize_segmentation(seg)
    if not seg["enabled"]:
        return []
    names = [v["name"] for v in seg["vrfs"]]
    carriers = vrf_carriers(resolve_hostvars(gv, inventory), names)
    missing = [v for v in names if not any(v in c for c in carriers.values())]
    if missing:
        raise SegmentationError(f"vrfs {missing}: not carried by any L3 network-service node of this fabric")
    nodes = {n.get("name"): n for _g, _t, n in iter_nodes(gv) if n.get("name")}
    for host, vrfs in carriers.items():
        if host in nodes:
            _deep_merge(nodes[host].setdefault("structured_config", {}), structured_config_for(seg, vrfs))
    return sorted(h for h in carriers if h in nodes)


def rendered_on(structured: dict[str, dict[str, Any]]) -> list[str]:
    return sorted(h for h, sc in structured.items() if (sc.get("router_segment_security") or {}).get("vrfs"))


# ------------------------------------------------------------------------------------------------ matrix
def _segment_policy(dst: dict[str, Any], src_name: str) -> str | None:
    pols = {p["from"]: p["policy"] for p in dst["policies"]}
    if src_name in pols:
        return pols[src_name]
    if FORWARDING_SEGMENTS in pols:
        return pols[FORWARDING_SEGMENTS]
    if dst["fallback"]:
        return f"policy-{dst['fallback']}-all"
    return None


def policy_matrix(seg: dict[str, Any]) -> dict[str, dict[str, dict[str, str | None]]]:
    """matrix[vrf][src_segment][dst_segment] = governing policy name (or None = unspecified)."""
    seg = normalize_segmentation(seg)
    out: dict[str, dict[str, dict[str, str | None]]] = {}
    for v in seg["vrfs"]:
        out[v["name"]] = {src["name"]: {dst["name"]: _segment_policy(dst, src["name"]) for dst in v["segments"]}
                          for src in v["segments"]}
    return out


def decide(seg: dict[str, Any], policy: str | None, application: str) -> str | None:
    """forward | drop | None (unspecified) for `application` under `policy`."""
    if policy is None:
        return None
    if policy in BUILTIN_POLICIES:
        return BUILTIN_POLICIES[policy]
    pol = next((p for p in seg["policies"] if p["name"] == policy), None)
    if pol is None:
        return None
    for r in pol["rules"]:
        if r["application"] == application or _eos_app(seg, r["application"]) == BUILTIN_APP:
            return r["action"]
    return "drop"


def segment_of(seg: dict[str, Any], vrf: str | None, ip: str | None) -> str | None:
    """Most specific segment of `vrf` whose prefixes contain `ip`."""
    if not ip or not vrf:
        return None
    addr = ipaddress.ip_address(str(ip).split("/")[0])
    best: tuple[int, str] | None = None
    for v in seg["vrfs"]:
        if v["name"] != vrf:
            continue
        for s in v["segments"]:
            for p in s["prefixes"]:
                net = ipaddress.ip_network(p)
                if addr in net and (best is None or net.prefixlen > best[0]):
                    best = (net.prefixlen, s["name"])
    return best[1] if best else None


def application(seg: dict[str, Any], name: str) -> dict[str, Any] | None:
    if name == BUILTIN_APP:
        return {"name": BUILTIN_APP, "protocol": "any", "ports": None}
    return next((a for a in seg["applications"] if a["name"] == name), None)


def mss_summary(seg: dict[str, Any] | None, structured: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Body of GET /labs/{id}/mss."""
    seg = normalize_segmentation(seg)
    return {"enabled": seg["enabled"], "applications": seg["applications"], "policies": seg["policies"],
            "vrfs": seg["vrfs"], "matrix": policy_matrix(seg), "rendered_on": rendered_on(structured or {})}
