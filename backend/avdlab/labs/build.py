"""Job `lab.build`: pyavd pipeline (validate → facts → structured configs → configs/docs → fabric docs → ANTA
catalogs) writing the standard AVD directories into the lab dir, then git commit, then containerlab topology.

`build_lab_dir()` is synchronous and CPU-bound (pyavd); the job runs it in a thread and streams progress through
`ctx.log`. It has no DB dependency so tests can call it on any AVD inventory directory.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..core.db import Lab, LabState
from ..jobs.engine import JobContext, job
from . import service
from .templates import dedupe_cv_tags, load_lab_hostvars, validate_hostvars

Log = Callable[[str], None]


class BuildError(Exception):
    def __init__(self, message: str, violations: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.violations = violations or []


@dataclass
class BuildResult:
    hosts: list[str]
    warnings: list[str] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    fabric_name: str = "FABRIC"
    structured_configs: dict[str, dict[str, Any]] = field(default_factory=dict)
    test_counts: dict[str, int] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {
            "hosts": self.hosts,
            "warnings": self.warnings,
            "changed_files": self.changed_files,
            "fabric_name": self.fabric_name,
            "anta_tests": self.test_counts,
        }


def _write(path: Path, content: str, changed: list[str], root: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.read_text(encoding="utf-8") != content:
        path.write_text(content, encoding="utf-8")
        changed.append(str(path.relative_to(root)))


def build_lab_dir(lab_dir: Path, *, log: Log = print, skip_tests: tuple[str, ...] = ("VerifyNTP", "VerifyReloadCause"),
                  check_cancelled: Callable[[], None] | None = None, fabric_name: str | None = None,
                  device_tags: dict[str, dict[str, str]] | None = None,
                  segmentation: dict[str, Any] | None = None,
                  digital_twin: dict[str, Any] | None = None) -> BuildResult:
    """Run the whole pyavd pipeline against an AVD inventory directory. Synchronous.
    `fabric_name` overrides the name taken from the first host's vars (custom labs: `lab.settings["fabric_name"]`).

    CloudVision tags: templated labs render per-device tags into the node `structured_config` (see
    `templates.apply_device_tags`), which eos_designs appends after the fabric-wide `generate_cv_tags` tags; every
    structured config then goes through `dedupe_cv_tags` so a per-device label overrides a global one (pyavd 6.4 merges
    that list by appending). `device_tags` ({host: {label: value}}) is the post-build fallback used by custom labs,
    whose group_vars are not rendered: the tags are injected into the structured config before it is written (and so
    before the ANTA catalogs and the topology are generated).

    `segmentation` (phase 4 C, custom labs only — templated labs render it into node structured_config): the MSS knob;
    `router_segment_security` & co. are merged into the structured config of every host whose `vrfs` carry a segmented
    VRF, and the CLI config/doc are rendered from that (re-validated) dict.

    `digital_twin` (phase 4 D, ACT): the eos_designs `digital_twin` input (`{environment: act, fabric: {...}}`, see
    `twin_inputs`), merged into every host's vars (the fabric group covers all hosts); pyavd then runs with
    `digital_twin=True` (vEOS-lab platform mapping, Management1, `metadata.digital_twin`). The raw ACT topology is
    written to `documentation/fabric/<fabric>-topology.yml` (post-processed later by `executors/act.py`), and the ACT
    node user (`fabric.act_username/act_password`) is added to every device's `local_users` so eAPI access with the
    ACT credentials survives the config replace done by deploy."""
    from pyavd import (
        get_avd_facts,
        get_device_config,
        get_device_doc,
        get_device_structured_config,
        get_device_test_catalog,
        get_fabric_documentation,
    )
    from pyavd.api.anta import AVDCatalogGenerationSettings, AVDFabricData

    lab_dir = Path(lab_dir)
    changed: list[str] = []
    warnings: list[str] = []

    # 1. inventory + group_vars -> per-host vars, validated/coerced by pyavd
    hostvars = load_lab_hostvars(lab_dir)
    if not hostvars:
        raise BuildError("no hosts found in inventory.yml")
    twin = digital_twin is not None  # phase 4 (D)
    if twin:
        for hv in hostvars.values():
            hv["digital_twin"] = _twin_merge(hv.get("digital_twin") or {}, digital_twin or {})
        log("digital twin mode (ACT): platforms map to their digital_twin platform, mgmt interface Management1")
    log(f"loaded {len(hostvars)} hosts from inventory")
    validated, violations = validate_hostvars(hostvars)
    if violations:
        for v in violations:
            log(f"[violation] {v['host']}: {v.get('path', '')}: {v['message']}")
        raise BuildError(f"{len(violations)} input violation(s)", violations)
    hostvars = validated
    fabric_name = fabric_name or str(next(iter(hostvars.values())).get("fabric_name", "FABRIC"))

    # 2. fabric-wide facts
    log("computing fabric facts (get_avd_facts)")
    facts = get_avd_facts(hostvars, all_hostvars=hostvars, digital_twin=twin)

    # 3. per-device structured config, CLI config and documentation
    structured: dict[str, dict[str, Any]] = {}
    for host in hostvars:
        if check_cancelled:
            check_cancelled()
        sc = get_device_structured_config(host, hostvars[host], facts, hostvars=hostvars[host], digital_twin=twin)
        structured[host] = sc._as_dict()
        dedupe_cv_tags(structured[host], (device_tags or {}).get(host))
        if segmentation:  # phase 4 (C): post-build MSS injection (custom labs)
            sc = _inject_segmentation(host, structured[host], segmentation, sc, log)
        if twin and _add_twin_user(structured[host], (digital_twin or {}).get("fabric") or {}):  # phase 4 (D)
            sc = structured[host]
        _write(lab_dir / "intended" / "structured_configs" / f"{host}.yml",
               yaml.safe_dump(structured[host], sort_keys=False), changed, lab_dir)
        _write(lab_dir / "intended" / "configs" / f"{host}.cfg", get_device_config(sc), changed, lab_dir)
        _write(lab_dir / "documentation" / "devices" / f"{host}.md", get_device_doc(sc, add_md_toc=True), changed, lab_dir)
        log(f"[{host}] structured config, config and documentation rendered")

    # 4. fabric documentation (+ CSVs used by the topology generator)
    fd = get_fabric_documentation(facts, structured, fabric_name=fabric_name, topology_csv=True, p2p_links_csv=True,
                                  digital_twin=twin)
    fab = lab_dir / "documentation" / "fabric"
    if twin:  # phase 4 (D): raw ACT topology, post-processed by executors/act.py after the clab topology step
        if fd.digital_twin is None:
            raise BuildError("digital twin mode: pyavd returned no ACT topology (digital_twin.environment must be act)")
        _write(fab / f"{fabric_name}-topology.yml", act_topology_yaml(fd.digital_twin), changed, lab_dir)
        log(f"ACT topology written ({fabric_name}-topology.yml: {len(fd.digital_twin.nodes)} nodes, "
            f"{len(fd.digital_twin.links or ())} links)")
    _write(fab / f"{fabric_name}-documentation.md", fd.fabric_documentation, changed, lab_dir)
    _write(fab / f"{fabric_name}-topology.csv", fd.topology_csv, changed, lab_dir)
    _write(fab / f"{fabric_name}-p2p-links.csv", fd.p2p_links_csv, changed, lab_dir)
    log(f"fabric documentation written ({fabric_name})")

    # 5. ANTA catalogs
    catalog_dir = lab_dir / "anta" / "avd_catalogs"
    catalog_dir.mkdir(parents=True, exist_ok=True)
    (lab_dir / "anta" / "user_catalogs").mkdir(parents=True, exist_ok=True)
    fabric_data = AVDFabricData.from_structured_configs(structured)
    anta_settings = AVDCatalogGenerationSettings(output_dir=catalog_dir, extra_fabric_validation=True, skip_tests=skip_tests)
    counts: dict[str, int] = {}
    for host, sc in structured.items():
        if not (sc.get("metadata") or {}).get("is_deployed", True):
            continue
        catalog = get_device_test_catalog(host, sc, fabric_data, anta_settings)
        counts[host] = len(catalog.tests)
        p = catalog_dir / f"{host}.json"
        if p.exists():
            changed.append(str(p.relative_to(lab_dir)))
        log(f"[{host}] ANTA catalog: {counts[host]} tests")
    _write(lab_dir / "anta" / "inventory.yml", anta_inventory(structured), changed, lab_dir)

    return BuildResult(hosts=sorted(hostvars), warnings=warnings, changed_files=sorted(set(changed)),
                       fabric_name=fabric_name, structured_configs=structured, test_counts=counts)


# ------------------------------------------------------------------------------------------------ phase 6 (C): partial build
def related_hosts(facts: dict[str, Any], hostnames: list[str]) -> list[str]:
    """`hostnames` + their MLAG peers + their uplink switches (+ those switches' MLAG peers): the devices whose config
    can change when an endpoint is added/removed on `hostnames` (VLANs in use, MLAG peer VLANs, uplink trunk pruning)."""

    def fd(h: str) -> dict[str, Any]:
        f = facts.get(h)
        return f._as_dict() if hasattr(f, "_as_dict") else (f or {})

    out: list[str] = []

    def add(h: Any) -> None:
        if h and str(h) in facts and str(h) not in out:
            out.append(str(h))

    for h in hostnames:
        add(h)
        add(fd(h).get("mlag_peer"))
    for h in list(out):
        for up in fd(h).get("uplink_peers") or [u.get("peer") for u in fd(h).get("uplinks") or []]:
            add(up)
            add(fd(str(up)).get("mlag_peer"))
    return out


def build_devices_dir(lab_dir: Path, hostnames: list[str], *, log: Log = print, fabric_name: str | None = None,
                      device_tags: dict[str, dict[str, str]] | None = None, segmentation: dict[str, Any] | None = None,
                      expand: bool = True) -> dict[str, Any]:
    """Regenerate only `hostnames` (expanded with `related_hosts` when `expand`): structured config, CLI config, device
    doc and ANTA catalog of those hosts (other hosts' intended files are read, never written), plus the fabric topology
    and p2p CSVs (needed by the containerlab topology). Facts are computed fabric-wide (AVD needs every host).
    Returns {hosts: [rebuilt], changed_hosts: [hosts whose CLI config changed], changed_files}. Synchronous."""
    from pyavd import (
        get_avd_facts,
        get_device_config,
        get_device_doc,
        get_device_structured_config,
        get_device_test_catalog,
        get_fabric_documentation,
    )
    from pyavd.api.anta import AVDCatalogGenerationSettings, AVDFabricData

    lab_dir = Path(lab_dir)
    hostvars = load_lab_hostvars(lab_dir)
    if not hostvars:
        raise BuildError("no hosts found in inventory.yml")
    validated, violations = validate_hostvars(hostvars)
    if violations:
        raise BuildError(f"{len(violations)} input violation(s)", violations)
    unknown = [h for h in hostnames if h not in validated]
    if unknown:
        raise BuildError(f"unknown device(s): {', '.join(unknown)}")
    fabric_name = fabric_name or str(next(iter(validated.values())).get("fabric_name", "FABRIC"))
    facts = get_avd_facts(validated, all_hostvars=validated)
    targets = related_hosts(facts, list(hostnames)) if expand else list(hostnames)
    sc_dir = lab_dir / "intended" / "structured_configs"
    structured: dict[str, dict[str, Any]] = {}
    for f in sorted(sc_dir.glob("*.yml")):
        if f.stem in validated and f.stem not in targets:
            structured[f.stem] = yaml.safe_load(f.read_text()) or {}
    changed: list[str] = []
    changed_hosts: list[str] = []
    for host in targets:
        sc = get_device_structured_config(host, validated[host], facts, hostvars=validated[host])
        structured[host] = sc._as_dict()
        dedupe_cv_tags(structured[host], (device_tags or {}).get(host))
        if segmentation:
            sc = _inject_segmentation(host, structured[host], segmentation, sc, log)
        cfg_path = lab_dir / "intended" / "configs" / f"{host}.cfg"
        before = cfg_path.read_text(encoding="utf-8") if cfg_path.exists() else None
        _write(sc_dir / f"{host}.yml", yaml.safe_dump(structured[host], sort_keys=False), changed, lab_dir)
        _write(cfg_path, get_device_config(sc), changed, lab_dir)
        _write(lab_dir / "documentation" / "devices" / f"{host}.md", get_device_doc(sc, add_md_toc=True), changed, lab_dir)
        if cfg_path.read_text(encoding="utf-8") != before:
            changed_hosts.append(host)
        log(f"[{host}] structured config, config and documentation rebuilt"
            + (" (config changed)" if host in changed_hosts else " (config unchanged)"))
    missing = [h for h in validated if h not in structured]
    if missing:
        raise BuildError(f"no structured config on disk for {', '.join(missing)}: run a full build first")
    fd = get_fabric_documentation(facts, structured, fabric_name=fabric_name, topology_csv=True, p2p_links_csv=True)
    fab = lab_dir / "documentation" / "fabric"
    _write(fab / f"{fabric_name}-topology.csv", fd.topology_csv, changed, lab_dir)
    _write(fab / f"{fabric_name}-p2p-links.csv", fd.p2p_links_csv, changed, lab_dir)
    catalog_dir = lab_dir / "anta" / "avd_catalogs"
    if catalog_dir.is_dir():
        fabric_data = AVDFabricData.from_structured_configs(structured)
        anta_settings = AVDCatalogGenerationSettings(output_dir=catalog_dir, extra_fabric_validation=True,
                                                     skip_tests=("VerifyNTP", "VerifyReloadCause"))
        for host in targets:
            if (structured[host].get("metadata") or {}).get("is_deployed", True):
                get_device_test_catalog(host, structured[host], fabric_data, anta_settings)
    _TYPE_CACHE.pop(str(lab_dir), None)
    return {"hosts": targets, "changed_hosts": changed_hosts, "changed_files": sorted(set(changed)),
            "fabric_name": fabric_name}


def build_options(lab: Lab) -> dict[str, Any]:
    """The per-lab build options `lab.build` passes to `build_lab_dir` (custom labs: fabric name, tags, MSS)."""
    st = lab.settings or {}
    custom = lab.template == "custom"
    knobs = st.get("knobs") or {}
    return {"fabric_name": st.get("fabric_name") if custom else None,
            "device_tags": (knobs.get("device_tags") or None) if custom else None,
            "segmentation": (knobs.get("segmentation") or None) if custom else None}


def build_devices(lab: Lab, hostnames: list[str], *, log: Log = print, expand: bool = True) -> dict[str, Any]:
    """Phase 6 (C): partial in-process build of `hostnames` (+ related devices) of a lab, then the containerlab
    topology is regenerated (so `clab/clients.json` and `topology.clab.yml` stay truthful). Not for ACT labs."""
    if (lab.target or "clab") == "act":
        raise BuildError("partial builds are not supported for ACT labs: run a full build")
    lab_dir = service.lab_dir(lab)
    res = build_devices_dir(lab_dir, hostnames, log=log, expand=expand, **build_options(lab))
    from ..topology.clab import generate_for_lab
    from ..topology.clients import apply_client_overrides

    generate_for_lab(lab, lab_dir, log=log)
    apply_client_overrides(lab_dir, (lab.settings or {}).get("hot_clients") or {})
    return res


def _inject_segmentation(host: str, sc_dict: dict[str, Any], segmentation: dict[str, Any], sc: Any, log: Log) -> Any:
    """Merge the MSS structured config into `sc_dict` when the host carries a segmented VRF; returns what to render
    the config/doc from (the original EOSConfig when nothing was injected, else the validated dict)."""
    from pyavd import validate_structured_config

    from . import mss

    seg = mss.normalize_segmentation(segmentation)
    if not seg["enabled"]:
        return sc
    vrfs = mss.carriers_from_structured({host: sc_dict}, [v["name"] for v in seg["vrfs"]]).get(host)
    extra = mss.structured_config_for(seg, vrfs or [])
    if not extra:
        return sc
    mss._deep_merge(sc_dict, extra)
    res = validate_structured_config(sc_dict)
    if res.validated_data is None:
        errors = [f"{getattr(e, 'path', '')}: {getattr(e, 'message', e)}" for e in res.validation_result.violations][:5]
        raise BuildError(f"{host}: segmentation produced an invalid structured config: {errors}")
    log(f"[{host}] router segment-security injected for VRF(s) {', '.join(vrfs or [])}")
    return res.validated_data


# ------------------------------------------------------------------------------------------------ phase 4 (D): ACT twin
def twin_inputs(act: dict[str, Any] | None) -> dict[str, Any]:
    """eos_designs `digital_twin` input from the lab knob `act`."""
    act = act or {}
    return {"environment": "act", "fabric": {
        "act_os_version": str(act.get("os_version") or "4.33.1F"),
        "act_username": str(act.get("username") or "cvpadmin"),
        "act_password": str(act.get("password") or "cvp123!"),
        "act_internet_access": bool(act.get("internet_access", True)),
        "act_ensure_eapi_access": True,
    }}


def _twin_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in over.items():
        out[k] = _twin_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _add_twin_user(sc: dict[str, Any], fabric: dict[str, Any]) -> bool:
    """Add the ACT node user (privilege 15, network-admin) unless a user of that name exists. True when changed."""
    from pyavd import validate_structured_config

    from .templates import sha512_password

    user = str(fabric.get("act_username") or "")
    if not user or any(u.get("name") == user for u in sc.get("local_users") or []):
        return False
    sc.setdefault("local_users", []).append({"name": user, "privilege": 15, "role": "network-admin",
                                             "sha512_password": sha512_password(str(fabric.get("act_password") or ""))})
    violations = validate_structured_config(sc).validation_result.violations
    if violations:
        raise BuildError(f"{sc.get('hostname')}: ACT user rejected: {violations[0]}")
    return True


def act_topology_yaml(twin: Any) -> str:
    """pyavd ACTDigitalTwin → ACT topology YAML, shaped like the eos_designs_documentation action plugin output
    (dataclass keys with `_` → `-`, empty values stripped, order: nodes, <node-type credentials>, links)."""
    from dataclasses import asdict

    def strip(v: Any) -> Any:
        if isinstance(v, dict):
            return {k: x2 for k, x in v.items() if (x2 := strip(x)) not in (None, {}, [], "")}
        if isinstance(v, (list, tuple)):
            return [x2 for x in v if (x2 := strip(x)) not in (None, {}, [], "")]
        return v

    content = strip({str(k).replace("_", "-"): v for k, v in asdict(twin).items()})
    return yaml.safe_dump(content, sort_keys=False, indent=2, width=130, default_flow_style=False)


def anta_inventory(structured: dict[str, dict[str, Any]]) -> str:
    """ANTA inventory (name == AVD hostname so AVD-generated catalogs match)."""
    hosts = []
    for host, sc in structured.items():
        mgmt = (sc.get("management_interfaces") or [{}])[0]
        ip = (mgmt.get("ip_address") or "").split("/")[0]
        if ip:
            hosts.append({"name": host, "host": ip, "tags": [str((sc.get("metadata") or {}).get("fabric_name", "FABRIC"))]})
    return yaml.safe_dump({"anta_inventory": {"hosts": hosts}}, sort_keys=False)


def device_summary(lab_dir: Path) -> list[dict[str, Any]]:
    """Table for GET /labs/{id}/devices, read from intended/structured_configs."""
    out: list[dict[str, Any]] = []
    for f in sorted((lab_dir / "intended" / "structured_configs").glob("*.yml")):
        sc = yaml.safe_load(f.read_text()) or {}
        meta = sc.get("metadata") or {}
        mgmt = (sc.get("management_interfaces") or [{}])[0]
        loopbacks = {lo.get("name"): lo.get("ip_address") for lo in sc.get("loopback_interfaces") or []}
        vxlan = ((sc.get("vxlan_interface") or {}).get("vxlan1") or {}).get("vxlan") or {}
        bgp = sc.get("router_bgp") or {}
        out.append({
            "hostname": sc.get("hostname", f.stem),
            "type": _node_type(sc, lab_dir),
            "platform": meta.get("platform"),
            "mgmt_ip": mgmt.get("ip_address"),
            "mgmt_interface": mgmt.get("name"),
            "serial": meta.get("serial_number"),
            "loopback0": loopbacks.get("Loopback0"),
            "vtep": loopbacks.get(vxlan.get("source_interface", "Loopback1")) if vxlan else None,
            "bgp_as": bgp.get("as"),
            "is_deployed": meta.get("is_deployed", True),
            "tags": device_tags_of(sc),
        })
    return out


def device_tags_of(sc: dict[str, Any]) -> dict[str, str]:
    """`metadata.cv_tags.device_tags` as {label: value} (global + per-device, later entries win), without the
    generated `topology_hint_*` tags."""
    tags = (((sc.get("metadata") or {}).get("cv_tags") or {}).get("device_tags")) or []
    out: dict[str, str] = {}
    for t in tags:
        if isinstance(t, dict) and t.get("name") and not str(t["name"]).startswith("topology_hint_"):
            out[str(t["name"])] = str(t.get("value"))
    return out


_TYPE_CACHE: dict[str, dict[str, str]] = {}


def _node_type(sc: dict[str, Any], lab_dir: Path) -> str | None:
    """Node type from the topology CSV (structured configs do not carry it)."""
    key = str(lab_dir)
    if key not in _TYPE_CACHE or not _TYPE_CACHE[key]:
        _TYPE_CACHE[key] = topology_types(lab_dir)
    return _TYPE_CACHE[key].get(sc.get("hostname", ""))


def topology_types(lab_dir: Path) -> dict[str, str]:
    import csv

    types: dict[str, str] = {}
    for f in (lab_dir / "documentation" / "fabric").glob("*-topology.csv"):
        for row in csv.DictReader(f.open()):
            types[row["Node"]] = row["Node Type"]
            if row.get("Peer Node") and row.get("Peer Type"):
                types.setdefault(row["Peer Node"], row["Peer Type"])
    return types


# ------------------------------------------------------------------------------------------------ job
@job("lab.build")
async def lab_build(ctx: JobContext) -> dict[str, Any]:
    lab = await ctx.get_lab()
    lab_dir = service.lab_dir(lab)
    ctx.log(f"building lab {lab.name} ({lab.template}) in {lab_dir}")
    try:
        fabric_override = (lab.settings or {}).get("fabric_name") if lab.template == "custom" else None
        injected = (((lab.settings or {}).get("knobs") or {}).get("device_tags") or None) if lab.template == "custom" else None
        mss_knob = (((lab.settings or {}).get("knobs") or {}).get("segmentation") or None) if lab.template == "custom" else None
        act_mode = (lab.target or "clab") == "act"  # phase 4 (D): ACT digital-twin build
        twin = twin_inputs(((lab.settings or {}).get("knobs") or {}).get("act")) if act_mode else None
        result = await asyncio.to_thread(build_lab_dir, lab_dir, log=ctx.log, check_cancelled=ctx.check_cancelled,
                                         fabric_name=fabric_override, device_tags=injected, segmentation=mss_knob,
                                         digital_twin=twin)
    except BuildError as exc:
        raise RuntimeError(f"build failed: {exc}") from exc
    _TYPE_CACHE.pop(str(lab_dir), None)
    (lab_dir / "intended" / "build-summary.json").write_text(json.dumps(result.summary(), indent=2))
    sha = await asyncio.to_thread(service.commit_all, lab_dir, f"build: {len(result.hosts)} devices")
    ctx.log(f"committed build outputs ({sha[:8] if sha else 'no changes'})")

    # topology as the last build step
    from ..topology.clab import generate_for_lab

    topo = await asyncio.to_thread(generate_for_lab, lab, lab_dir, log=ctx.log)
    if (lab.settings or {}).get("hot_clients"):  # phase 6 (C): explicit IP / image of hot-added test hosts
        from ..topology.clients import apply_client_overrides

        await asyncio.to_thread(apply_client_overrides, lab_dir, (lab.settings or {}).get("hot_clients") or {})
    sha2 = await asyncio.to_thread(service.commit_all, lab_dir, "topology: containerlab")
    ctx.log(f"containerlab topology: {len(topo.topology.topology.nodes)} nodes, {len(topo.topology.topology.links)} links "
            f"({sha2[:8] if sha2 else 'unchanged'})")
    ctx.artefact("topology", lab_dir / "clab" / "topology.clab.yml")
    act_summary = None
    if act_mode:  # phase 4 (D): tools server + client nodes appended to the ACT topology, client scripts
        from ..executors.act import prepare_act_topology

        act_summary = await asyncio.to_thread(prepare_act_topology, lab, lab_dir, result.fabric_name, log=ctx.log)
        await asyncio.to_thread(service.commit_all, lab_dir, "act: topology + client scripts")
        ctx.artefact("act-topology", lab_dir / act_summary["topology_path"])
    ctx.artefact("fabric-documentation", lab_dir / "documentation" / "fabric" / f"{result.fabric_name}-documentation.md")

    async with ctx.session() as s:
        row = await s.get(Lab, lab.id)
        if row and row.state in (LabState.draft, LabState.built, LabState.destroyed):
            row.state = LabState.built
        if row:
            row.settings = {**(row.settings or {}), "last_build_job": ctx.job_id}
            await s.commit()
    summary = result.summary()
    summary["commit"] = sha
    summary["topology"] = {"nodes": len(topo.topology.topology.nodes), "links": len(topo.topology.topology.links)}
    if act_summary is not None:  # phase 4 (D)
        summary["act"] = act_summary
    return summary
