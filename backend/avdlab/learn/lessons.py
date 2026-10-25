"""Lessons: `learn/lessons/*.md` = YAML front-matter + markdown with three special blocks, each a YAML body:

    :::try                         an action the UI performs
    id: open-leaf-config
    label: Open dc1-leaf1a's intended config
    action: open_file              open_file {path, line?} | open_tab {tab} | run_job {job} | set_knob {knob, value}
    args: {path: intended/configs/dc1-leaf1a.cfg}
    :::

    :::check                       a checkpoint evaluated server-side (POST /learn/lessons/{id}/check)
    id: lo0
    label: dc1-leaf1a has Loopback0 10.255.0.3
    type: config_contains          config_contains {node, pattern|text, file?} | knob_equals {knob, value} |
    args: {node: dc1-leaf1a, ...}  job_succeeded {job} | anta_status {...} | file_exists {path}
    hint: ...
    :::

    :::quiz                        single choice, answer = 0-based index, explanation shown after answering
    id: q1
    question: ...
    options: [..]
    answer: 1
    explanation: ...
    :::
"""
from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

LESSONS_DIR = Path(__file__).parent / "lessons"
LEVELS = ("intro", "core", "advanced")
REQUIRES = ("built", "deployed", "validated")
TRY_ACTIONS = ("open_file", "open_tab", "run_job", "set_knob")
CHECK_TYPES = ("config_contains", "knob_equals", "job_succeeded", "anta_status", "file_exists")
LAB_TABS = ("overview", "settings", "files", "build", "topology", "runtime", "devices", "clients", "validation",
            "cloudvision", "mss", "netbox", "act", "git", "jobs")
# job type → endpoint the UI calls for a run_job action
RUN_JOBS: dict[str, dict[str, str]] = {
    "lab.build": {"method": "POST", "path": "/api/labs/{lab_id}/build"},
    "lab.deploy": {"method": "POST", "path": "/api/labs/{lab_id}/deploy"},
    "lab.validate": {"method": "POST", "path": "/api/labs/{lab_id}/validate"},
    "lab.destroy": {"method": "POST", "path": "/api/labs/{lab_id}/destroy"},
    "lab.status": {"method": "POST", "path": "/api/labs/{lab_id}/status/refresh"},
    "lab.topology": {"method": "POST", "path": "/api/topology/{lab_id}/regenerate"},
    "lab.mss_verify": {"method": "POST", "path": "/api/labs/{lab_id}/mss/verify"},
    "lab.cv_onboard": {"method": "POST", "path": "/api/labs/{lab_id}/cv/onboard"},
    "lab.cv_deploy": {"method": "POST", "path": "/api/labs/{lab_id}/cv/deploy"},
}
BLOCK_RE = re.compile(r"^:::(try|check|quiz)[ \t]*\n(.*?)^:::[ \t]*$", re.S | re.M)


class LessonError(ValueError):
    pass


@dataclass
class Lesson:
    meta: dict[str, Any]
    blocks: list[dict[str, Any]]
    path: Path

    @property
    def id(self) -> str:
        return self.meta["id"]

    def summary(self) -> dict[str, Any]:
        return {**{k: self.meta.get(k) for k in ("id", "title", "level", "minutes", "requires", "tags", "order", "summary")},
                "checkpoints": [b["id"] for b in self.blocks if b["kind"] == "check"],
                "tries": sum(1 for b in self.blocks if b["kind"] == "try"),
                "quizzes": sum(1 for b in self.blocks if b["kind"] == "quiz")}

    def public(self) -> dict[str, Any]:
        return {**self.summary(), "blocks": self.blocks}

    def checkpoint(self, cid: str) -> dict[str, Any] | None:
        return next((b for b in self.blocks if b["kind"] == "check" and b["id"] == cid), None)


def _validate_block(kind: str, data: dict[str, Any], where: str) -> dict[str, Any]:
    if not isinstance(data, dict) or not data.get("id"):
        raise LessonError(f"{where}: :::{kind} block needs an id")
    args = data.get("args") or {}
    if kind == "try":
        if data.get("action") not in TRY_ACTIONS:
            raise LessonError(f"{where}: try {data['id']}: action must be one of {TRY_ACTIONS}")
        if data["action"] == "open_file" and not args.get("path"):
            raise LessonError(f"{where}: try {data['id']}: open_file needs args.path")
        if data["action"] == "open_tab" and args.get("tab") not in LAB_TABS and not str(args.get("page", "")).startswith("/"):
            raise LessonError(f"{where}: try {data['id']}: open_tab needs args.tab in {LAB_TABS} or args.page (/…)")
        if data["action"] == "run_job":
            if args.get("job") not in RUN_JOBS:
                raise LessonError(f"{where}: try {data['id']}: run_job job must be one of {sorted(RUN_JOBS)}")
            data["endpoint"] = RUN_JOBS[args["job"]]
        if data["action"] == "set_knob" and (not args.get("knob") or "value" not in args):
            raise LessonError(f"{where}: try {data['id']}: set_knob needs args.knob and args.value")
    elif kind == "check":
        if data.get("type") not in CHECK_TYPES:
            raise LessonError(f"{where}: check {data['id']}: type must be one of {CHECK_TYPES}")
        need = {"config_contains": (), "knob_equals": ("knob", "value"), "job_succeeded": ("job",),
                "anta_status": (), "file_exists": ("path",)}[data["type"]]
        missing = [k for k in need if k not in args]
        if missing:
            raise LessonError(f"{where}: check {data['id']}: missing args {missing}")
        if data["type"] == "config_contains" and not (args.get("node") or args.get("file")):
            raise LessonError(f"{where}: check {data['id']}: config_contains needs node or file")
        if data["type"] == "config_contains" and not (args.get("pattern") or args.get("text")):
            raise LessonError(f"{where}: check {data['id']}: config_contains needs pattern or text")
        if data["type"] == "config_contains" and args.get("pattern"):
            re.compile(args["pattern"], re.M)
    else:
        opts = data.get("options") or []
        if not data.get("question") or len(opts) < 2 or not isinstance(data.get("answer"), int) \
                or not 0 <= data["answer"] < len(opts) or not data.get("explanation"):
            raise LessonError(f"{where}: quiz {data['id']}: needs question, ≥2 options, a valid answer index, explanation")
    data["args"] = args if kind != "quiz" else data.get("args")
    if kind == "quiz":
        data.pop("args", None)
    return data


def parse_lesson(text: str, path: Path | None = None) -> Lesson:
    where = path.name if path else "lesson"
    if not text.startswith("---"):
        raise LessonError(f"{where}: missing front-matter")
    _, fm, body = text.split("---", 2)
    try:
        meta = yaml.safe_load(fm) or {}
    except yaml.YAMLError as exc:
        raise LessonError(f"{where}: invalid front-matter: {exc}") from exc
    for key in ("id", "title", "level", "minutes"):
        if key not in meta:
            raise LessonError(f"{where}: front-matter needs {key}")
    if meta["level"] not in LEVELS:
        raise LessonError(f"{where}: level must be one of {LEVELS}")
    meta.setdefault("requires", [])
    meta.setdefault("tags", [])
    bad = [r for r in meta["requires"] if r not in REQUIRES]
    if bad:
        raise LessonError(f"{where}: requires must be within {REQUIRES}")
    blocks: list[dict[str, Any]] = []
    pos = 0
    ids: set[str] = set()
    for m in BLOCK_RE.finditer(body):
        md = body[pos:m.start()].strip()
        if md:
            blocks.append({"kind": "markdown", "text": md})
        try:
            raw = yaml.safe_load(m.group(2)) or {}
        except yaml.YAMLError as exc:
            raise LessonError(f"{where}: invalid YAML in :::{m.group(1)} block: {exc}") from exc
        data = _validate_block(m.group(1), raw, where)
        if data["id"] in ids:
            raise LessonError(f"{where}: duplicate block id {data['id']}")
        ids.add(data["id"])
        blocks.append({"kind": m.group(1), **data})
        pos = m.end()
    tail = body[pos:].strip()
    if tail:
        blocks.append({"kind": "markdown", "text": tail})
    if ":::" in "".join(b.get("text", "") for b in blocks if b["kind"] == "markdown"):
        raise LessonError(f"{where}: unterminated or unknown ::: block")
    return Lesson(meta=meta, blocks=blocks, path=path or Path(where))


@lru_cache(maxsize=1)
def load_lessons(root: Path = LESSONS_DIR) -> dict[str, Lesson]:
    out: dict[str, Lesson] = {}
    for f in sorted(root.glob("*.md")):
        lesson = parse_lesson(f.read_text(encoding="utf-8"), f)
        if lesson.id in out:
            raise LessonError(f"duplicate lesson id {lesson.id}")
        out[lesson.id] = lesson
    return dict(sorted(out.items(), key=lambda kv: (kv[1].meta.get("order", 999), kv[0])))


# ------------------------------------------------------------------------------------------------ checkpoints
@dataclass
class CheckContext:
    """What a checkpoint may look at (filled by the API from the DB; tests build it directly)."""
    lab_dir: Path | None
    knobs: dict[str, Any]
    jobs: list[dict[str, Any]]            # [{type, status, created_at}] newest first
    validation: dict[str, Any] | None     # latest ValidationRun: {totals, report_path}
    state: str | None = None


def _dig(data: Any, dotted: str) -> Any:
    cur = data
    for part in [p for p in dotted.split(".") if p]:
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
    return cur


def _safe_path(root: Path, rel: str) -> Path:
    p = (root / rel).resolve()
    if not str(p).startswith(str(root.resolve())):
        raise LessonError("path escapes the lab directory")
    return p


def evaluate(check: dict[str, Any], ctx: CheckContext) -> tuple[bool, str]:
    t, a = check["type"], check.get("args") or {}
    if t == "knob_equals":
        val = _dig(ctx.knobs, a["knob"])
        want = a["value"]
        ok = str(val).strip().lower() == str(want).strip().lower() if not isinstance(want, (dict, list)) else val == want
        return ok, f"knob {a['knob']} = {json.dumps(val, default=str)}" + ("" if ok else f" (expected {json.dumps(want)})")
    if t == "job_succeeded":
        jobs = [j for j in ctx.jobs if j["type"] == a["job"]]
        if not jobs:
            return False, f"no {a['job']} job has run for this lab yet"
        last = jobs[0]
        return last["status"] == "succeeded", f"last {a['job']} job: {last['status']}"
    if ctx.lab_dir is None:
        return False, "no lab selected"
    if t == "file_exists":
        root = ctx.lab_dir
        hits = [p for p in root.glob(a["path"]) if p.is_file()] if any(c in a["path"] for c in "*?[") else \
            ([_safe_path(root, a["path"])] if _safe_path(root, a["path"]).is_file() else [])
        return bool(hits), (f"found {hits[0].relative_to(root)}" + (f" (+{len(hits) - 1} more)" if len(hits) > 1 else "")
                            if hits else f"{a['path']} does not exist yet — build the lab first")
    if t == "config_contains":
        rel = a.get("file") or f"intended/configs/{a['node']}.cfg"
        path = _safe_path(ctx.lab_dir, rel)
        if not path.is_file():
            return False, f"{rel} not found — build the lab first"
        text = path.read_text(encoding="utf-8", errors="replace")
        if a.get("pattern"):
            m = re.search(a["pattern"], text, re.M)
            ok = bool(m) != bool(a.get("absent"))
            found = m.group(0).strip().split("\n")[-1].strip() if m else None
        else:
            ok = (a["text"] in text) != bool(a.get("absent"))
            found = a["text"] if a["text"] in text else None
        if a.get("absent"):
            return ok, f"{rel}: {'absent as expected' if ok else 'still contains ' + repr(found)}"
        return ok, f"{rel}: found {found!r}" if ok else f"{rel}: no line matches {a.get('pattern') or a.get('text')!r}"
    if t == "anta_status":
        v = ctx.validation
        if not v:
            return False, "no ANTA validation has run for this lab yet"
        totals = v.get("totals") or {}
        if a.get("test") or a.get("device"):
            results = _anta_results(ctx.lab_dir, v.get("report_path"))
            sel = [r for r in results if (not a.get("test") or r.get("test") == a["test"])
                   and (not a.get("device") or r.get("name", r.get("device")) == a["device"])]
            if not sel:
                return False, f"no result for {a.get('test') or '*'} on {a.get('device') or '*'} in the last report"
            want = a.get("status", "success")
            bad = [r for r in sel if r.get("result") != want]
            return not bad, (f"{len(sel)} result(s) {want}" if not bad else
                             f"{len(bad)}/{len(sel)} not {want}: " + "; ".join(f"{r.get('name')} {r.get('test')} {r.get('result')}"
                                                                          for r in bad[:3]))
        fails = int(totals.get("failure", 0)) + int(totals.get("error", 0))
        ok = fails <= int(a.get("max_failure", 0)) and int(totals.get("success", 0)) >= int(a.get("min_success", 1))
        return ok, f"last run: {totals.get('success', 0)} success, {totals.get('failure', 0)} failure, " \
                   f"{totals.get('error', 0)} error, {totals.get('skipped', 0)} skipped"
    return False, f"unknown checkpoint type {t}"


def _anta_results(lab_dir: Path, report: str | None) -> list[dict[str, Any]]:
    if not report:
        return []
    p = _safe_path(lab_dir, report)
    if not p.is_file():
        return []
    data = json.loads(p.read_text())
    return data if isinstance(data, list) else data.get("results", [])


def fnmatch_any(name: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(name, p) for p in patterns)
