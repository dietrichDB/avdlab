"""Custom labs (phase 3 §4): archive import (zip / tar.gz), fabric_name detection, build + topology, export."""
from __future__ import annotations

import io
import tarfile
import uuid
import zipfile
from pathlib import Path

import yaml

from avdlab.jobs.engine import read_log, runner
from avdlab.labs import archive
from avdlab.labs.templates import get_template, render_lab


def lab_files(fabric_name: str = "FABRIC") -> dict[str, str]:
    files = render_lab(get_template("single-dc-l3ls"), {}, slug="imp").files()
    if fabric_name != "FABRIC":
        gv = yaml.safe_load(files["group_vars/FABRIC.yml"])
        gv["fabric_name"] = fabric_name
        files["group_vars/FABRIC.yml"] = yaml.safe_dump(gv, sort_keys=False)
    return files


def make_zip(files: dict[str, str], top: str | None = "my-fabric") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for rel, content in files.items():
            zf.writestr(f"{top}/{rel}" if top else rel, content)
    return buf.getvalue()


def make_tgz(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for rel, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(rel)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


async def upload(client, data: bytes, name: str, filename: str = "lab.zip"):
    return await client.post("/api/labs/import", files={"file": (filename, data, "application/octet-stream")},
                             data={"name": name})


def test_extract_rules(tmp_path):
    files = {**lab_files(), "intended/configs/x.cfg": "stale", ".git/HEAD": "ref", "documentation/a.md": "#"}
    written = archive.extract_archive(make_zip(files), "a.zip", tmp_path / "a")
    assert "inventory.yml" in written and not any(w.startswith(("intended/", ".git/", "documentation/")) for w in written)
    assert (tmp_path / "a" / "group_vars" / "FABRIC.yml").exists()  # top-level dir stripped
    assert archive.detect_fabric_name(tmp_path / "a") == "FABRIC"
    import pytest

    with pytest.raises(archive.ImportError_):
        archive.extract_archive(make_zip({"inventory.yml": "all: {}"}, top=None), "b.zip", tmp_path / "b")
    with pytest.raises(archive.ImportError_):
        archive.extract_archive(make_zip({"../evil.yml": "x", "inventory.yml": "", "group_vars/a.yml": ""}, top=None),
                                "c.zip", tmp_path / "c")
    with pytest.raises(archive.ImportError_):
        archive.extract_archive(b"not an archive", "d.zip", tmp_path / "d")


async def test_import_zip_build_export(client):
    name = f"imported-{uuid.uuid4().hex[:6]}"
    files = {**lab_files("MYFAB"), "intended/configs/stale.cfg": "old"}
    r = await upload(client, make_zip(files), name)
    assert r.status_code == 201, r.text
    lab = r.json()
    assert lab["template"] == "custom" and lab["state"] == "draft" and lab["import_warnings"] == []
    assert lab["settings"]["fabric_name"] == "MYFAB" and lab["settings"]["import_warnings"] == []
    assert lab["knobs"]["ceos_version"] == "4.34.2F" and lab["knobs"]["ceos_arch"] == "x86_64"
    root = Path(lab["dir"])
    assert not (root / "intended" / "configs" / "stale.cfg").exists()
    hist = (await client.get(f"/api/labs/{lab['id']}/history")).json()
    assert hist and hist[-1]["message"].startswith("import")
    assert (await upload(client, make_zip(files), name)).status_code == 409

    # knob change on a custom lab: nothing re-rendered, group_vars untouched
    gv_before = sorted(p.name for p in (root / "group_vars").iterdir())
    r = await client.patch(f"/api/labs/{lab['id']}", json={"knobs": {"ceos_arch": "arm64", "ceos_version": "4.35.0F"}})
    assert r.status_code == 200, r.text
    assert r.json()["knobs"]["ceos_arch"] == "arm64" and r.json()["ceos_version"] == "4.35.0F"
    assert sorted(p.name for p in (root / "group_vars").iterdir()) == gv_before

    # build uses settings.fabric_name for the documentation / topology CSV, then the topology picks the arm tag
    job_id = (await client.post(f"/api/labs/{lab['id']}/build")).json()["job_id"]
    await runner.wait(job_id)
    job = (await client.get(f"/api/jobs/{job_id}")).json()
    assert job["status"] == "succeeded", (job["error"], read_log(job_id)[-2000:])
    assert job["result"]["fabric_name"] == "MYFAB"
    assert (root / "documentation" / "fabric" / "MYFAB-topology.csv").exists()
    topo = yaml.safe_load((root / "clab" / "topology.clab.yml").read_text())
    assert topo["topology"]["kinds"]["arista_ceos"]["image"] == "arista/ceos:4.35.0F-arm64"

    # export: zip of the lab without .git
    r = await client.get(f"/api/labs/{lab['id']}/export")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    slug = lab["slug"]
    assert f"{slug}/inventory.yml" in names and f"{slug}/intended/configs/dc1-spine1.cfg" in names
    assert not any("/.git/" in n for n in names)
    assert (await client.delete(f"/api/labs/{lab['id']}")).status_code == 204


async def test_import_tgz_hosts_yml_and_warnings(client):
    files = lab_files()
    files["hosts.yml"] = files.pop("inventory.yml")
    files["group_vars/NETWORK_SERVICES.yml"] = files["group_vars/NETWORK_SERVICES.yml"].replace(
        "vrf_vni: 10", "vrf_vni: not-a-number", 1)
    r = await upload(client, make_tgz(files), f"tgz-{uuid.uuid4().hex[:6]}", "lab.tar.gz")
    assert r.status_code == 201, r.text
    lab = r.json()
    root = Path(lab["dir"])
    assert (root / "inventory.yml").exists() and not (root / "hosts.yml").exists()
    assert lab["import_warnings"] and lab["state"] == "draft"  # created + warned, fixable in the editor
    assert any("vrf_vni" in (w.get("path") or "") for w in lab["settings"]["import_warnings"])
    assert (await client.delete(f"/api/labs/{lab['id']}")).status_code == 204

    # structural problems → 422, no lab created
    r = await upload(client, make_zip({"README.md": "hi"}), "broken")
    assert r.status_code == 422 and r.json()["detail"]["violations"]
    r = await upload(client, b"garbage", "broken2", "x.zip")
    assert r.status_code == 422
    assert not [x for x in (await client.get("/api/labs")).json() if x["name"] in ("broken", "broken2")]
    # custom template cannot be rendered from knobs
    r = await client.post("/api/labs", json={"name": "custom-direct", "template": "custom"})
    assert r.status_code == 422
