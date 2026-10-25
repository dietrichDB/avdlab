#!/usr/bin/env python3
"""Export the AVD 6.4 input schemas (eos_designs + eos_cli_config_gen) to the compact JSON the learn/ schema browser reads.

The installed pyavd wheel only ships pickles (fully $ref-expanded, ~20 MB for eos_designs); the AVD repo checkout ships
the merged `*.schema.yml` files built from `schema_fragments/`. This script reads the repo checkout (preferred) or the
installed pickles (fallback), resolves `eos_designs#` $refs itself, keeps `eos_cli_config_gen#` refs as links (no
expansion of the whole EOS Config model under every `structured_config`), and flattens the tree to

    {"schema": "eos_designs", "avd_version": "6.4.0", "source": "...",
     "aliases": {"l3leaf": "<node_type_keys.key>", "servers": "<connected_endpoints_keys.key>", ...},
     "sections": {"<table>": "<data-models.md heading slug>"},
     "nodes": {"<dotted path>": {"t": type, "d": description, "df": default, "vv": valid_values, "min": .., "max": ..,
                                 "req": bool, "dep": deprecation, "tbl": table, "k": [child names], "it": item type,
                                 "pk": primary_key, "ref": "eos_cli_config_gen:<path>", "fmt": format, "pat": pattern}}}

List items are addressed without an index (`router_bgp.peer_groups.name`). Output (committed, used inside the docker
image where the AVD repo is not present): backend/avdlab/learn/schema/{eos_designs,eos_cli_config_gen}.json

Usage: python3 scripts/export_schema.py [--avd-repo /home/claude/arista/avd] [--out backend/avdlab/learn/schema]
"""
from __future__ import annotations

import argparse
import copy
import json
import pickle
import re
import sys
from pathlib import Path
from typing import Any

import yaml

REPO_DEFAULT = Path("/home/claude/arista/avd")
OUT_DEFAULT = Path(__file__).resolve().parents[1] / "backend" / "avdlab" / "learn" / "schema"
SCHEMAS = {
    "eos_designs": ("python-avd/pyavd/_eos_designs/schema", "ansible_collections/arista/avd/roles/eos_designs/docs/data-models.md"),
    "eos_cli_config_gen": ("python-avd/pyavd/_eos_cli_config_gen/schema",
                           "ansible_collections/arista/avd/roles/eos_cli_config_gen/docs/data-models.md"),
}
MAX_DEFAULT_JSON = 600  # larger defaults (e.g. the node_type_keys list) are summarised
Loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


# ------------------------------------------------------------------------------------------------ loading
def _deep_merge(base: Any, over: Any) -> Any:
    """`over` wins; dicts merged recursively (AVD: keys next to a $ref take precedence over the referenced schema)."""
    if isinstance(base, dict) and isinstance(over, dict):
        out = dict(base)
        for k, v in over.items():
            out[k] = _deep_merge(base[k], v) if k in base else v
        return out
    return over


def load_from_repo(repo: Path, name: str) -> tuple[dict[str, Any], str] | None:
    sdir = repo / SCHEMAS[name][0]
    merged = sdir / f"{name}.schema.yml"
    if merged.is_file():
        return yaml.load(merged.read_text(), Loader=Loader), str(merged.relative_to(repo))
    frags = sdir / "schema_fragments"
    if frags.is_dir():  # merge fragments like schema_tools/build_schemas.py does
        data: dict[str, Any] = {}
        for f in sorted(frags.glob("*.y*ml")):
            data = _deep_merge(data, yaml.load(f.read_text(), Loader=Loader) or {})
        return data, str(frags.relative_to(repo))
    return None


def load_from_pickle(name: str) -> tuple[dict[str, Any], str]:
    import pyavd

    sub = "_eos_designs" if name == "eos_designs" else "_eos_cli_config_gen"
    path = Path(pyavd.__file__).parent / sub / "schema" / f"{name}.schema.pickle"
    with path.open("rb") as fh:
        return pickle.load(fh), f"pyavd {pyavd.__version__} wheel ({path.name})"  # noqa: S301 - our own dependency


def section_slugs(repo: Path, name: str) -> dict[str, str]:
    """table name → slug of the data-models.md heading that embeds `tables/<table>.md` (mkdocs toc slugify)."""
    doc = repo / SCHEMAS[name][1]
    out: dict[str, str] = {}
    if not doc.is_file():
        return out
    heading = ""
    for line in doc.read_text().splitlines():
        m = re.match(r"^#{2,6}\s+(.*)$", line)
        if m:
            heading = m.group(1).strip()
            continue
        m = re.search(r"docs/tables/([\w.-]+)\.md", line)
        if m and heading:
            out.setdefault(m.group(1), slugify(heading))
    return out


def slugify(text: str) -> str:
    text = re.sub(r"`|\*|_(?=\w)", lambda m: "_" if m.group(0) == "_" else "", text)
    text = re.sub(r"[^\w\s-]", "", text).strip().lower()
    return re.sub(r"[-\s]+", "-", text)


# ------------------------------------------------------------------------------------------------ resolving
class Resolver:
    def __init__(self, schemas: dict[str, dict[str, Any]]):
        self.schemas = schemas

    def target(self, ref: str) -> tuple[str, dict[str, Any]]:
        schema_id, _, pointer = ref.partition("#")
        node: Any = self.schemas[schema_id]
        for part in [p for p in pointer.split("/") if p]:
            node = node[part.replace("~1", "/").replace("~0", "~")]
        return schema_id, node

    def resolve(self, node: dict[str, Any], own: str, depth: int = 0) -> dict[str, Any]:
        """Expand `$ref`s into `own` schema (recursively); refs into another schema become `ref_link`."""
        ref = node.get("$ref")
        if not ref or depth > 20:
            return node
        schema_id, target = self.target(ref)
        local = {k: v for k, v in node.items() if k != "$ref"}
        if schema_id != own:
            local["_ref_link"] = f"{schema_id}:{_pointer_to_path(ref.partition('#')[2])}"
            local.setdefault("type", target.get("type"))
            local.setdefault("description", target.get("description"))
            return local
        return _deep_merge(self.resolve(copy.deepcopy(target), own, depth + 1), local)


def _pointer_to_path(pointer: str) -> str:
    parts = [p for p in pointer.split("/") if p and p not in ("keys", "items", "$defs", "dynamic_keys")]
    return ".".join(parts)


# ------------------------------------------------------------------------------------------------ flattening
def _compact_default(value: Any) -> Any:
    text = json.dumps(value, default=str)
    if len(text) <= MAX_DEFAULT_JSON:
        return value
    if isinstance(value, list):
        return {"_summary": f"list of {len(value)} items (see AVD docs)"}
    return {"_summary": "large default (see AVD docs)"}


def flatten(schema: dict[str, Any], own: str, resolver: Resolver) -> dict[str, dict[str, Any]]:
    nodes: dict[str, dict[str, Any]] = {}

    def entry(node: dict[str, Any]) -> dict[str, Any]:
        e: dict[str, Any] = {"t": node.get("type")}
        if node.get("description"):
            e["d"] = str(node["description"]).strip()
        elif node.get("display_name"):
            e["d"] = str(node["display_name"])
        if "default" in node:
            e["df"] = _compact_default(node["default"])
        for src, dst in (("valid_values", "vv"), ("min", "min"), ("max", "max"), ("min_length", "minl"),
                         ("max_length", "maxl"), ("format", "fmt"), ("pattern", "pat"), ("primary_key", "pk"),
                         ("deprecation", "dep")):
            if node.get(src) is not None:
                e[dst] = node[src]
        if node.get("required"):
            e["req"] = True
        table = (node.get("documentation_options") or {}).get("table")
        if table:
            e["tbl"] = table
        if node.get("_ref_link"):
            e["ref"] = node["_ref_link"]
        return e

    def walk(path: str, node: dict[str, Any], table: str | None, depth: int) -> None:
        node = resolver.resolve(node, own)
        e = entry(node)
        if "tbl" not in e and table:
            e["tbl"] = table
        table = e.get("tbl")
        nodes[path] = e
        if depth > 14 or e.get("ref"):
            return
        children: dict[str, Any] | None = None
        if node.get("type") == "dict":
            children = node.get("keys") or {}
            if node.get("dynamic_keys"):
                e["dyn"] = sorted(node["dynamic_keys"])
        elif node.get("type") == "list" and isinstance(node.get("items"), dict):
            items = resolver.resolve(node["items"], own)
            e["it"] = items.get("type")
            if items.get("_ref_link"):
                e["ref"] = items["_ref_link"]
                return
            if items.get("type") == "dict":
                children = items.get("keys") or {}
            for src, dst in (("valid_values", "ivv"), ("format", "ifmt"), ("pattern", "ipat")):
                if items.get(src) is not None:
                    e[dst] = items[src]
        if children:
            e["k"] = list(children)
            for name, child in children.items():
                if isinstance(child, dict):
                    walk(f"{path}.{name}" if path else name, child, table, depth + 1)

    root = resolver.resolve(schema, own)
    top = root.get("keys") or {}
    for name, child in top.items():
        walk(name, child, None, 0)
    for dyn, child in (root.get("dynamic_keys") or {}).items():
        walk(f"<{dyn}>", child, None, 0)
    return nodes


def aliases_for(schema: dict[str, Any], name: str) -> dict[str, str]:
    """Default dynamic key names → their `<dynamic key>` path (eos_designs: node types, endpoint keys, tenants)."""
    if name != "eos_designs":
        return {}
    keys = schema.get("keys") or {}
    out: dict[str, str] = {}
    for item in (keys.get("node_type_keys") or {}).get("default") or []:
        out[item["key"]] = "<node_type_keys.key>"
    for item in (keys.get("connected_endpoints_keys") or {}).get("default") or []:
        out[item["key"]] = "<connected_endpoints_keys.key>"
    for item in (keys.get("network_services_keys") or {}).get("default") or []:
        out[item["name"]] = "<network_services_keys.name>"
    return out


def export(repo: Path | None, out_dir: Path) -> dict[str, int]:
    raw: dict[str, dict[str, Any]] = {}
    sources: dict[str, str] = {}
    for name in SCHEMAS:
        loaded = load_from_repo(repo, name) if repo and repo.is_dir() else None
        if loaded is None:
            loaded = load_from_pickle(name)
        raw[name], sources[name] = loaded
    resolver = Resolver(raw)
    try:
        import pyavd

        version = pyavd.__version__
    except ImportError:  # pragma: no cover
        version = "6.4.0"
    out_dir.mkdir(parents=True, exist_ok=True)
    sizes: dict[str, int] = {}
    for name in SCHEMAS:
        nodes = flatten(raw[name], name, resolver)
        doc = {
            "schema": name,
            "avd_version": version,
            "source": sources[name],
            "aliases": aliases_for(raw[name], name),
            "sections": section_slugs(repo, name) if repo else {},
            "top": list((raw[name].get("keys") or {}).keys()),
            "nodes": nodes,
        }
        path = out_dir / f"{name}.json"
        path.write_text(json.dumps(doc, separators=(",", ":"), sort_keys=False, default=str))
        sizes[name] = len(nodes)
        print(f"{path}: {len(nodes)} keys, {path.stat().st_size // 1024} KiB (from {sources[name]})")
    return sizes


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--avd-repo", type=Path, default=REPO_DEFAULT)
    ap.add_argument("--out", type=Path, default=OUT_DEFAULT)
    args = ap.parse_args(argv)
    export(args.avd_repo, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
