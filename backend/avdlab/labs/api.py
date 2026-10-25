"""Labs API: templates, lab CRUD (render + pyavd validation + git), files, build job, history/diff, devices.

  GET    /labs/templates
  GET    /labs                       POST /labs {name, template, host_id?, ceos_version?, knobs}
  GET    /labs/{id}                  PATCH /labs/{id} {knobs?, host_id?, ceos_version?, name?, cv_target_id?, target?}
  DELETE /labs/{id}
  GET    /labs/{id}/files            GET/PUT /labs/{id}/files/{path}  (PUT body {content})
  POST   /labs/{id}/build  → 202 job lab.build
  GET    /labs/{id}/history          GET /labs/{id}/diff?a=&b=&path=
  GET    /labs/{id}/devices
  POST   /labs/{id}/deploy {reconfigure, wait_eapi, wait_timeout} → 202 job lab.deploy
  POST   /labs/{id}/destroy → 202 job lab.destroy
  GET    /labs/{id}/status (inline, 20 s budget)    POST /labs/{id}/status/refresh → 202 job lab.status
  POST   /labs/{id}/validate {extra_fabric_validation, skip_tests, user_catalogs, tags} → 202 job lab.validate
  GET    /labs/{id}/validations      GET /labs/{id}/validations/{run_id}/report?format=json|md|csv

Input violations (knob coercion or pyavd schema) are returned as HTTP 422 with
`{"detail": {"message": str, "violations": [{"host"?, "path"?, "message"}]}}`.
Secret knobs (type `secret`) are stored encrypted in `Lab.settings.secret_knobs` and returned masked.
"""
from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete as sa_delete
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..api.jobs import JobOut
from ..core.db import CVTarget, Host, Job, JobStatus, Lab, LabState, ValidationRun, get_session
from ..core.security import current_user, secret_store
from ..jobs.engine import runner
from .. import executors  # noqa: F401 - registers jobs lab.deploy/destroy/status/validate
from ..executors import clab as clab_exec
from ..executors.base import DeployParams, InlineContext, ValidateParams
from . import build as _build  # noqa: F401 - registers job lab.build
from . import service
from .build import device_summary
from .templates import (
    KnobError,
    TemplateDescriptor,
    coerce_knobs,
    get_template,
    load_lab_hostvars,
    load_templates,
    render_lab,
    validate_hostvars,
)

router = APIRouter(prefix="/labs", tags=["labs"])
MASK = "********"


# ------------------------------------------------------------------------------------------------ schemas
class LabCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    template: str
    host_id: str | None = None
    ceos_version: str | None = None
    target: str = Field(default="clab", pattern="^(clab|act)$")
    knobs: dict[str, Any] = {}


class LabPatchIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    host_id: str | None = None
    ceos_version: str | None = None
    knobs: dict[str, Any] | None = None
    cv_target_id: str | None = None  # CloudVision target binding (null unbinds); 409 while cloudvision.mode is none
    netbox_target_id: str | None = None  # phase 4 (backend B): NetBox target binding (null unbinds)
    target: str | None = Field(default=None, pattern="^(clab|act)$")  # phase 4 (backend D): clab | act


class FileIn(BaseModel):
    content: str
    message: str | None = None


# ------------------------------------------------------------------------------------------------ helpers
def _secret_keys(tpl: TemplateDescriptor | None) -> set[str]:
    return {k.key for k in tpl.knobs if k.type == "secret"} if tpl else set()


def _template_or_none(name: str) -> TemplateDescriptor | None:
    try:
        return get_template(name)
    except KeyError:
        return None


def _lab_out(lab: Lab, last_job: Job | None = None) -> dict[str, Any]:
    tpl = _template_or_none(lab.template)
    st = dict(lab.settings or {})
    knobs = dict(st.get("knobs") or {})
    for key in _secret_keys(tpl):
        if key in (st.get("secret_knobs") or {}):
            knobs[key] = MASK
    public_settings = {k: v for k, v in st.items() if k not in ("knobs", "secret_knobs")}
    host = lab.__dict__.get("host")
    return {
        "id": lab.id, "name": lab.name, "slug": lab.slug, "template": lab.template, "host_id": lab.host_id,
        "host": {"id": host.id, "name": host.name, "address": host.address} if host is not None else None,
        "target": lab.target, "state": lab.state.value, "ceos_version": lab.ceos_version,
        "cv_target_id": lab.cv_target_id,
        "netbox_target_id": lab.netbox_target_id,  # phase 4 (backend B)
        "knobs": knobs, "custom_knobs": st.get("custom_knobs", []), "custom_files": st.get("custom_files", []),
        "settings": public_settings,
        "last_build_job": JobOut.from_row(last_job).model_dump() if last_job is not None else None,
        "dir": str(service.lab_dir(lab)),
        "created_at": lab.created_at.isoformat(), "updated_at": lab.updated_at.isoformat(),
    }


def _cv_mode(lab: Lab) -> str:
    return str((((lab.settings or {}).get("knobs") or {}).get("cloudvision") or {}).get("mode") or "none")


async def _get_lab(session: AsyncSession, lab_id: str) -> Lab:
    lab = await session.get(Lab, lab_id)
    if lab is None:
        raise HTTPException(404, "lab not found")
    if lab.host_id:
        await session.refresh(lab, ["host"])
    return lab


async def _last_build(session: AsyncSession, lab: Lab) -> Job | None:
    job_id = (lab.settings or {}).get("last_build_job")
    if job_id:
        j = await session.get(Job, job_id)
        if j is not None:
            return j
    q = select(Job).where(Job.lab_id == lab.id, Job.type == "lab.build").order_by(Job.created_at.desc()).limit(1)
    return (await session.execute(q)).scalar_one_or_none()


def _unprocessable(message: str, violations: list[dict[str, Any]]) -> HTTPException:
    return HTTPException(422, {"message": message, "violations": violations})


def _render_and_validate(tpl: TemplateDescriptor, knobs: dict[str, Any], slug: str,
                         overrides: dict[str, Any] | None = None) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """Blocking: render + pyavd.validate_inputs on the per-host vars. Returns (files, violations).
    `overrides`: phase 5 topology-editor overrides (`lab.settings["topology_overrides"]`), re-applied after the render."""
    try:
        rendered = render_lab(tpl, knobs, slug=slug, overrides=overrides)
    except KnobError as exc:
        return {}, [{"path": e.split(":", 1)[0], "message": e} for e in exc.errors]
    _validated, violations = validate_hostvars(rendered.hostvars())
    return rendered.files(), violations


def _split_secrets(tpl: TemplateDescriptor, values: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    secret_keys = _secret_keys(tpl)
    public = {k: v for k, v in values.items() if k not in secret_keys}
    secrets = {k: secret_store.encrypt(str(v)) for k, v in values.items() if k in secret_keys and v is not None}
    return public, secrets


def _stored_knobs(lab: Lab) -> dict[str, Any]:
    st = lab.settings or {}
    values = dict(st.get("knobs") or {})
    for k, tok in (st.get("secret_knobs") or {}).items():
        values[k] = secret_store.decrypt(tok)
    return values


async def _unique_slug(session: AsyncSession, name: str) -> str:
    base = service.slugify(name)
    slug, n = base, 1
    while (await session.execute(select(Lab.id).where(Lab.slug == slug))).first() or service.lab_dir(slug).exists():
        n += 1
        slug = f"{base[:60]}-{n}"
    return slug


def _knob_groups(tpl: TemplateDescriptor) -> dict[str, str | None]:
    """knob -> group_vars group it lives in (None = structural knob that shapes several files)."""
    out: dict[str, str | None] = {}
    for k in tpl.knobs:
        ref = k.path or k.default_from
        out[k.key] = ref.split(":", 1)[0] if ref else None
    return out


def _affected_knobs(tpl: TemplateDescriptor | None, rel: str) -> list[str]:
    if tpl is None:
        return []
    groups = _knob_groups(tpl)
    group = None
    if rel.startswith("group_vars/"):
        part = Path(rel).parts[1]
        group = Path(part).stem if part.endswith((".yml", ".yaml")) else part
    specific = [k for k, g in groups.items() if g and g == group]
    if specific:
        return specific
    return [k for k, g in groups.items() if g is None]


def _validate_candidate(root: Path, rel: str, content: str) -> list[dict[str, Any]]:
    """Blocking: apply the edit to a scratch copy of the inventory + group_vars and run pyavd validation."""
    with tempfile.TemporaryDirectory(prefix="avdlab-edit-") as tmp:
        t = Path(tmp)
        for item in ("inventory.yml", "group_vars", "host_vars"):
            src = root / item
            if src.is_dir():
                shutil.copytree(src, t / item)
            elif src.is_file():
                shutil.copy2(src, t / item)
        dest = t / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
        try:
            hostvars = load_lab_hostvars(t)
        except Exception as exc:  # noqa: BLE001
            return [{"path": rel, "message": f"cannot load inventory/group_vars: {exc}"}]
        if not hostvars:
            return [{"path": "inventory.yml", "message": "no hosts in inventory"}]
        _validated, violations = validate_hostvars(hostvars)
        return violations


# ------------------------------------------------------------------------------------------------ templates
@router.get("/templates")
async def list_templates(user: str = Depends(current_user)) -> list[dict[str, Any]]:
    from ..learn.knobs import attach_knob_learn  # phase 6 (backend B): knobs[].learn (summary, avd_keys, renders, pitfalls)

    return await asyncio.to_thread(lambda: [attach_knob_learn(t.public(), t) for t in load_templates().values()])


# ------------------------------------------------------------------------------------------------ CRUD
@router.get("")
async def list_labs(session: AsyncSession = Depends(get_session), user: str = Depends(current_user)) -> list[dict[str, Any]]:
    labs = (await session.execute(select(Lab).order_by(Lab.created_at.desc()))).scalars().all()
    out = []
    for lab in labs:
        if lab.host_id:
            await session.refresh(lab, ["host"])
        out.append(_lab_out(lab, await _last_build(session, lab)))
    return out


@router.post("", status_code=201)
async def create_lab(body: LabCreateIn, session: AsyncSession = Depends(get_session),
                     user: str = Depends(current_user)) -> dict[str, Any]:
    tpl = _template_or_none(body.template)
    if tpl is None:
        raise HTTPException(422, {"message": f"unknown template {body.template!r}",
                                  "violations": [{"path": "template", "message": "unknown template"}]})
    if (await session.execute(select(Lab.id).where(Lab.name == body.name))).first():
        raise HTTPException(409, "a lab with this name exists")
    if body.host_id and await session.get(Host, body.host_id) is None:
        raise HTTPException(422, {"message": "host not found", "violations": [{"path": "host_id", "message": "unknown host"}]})
    knobs = dict(body.knobs)
    if body.ceos_version and tpl.knob("ceos_version"):
        knobs["ceos_version"] = body.ceos_version
    try:
        values = await asyncio.to_thread(coerce_knobs, tpl, knobs)
    except KnobError as exc:
        raise _unprocessable("invalid knob values", [{"path": e.split(":", 1)[0], "message": e} for e in exc.errors])
    slug = await _unique_slug(session, body.name)
    files, violations = await asyncio.to_thread(_render_and_validate, tpl, values, slug)
    if violations:
        raise _unprocessable(f"{len(violations)} AVD input violation(s)", violations)
    public, secrets = _split_secrets(tpl, values)
    lab = Lab(name=body.name, slug=slug, template=tpl.name, host_id=body.host_id, target=body.target,
              ceos_version=values.get("ceos_version") or body.ceos_version, state=LabState.draft,
              settings={"knobs": public, "secret_knobs": secrets, "fabric_name": tpl.fabric_name,
                        "custom_knobs": [], "custom_files": []})
    await asyncio.to_thread(service.init_lab_dir, slug, files, f"create lab from template {tpl.name}")
    session.add(lab)
    try:
        await session.commit()
    except Exception:
        await asyncio.to_thread(service.delete_lab_dir, slug)
        raise
    return _lab_out(await _get_lab(session, lab.id))


@router.get("/{lab_id}")
async def get_lab(lab_id: str, session: AsyncSession = Depends(get_session), user: str = Depends(current_user)) -> dict[str, Any]:
    lab = await _get_lab(session, lab_id)
    return _lab_out(lab, await _last_build(session, lab))


@router.patch("/{lab_id}")
async def patch_lab(lab_id: str, body: LabPatchIn, session: AsyncSession = Depends(get_session),
                    user: str = Depends(current_user)) -> dict[str, Any]:
    lab = await _get_lab(session, lab_id)
    fields = body.model_fields_set
    if "cv_target_id" in fields and body.cv_target_id:  # checked before anything is written
        if await session.get(CVTarget, body.cv_target_id) is None:
            raise HTTPException(422, {"message": "CloudVision target not found",
                                      "violations": [{"path": "cv_target_id", "message": "unknown target"}]})
        new_cv = (body.knobs or {}).get("cloudvision", ((lab.settings or {}).get("knobs") or {}).get("cloudvision"))
        if isinstance(new_cv, str):
            new_cv = yaml.safe_load(new_cv)
        if str((new_cv or {}).get("mode") or "none") == "none":
            raise HTTPException(409, "set the cloudvision knob to cvaas or onprem before binding a CloudVision target")
    # ---- phase 4 (backend B): NetBox target binding (validated before anything is written)
    if "netbox_target_id" in fields:
        if body.netbox_target_id:
            from ..core.db import NetBoxTarget

            if await session.get(NetBoxTarget, body.netbox_target_id) is None:
                raise HTTPException(422, {"message": "NetBox target not found",
                                          "violations": [{"path": "netbox_target_id", "message": "unknown target"}]})
        lab.netbox_target_id = body.netbox_target_id or None
    # ---- end phase 4 (backend B)
    if "name" in fields and body.name and body.name != lab.name:
        if (await session.execute(select(Lab.id).where(Lab.name == body.name, Lab.id != lab.id))).first():
            raise HTTPException(409, "a lab with this name exists")
        lab.name = body.name
    if "host_id" in fields:
        if body.host_id and await session.get(Host, body.host_id) is None:
            raise HTTPException(422, {"message": "host not found", "violations": [{"path": "host_id", "message": "unknown host"}]})
        lab.host_id = body.host_id
    if "target" in fields and body.target and body.target != (lab.target or "clab"):  # phase 4 (backend D)
        if lab.state in clab_exec.DEPLOYED_STATES:
            raise HTTPException(409, f"lab is {lab.state.value}: destroy it before changing the target")
        lab.target = body.target
        if lab.state == LabState.built:
            lab.state = LabState.draft  # containerlab and digital-twin (ACT) build outputs differ: rebuild
    rerender = body.knobs is not None or ("ceos_version" in fields and body.ceos_version)
    if rerender:
        tpl = _template_or_none(lab.template)
        if tpl is None:
            raise HTTPException(409, f"template {lab.template!r} no longer exists; edit files directly")
        values = _stored_knobs(lab)
        for k, v in (body.knobs or {}).items():
            if v == MASK:  # masked secret echoed back by the UI -> keep stored value
                continue
            values[k] = v
        if body.ceos_version and tpl.knob("ceos_version"):
            values["ceos_version"] = body.ceos_version
        values = {k: v for k, v in values.items() if tpl.knob(k)}
        try:
            values = await asyncio.to_thread(coerce_knobs, tpl, values)
        except KnobError as exc:
            raise _unprocessable("invalid knob values", [{"path": e.split(":", 1)[0], "message": e} for e in exc.errors])
        files, violations = await asyncio.to_thread(_render_and_validate, tpl, values, lab.slug,
                                                    (lab.settings or {}).get("topology_overrides"))
        if violations:
            raise _unprocessable(f"{len(violations)} AVD input violation(s)", violations)
        changed = sorted(k for k in values if _stored_knobs(lab).get(k) != values[k])
        sha = await asyncio.to_thread(service.write_files, lab, files,
                                      f"knobs: {', '.join(changed) or 're-render'}", prune_group_vars=True)
        public, secrets = _split_secrets(tpl, values)
        lab.settings = {**(lab.settings or {}), "knobs": public, "secret_knobs": secrets, "custom_knobs": [],
                        "custom_files": []}
        lab.ceos_version = values.get("ceos_version") or body.ceos_version or lab.ceos_version
        if "ceos_arch" in (body.knobs or {}):  # phase 3 (B): an arch change only re-tags the topology image
            fields = {*fields, "ceos_version"}
        if sha and lab.state == LabState.built:
            lab.state = LabState.draft  # intended configs are stale until the next build
        topo_knob = "ceos_version" in fields or "client_image" in (body.knobs or {})  # phase 3 (A): client_image
        if not sha and topo_knob and (service.lab_dir(lab) / "clab" / "topology.clab.yml").exists():
            from ..topology.clab import generate_for_lab

            await asyncio.to_thread(generate_for_lab, lab, service.lab_dir(lab), log=lambda _l: None)
            await asyncio.to_thread(service.commit_all, service.lab_dir(lab), f"topology: image {lab.ceos_version}")
    elif "ceos_version" in fields:
        lab.ceos_version = body.ceos_version
    cv_mode = _cv_mode(lab)
    if "cv_target_id" in fields:
        lab.cv_target_id = body.cv_target_id or None
    elif cv_mode == "none" and lab.cv_target_id:
        lab.cv_target_id = None  # knob switched to none → binding dropped
    await session.commit()
    lab = await _get_lab(session, lab.id)
    return _lab_out(lab, await _last_build(session, lab))


@router.delete("/{lab_id}", status_code=204)
async def delete_lab(lab_id: str, session: AsyncSession = Depends(get_session), user: str = Depends(current_user)) -> None:
    lab = await _get_lab(session, lab_id)
    if lab.state in (LabState.deployed, LabState.validated):
        raise HTTPException(409, f"lab is {lab.state.value}; destroy it first")
    running = (await session.execute(select(Job.id).where(
        Job.lab_id == lab.id, Job.status.in_([JobStatus.queued, JobStatus.running])))).first()
    if running:
        raise HTTPException(409, "a job is running for this lab")
    slug = lab.slug
    await session.execute(sa_delete(ValidationRun).where(ValidationRun.lab_id == lab.id))
    from ..core.db import TrafficRun  # phase 3 (A)

    await session.execute(sa_delete(TrafficRun).where(TrafficRun.lab_id == lab.id))
    await session.execute(update(Job).where(Job.lab_id == lab.id).values(lab_id=None))
    await session.delete(lab)
    await session.commit()
    await asyncio.to_thread(service.delete_lab_dir, slug)


# ------------------------------------------------------------------------------------------------ files
@router.get("/{lab_id}/files")
async def list_files(lab_id: str, session: AsyncSession = Depends(get_session), user: str = Depends(current_user)) -> list[dict[str, Any]]:
    lab = await _get_lab(session, lab_id)
    tree = await asyncio.to_thread(service.file_tree, lab)
    return [{**e, "type": "file"} for e in tree]


@router.get("/{lab_id}/files/{path:path}")
async def get_file(lab_id: str, path: str, session: AsyncSession = Depends(get_session),
                   user: str = Depends(current_user)) -> dict[str, Any]:
    lab = await _get_lab(session, lab_id)
    try:
        content = await asyncio.to_thread(service.read_file, lab, path)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except FileNotFoundError:
        raise HTTPException(404, "file not found")
    except UnicodeDecodeError:
        raise HTTPException(415, "binary file")
    return {"path": path, "content": content, "editable": service.is_editable(path),
            "knob_managed": service.is_knob_managed(path)}


@router.put("/{lab_id}/files/{path:path}")
async def put_file(lab_id: str, path: str, body: FileIn, session: AsyncSession = Depends(get_session),
                   user: str = Depends(current_user)) -> dict[str, Any]:
    lab = await _get_lab(session, lab_id)
    try:
        service._safe(service.lab_dir(lab), path)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not service.is_editable(path):
        raise HTTPException(403, f"{path} is generated or not editable")
    if path.endswith((".yml", ".yaml")):
        try:
            yaml.safe_load(body.content)
        except yaml.YAMLError as exc:
            raise _unprocessable("YAML syntax error", [{"path": path, "message": str(exc)}])
    if service.is_knob_managed(path) or path.startswith("host_vars/"):
        violations = await asyncio.to_thread(_validate_candidate, service.lab_dir(lab), path, body.content)
        if violations:
            raise _unprocessable(f"{len(violations)} AVD input violation(s)", violations)
    sha = await asyncio.to_thread(service.write_files, lab, {path: body.content}, body.message or f"edit {path}")
    custom: list[str] = []
    if sha and service.is_knob_managed(path):
        st = dict(lab.settings or {})
        custom = sorted(set(st.get("custom_knobs", [])) | set(_affected_knobs(_template_or_none(lab.template), path)))
        st["custom_knobs"] = custom
        st["custom_files"] = sorted(set(st.get("custom_files", [])) | {path})
        lab.settings = st
        if lab.state == LabState.built:
            lab.state = LabState.draft
        await session.commit()
    return {"path": path, "commit": sha, "changed": bool(sha), "custom_knobs": custom or (lab.settings or {}).get("custom_knobs", [])}


# ------------------------------------------------------------------------------------------------ build / git
@router.post("/{lab_id}/build", status_code=202)
async def build(lab_id: str, session: AsyncSession = Depends(get_session), user: str = Depends(current_user)) -> dict[str, str]:
    lab = await _get_lab(session, lab_id)
    running = (await session.execute(select(Job.id).where(
        Job.lab_id == lab.id, Job.type == "lab.build", Job.status.in_([JobStatus.queued, JobStatus.running])))).first()
    if running:
        raise HTTPException(409, {"message": "a build is already running", "job_id": running[0]})
    return {"job_id": await runner.submit("lab.build", lab_id=lab.id)}


@router.get("/{lab_id}/history")
async def history(lab_id: str, limit: int = 50, session: AsyncSession = Depends(get_session),
                  user: str = Depends(current_user)) -> list[dict[str, Any]]:
    lab = await _get_lab(session, lab_id)
    return await asyncio.to_thread(service.history, lab, limit)


@router.get("/{lab_id}/diff")
async def diff(lab_id: str, a: str | None = None, b: str | None = None, path: str | None = None,
               session: AsyncSession = Depends(get_session), user: str = Depends(current_user)) -> dict[str, Any]:
    lab = await _get_lab(session, lab_id)
    try:
        text = await asyncio.to_thread(service.diff, lab, a or None, b or None, path)
    except Exception as exc:  # noqa: BLE001 - git errors on unknown revisions
        raise HTTPException(400, f"cannot diff: {exc}") from exc
    return {"a": a or "HEAD~1", "b": b or "HEAD", "diff": text, "files": _split_diff(text)}


def _split_diff(text: str) -> list[dict[str, str]]:
    files: list[dict[str, str]] = []
    for chunk in text.split("\ndiff --git ") if text else []:
        chunk = chunk if chunk.startswith("diff --git ") else "diff --git " + chunk
        header = chunk.split("\n", 1)[0]
        target = header.rsplit(" b/", 1)[-1] if " b/" in header else header
        files.append({"path": target, "diff": chunk})
    return files


@router.get("/{lab_id}/devices")
async def devices(lab_id: str, session: AsyncSession = Depends(get_session), user: str = Depends(current_user)) -> list[dict[str, Any]]:
    lab = await _get_lab(session, lab_id)
    return await asyncio.to_thread(device_summary, service.lab_dir(lab))


# ------------------------------------------------------------------------------------------------ runtime (executors)
RUNTIME_JOBS = ("lab.build", "lab.deploy", "lab.destroy", "lab.validate", "lab.cv_onboard", "lab.cv_deploy")
STATUS_BUDGET = 20.0


async def _busy(session: AsyncSession, lab: Lab) -> None:
    running = (await session.execute(select(Job.id, Job.type).where(
        Job.lab_id == lab.id, Job.type.in_(RUNTIME_JOBS), Job.status.in_([JobStatus.queued, JobStatus.running])))).first()
    if running:
        raise HTTPException(409, {"message": f"{running[1]} is already running for this lab", "job_id": running[0]})


def _require_host(lab: Lab) -> None:
    if not lab.host_id:
        raise HTTPException(409, "lab has no host: set one in the lab settings")


def _require_target(lab: Lab) -> None:
    if (lab.target or "clab") not in ("clab", "act"):  # phase 4 (backend D): act → ActExecutor
        raise HTTPException(409, f"unknown lab target {lab.target!r}")


@router.post("/{lab_id}/deploy", status_code=202)
async def deploy(lab_id: str, body: DeployParams | None = None, session: AsyncSession = Depends(get_session),
                 user: str = Depends(current_user)) -> dict[str, str]:
    lab = await _get_lab(session, lab_id)
    _require_host(lab)
    _require_target(lab)
    if lab.state not in clab_exec.DEPLOYABLE_STATES:
        raise HTTPException(409, f"lab is {lab.state.value}: build it before deploying")
    if (lab.target or "clab") == "act":  # phase 4 (backend D): host kind act + prepared ACT topology
        from .act_api import deploy_precheck

        deploy_precheck(lab)
    elif not (service.lab_dir(lab) / clab_exec.TOPO_REL).exists():
        raise HTTPException(409, "no containerlab topology: build the lab first")
    await _busy(session, lab)
    arch_error = clab_exec.lab_arch_mismatch(lab) if (lab.target or "clab") == "clab" else None  # phase 3 (B)
    if arch_error:
        raise HTTPException(409, arch_error)
    params = (body or DeployParams()).model_dump()
    return {"job_id": await runner.submit("lab.deploy", lab_id=lab.id, host_id=lab.host_id, params=params)}


@router.post("/{lab_id}/destroy", status_code=202)
async def destroy(lab_id: str, session: AsyncSession = Depends(get_session), user: str = Depends(current_user)) -> dict[str, str]:
    lab = await _get_lab(session, lab_id)
    _require_host(lab)
    _require_target(lab)
    await _busy(session, lab)
    return {"job_id": await runner.submit("lab.destroy", lab_id=lab.id, host_id=lab.host_id)}


@router.get("/{lab_id}/status")
async def status(lab_id: str, session: AsyncSession = Depends(get_session), user: str = Depends(current_user)) -> dict[str, Any]:
    """Inline status: `{deployed, nodes: [{name, kind, state, mgmt_ip, image, eapi}], checked_at, cached, error}`."""
    import time

    lab = await _get_lab(session, lab_id)
    st = lab.settings or {}
    never = "deployed_at" not in st and lab.state not in clab_exec.DEPLOYED_STATES
    act = (lab.target or "clab") == "act"  # phase 4 (backend D): ActExecutor.status (eAPI probe from the tools server)
    if not lab.host_id or never or not (act or (service.lab_dir(lab) / clab_exec.TOPO_REL).exists()):
        return {"deployed": False, "nodes": [], "checked_at": None, "cached": False, "error": None}
    host = lab.host
    assert host is not None
    ctx = InlineContext()
    try:
        from ..executors.base import get_executor

        result = await asyncio.wait_for(get_executor(lab).status(ctx, lab, host, session=session), STATUS_BUDGET)
    except Exception as exc:  # noqa: BLE001 - timeout / ssh error → last known status + error
        last = st.get("last_status") or {}
        msg = "status check timed out after 20 s: use Refresh (job)" if isinstance(exc, TimeoutError) else str(exc)
        return {"deployed": bool(last.get("deployed", lab.state in clab_exec.DEPLOYED_STATES)),
                "nodes": last.get("nodes", []), "checked_at": last.get("at"), "cached": True, "error": msg}
    now = time.time()
    lab.settings = {**st, "last_status": {"at": now, **result}}
    await session.commit()
    return {**result, "checked_at": now, "cached": False, "error": None}


@router.post("/{lab_id}/status/refresh", status_code=202)
async def status_refresh(lab_id: str, session: AsyncSession = Depends(get_session),
                         user: str = Depends(current_user)) -> dict[str, str]:
    lab = await _get_lab(session, lab_id)
    _require_host(lab)
    _require_target(lab)
    return {"job_id": await runner.submit("lab.status", lab_id=lab.id, host_id=lab.host_id)}


@router.post("/{lab_id}/validate", status_code=202)
async def validate(lab_id: str, body: ValidateParams | None = None, session: AsyncSession = Depends(get_session),
                   user: str = Depends(current_user)) -> dict[str, str]:
    lab = await _get_lab(session, lab_id)
    _require_host(lab)
    _require_target(lab)
    if lab.state not in clab_exec.DEPLOYED_STATES:
        raise HTTPException(409, f"lab is {lab.state.value}: deploy it before validating")
    await _busy(session, lab)
    params = (body or ValidateParams()).model_dump()
    return {"job_id": await runner.submit("lab.validate", lab_id=lab.id, host_id=lab.host_id, params=params)}


def _run_out(r: ValidationRun) -> dict[str, Any]:
    return {"id": r.id, "job_id": r.job_id, "created_at": r.created_at.isoformat(), "totals": r.totals,
            "report_path": r.report_path}


@router.get("/{lab_id}/validations")
async def validations(lab_id: str, session: AsyncSession = Depends(get_session),
                      user: str = Depends(current_user)) -> list[dict[str, Any]]:
    lab = await _get_lab(session, lab_id)
    rows = (await session.execute(select(ValidationRun).where(ValidationRun.lab_id == lab.id)
                                  .order_by(ValidationRun.created_at.desc()))).scalars().all()
    return [_run_out(r) for r in rows]


@router.get("/{lab_id}/validations/{run_id}/report")
async def validation_report(lab_id: str, run_id: str, format: str = "json", session: AsyncSession = Depends(get_session),
                            user: str = Depends(current_user)) -> Any:
    """format=json → the ANTA result list as JSON; md / csv → text (text/markdown, text/csv)."""
    lab = await _get_lab(session, lab_id)
    if format not in clab_exec.REPORT_FORMATS:
        raise HTTPException(422, "format must be one of json, md, csv")
    run = await session.get(ValidationRun, run_id)
    if run is None or run.lab_id != lab.id:
        raise HTTPException(404, "validation run not found")
    rel = str(Path(run.report_path).with_suffix(f".{format}"))
    try:
        path = service._safe(service.lab_dir(lab), rel)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not path.is_file():
        raise HTTPException(404, f"{format} report not available for this run")
    text = await asyncio.to_thread(path.read_text, encoding="utf-8")
    if format == "json":
        import json

        return json.loads(text)
    return PlainTextResponse(text, media_type="text/markdown" if format == "md" else "text/csv")


# ================================================================ phase 3 — backend A (clients/traffic, running configs,
# snapshots, node terminal); routes live in executors/lab_routes.py, keep this block self-contained.
from ..executors import lab_routes as _phase3a_routes  # noqa: E402

router.include_router(_phase3a_routes.router)
# ================================================================ end phase 3 — backend A
