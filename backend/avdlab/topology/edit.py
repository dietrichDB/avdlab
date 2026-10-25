"""Phase 5: editable topology. The graph is a *view of the AVD inputs*: every edit becomes a change of the lab inputs
(knobs, `lab.settings["topology_overrides"]`, or group_vars files for custom labs), is validated with pyavd and the graph
is re-rendered from a topology rebuilt from those inputs. Node positions are cosmetic (`clab/layout.json`).

Ops (`POST /topology/{id}/edit {ops: [...], dry_run}`), each `{"op": <name>, ...}`:
  set_uplink {node, index, switch, switch_port}      node-level uplink_switches / uplink_switch_interfaces
  add_endpoint {name, type=servers, adapters}        servers knob (l3ls templates) / endpoint list in group_vars
  remove_endpoint {name}   update_endpoint {name, adapters}
  add_leaf_pair {}  remove_last_leaf_pair {}  set_l2leaf_per_pair {n}  set_spine_count {n}   (l3ls templates only)
  rename_node → 409 (hostnames are keys everywhere)

Generated labs (template generator l3ls/static) never get their files edited directly: node and non-knob endpoint
edits are stored as overrides and re-applied after every knob render (`templates.apply_topology_overrides`). Custom
labs (generator none) get their group_vars files edited in place (YAML re-dumped: comments in touched files are lost).
"""
from __future__ import annotations

import copy
import difflib
import json
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

import yaml

from ..core.db import Lab

from ..labs.templates import (
    CONNECTED_ENDPOINT_KEYS,
    KnobError,
    TemplateDescriptor,
    _dump_yaml,
    coerce_knobs,
    endpoint_target_group,
    get_template,
    iter_nodes,
    load_group_vars_dir,
    load_inventory,
    render_lab,
    resolve_hostvars,
    serials_from_group_vars,
    validate_hostvars,
)
from .models import TopologyGraph

IF_RE = re.compile(r"^Ethernet\d+(/\d+)*$")
ETH_NUM_RE = re.compile(r"^Ethernet(\d+)$")
MIN_PORTS = 8
LAYOUT_REL = "clab/layout.json"
GRAPH_REL = "clab/graph.json"
KNOB_OPS = ("add_leaf_pair", "remove_last_leaf_pair", "set_l2leaf_per_pair", "set_spine_count")
ALL_OPS = ("set_uplink", "add_endpoint", "remove_endpoint", "update_endpoint", "rename_node", *KNOB_OPS)


class EditError(Exception):
    """HTTP error of an edit: 422 {message, violations, op_index} / 409 {message, hint, op_index}."""

    def __init__(self, status: int, message: str, *, violations: list[dict[str, Any]] | None = None,
                 op_index: int | None = None, hint: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.violations = violations if violations is not None else [{"message": message}]
        self.op_index = op_index
        self.hint = hint

    def detail(self) -> dict[str, Any]:
        out: dict[str, Any] = {"message": self.message, "violations": self.violations, "op_index": self.op_index}
        if self.hint:
            out["hint"] = self.hint
        return out


# ------------------------------------------------------------------------------------------------ helpers
def _facts_dict(f: Any) -> dict[str, Any]:
    if isinstance(f, dict):
        return f
    return f._as_dict() if hasattr(f, "_as_dict") else dict(vars(f))


def _eth_numbers(spec: Any) -> list[int]:
    """'Ethernet1-8' / 'Ethernet5' / ['Ethernet1-2', 'Ethernet9'] → [1..8] (slot/port notation ignored)."""
    out: list[int] = []
    items = spec if isinstance(spec, list) else [spec]
    for item in items:
        for part in str(item or "").split(","):
            m = re.match(r"^\s*Ethernet(\d+)(?:-(\d+))?\s*$", part)
            if m:
                a = int(m.group(1))
                b = int(m.group(2) or a)
                out.extend(range(a, b + 1))
    return out


def _if_key(name: str) -> list[int]:
    return [int(n) for n in re.findall(r"\d+", name)] or [0]


def _group_of(rel: str) -> str:
    parts = Path(rel).parts
    return Path(parts[1]).stem if len(parts) == 2 else parts[1]


def _clean_overrides(raw: Any) -> dict[str, Any]:
    raw = copy.deepcopy(raw) if isinstance(raw, dict) else {}
    return {"nodes": dict(raw.get("nodes") or {}), "endpoints": dict(raw.get("endpoints") or {})}


# ------------------------------------------------------------------------------------------------ session
class EditSession:
    """Candidate inputs of a lab while ops are applied (nothing touches disk until `commit`)."""

    def __init__(self, lab: Lab, root: Path, *, knobs: dict[str, Any] | None = None) -> None:
        self.lab = lab
        self.root = root
        try:
            self.tpl: TemplateDescriptor | None = get_template(lab.template)
        except KeyError:
            self.tpl = None
        self.generated = self.tpl is not None and self.tpl.generator in ("l3ls", "static")
        self.l3ls = self.generated and self.tpl is not None and self.tpl.generator == "l3ls"
        self.knobs: dict[str, Any] = dict(knobs or {})
        if self.tpl is not None:
            self.knobs = {k: v for k, v in self.knobs.items() if self.tpl.knob(k)}
        self.overrides = _clean_overrides((lab.settings or {}).get("topology_overrides"))
        self.fabric_name = str((lab.settings or {}).get("fabric_name") or (self.tpl.fabric_name if self.tpl else "FABRIC"))
        self.files: dict[str, Any] = {}
        self.inventory: dict[str, Any] = {}
        self.orig_text: dict[str, str] = {}
        if not self.generated:
            for f in sorted((root / "group_vars").rglob("*")):
                if f.is_file() and f.suffix in (".yml", ".yaml"):
                    rel = str(f.relative_to(root))
                    self.orig_text[rel] = f.read_text(encoding="utf-8")
                    self.files[rel] = yaml.safe_load(self.orig_text[rel]) or {}
            self.inventory = load_inventory(root)
        self.warnings: list[str] = []
        self._rendered: Any = None
        self._facts: dict[str, dict[str, Any]] | None = None
        self._hostvars: dict[str, dict[str, Any]] | None = None

    # ---- views of the candidate state
    def invalidate(self) -> None:
        self._rendered = None
        self._facts = None
        self._hostvars = None

    def rendered(self) -> Any:
        assert self.tpl is not None
        if self._rendered is None:
            self._rendered = render_lab(self.tpl, self.knobs, slug=self.lab.slug, overrides=self.overrides)
        return self._rendered

    def holders(self) -> dict[str, dict[str, Any]]:
        """{holder: vars} — holder is the group name (generated labs) or the group_vars file (custom labs)."""
        if self.generated:
            return self.rendered().group_vars
        return self.files

    def group_vars(self) -> dict[str, dict[str, Any]]:
        if self.generated:
            return self.rendered().group_vars
        merged: dict[str, dict[str, Any]] = {}
        for rel in sorted(self.files):
            merged.setdefault(_group_of(rel), {}).update(self.files[rel] or {})
        return merged

    def hostvars(self) -> dict[str, dict[str, Any]]:
        if self._hostvars is None:
            if self.generated:
                self._hostvars = self.rendered().hostvars()
            else:
                self._hostvars = resolve_hostvars(self.group_vars(), self.inventory)
        return self._hostvars

    def validated(self, op_index: int | None = None) -> dict[str, dict[str, Any]]:
        hv = self.hostvars()
        if not hv:
            raise EditError(422, "no hosts in the inventory", op_index=op_index)
        validated, violations = validate_hostvars(hv)
        if violations:
            raise EditError(422, f"{len(violations)} AVD input violation(s)", violations=violations, op_index=op_index)
        return validated

    def facts(self, op_index: int | None = None) -> dict[str, dict[str, Any]]:
        if self._facts is None:
            from pyavd import get_avd_facts

            validated = self.validated(op_index)
            try:
                facts = get_avd_facts(validated, all_hostvars=validated)
            except Exception as exc:  # noqa: BLE001 - AristaAvdError & co.
                raise EditError(422, f"AVD rejected the inputs: {exc}", op_index=op_index) from exc
            self._facts = {h: _facts_dict(f) for h, f in facts.items()}
        return self._facts

    # ---- endpoints
    def endpoints(self) -> list[tuple[str, str, dict[str, Any]]]:
        """[(holder, key, endpoint)] of every connected endpoint in the candidate inputs (first definition wins)."""
        out: list[tuple[str, str, dict[str, Any]]] = []
        seen: set[str] = set()
        for holder, data in self.holders().items():
            if not isinstance(data, dict):
                continue
            for key in CONNECTED_ENDPOINT_KEYS:
                for ep in data.get(key) or []:
                    if isinstance(ep, dict) and ep.get("name") and ep["name"] not in seen:
                        seen.add(ep["name"])
                        out.append((holder, key, ep))
        return out

    def find_endpoint(self, name: str) -> tuple[str, str] | None:
        for holder, key, ep in self.endpoints():
            if ep["name"] == name:
                return holder, key
        return None

    def endpoint_list(self, holder: str, key: str) -> list[dict[str, Any]]:
        return copy.deepcopy((self.holders().get(holder) or {}).get(key) or [])

    def set_endpoint_list(self, holder: str, key: str, items: list[dict[str, Any]]) -> None:
        if self.generated:
            assert self.tpl is not None
            if self.l3ls and key == "servers" and holder == self.tpl.l3ls.endpoints_group and self.tpl.knob("servers"):
                self.knobs["servers"] = items
            else:
                self.overrides["endpoints"].setdefault(holder, {})[key] = items
        else:
            self.files.setdefault(holder, {})[key] = items
        self.invalidate()

    def new_endpoint_holder(self, key: str) -> str:
        if self.generated:
            return endpoint_target_group(self.tpl, self.holders(), key)
        holder = next((rel for rel, d in self.files.items() if isinstance(d, dict) and isinstance(d.get(key), list)), None)
        holder = holder or next((rel for rel, d in self.files.items() if isinstance(d, dict)
                                 and any(k in d for k in CONNECTED_ENDPOINT_KEYS)), None)
        if holder:
            return holder
        fabric = next((rel for rel in self.files if _group_of(rel) == self.fabric_name), None)
        return fabric or f"group_vars/{self.fabric_name}.yml"

    # ---- nodes
    def set_node_keys(self, node: str, keys: dict[str, Any], op_index: int) -> None:
        if self.generated:
            self.overrides["nodes"].setdefault(node, {}).update(copy.deepcopy(keys))
        else:
            entry = next((n for _h, _t, n in iter_nodes(self.files) if n.get("name") == node), None)
            if entry is None:
                raise EditError(409, f"{node} is not defined as a node entry in group_vars", op_index=op_index,
                                hint="edit the node definition in the Files tab")
            entry.update(copy.deepcopy(keys))
        self.invalidate()

    # ---- ports
    def ports(self) -> dict[str, dict[str, Any]]:
        return compute_ports(self.hostvars(), self.facts(), self.endpoints())

    # ---- output
    def output_files(self) -> tuple[dict[str, str], list[str]]:
        """(files to write, deleted paths). Generated labs: the complete render (for prune); custom: changed files."""
        if self.generated:
            files = self.rendered().files()
            existing = {str(f.relative_to(self.root)) for f in (self.root / "group_vars").rglob("*") if f.is_file()} \
                if (self.root / "group_vars").is_dir() else set()
            deleted = sorted(p for p in existing if p not in files)
            return files, deleted
        out: dict[str, str] = {}
        for rel, data in self.files.items():
            if rel in self.orig_text and (yaml.safe_load(self.orig_text[rel]) or {}) == data:
                continue
            out[rel] = _dump_yaml(data)
        return out, []


# ------------------------------------------------------------------------------------------------ ports
def compute_ports(hostvars: dict[str, dict[str, Any]], facts: dict[str, dict[str, Any]],
                  endpoints: list[tuple[str, str, dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    """{node: {type, used: [if], free: [if], peers: {if: "peer:peer_if"}}} from the eos_designs facts (uplinks both ends,
    MLAG peer links), l3_edge/core_interfaces p2p links and the connected-endpoint adapters. Free ports of a switch are
    Ethernet1..N minus the used ones, N = max(8, default_interfaces ranges of its type, highest used port)."""
    peers: dict[str, dict[str, str]] = {h: {} for h in facts}
    types: dict[str, str] = {}
    for host, f in facts.items():
        types[host] = str(f.get("type") or "")
        for u in f.get("uplinks") or []:
            if u.get("interface") and u.get("peer") and u.get("peer_interface"):
                peers.setdefault(host, {})[u["interface"]] = f"{u['peer']}:{u['peer_interface']}"
                peers.setdefault(u["peer"], {})[u["peer_interface"]] = f"{host}:{u['interface']}"
        for intf in f.get("mlag_interfaces") or []:
            peers[host][intf] = f"{f.get('mlag_peer') or 'mlag'}:{intf}"
    hv0 = next(iter(hostvars.values()), {})
    for section in ("l3_edge", "core_interfaces"):
        for link in ((hv0.get(section) or {}).get("p2p_links") or []):
            nodes, ifs = link.get("nodes") or [], link.get("interfaces") or []
            if len(nodes) == 2 and len(ifs) == 2:
                peers.setdefault(nodes[0], {})[ifs[0]] = f"{nodes[1]}:{ifs[1]}"
                peers.setdefault(nodes[1], {})[ifs[1]] = f"{nodes[0]}:{ifs[0]}"
    ep_ports: dict[str, dict[str, str]] = {}
    ep_types: dict[str, str] = {}
    for _holder, key, ep in endpoints:
        name = ep["name"]
        ep_types[name] = key
        eports = ep_ports.setdefault(name, {})
        for ad in ep.get("adapters") or []:
            switches, sports = ad.get("switches") or [], ad.get("switch_ports") or []
            eps = ad.get("endpoint_ports") or [None] * len(switches)
            for i, (sw, sp) in enumerate(zip(switches, sports)):
                if sw not in facts:
                    continue
                ep_if = str(eps[i]) if i < len(eps) and eps[i] else f"eth{len(eports) + 1}"
                peers.setdefault(sw, {})[str(sp)] = f"{name}:{ep_if}"
                eports[ep_if] = f"{sw}:{sp}"
    out: dict[str, dict[str, Any]] = {}
    for host in facts:
        used = peers.get(host, {})
        highest = max([MIN_PORTS] + [int(m.group(1)) for i in used if (m := ETH_NUM_RE.match(i))])
        platform = str(facts[host].get("platform") or "")
        for entry in hostvars.get(host, {}).get("default_interfaces") or []:
            if types[host] in (entry.get("types") or []) and ({"default", platform} & set(entry.get("platforms") or [])):
                for key in ("uplink_interfaces", "mlag_interfaces", "downlink_interfaces"):
                    highest = max([highest] + _eth_numbers(entry.get(key)))
        free = [f"Ethernet{n}" for n in range(1, highest + 1) if f"Ethernet{n}" not in used]
        out[host] = {"type": types[host], "used": sorted(used, key=_if_key), "free": free,
                     "peers": dict(sorted(used.items(), key=lambda kv: _if_key(kv[0])))}
    for name, eports in ep_ports.items():
        out[name] = {"type": ep_types[name], "used": sorted(eports), "free": [], "peers": dict(sorted(eports.items()))}
    return out


# ------------------------------------------------------------------------------------------------ ops
def _validate_adapters(sess: EditSession, name: str, adapters: Any, op_index: int) -> list[dict[str, Any]]:
    if not isinstance(adapters, list) or not adapters:
        raise EditError(422, f"{name}: at least one adapter is required", op_index=op_index)
    ports = sess.ports()
    own = {(p.split(":", 1)[0], p.split(":", 1)[1]) for p in (ports.get(name) or {}).get("peers", {}).values()}
    taken: set[tuple[str, str]] = set()
    violations: list[dict[str, Any]] = []
    out: list[dict[str, Any]] = []
    for ai, ad in enumerate(adapters):
        if not isinstance(ad, dict):
            raise EditError(422, f"adapters[{ai}] must be an object", op_index=op_index)
        switches = [str(s) for s in ad.get("switches") or []]
        sports = [str(p) for p in ad.get("switch_ports") or []]
        path = f"adapters[{ai}]"
        if not switches or len(switches) != len(sports):
            violations.append({"path": path, "message": "switches and switch_ports must be non-empty and of equal length"})
            continue
        eports = ad.get("endpoint_ports")
        if eports is not None and len(eports) != len(switches):
            violations.append({"path": path, "message": "endpoint_ports must match switch_ports in length"})
        for sw, sp in zip(switches, sports):
            if sw not in ports or ports[sw]["type"] in CONNECTED_ENDPOINT_KEYS:
                violations.append({"path": path, "message": f"unknown switch {sw!r}"})
                continue
            if not IF_RE.match(sp):
                violations.append({"path": path, "message": f"invalid port {sp!r} on {sw} (expected EthernetN)"})
                continue
            if (sw, sp) in taken:
                violations.append({"path": path, "message": f"{sw} {sp} used twice"})
            elif sp in ports[sw]["used"] and (sw, sp) not in own:
                violations.append({"path": path, "message": f"{sw} {sp} is in use ({ports[sw]['peers'][sp]})"})
            taken.add((sw, sp))
        clean = {k: v for k, v in ad.items() if v is not None}
        clean["switches"], clean["switch_ports"] = switches, sports
        if eports is None:
            first = sum(len(a["switches"]) for a in out) + 1
            clean["endpoint_ports"] = [f"eth{first + n}" for n in range(len(switches))]
        if clean.get("port_channel") and not isinstance(clean["port_channel"], dict):
            violations.append({"path": path, "message": "port_channel must be an object ({mode: active})"})
        out.append(clean)
    if violations:
        raise EditError(422, f"{name}: invalid adapter(s)", violations=violations, op_index=op_index)
    return out


def _require_l3ls(sess: EditSession, op: str, i: int) -> None:
    if not sess.l3ls:
        raise EditError(409, f"{op} is only supported for generated L3LS template labs", op_index=i,
                        hint="edit the node definitions in group_vars (Files tab) for custom or static-template labs")


def apply_op(sess: EditSession, i: int, op: dict[str, Any]) -> str:
    """Apply one op to the session. Returns a short description for the commit message."""
    name = op.get("op")
    if name not in ALL_OPS:
        raise EditError(422, f"unknown op {name!r}; supported: {list(ALL_OPS)}", op_index=i)
    if name == "rename_node":
        raise EditError(409, "rename_node is not supported: hostnames are keys in inventory, group_vars and outputs",
                        op_index=i, hint="create a new lab or edit inventory.yml and group_vars by hand")

    if name == "set_uplink":
        node, switch, port = str(op.get("node") or ""), str(op.get("switch") or ""), str(op.get("switch_port") or "")
        try:
            index = int(op.get("index", 0))
        except (TypeError, ValueError):
            raise EditError(422, "index must be an integer", op_index=i) from None
        facts = sess.facts(i)
        if node not in facts:
            raise EditError(422, f"unknown node {node!r}", op_index=i)
        uplinks = facts[node].get("uplinks") or []
        if not uplinks:
            raise EditError(422, f"{node} has no uplinks", op_index=i)
        if not 0 <= index < len(uplinks):
            raise EditError(422, f"{node} has {len(uplinks)} uplink(s): index must be 0..{len(uplinks) - 1}", op_index=i)
        if switch not in facts or switch == node:
            raise EditError(422, f"unknown uplink switch {switch!r}", op_index=i,
                            violations=[{"path": "switch", "message": f"unknown switch {switch!r}"}])
        if not IF_RE.match(port):
            raise EditError(422, f"invalid port {port!r} (expected EthernetN)", op_index=i,
                            violations=[{"path": "switch_port", "message": f"invalid port {port!r}"}])
        cur = uplinks[index]
        if (switch, port) != (cur.get("peer"), cur.get("peer_interface")):
            ports = sess.ports()
            if port in ports[switch]["used"]:
                raise EditError(422, f"{switch} {port} is in use ({ports[switch]['peers'][port]})", op_index=i,
                                violations=[{"path": "switch_port", "message": f"{switch} {port} is in use"}])
        switches = [str(u.get("peer")) for u in uplinks]
        ifs = [str(u.get("peer_interface")) for u in uplinks]
        switches[index], ifs[index] = switch, port
        sess.set_node_keys(node, {"uplink_switches": switches, "uplink_switch_interfaces": ifs}, i)
        return f"{node} uplink {index} -> {switch}:{port}"

    if name in ("add_endpoint", "update_endpoint", "remove_endpoint"):
        ep_name = str(op.get("name") or "").strip()
        if not ep_name:
            raise EditError(422, "name is required", op_index=i)
        found = sess.find_endpoint(ep_name)
        if name == "add_endpoint":
            key = str(op.get("type") or "servers")
            if key not in CONNECTED_ENDPOINT_KEYS:
                raise EditError(422, f"type must be one of {list(CONNECTED_ENDPOINT_KEYS)}", op_index=i)
            if found or ep_name in sess.hostvars():
                raise EditError(422, f"{ep_name} already exists", op_index=i)
            adapters = _validate_adapters(sess, ep_name, op.get("adapters"), i)
            holder = sess.new_endpoint_holder(key)
            items = sess.endpoint_list(holder, key)
            entry: dict[str, Any] = {"name": ep_name, "adapters": adapters}
            for extra in ("rack", "description"):
                if op.get(extra):
                    entry[extra] = op[extra]
            items.append(entry)
            sess.set_endpoint_list(holder, key, items)
            return f"add {key[:-1] if key.endswith('s') else key} {ep_name}"
        if not found:
            raise EditError(422, f"endpoint {ep_name!r} not found", op_index=i)
        holder, key = found
        items = sess.endpoint_list(holder, key)
        idx = next(n for n, e in enumerate(items) if e.get("name") == ep_name)
        if name == "remove_endpoint":
            items.pop(idx)
            sess.set_endpoint_list(holder, key, items)
            return f"remove {ep_name}"
        items[idx] = {**items[idx], "adapters": _validate_adapters(sess, ep_name, op.get("adapters"), i)}
        sess.set_endpoint_list(holder, key, items)
        return f"update {ep_name}"

    # ---- knob ops (generated l3ls only)
    _require_l3ls(sess, name, i)
    assert sess.tpl is not None
    k = sess.knobs
    if name == "add_leaf_pair":
        k["leaf_pairs"] = int(k.get("leaf_pairs") or 1) + 1
        desc = f"leaf_pairs={k['leaf_pairs']}"
    elif name == "remove_last_leaf_pair":
        k["leaf_pairs"] = int(k.get("leaf_pairs") or 1) - 1
        desc = f"leaf_pairs={k['leaf_pairs']}"
    elif name == "set_l2leaf_per_pair":
        k["l2leaf_per_pair"] = op.get("n")
        desc = f"l2leaf_per_pair={op.get('n')}"
    else:
        old = int(k.get("spine_count") or 2)
        k["spine_count"] = op.get("n")
        desc = f"spine_count={op.get('n')}"
        try:
            changed = int(op.get("n")) != old  # type: ignore[arg-type]
        except (TypeError, ValueError):
            changed = True
        dropped = [n for n, keys in sess.overrides["nodes"].items() if "uplink_switches" in keys]
        if changed and dropped:
            for n in dropped:
                sess.overrides["nodes"].pop(n)
            sess.warnings.append(f"spine count changed: uplink overrides of {', '.join(sorted(dropped))} were reset")
    try:
        sess.knobs = coerce_knobs(sess.tpl, k)
    except KnobError as exc:
        raise EditError(422, "invalid knob values", op_index=i,
                        violations=[{"path": e.split(":", 1)[0], "message": e} for e in exc.errors]) from exc
    sess.invalidate()
    return desc


def _post_checks(sess: EditSession) -> None:
    """Drop overrides of nodes that no longer exist; warn about endpoints attached to removed switches."""
    nodes = set(sess.hostvars())
    if sess.generated:
        for n in [n for n in sess.overrides["nodes"] if n not in nodes]:
            sess.overrides["nodes"].pop(n)
            sess.warnings.append(f"uplink override of removed node {n} dropped")
    for _h, _k, ep in sess.endpoints():
        gone = sorted({sw for ad in ep.get("adapters") or [] for sw in ad.get("switches") or [] if sw not in nodes})
        if gone:
            sess.warnings.append(f"{ep['name']} references switch(es) not in the fabric: {', '.join(gone)}")


# ------------------------------------------------------------------------------------------------ topology preview
def preview_graph(lab: Lab, files: dict[str, str], root: Path, fabric_name: str | None,
                  knobs: dict[str, Any]) -> TopologyGraph:
    """Rebuild the topology graph from candidate inputs in a scratch dir: pyavd facts → structured configs → topology
    CSV → `build_clab_topology` (CLI configs, docs and ANTA catalogs are left to the real build)."""
    from pyavd import get_avd_facts, get_device_structured_config, get_fabric_documentation

    from ..labs.templates import load_lab_hostvars
    from .clab import build_clab_topology, client_image_for, image_for, ztp_network_facts
    from .ztp import provisioning_of

    with tempfile.TemporaryDirectory(prefix="avdlab-topo-edit-") as tmp:
        t = Path(tmp)
        for item in ("inventory.yml", "group_vars"):
            src = root / item
            if src.is_dir():
                shutil.copytree(src, t / item)
            elif src.is_file():
                shutil.copy2(src, t / item)
        if any(p.startswith("group_vars/") for p in files) and (t / "group_vars").is_dir() and "inventory.yml" in files:
            shutil.rmtree(t / "group_vars")  # generated labs: the render is the complete set
        for rel, content in files.items():
            p = t / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        validated, violations = validate_hostvars(load_lab_hostvars(t))
        if violations:
            raise EditError(422, f"{len(violations)} AVD input violation(s)", violations=violations)
        fabric = fabric_name or str(next(iter(validated.values())).get("fabric_name", "FABRIC"))
        try:
            facts = get_avd_facts(validated, all_hostvars=validated)
            structured = {h: get_device_structured_config(h, validated[h], facts, hostvars=validated[h])._as_dict()
                          for h in validated}
            fd = get_fabric_documentation(facts, structured, fabric_name=fabric, topology_csv=True)
        except EditError:
            raise
        except Exception as exc:  # noqa: BLE001 - pyavd errors (duplicate interfaces, unknown peers ...)
            raise EditError(422, f"AVD rejected the edited inputs: {exc}") from exc
        sc_dir = t / "intended" / "structured_configs"
        sc_dir.mkdir(parents=True)
        for h, sc in structured.items():
            (sc_dir / f"{h}.yml").write_text(yaml.safe_dump(sc, sort_keys=False))
        fab = t / "documentation" / "fabric"
        fab.mkdir(parents=True)
        (fab / f"{fabric}-topology.csv").write_text(fd.topology_csv)
        gv = load_group_vars_dir(t)
        extra: dict[str, Any] = {}
        prov = provisioning_of(knobs)
        if prov["mode"] == "ztp":
            extra = {"provisioning": prov, **ztp_network_facts(knobs, gv)}
        build = build_clab_topology(t, fabric, image_for(lab), serials=serials_from_group_vars(gv),
                                    mgmt_subnet=knobs.get("mgmt_subnet"), lab_name=lab.slug,
                                    host_image=client_image_for(lab), lab_id=lab.id, **extra)
        return build.graph


# ------------------------------------------------------------------------------------------------ entry points
def run_edit(lab: Lab, root: Path, ops: list[dict[str, Any]], *, knobs: dict[str, Any]) -> dict[str, Any]:
    """Blocking. Apply `ops` to a candidate copy of the inputs, validate, preview the topology. Returns
    {session, files, deleted, changed_files, diff, graph, summary}; raises EditError."""
    if not isinstance(ops, list) or not ops:
        raise EditError(422, "ops must be a non-empty list")
    sess = EditSession(lab, root, knobs=knobs)
    summary: list[str] = []
    for i, op in enumerate(ops):
        if not isinstance(op, dict):
            raise EditError(422, "each op must be an object with an `op` key", op_index=i)
        summary.append(apply_op(sess, i, op))
    if sess.generated:
        try:
            sess.rendered()
        except KnobError as exc:
            raise EditError(422, "render failed", violations=[{"message": e} for e in exc.errors]) from exc
    sess.validated()
    _post_checks(sess)
    sess.invalidate()  # overrides may have been pruned
    files, deleted = sess.output_files()
    changed: list[str] = []
    diff_parts: list[str] = []
    for rel, content in sorted(files.items()):
        p = root / rel
        old = p.read_text(encoding="utf-8") if p.is_file() else ""
        if old != content:
            changed.append(rel)
            diff_parts.extend(difflib.unified_diff(old.splitlines(keepends=True), content.splitlines(keepends=True),
                                                   f"a/{rel}", f"b/{rel}"))
    for rel in deleted:
        changed.append(rel)
        old = (root / rel).read_text(encoding="utf-8")
        diff_parts.extend(difflib.unified_diff(old.splitlines(keepends=True), [], f"a/{rel}", "/dev/null"))
    if (lab.settings or {}).get("custom_files") and sess.generated:
        sess.warnings.append("hand edits of knob-managed files were replaced by the knob render: "
                             + ", ".join((lab.settings or {}).get("custom_files") or []))
    graph = preview_graph(lab, files, root, None if sess.generated else sess.fabric_name, sess.knobs)
    graph.lab_id = lab.id
    return {"session": sess, "files": files, "deleted": deleted, "changed_files": sorted(changed),
            "diff": "".join(diff_parts), "graph": graph, "summary": summary}


def lab_ports(lab: Lab, root: Path, *, knobs: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return EditSession(lab, root, knobs=knobs).ports()


# ------------------------------------------------------------------------------------------------ layout
def read_layout(root: Path) -> dict[str, Any] | None:
    p = root / LAYOUT_REL
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return None
    return {"positions": data.get("positions") or {}, "locked": bool(data.get("locked", False))}


def layout_json(positions: dict[str, dict[str, float]], locked: bool) -> str:
    return json.dumps({"positions": positions, "locked": locked}, indent=2, sort_keys=True) + "\n"
