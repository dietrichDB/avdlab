"""AI lab designer: prompt → structured, validated AVD input changes.

Hard rule of the project: AVD inputs are the source of truth and the model never writes YAML into a lab. It can only
call one of two tools:

* `propose_lab_changes {knobs, topology_ops, summary, assumptions}` — partial knob values of the lab's template (the
  JSON schema is generated from the template descriptor: type, min/max, choices, help) and topology edit ops (the
  ops of `topology/edit.py`, same argument shapes as `POST /topology/{id}/edit`);
* `ask_user {question}` — one clarifying question.

Every proposal is dry-run through the existing code paths before it is returned (knob coercion + render + pyavd
validation, `topology.edit.run_edit` for ops, `preview_graph` for the topology), so the UI never shows an unvalidated
plan. A proposal that fails validation is sent back to the model once with the violations ("repair round").
Apply goes through `PATCH /labs/{id}` (knobs) and `POST /topology/{id}/edit` (ops), which commit to the lab's git.

What is sent to the provider: the template descriptor (knob keys, help, defaults), the lab's knob values *without*
secret knobs, and a topology summary (hostnames, node types, endpoints, free switch ports). Never secrets.
"""
from __future__ import annotations

import asyncio
import copy
import difflib
import json
import logging
import re
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

from ..core.config import settings
from ..core.db import Lab
from ..labs import service as lab_service
from ..labs.templates import (
    CONNECTED_ENDPOINT_KEYS,
    Knob,
    KnobError,
    TemplateDescriptor,
    _knob_default,
    coerce_knobs,
    get_template,
    inventory_hosts,
    load_base,
    render_lab,
    validate_hostvars,
    load_templates,
)
from ..topology import edit as topo_edit

log = logging.getLogger("avdlab.ai")

MASK = "********"
MAX_OPS = 50
VALUE_CHARS = 4000  # per knob value in the prompt
PROPOSE = "propose_lab_changes"
ASK = "ask_user"


# ================================================================================================ rate limit
_calls: dict[str, deque[float]] = defaultdict(deque)


def reset_rate_limit() -> None:
    _calls.clear()


def rate_limited(username: str, now: float | None = None) -> bool:
    now = time.monotonic() if now is None else now
    q = _calls[username]
    while q and now - q[0] > 3600.0:
        q.popleft()
    if len(q) >= max(1, int(settings.ai_rate_limit)):
        return True
    q.append(now)
    return False


# ================================================================================================ tool schemas
def _secret_keys(tpl: TemplateDescriptor | None) -> set[str]:
    return {k.key for k in tpl.knobs if k.type == "secret"} if tpl else set()


def knob_defaults(tpl: TemplateDescriptor) -> dict[str, Any]:
    base_gv, _ = load_base(tpl)
    return {k.key: _knob_default(k, base_gv) for k in tpl.knobs}


def knob_schema(k: Knob, default: Any) -> dict[str, Any]:
    """JSON schema of one knob, generated from the descriptor (type/min/max/choices/help)."""
    desc = " — ".join(x for x in (k.label, k.help) if x)
    s: dict[str, Any]
    match k.type:
        case "int":
            s = {"type": "integer"}
            if k.min is not None:
                s["minimum"] = k.min
            if k.max is not None:
                s["maximum"] = k.max
        case "bool":
            s = {"type": "boolean"}
        case "cidr":
            s = {"type": "string"}
            desc += " (IPv4 prefix a.b.c.d/nn)"
        case "ipv4":
            s = {"type": "string"}
            desc += " (IPv4 address)"
        case "pool":
            s = {"type": "string"}
            desc += " (AVD pool: comma-separated prefixes or a.b.c.d-e.f.g.h ranges)"
        case "object":
            s = {"type": "object"}
            desc += " (partial mapping, merged over the current value"
            desc += f"; every value is a {k.value_type})" if k.value_type else ")"
        case "yaml":
            s = {"type": "array", "items": {"type": "object"}} if isinstance(default, list) and k.key != "dns_servers" \
                else ({"type": "array", "items": {"type": "string"}} if k.key == "dns_servers" else {"type": "object"})
            desc += " (AVD data model as JSON; replaces the whole value — include existing entries you keep)"
        case _:
            s = {"type": "string"}
            if k.choices:
                s["enum"] = [str(c) for c in k.choices]
    s["description"] = desc.strip(" —")[:900]
    return s


_ADAPTER = {
    "type": "object",
    "properties": {
        "switches": {"type": "array", "items": {"type": "string"}, "description": "switch hostnames (two leafs of one MLAG pair for a dual-homed server)"},
        "switch_ports": {"type": "array", "items": {"type": "string"}, "description": "EthernetN per switch (index-aligned), must be free"},
        "endpoint_ports": {"type": "array", "items": {"type": "string"}, "description": "optional server-side names (eth1, eth2)"},
        "port_channel": {"type": "object", "properties": {"mode": {"type": "string", "enum": ["active", "passive", "on"]}},
                         "description": "set {mode: active} for an MLAG port-channel"},
        "mode": {"type": "string", "enum": ["access", "trunk"]},
        "vlans": {"type": "string", "description": "VLAN id or range list, e.g. '110' or '110,120'"},
        "native_vlan": {"type": "integer"},
        "description": {"type": "string"},
    },
    "required": ["switches", "switch_ports"],
}


def op_schemas(l3ls: bool) -> list[dict[str, Any]]:
    def op(name: str, desc: str, props: dict[str, Any] | None = None, required: list[str] | None = None) -> dict[str, Any]:
        return {"type": "object", "description": desc,
                "properties": {"op": {"type": "string", "const": name}, **(props or {})},
                "required": ["op", *(required or [])]}

    ep_type = {"type": "string", "enum": list(CONNECTED_ENDPOINT_KEYS), "description": "connected endpoint list (default servers)"}
    out = [
        op("add_endpoint", "connect a new endpoint (server, firewall …) to free switch ports",
           {"name": {"type": "string"}, "type": ep_type, "adapters": {"type": "array", "items": _ADAPTER},
            "description": {"type": "string"}, "rack": {"type": "string"}}, ["name", "adapters"]),
        op("update_endpoint", "replace the adapters of an existing endpoint",
           {"name": {"type": "string"}, "adapters": {"type": "array", "items": _ADAPTER}}, ["name", "adapters"]),
        op("remove_endpoint", "remove an endpoint", {"name": {"type": "string"}}, ["name"]),
        op("set_uplink", "move uplink number `index` of a leaf to another switch/port",
           {"node": {"type": "string"}, "index": {"type": "integer", "minimum": 0}, "switch": {"type": "string"},
            "switch_port": {"type": "string"}}, ["node", "index", "switch", "switch_port"]),
    ]
    if l3ls:
        out += [
            op("add_leaf_pair", "add one MLAG L3 leaf pair (leaf_pairs + 1)"),
            op("remove_last_leaf_pair", "remove the highest-numbered leaf pair (leaf_pairs - 1)"),
            op("set_l2leaf_per_pair", "number of L2 leafs below every leaf pair", {"n": {"type": "integer"}}, ["n"]),
            op("set_spine_count", "number of spines", {"n": {"type": "integer"}}, ["n"]),
        ]
    return out


def tools(tpl: TemplateDescriptor | None, *, lab_mode: bool, l3ls: bool) -> list[dict[str, Any]]:
    secret = _secret_keys(tpl)
    defaults = knob_defaults(tpl) if tpl else {}
    knob_props = {k.key: knob_schema(k, defaults.get(k.key)) for k in (tpl.knobs if tpl else []) if k.key not in secret}
    props: dict[str, Any] = {
        "summary": {"type": "string", "description": "2–4 sentences: what changes and why, in the user's language"},
        "assumptions": {"type": "array", "items": {"type": "string"},
                        "description": "every value you chose that the user did not state"},
        "knobs": {"type": "object", "properties": knob_props, "additionalProperties": False,
                  "description": "only the knobs you change (partial)"},
    }
    if lab_mode:
        props["topology_ops"] = {"type": "array", "items": {"anyOf": op_schemas(l3ls)}, "maxItems": MAX_OPS,
                                 "description": "topology edit operations, applied in order after the knobs"}
    return [
        {"name": PROPOSE, "description": "Propose AVD input changes for the lab (validated by the server with pyavd; "
                                         "nothing changes until the user presses Apply).",
         "input_schema": {"type": "object", "properties": props, "required": ["summary", "knobs"]}},
        {"name": ASK, "description": "Ask the user ONE short clarifying question when an essential detail is missing.",
         "input_schema": {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]}},
    ]


# ================================================================================================ context / prompt
def _short(v: Any, n: int = VALUE_CHARS) -> str:
    s = json.dumps(v, default=str, separators=(",", ":"))
    return s if len(s) <= n else s[:n] + "…(truncated)"


def public_knobs(tpl: TemplateDescriptor | None, values: dict[str, Any]) -> dict[str, Any]:
    secret = _secret_keys(tpl)
    return {k: v for k, v in values.items() if k not in secret}


def topology_summary(lab: Lab | None, tpl: TemplateDescriptor | None, knobs: dict[str, Any]) -> dict[str, Any]:
    """Blocking. Hostnames/types, endpoints and free switch ports (lab mode; best effort, never raises)."""
    out: dict[str, Any] = {}
    try:
        if lab is not None:
            sess = topo_edit.EditSession(lab, lab_service.lab_dir(lab), knobs=knobs)
            hv = sess.hostvars()
            out["nodes"] = {h: str(v.get("type") or "?") for h, v in hv.items()}
            out["endpoints"] = [
                {"name": ep["name"], "type": key, "ports": [f"{sw}:{sp}" for ad in ep.get("adapters") or []
                                                            for sw, sp in zip(ad.get("switches") or [], ad.get("switch_ports") or [])]}
                for _h, key, ep in sess.endpoints()]
            try:
                ports = sess.ports()
                out["free_ports"] = {h: p["free"][:16] for h, p in ports.items()
                                     if p["type"] not in CONNECTED_ENDPOINT_KEYS and p["type"] not in ("spine", "super_spine")}
            except Exception:  # noqa: BLE001 - invalid inputs: facts unavailable
                pass
        elif tpl is not None and tpl.generator != "none":
            r = render_lab(tpl, knobs, slug="preview")
            out["nodes"] = {h: str(v.get("type") or "?") for h, v in r.hostvars().items()}
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"topology summary unavailable: {str(exc)[:200]}"
    return out


SYSTEM_BASE = """You are the lab designer of avdlab, a web app that builds, deploys (containerlab / Arista Cloud Test) and \
validates (ANTA) Arista AVD labs. AVD = Arista Validated Designs: the eos_designs data model (group_vars) is turned \
into structured configs and EOS CLI by pyavd.

HARD RULES
- AVD inputs are the source of truth. You never write YAML, CLI or files. You change a lab ONLY by calling \
`propose_lab_changes` with (a) knob values of the lab template and/or (b) topology edit operations.
- The server validates every proposal with pyavd and shows the user a diff; nothing changes until the user presses Apply.
- If an essential detail is missing and there is no sensible default, call `ask_user` with ONE short question. \
Otherwise propose, and list every value you chose yourself in `assumptions`.
- Only include knobs you change. List/YAML knobs (tenants, servers, dns_servers …) replace the whole value: include \
the existing entries you want to keep.
- Never set secret knobs (passwords, tokens) — tell the user to set them in the lab Settings.
- Write `summary`, `assumptions` and questions in the user's language.

AVD VOCABULARY
spine / l3leaf (routed leaf; two l3leafs with the same node group form an MLAG pair and share a VTEP) / l2leaf \
(L2-only, uplinked to a leaf pair); tenants[] → vrfs[] (vrf_vni, L3VNI) → svis[] (id = VLAN id, name, \
ip_address_virtual a.b.c.d/nn anycast gateway, enabled: true — SVIs render shutdown without it); tenants[].l2vlans[] \
(id, name) for VLANs without a gateway; mac_vrf_vni_base (L2VNI = base + VLAN id). Connected endpoints \
(servers[] …): name + adapters[] {switches, switch_ports, endpoint_ports, mode access|trunk, vlans, port_channel \
{mode: active}}; a server dual-homed to both leafs of an MLAG pair uses ONE adapter with both switches, the same port \
on each and port_channel {mode: active}. CloudVision: knob `cloudvision` {mode: none|cvaas|onprem, region (cvaas), \
servers (onprem)}; CVaaS needs public DNS (dns_servers 1.1.1.1, 8.8.8.8)."""


def system_prompt(tpl: TemplateDescriptor | None, *, lab: Lab | None, values: dict[str, Any],
                  topo: dict[str, Any], l3ls: bool) -> str:
    from ..learn.knobs import knob_learn

    parts = [SYSTEM_BASE]
    if tpl is not None:
        parts.append(f"TEMPLATE {tpl.name} — {tpl.title} (generator {tpl.generator})\n{tpl.description.strip()}")
        if tpl.notes:
            parts.append(f"Template notes: {tpl.notes.strip()}")
        secret = _secret_keys(tpl)
        lines = []
        for k in tpl.knobs:
            if k.key in secret:
                lines.append(f"- {k.key} (secret: never set, never shown)")
                continue
            rng = f" [{k.min}..{k.max}]" if k.min is not None or k.max is not None else ""
            line = f"- {k.key} ({k.type}{rng}): {k.help or k.label or ''}".rstrip()
            learn = knob_learn(tpl, k.key) or {}
            if learn.get("pitfalls"):
                line += " Pitfalls: " + " ".join(str(p) for p in learn["pitfalls"][:3])
            line += f"\n  current: {_short(values.get(k.key))}"
            lines.append(line)
        parts.append("KNOBS (key (type): help; current value)\n" + "\n".join(lines))
    if lab is not None:
        parts.append(f"CURRENT LAB {lab.name!r} (state {lab.state.value}, target {lab.target})")
        ops = ("set_uplink, add_endpoint, update_endpoint, remove_endpoint"
               + (", add_leaf_pair, remove_last_leaf_pair, set_l2leaf_per_pair, set_spine_count" if l3ls else ""))
        parts.append(f"TOPOLOGY OPS available: {ops}. Knob ops (counts) and the equivalent knobs do the same thing — "
                     "use one, not both. Ops run after the knobs, in order; endpoint ops need free switch ports. "
                     "Switches added by knob changes in the same proposal exist for the ops.")
    else:
        parts.append("NEW LAB: there is no lab yet. Propose knobs only (servers go in the `servers` knob); "
                     "the user fills the New-lab wizard with them.")
    if tpl is not None and tpl.generator == "none":
        parts.append("This is a custom lab (files are the source of truth): only topology ops can change it.")
    if topo:
        parts.append("TOPOLOGY SUMMARY (from the current inputs)\n" + _short(topo, 6000))
    return "\n\n".join(parts)


# ================================================================================================ dry run
def _diff(root: Path | None, old: dict[str, str], files: dict[str, str], deleted: list[str]) -> tuple[list[str], str]:
    changed: list[str] = []
    parts: list[str] = []
    for rel, content in sorted(files.items()):
        before = old.get(rel)
        if before is None and root is not None:
            p = root / rel
            before = p.read_text(encoding="utf-8") if p.is_file() else ""
        before = before or ""
        if before != content:
            changed.append(rel)
            parts.extend(difflib.unified_diff(before.splitlines(keepends=True), content.splitlines(keepends=True),
                                              f"a/{rel}", f"b/{rel}"))
    for rel in deleted:
        before = old.get(rel) if rel in old else ((root / rel).read_text(encoding="utf-8") if root else "")
        changed.append(rel)
        parts.extend(difflib.unified_diff((before or "").splitlines(keepends=True), [], f"a/{rel}", "/dev/null"))
    return changed, "".join(parts)


def _knob_violations(exc: KnobError) -> list[dict[str, Any]]:
    return [{"path": "knobs." + e.split(":", 1)[0] if ":" in e else "knobs", "message": e} for e in exc.errors]


def _edit_violations(exc: topo_edit.EditError, prefix: str = "topology_ops") -> list[dict[str, Any]]:
    where = f"{prefix}[{exc.op_index}]" if exc.op_index is not None else prefix
    out = [{"path": f"{where}{'.' + v['path'] if v.get('path') else ''}", "message": v.get("message", exc.message)}
           for v in exc.violations]
    if exc.hint:
        out.append({"path": where, "message": f"hint: {exc.hint}"})
    return out or [{"path": where, "message": exc.message}]


def sanitize(tpl: TemplateDescriptor | None, knobs: Any, ops: Any) -> tuple[dict[str, Any], list[dict[str, Any]], list[str], list[dict[str, Any]]]:
    """(knobs, ops, notes, violations): drops secret/masked knobs, checks shapes."""
    notes: list[str] = []
    violations: list[dict[str, Any]] = []
    if knobs is None:
        knobs = {}
    if not isinstance(knobs, dict):
        violations.append({"path": "knobs", "message": "knobs must be an object"})
        knobs = {}
    secret = _secret_keys(tpl)
    clean: dict[str, Any] = {}
    for k, v in knobs.items():
        if k in secret:
            notes.append(f"secret knob {k} was not changed (set it yourself in the lab Settings)")
        elif v != MASK:
            clean[k] = v
    if ops is None:
        ops = []
    if not isinstance(ops, list) or not all(isinstance(o, dict) and o.get("op") for o in ops):
        violations.append({"path": "topology_ops", "message": "topology_ops must be a list of objects with an `op` key"})
        ops = []
    if len(ops) > MAX_OPS:
        violations.append({"path": "topology_ops", "message": f"at most {MAX_OPS} ops per proposal"})
    return clean, [dict(o) for o in ops], notes, violations


def _changes(old: dict[str, Any], new: dict[str, Any], keys: list[str]) -> list[dict[str, Any]]:
    return [{"key": k, "old": old.get(k), "new": new.get(k)} for k in keys if old.get(k) != new.get(k)]


def dry_run_lab(lab: Lab, knobs: dict[str, Any], ops: list[dict[str, Any]]) -> dict[str, Any]:
    """Blocking. Validate knobs (+ ops) against the lab exactly like PATCH /labs + POST /topology/edit would."""
    from ..labs.api import _render_and_validate, _stored_knobs, _template_or_none

    tpl = _template_or_none(lab.template)
    root = lab_service.lab_dir(lab)
    out: dict[str, Any] = {"valid": False, "violations": [], "changed_files": [], "diff": "", "warnings": [],
                           "graph": None, "knob_changes": [], "knobs": {}}
    stored = _stored_knobs(lab)
    if tpl is None:
        if knobs:
            out["violations"].append({"path": "knobs", "message": f"template {lab.template!r} no longer exists: knobs cannot change"})
            return out
        new_values = stored
    else:
        old = {k: v for k, v in stored.items() if tpl.knob(k)}
        try:
            old_values = coerce_knobs(tpl, old)
        except KnobError:
            old_values = old
        try:
            new_values = coerce_knobs(tpl, {**old, **knobs})
        except KnobError as exc:
            out["violations"] = _knob_violations(exc)
            return out
        keys = [k for k in knobs if k in new_values]
        out["knob_changes"] = _changes(public_knobs(tpl, old_values), public_knobs(tpl, new_values), keys)
        out["knobs"] = {c["key"]: c["new"] for c in out["knob_changes"]}
    if ops:
        try:
            res = topo_edit.run_edit(lab, root, ops, knobs=new_values)
        except topo_edit.EditError as exc:
            out["violations"] = _edit_violations(exc)
            return out
        except KnobError as exc:
            out["violations"] = _knob_violations(exc)
            return out
        out.update(changed_files=res["changed_files"], diff=res["diff"], warnings=list(res["session"].warnings),
                   graph=res["graph"].model_dump(), ops=res["summary"])
    elif out["knob_changes"] and tpl is not None:
        overrides = (lab.settings or {}).get("topology_overrides")
        files, violations = _render_and_validate(tpl, new_values, lab.slug, overrides)
        if violations:
            out["violations"] = [{**v, "path": v.get("path") or "knobs"} for v in violations]
            return out
        if files:
            existing = {str(f.relative_to(root)) for f in (root / "group_vars").rglob("*") if f.is_file()} \
                if (root / "group_vars").is_dir() else set()
            deleted = sorted(p for p in existing if p not in files)
            out["changed_files"], out["diff"] = _diff(root, {}, files, deleted)
            try:
                graph = topo_edit.preview_graph(lab, files, root, None, new_values)
                graph.lab_id = lab.id
                out["graph"] = graph.model_dump()
            except topo_edit.EditError as exc:
                out["violations"] = _edit_violations(exc, "knobs")
                return out
    out["valid"] = True
    if lab.state.value in ("deployed", "validated") and out["changed_files"]:
        out["warnings"].append(f"lab is {lab.state.value}: build and redeploy to apply the change")
    return out


KNOB_OP_MAP = {"set_spine_count": "spine_count", "set_l2leaf_per_pair": "l2leaf_per_pair"}


def dry_run_template(tpl: TemplateDescriptor, base: dict[str, Any], knobs: dict[str, Any],
                     ops: list[dict[str, Any]]) -> dict[str, Any]:
    """Blocking. New-lab flow: validate knobs over the wizard's current values; diff against those values' render."""
    out: dict[str, Any] = {"valid": False, "violations": [], "changed_files": [], "diff": "", "warnings": [],
                           "graph": None, "knob_changes": [], "knobs": {}}
    knobs = dict(knobs)
    base = {k: v for k, v in (base or {}).items() if tpl.knob(k) and v != MASK}
    for i, op in enumerate(ops):  # count ops are just knobs before a lab exists
        name = op.get("op")
        if name in KNOB_OP_MAP:
            knobs[KNOB_OP_MAP[name]] = op.get("n")
        elif name in ("add_leaf_pair", "remove_last_leaf_pair") and tpl.knob("leaf_pairs"):
            cur = int(knobs.get("leaf_pairs", base.get("leaf_pairs", knob_defaults(tpl).get("leaf_pairs") or 1)))
            knobs["leaf_pairs"] = cur + (1 if name == "add_leaf_pair" else -1)
        else:
            out["violations"].append({"path": f"topology_ops[{i}]",
                                      "message": f"{name} needs an existing lab: use knobs (e.g. `servers`) for a new lab"})
    if out["violations"]:
        return out
    try:
        old_values = coerce_knobs(tpl, base)
        new_values = coerce_knobs(tpl, {**base, **knobs})
    except KnobError as exc:
        out["violations"] = _knob_violations(exc)
        return out
    keys = [k for k in knobs if k in new_values]
    out["knob_changes"] = _changes(public_knobs(tpl, old_values), public_knobs(tpl, new_values), keys)
    out["knobs"] = {c["key"]: c["new"] for c in out["knob_changes"]}
    if tpl.generator != "none":
        try:
            rendered = render_lab(tpl, new_values, slug="preview")
            before = render_lab(tpl, old_values, slug="preview").files()
        except KnobError as exc:
            out["violations"] = _knob_violations(exc)
            return out
        validated, violations = validate_hostvars(rendered.hostvars())
        if violations:
            out["violations"] = [{**v, "path": v.get("path") or "knobs"} for v in violations]
            return out
        try:
            from pyavd import get_avd_facts, get_device_structured_config

            facts = get_avd_facts(validated, all_hostvars=validated)
            for h in validated:  # catches duplicate interfaces & co. (same depth as the lab path's preview_graph)
                get_device_structured_config(h, validated[h], facts, hostvars=validated[h])
        except Exception as exc:  # noqa: BLE001 - AristaAvdError & co.
            out["violations"] = [{"path": "knobs", "message": f"AVD rejected the inputs: {exc}"}]
            return out
        files = rendered.files()
        out["changed_files"], out["diff"] = _diff(None, before, files, sorted(set(before) - set(files)))
        out["nodes"] = len(inventory_hosts(rendered.inventory))
    out["valid"] = True
    return out


# ================================================================================================ model turns
def _clean_messages(history: list[dict[str, str]], prompt: str) -> list[dict[str, str]]:
    """user/assistant only, consecutive same-role turns merged, first turn is the user's."""
    msgs: list[dict[str, str]] = []
    for m in [*history, {"role": "user", "content": prompt}]:
        role = m.get("role")
        content = str(m.get("content") or "").strip()[:8000]
        if role not in ("user", "assistant") or not content:
            continue
        if msgs and msgs[-1]["role"] == role:
            msgs[-1]["content"] += "\n\n" + content
        else:
            msgs.append({"role": role, "content": content})
    while msgs and msgs[0]["role"] != "user":
        msgs.pop(0)
    return msgs


_JSON_RX = re.compile(r"\{.*\}", re.S)


def pick_call(result: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    """The first known tool call; local models without tool support sometimes answer with the JSON as text."""
    for c in result.get("tool_calls") or []:
        if c.get("name") in (PROPOSE, ASK):
            return c["name"], c.get("arguments") or {}
    text = result.get("text") or ""
    m = _JSON_RX.search(text)
    if m:
        try:
            data = json.loads(m.group(0))
        except ValueError:
            return None, {}
        if isinstance(data, dict):
            if data.get("name") in (PROPOSE, ASK) and isinstance(data.get("arguments") or data.get("parameters"), dict):
                return data["name"], data.get("arguments") or data.get("parameters")
            if "knobs" in data or "topology_ops" in data:
                return PROPOSE, data
            if set(data) == {"question"}:
                return ASK, data
    return None, {}


def proposal_text(args: dict[str, Any]) -> str:
    """How a proposal is fed back as an assistant turn (history and repair round)."""
    return f"I called {PROPOSE} with: " + _short({k: args.get(k) for k in ("summary", "knobs", "topology_ops", "assumptions")
                                                  if args.get(k) is not None}, 12000)


def repair_text(violations: list[dict[str, Any]]) -> str:
    lines = "\n".join(f"- {v.get('path') or ''}: {v.get('message')}".replace("- : ", "- ") for v in violations[:40])
    return ("The server validated your proposal with pyavd and it FAILED:\n" + lines
            + f"\nFix these problems and call {PROPOSE} again with the corrected, complete proposal "
              "(or ask_user if you need the user's input).")


async def run_design(provider: Any, *, system: str, messages: list[dict[str, str]], tool_list: list[dict[str, Any]],
                     validate: Any) -> dict[str, Any]:
    """Call the model; on a proposal run `validate(knobs, ops)` (async); one repair round when invalid."""
    convo = list(messages)
    repaired = False
    last: dict[str, Any] | None = None
    for attempt in range(2):
        result = await provider.tool_call(system, convo, tool_list, "any")
        name, args = pick_call(result)
        if name == ASK:
            q = str(args.get("question") or "").strip() or "Could you give more detail?"
            return {"kind": "question", "question": q, "summary": q, "assistant_message": q, "repaired": repaired}
        if name != PROPOSE:
            text = (result.get("text") or "").strip() or "The model did not return a proposal."
            if last is not None:  # repair turn answered in prose: keep the invalid proposal
                return last
            return {"kind": "text", "text": text, "summary": text, "assistant_message": text, "repaired": repaired}
        summary = str(args.get("summary") or "").strip()[:2000]
        assumptions = [str(a)[:500] for a in (args.get("assumptions") or []) if str(a).strip()][:20] \
            if isinstance(args.get("assumptions"), list) else []
        dry = await validate(args.get("knobs"), args.get("topology_ops"))
        assumptions += dry.pop("notes", [])
        last = {"kind": "proposal", "summary": summary or "Proposed changes", "assumptions": assumptions,
                "knobs": dry.pop("knobs", {}), "knob_changes": dry.pop("knob_changes", []),
                "topology_ops": dry.pop("topology_ops", []), "dry_run": dry, "repaired": repaired,
                "assistant_message": proposal_text(args)}
        if dry["valid"] or attempt == 1:
            return last
        log.info("ai proposal invalid (%d violation(s)): repair round", len(dry["violations"]))
        convo = convo + [{"role": "assistant", "content": proposal_text(args)},
                         {"role": "user", "content": repair_text(dry["violations"])}]
        repaired = True
    return last or {"kind": "text", "text": "no answer", "summary": "no answer", "assistant_message": "", "repaired": repaired}


def validator_for(lab: Lab | None, tpl: TemplateDescriptor | None, base: dict[str, Any]) -> Any:
    async def validate(knobs: Any, ops: Any) -> dict[str, Any]:
        k, o, notes, violations = sanitize(tpl, knobs, ops)
        if violations:
            return {"valid": False, "violations": violations, "changed_files": [], "diff": "", "warnings": [],
                    "graph": None, "knob_changes": [], "knobs": k, "topology_ops": o, "notes": notes}
        if lab is not None:
            res = await asyncio.to_thread(dry_run_lab, lab, k, o)
        else:
            assert tpl is not None
            res = await asyncio.to_thread(dry_run_template, tpl, base, k, o)
            o = []  # count ops were folded into the knobs
        if not res["valid"] and not res.get("knobs"):
            res["knobs"] = k  # show what the model asked for
        res["topology_ops"] = o
        res["notes"] = notes
        return res

    return validate


def template_for(name: str) -> TemplateDescriptor | None:
    try:
        return get_template(name)
    except KeyError:
        return None


def is_l3ls(tpl: TemplateDescriptor | None) -> bool:
    return tpl is not None and tpl.generator == "l3ls"


def current_values(tpl: TemplateDescriptor | None, values: dict[str, Any]) -> dict[str, Any]:
    if tpl is None:
        return dict(values)
    merged = {**knob_defaults(tpl), **{k: v for k, v in values.items() if tpl.knob(k)}}
    return public_knobs(tpl, copy.deepcopy(merged))


# ================================================================================================ from scratch (0.10.0)
CHOOSE_TEMPLATE = "choose_template"


def template_catalog() -> list[dict[str, Any]]:
    """What the model sees when it has to pick a template: name, title, description, tags, node count, what it teaches."""
    out: list[dict[str, Any]] = []
    for name, t in load_templates().items():
        if name == "custom":
            continue
        out.append({"name": name, "title": t.title, "description": t.description, "tags": list(t.tags),
                    "node_count": t.node_count(), "generator": t.generator, "notes": t.notes,
                    "learn": (t.learn or "")[:400], "knobs": [k.key for k in t.knobs]})
    return out


def choose_template_tool(catalog: list[dict[str, Any]]) -> dict[str, Any]:
    return {"name": CHOOSE_TEMPLATE,
            "description": "Pick the avdlab template (an Arista AVD example design) that best fits the user's request. "
                           "Prefer single-dc-l3ls for any generic spine/leaf, EVPN/VXLAN or 'data centre fabric' request; it is the "
                           "only fully knob-driven template (spines, leaf pairs, L2 leaves, tenants, servers, CloudVision).",
            "input_schema": {"type": "object", "additionalProperties": False,
                             "required": ["template", "reason", "lab_name"],
                             "properties": {"template": {"type": "string", "enum": [c["name"] for c in catalog]},
                                            "reason": {"type": "string", "description": "one sentence for the user"},
                                            "lab_name": {"type": "string", "description": "short lowercase slug-like lab name, e.g. acme-dc-lab"}}}}


def choose_system_prompt(catalog: list[dict[str, Any]]) -> str:
    lines = [f"- {c['name']}: {c['title']} — {c['description']} tags={','.join(c['tags'])} nodes={c['node_count']} "
             f"generator={c['generator']} knobs={','.join(c['knobs'][:12])}" + (f" notes={c['notes']}" if c.get('notes') else "")
             for c in catalog]
    return ("You are the avdlab lab designer. avdlab builds Arista AVD labs from templates (AVD example designs) whose inputs are "
            "knobs validated by pyavd. Step 1 of creating a lab from a sentence: choose the template. Available templates:\n"
            + "\n".join(lines) + "\nCall choose_template exactly once. If the request is ambiguous between templates, still choose "
            "the closest one and say why; the user refines knobs afterwards.")


async def choose_template(provider: Any, prompt: str, history: list[dict[str, str]]) -> dict[str, Any]:
    """One tool call → {template, reason, lab_name}. Falls back to single-dc-l3ls when the model does not call the tool."""
    catalog = template_catalog()
    names = {c["name"] for c in catalog}
    res = await provider.tool_call(choose_system_prompt(catalog), _clean_messages(history, prompt), [choose_template_tool(catalog)],
                                   tool_choice=CHOOSE_TEMPLATE)
    args: dict[str, Any] = {}
    for c in res.get("tool_calls") or []:
        if c.get("name") == CHOOSE_TEMPLATE:
            args = c.get("arguments") or {}
            break
    else:  # plain-text answer (local models): accept a JSON object with a template key
        m = _JSON_RX.search(res.get("text") or "")
        try:
            data = json.loads(m.group(0)) if m else None
        except ValueError:
            data = None
        if isinstance(data, dict):
            args = data.get("arguments") if isinstance(data.get("arguments"), dict) else data
    if args.get("template") not in names:
        return {"template": "single-dc-l3ls", "reason": "default: fully knob-driven spine/leaf EVPN fabric", "lab_name": "ai-lab",
                "fallback": True}
    slug = re.sub(r"[^a-z0-9-]+", "-", str(args.get("lab_name") or "ai-lab").lower()).strip("-")[:40] or "ai-lab"
    return {"template": str(args["template"]), "reason": str(args.get("reason") or ""), "lab_name": slug, "fallback": False}

