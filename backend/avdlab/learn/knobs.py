"""Knob help: `learn` blocks per knob — `{summary, avd_keys, renders, pitfalls, doc_url}`.

Source of truth for each template is its `templates/<name>/template.yml` (`knobs[].learn`); `knob_learn.yml` is the
catalogue those blocks were written from and the fallback for a knob without one (a template added later).
`attach_knob_learn(public, template)` adds `learn` to every knob of a `GET /labs/templates` descriptor.

`python3 -m avdlab.learn.knobs --write [templates_dir]` inserts missing learn blocks into template.yml files
(textual insertion: comments and key order of the descriptor are preserved; existing blocks are left alone).
"""
from __future__ import annotations

import copy
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

CATALOG = Path(__file__).parent / "knob_learn.yml"
LEARN_FIELDS = ("summary", "avd_keys", "renders", "pitfalls", "doc_url")


@lru_cache(maxsize=1)
def catalog() -> dict[str, dict[str, Any]]:
    return yaml.safe_load(CATALOG.read_text()) or {}


def catalog_learn(key: str, generator: str | None = None) -> dict[str, Any] | None:
    entry = catalog().get(key)
    if not entry:
        return None
    out = {k: copy.deepcopy(v) for k, v in entry.items() if k in LEARN_FIELDS}
    out.update(copy.deepcopy((entry.get("by_generator") or {}).get(generator or "", {})))
    out.setdefault("avd_keys", [])
    out.setdefault("pitfalls", [])
    return out


def raw_knob_learn(template_dir: Path | None) -> dict[str, dict[str, Any]]:
    """knob key → learn block as written in template.yml (the Knob model does not carry it)."""
    if template_dir is None or not (template_dir / "template.yml").is_file():
        return {}
    data = yaml.safe_load((template_dir / "template.yml").read_text()) or {}
    return {k["key"]: k["learn"] for k in data.get("knobs") or [] if isinstance(k, dict) and isinstance(k.get("learn"), dict)}


def knob_learn(template: Any, key: str) -> dict[str, Any] | None:
    raw = raw_knob_learn(getattr(template, "dir", None)).get(key)
    if raw:
        out = {f: copy.deepcopy(raw.get(f)) for f in LEARN_FIELDS}
        out["avd_keys"] = out["avd_keys"] or []
        out["pitfalls"] = out["pitfalls"] or []
        return out
    return catalog_learn(key, getattr(template, "generator", None))


def attach_knob_learn(public: dict[str, Any], template: Any) -> dict[str, Any]:
    raw = raw_knob_learn(getattr(template, "dir", None))
    for knob in public.get("knobs") or []:
        if knob.get("learn"):
            continue
        block = raw.get(knob["key"])
        if block:
            knob["learn"] = {f: copy.deepcopy(block.get(f)) for f in LEARN_FIELDS}
            knob["learn"]["avd_keys"] = knob["learn"]["avd_keys"] or []
            knob["learn"]["pitfalls"] = knob["learn"]["pitfalls"] or []
        else:
            knob["learn"] = catalog_learn(knob["key"], getattr(template, "generator", None))
    return public


# ------------------------------------------------------------------------------------------------ writer
def _dump_block(block: dict[str, Any], indent: int) -> list[str]:
    text = yaml.safe_dump({"learn": block}, sort_keys=False, allow_unicode=True, width=130, default_flow_style=False)
    return [(" " * indent + line) if line else line for line in text.rstrip("\n").split("\n")]


def insert_learn_blocks(text: str, generator: str | None, overrides: dict[str, dict[str, Any]] | None = None) -> tuple[str, list[str]]:
    """Insert `learn:` into every `  - key: X` knob block lacking one. Returns (new text, keys written)."""
    lines = text.split("\n")
    out: list[str] = []
    written: list[str] = []
    in_knobs = False
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("knobs:"):
            in_knobs = True
            out.append(line)
            i += 1
            continue
        if in_knobs and line and not line.startswith(" ") and not line.startswith("#"):
            in_knobs = False
        if in_knobs and line.startswith("  - key:"):
            key = line.split(":", 1)[1].split(" #")[0].strip().strip("'\"")
            block = [line]
            i += 1
            while i < len(lines) and not lines[i].startswith("  - ") and (lines[i].startswith("    ") or not lines[i].strip()):
                block.append(lines[i])
                i += 1
            if i < len(lines) and lines[i].startswith("  #"):
                pass
            has = any(b.startswith("    learn:") for b in block)
            trailing: list[str] = []
            while block and not block[-1].strip():
                trailing.insert(0, block.pop())
            learn = (overrides or {}).get(key) or catalog_learn(key, generator)
            if not has and learn:
                block += _dump_block(learn, 4)
                written.append(key)
            out += block + trailing
            continue
        out.append(line)
        i += 1
    return "\n".join(out), written


def write_templates(templates_dir: Path, only: list[str] | None = None) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for f in sorted(templates_dir.glob("*/template.yml")):
        if only and f.parent.name not in only:
            continue
        data = yaml.safe_load(f.read_text()) or {}
        new, written = insert_learn_blocks(f.read_text(), data.get("generator", "static"))
        if written:
            yaml.safe_load(new)  # must stay valid YAML
            f.write_text(new)
        result[f.parent.name] = written
    return result


if __name__ == "__main__":  # pragma: no cover
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    root = Path(args[0]) if args else Path(__file__).resolve().parents[3] / "templates"
    only = args[1:] or None
    if "--write" in sys.argv:
        for name, keys in write_templates(root, only).items():
            print(f"{name}: {len(keys)} learn blocks written {keys}")
