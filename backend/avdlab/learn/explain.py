"""Config explainer: for a line of a lab's intended EOS config, say which AVD input produced it and why.

`explain_lines(lab_dir, host, lines, knobs=..., template=...)` parses `intended/configs/<host>.cfg` into sections
(top-level line + indented children), builds a `NodeCtx` (merged hostvars, the node's own `<type>` settings with their
provenance defaults → node_group → node, the structured config, peers' structured configs) and runs the rules table in
`explain_rules.py`. Every rule returns an `Expl`; values are read from the lab (group_vars / knobs / structured
config), never hard-coded, so the text is specific: "Loopback0 10.255.0.3 = loopback_ipv4_pool 10.255.0.0/27 + id 1 +
loopback_ipv4_offset 2".
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Any, Callable

import yaml

from ..labs.templates import load_group_vars_dir, load_inventory

DEFAULT_TYPE_KEYS = {"super-spine": "super_spine", "overlay-controller": "overlay_controller"}
NOT_SET = "AVD default (not set in the lab's group_vars)"


# ------------------------------------------------------------------------------------------------ config parsing
@dataclass
class ConfigLine:
    no: int                 # 1-based line number in the file
    text: str               # raw line (with indentation)
    indent: int
    section: int | None     # line number of the top-level section header (None for '!' / blank)
    parent: int | None      # line number of the direct parent (None for top-level lines)

    @property
    def stripped(self) -> str:
        return self.text.strip()


def parse_config(text: str) -> list[ConfigLine]:
    out: list[ConfigLine] = []
    stack: list[tuple[int, int]] = []  # (indent, line no)
    for no, raw in enumerate(text.splitlines(), start=1):
        body = raw.rstrip()
        stripped = body.strip()
        indent = len(body) - len(body.lstrip(" "))
        if not stripped or stripped == "!" or stripped.startswith("! "):
            out.append(ConfigLine(no, raw, indent, stack[0][1] if stack and indent > 0 else None, None))
            if indent == 0:
                stack = []
            continue
        while stack and stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1] if stack else None
        section = stack[0][1] if stack else no
        out.append(ConfigLine(no, raw, indent, section, parent))
        stack.append((indent, no))
    return out


# ------------------------------------------------------------------------------------------------ explanation
@dataclass
class Input:
    key: str
    value: Any
    where: str
    knob: str | None = None

    def as_dict(self) -> dict[str, Any]:
        d = {"key": self.key, "value": self.value, "where": self.where}
        if self.knob:
            d["knob"] = self.knob
        return d


@dataclass
class Expl:
    why: str
    inputs: list[Input] = field(default_factory=list)
    doc_url: str | None = None
    related: list[str] = field(default_factory=list)  # regexes matched against the whole config
    topic: str | None = None


# ------------------------------------------------------------------------------------------------ lab / node context
class LabCtx:
    """Everything about a lab the rules may read. Cheap to build; per-host data is cached."""

    def __init__(self, lab_dir: Path, knobs: dict[str, Any] | None = None, template: str | None = None,
                 template_knobs: set[str] | None = None):
        self.dir = Path(lab_dir)
        self.knobs = knobs or {}
        self.template = template
        self.template_knobs = template_knobs if template_knobs is not None else set(self.knobs)
        self._sc: dict[str, dict[str, Any]] = {}

    @cached_property
    def gv(self) -> dict[str, dict[str, Any]]:
        return load_group_vars_dir(self.dir)

    @cached_property
    def gv_where(self) -> dict[str, str]:
        out = {}
        gdir = self.dir / "group_vars"
        for g in self.gv:
            out[g] = f"group_vars/{g}/" if (gdir / g).is_dir() else (
                f"group_vars/{g}.yaml" if (gdir / f"{g}.yaml").is_file() else f"group_vars/{g}.yml")
        return out

    @cached_property
    def inventory(self) -> dict[str, Any]:
        return load_inventory(self.dir)

    @cached_property
    def _tree(self) -> tuple[dict[str, set[str]], dict[str, dict[str, Any]]]:
        children: dict[str, set[str]] = {}
        hosts: dict[str, dict[str, Any]] = {}

        def walk(name: str, node: dict[str, Any] | None) -> None:
            node = node or {}
            hosts.setdefault(name, {}).update(node.get("hosts") or {})
            children.setdefault(name, set()).update((node.get("children") or {}).keys())
            for c, cn in (node.get("children") or {}).items():
                walk(c, cn)

        walk("all", self.inventory.get("all") or {})
        return children, hosts

    def groups_of(self, host: str) -> list[str]:
        """Groups of `host` in Ansible precedence order, lowest first (parents before children)."""
        children, hosts = self._tree
        parents = lambda g: {p for p, ch in children.items() if g in ch}  # noqa: E731

        def ancestors(g: str) -> set[str]:
            return parents(g) | {a for p in parents(g) for a in ancestors(p)}

        def depth(g: str) -> int:
            ps = parents(g)
            return 0 if not ps else 1 + max(depth(p) for p in ps)

        direct = {g for g, hs in hosts.items() if host in hs}
        allg = set(direct)
        for g in direct:
            allg |= ancestors(g)
        return sorted(allg, key=lambda x: (depth(x), x))

    def hostvars(self, host: str) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        for g in self.groups_of(host):
            merged.update(copy.deepcopy(self.gv.get(g, {})))
        _c, hosts = self._tree
        for g in self.groups_of(host):
            merged.update((hosts.get(g) or {}).get(host) or {})
        return merged

    def sc(self, host: str) -> dict[str, Any]:
        if host not in self._sc:
            p = self.dir / "intended" / "structured_configs" / f"{host}.yml"
            self._sc[host] = (yaml.safe_load(p.read_text()) or {}) if p.is_file() else {}
        return self._sc[host]

    def config(self, host: str) -> str:
        p = self.dir / "intended" / "configs" / f"{host}.cfg"
        return p.read_text() if p.is_file() else ""

    def knob_if(self, name: str) -> str | None:
        """Knob name when the lab's template exposes it (so the UI can link to Settings)."""
        root = name.split(".", 1)[0]
        return name if root in self.template_knobs else None


class NodeCtx:
    def __init__(self, lab: LabCtx, host: str):
        self.lab = lab
        self.host = host
        self.hv = lab.hostvars(host)
        self.sc = lab.sc(host)
        self.type = str(self.hv.get("type") or "")
        self.type_key = self._type_key()
        self._node_path = self._find_node()

    # ---- node type settings (defaults <- node_group <- node) with provenance
    def _type_key(self) -> str:
        for item in (self.hv.get("custom_node_type_keys") or []) + (self.hv.get("node_type_keys") or []):
            if isinstance(item, dict) and item.get("type") == self.type:
                return str(item.get("key"))
        return DEFAULT_TYPE_KEYS.get(self.type, self.type)

    def _find_node(self) -> tuple[str | None, dict[str, Any], dict[str, Any] | None, dict[str, Any] | None]:
        """(group file holding the `<type_key>` block, block, node_group, node)."""
        for g in reversed(self.lab.groups_of(self.host)):
            block = (self.lab.gv.get(g) or {}).get(self.type_key)
            if not isinstance(block, dict):
                continue
            for node in block.get("nodes") or []:
                if node.get("name") == self.host:
                    return g, block, None, node
            for ng in block.get("node_groups") or []:
                for node in ng.get("nodes") or []:
                    if node.get("name") == self.host:
                        return g, block, ng, node
            return g, block, None, None
        return None, {}, None, None

    @property
    def node_group(self) -> dict[str, Any] | None:
        return self._node_path[2]

    @property
    def group_name(self) -> str | None:
        return (self.node_group or {}).get("group")

    def setting(self, name: str, default: Any = None) -> Input:
        g, block, ng, node = self._node_path
        where = self.lab.gv_where.get(g, "group_vars") if g else NOT_SET
        tk = self.type_key
        if node is not None and name in node:
            prefix = f"{tk}.node_groups[group={ng['group']}].nodes[name={self.host}]" if ng else f"{tk}.nodes[name={self.host}]"
            return Input(f"{prefix}.{name}", node[name], where)
        if ng is not None and name in ng:
            return Input(f"{tk}.node_groups[group={ng['group']}].{name}", ng[name], where)
        if name in (block.get("defaults") or {}):
            return Input(f"{tk}.defaults.{name}", block["defaults"][name], where)
        return Input(f"{tk}.defaults.{name}", default, NOT_SET)

    def fabric(self, dotted: str, default: Any = None) -> Input:
        """A fabric-level (top-level) input: most specific group that defines the top-level key wins."""
        top, _, rest = dotted.partition(".")
        for g in reversed(self.lab.groups_of(self.host)):
            data = self.lab.gv.get(g) or {}
            if top in data:
                val: Any = data[top]
                for part in [p for p in rest.split(".") if p]:
                    val = val.get(part) if isinstance(val, dict) else None
                return Input(dotted, val if val is not None else default,
                             self.lab.gv_where.get(g, "group_vars") if val is not None else NOT_SET)
        return Input(dotted, default, NOT_SET)

    def where_of(self, top: str) -> str:
        return self.fabric(top).where

    # ---- network services
    def tenants(self) -> list[tuple[str, dict[str, Any]]]:
        keys = [k.get("name") for k in (self.hv.get("network_services_keys") or [])] or ["tenants"]
        out = []
        for k in keys + ["network_services"]:
            for t in self.hv.get(k) or []:
                if isinstance(t, dict):
                    out.append((k, t))
        return out

    def find_vlan(self, vid: int) -> tuple[str, dict[str, Any], dict[str, Any] | None, dict[str, Any], str] | None:
        """(services key, tenant, vrf|None, svi|l2vlan, kind 'svi'|'l2vlan')"""
        for key, t in self.tenants():
            for vrf in t.get("vrfs") or []:
                for svi in vrf.get("svis") or []:
                    if int(svi.get("id", -1)) == vid:
                        return key, t, vrf, svi, "svi"
            for l2 in t.get("l2vlans") or []:
                if int(l2.get("id", -1)) == vid:
                    return key, t, None, l2, "l2vlan"
        return None

    def find_vrf(self, name: str) -> tuple[str, dict[str, Any], dict[str, Any]] | None:
        for key, t in self.tenants():
            for vrf in t.get("vrfs") or []:
                if vrf.get("name") == name:
                    return key, t, vrf
        return None

    # ---- structured config helpers
    def sc_list(self, key: str, name: str, field: str = "name") -> dict[str, Any] | None:
        for item in self.sc.get(key) or []:
            if isinstance(item, dict) and str(item.get(field)) == str(name):
                return item
        return None

    def ethernet(self, name: str) -> dict[str, Any] | None:
        return self.sc_list("ethernet_interfaces", name)

    def port_channel(self, name: str) -> dict[str, Any] | None:
        return self.sc_list("port_channel_interfaces", name)

    def router_id(self) -> str | None:
        return (self.sc.get("router_bgp") or {}).get("router_id")

    def peer_loopback0(self, host: str) -> str | None:
        return (self.lab.sc(host).get("router_bgp") or {}).get("router_id")

    def host_by_router_id(self, ip: str) -> str | None:
        for p in (self.lab.dir / "intended" / "structured_configs").glob("*.yml"):
            if self.peer_loopback0(p.stem) == ip:
                return p.stem
        return None

    def mlag_peer(self) -> str | None:
        ng = self.node_group
        if not ng:
            return None
        names = [n.get("name") for n in ng.get("nodes") or []]
        return next((n for n in names if n != self.host), None) if len(names) == 2 else None

    def mlag_primary(self) -> tuple[str | None, int | None]:
        ng = self.node_group
        if not ng or len(ng.get("nodes") or []) != 2:
            return None, None
        first = ng["nodes"][0]
        pid = first.get("id")
        if pid is None:  # id inherited from group/defaults is unusual; fall back to the peer's context
            pid = NodeCtx(self.lab, first["name"]).setting("id").value
        return first.get("name"), pid

    def endpoint_adapter(self, port: str) -> tuple[str, dict[str, Any], dict[str, Any], int] | None:
        """(endpoint key, endpoint, adapter, index of this switch in the adapter) for a switch port of this host."""
        keys = [k.get("key") for k in (self.hv.get("connected_endpoints_keys") or [])] or [
            "servers", "firewalls", "routers", "load_balancers", "storage_arrays", "cpes", "workstations",
            "access_points", "phones", "printers", "cameras", "generic_devices"]
        for k in keys + ["connected_endpoints"]:
            for ep in self.hv.get(k) or []:
                if not isinstance(ep, dict):
                    continue
                for ad in ep.get("adapters") or []:
                    for i, (sw, sp) in enumerate(zip(ad.get("switches") or [], ad.get("switch_ports") or [])):
                        if sw == self.host and sp == port:
                            return k, ep, ad, i
        return None

    def endpoint_where(self, key: str) -> str:
        return self.where_of(key)


# ------------------------------------------------------------------------------------------------ engine
Handler = Callable[[NodeCtx, "re.Match[str]", ConfigLine, "Lines"], Expl | None]


class Lines:
    """The parsed config with lookup helpers for rules (section header / parent text)."""

    def __init__(self, text: str):
        self.items = parse_config(text)
        self.by_no = {cl.no: cl for cl in self.items}

    def header(self, cl: ConfigLine) -> str:
        return self.by_no[cl.section].stripped if cl.section else ""

    def parent(self, cl: ConfigLine) -> str:
        return self.by_no[cl.parent].stripped if cl.parent else ""

    def find(self, text: str) -> ConfigLine | None:
        """First line whose stripped text equals `text` (the caller's line) — exact, then prefix match."""
        t = text.strip()
        for cl in self.items:
            if cl.stripped == t:
                return cl
        return None

    def matching(self, patterns: list[str], exclude: int | None = None, limit: int = 12) -> list[dict[str, Any]]:
        out = []
        regs = [re.compile(p) for p in patterns]
        for cl in self.items:
            if cl.no == exclude or not cl.stripped or cl.stripped == "!":
                continue
            if any(r.search(cl.stripped) for r in regs):
                out.append({"no": cl.no, "text": cl.stripped, "section": self.header(cl)})
                if len(out) >= limit:
                    break
        return out


def explain_one(node: NodeCtx, lines: Lines, cl: ConfigLine) -> dict[str, Any]:
    from .explain_rules import RULES

    header = lines.header(cl)
    parent = lines.parent(cl)
    base: dict[str, Any] = {"line": cl.stripped, "no": cl.no, "section": header, "parent": parent or None,
                            "rule": None, "avd_input": None, "inputs": [], "why": None, "doc_url": None,
                            "related_lines": []}
    if not cl.stripped or cl.stripped == "!":
        return base
    for rule in RULES:
        if not rule.applies(cl, header, parent):
            continue
        m = rule.line.search(cl.stripped)
        if not m:
            continue
        try:
            ex = rule.fn(node, m, cl, lines)
        except (KeyError, IndexError, TypeError, ValueError, AttributeError):
            ex = None
        if ex is None:
            continue
        base.update({"rule": rule.id, "why": ex.why, "doc_url": ex.doc_url,
                     "inputs": [i.as_dict() for i in ex.inputs],
                     "avd_input": ex.inputs[0].as_dict() if ex.inputs else None,
                     "related_lines": lines.matching(ex.related, exclude=cl.no) if ex.related else [],
                     "topic": ex.topic or rule.topic})
        return base
    return base


def explain_lines(lab_dir: Path, host: str, lines_in: list[str], *, knobs: dict[str, Any] | None = None,
                  template: str | None = None, template_knobs: set[str] | None = None) -> list[dict[str, Any]]:
    lab = LabCtx(lab_dir, knobs, template, template_knobs)
    text = lab.config(host)
    if not text:
        raise FileNotFoundError(f"intended/configs/{host}.cfg not found — build the lab first")
    node = NodeCtx(lab, host)
    lines = Lines(text)
    out = []
    for raw in lines_in:
        cl = None
        if isinstance(raw, int):
            cl = lines.by_no.get(raw)
        else:
            m = re.match(r"^#?(\d+)$", str(raw).strip())
            cl = lines.by_no.get(int(m.group(1))) if m else lines.find(str(raw))
        if cl is None:
            out.append({"line": str(raw).strip(), "no": None, "section": None, "parent": None, "rule": None,
                        "avd_input": None, "inputs": [], "why": None, "doc_url": None, "related_lines": [],
                        "error": "line not found in the intended config"})
            continue
        out.append(explain_one(node, lines, cl))
    return out


def explain_file(lab_dir: Path, host: str, **kw: Any) -> dict[str, Any]:
    """Batch 'explain this file': every non-trivial line + per-section counts."""
    lab = LabCtx(lab_dir, kw.get("knobs"), kw.get("template"), kw.get("template_knobs"))
    text = lab.config(host)
    if not text:
        raise FileNotFoundError(f"intended/configs/{host}.cfg not found — build the lab first")
    node = NodeCtx(lab, host)
    lines = Lines(text)
    results = [explain_one(node, lines, cl) for cl in lines.items if cl.stripped and cl.stripped != "!"
               and cl.stripped != "end"]
    sections: dict[str, dict[str, Any]] = {}
    for r in results:
        s = sections.setdefault(r["section"], {"section": r["section"], "lines": 0, "explained": 0, "topics": set()})
        s["lines"] += 1
        if r["why"]:
            s["explained"] += 1
            if r.get("topic"):
                s["topics"].add(r["topic"])
    secs = [{**v, "topics": sorted(v["topics"])} for v in sections.values()]
    return {"host": host, "lines": results, "sections": secs,
            "explained": sum(1 for r in results if r["why"]), "total": len(results)}
