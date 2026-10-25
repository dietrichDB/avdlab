"""Phase 5: /api/meta, changelog parsing, notices."""
from __future__ import annotations

from pathlib import Path

from avdlab.core.config import settings
from avdlab.meta import api as meta

ROOT = Path(__file__).resolve().parents[2]
VERSION = (ROOT / "VERSION").read_text().strip()


async def test_meta(client, monkeypatch):
    monkeypatch.setattr(settings, "git_sha", "abc123def456")
    monkeypatch.setattr(settings, "build_date", "2026-09-26T10:00:00Z")
    r = await client.get("/api/meta")
    assert r.status_code == 200
    m = r.json()
    assert m["version"] == (ROOT / "VERSION").read_text().strip() == VERSION
    assert m["git_sha"] == "abc123def456" and m["build_date"] == "2026-09-26T10:00:00Z"
    assert m["avd_version"].startswith("6.4") and m["anta_version"] and m["python"].startswith("3.")
    assert m["copyright"] == "© 2026 Dietrich De Bruyne — Systems Engineer, Arista Networks"
    assert m["copyright_notice"] == "© 2026 Dietrich De Bruyne — Systems Engineer, Arista Networks. All rights reserved."
    latest = m["changelog_latest"]
    assert latest["version"] == VERSION and latest["date"] and latest["sections"]


async def test_meta_is_public_and_copyright_configurable(monkeypatch):
    from httpx import ASGITransport, AsyncClient

    from avdlab.main import app

    monkeypatch.setattr(settings, "copyright", "© 2027 Someone Else")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:  # no login
        m = (await c.get("/api/meta")).json()
        assert m["copyright_notice"] == "© 2027 Someone Else. All rights reserved."
        assert (await c.get("/api/meta/changelog")).status_code == 200
        assert (await c.get("/api/feedback")).status_code == 401


async def test_changelog(client):
    data = (await client.get("/api/meta/changelog")).json()
    versions = [v["version"] for v in data["versions"]]
    assert versions[0] == VERSION and versions[-1] == "0.1.0" and "0.10.0" in versions and "0.9.0" in versions
    assert all(tuple(map(int, a.split("."))) > tuple(map(int, b.split("."))) for a, b in zip(versions, versions[1:]))  # descending
    assert all(v["date"] for v in data["versions"])
    assert all(set(v["sections"]) <= {"Added", "Changed", "Fixed", "Removed", "Deprecated", "Security"} for v in data["versions"])
    assert data["raw"].startswith("# Changelog")
    fixed = next(v for v in data["versions"] if v["version"] == "0.5.3")["sections"]["Fixed"]
    assert fixed and "SSH" in fixed[0]


def test_parse_changelog_wrapping_and_unreleased():
    text = """# Changelog

## [Unreleased]

## [1.2.0] - 2026-01-02
### Added
- first item
  continued here
- second
### Fixed
* bug

## 1.1.0 - 2025-12-01
### Changed
- x

[1.2.0]: https://example.invalid/compare
"""
    parsed = meta.parse_changelog(text)
    assert [v["version"] for v in parsed] == ["1.2.0", "1.1.0"]
    assert parsed[0]["sections"] == {"Added": ["first item continued here", "second"], "Fixed": ["bug"]}
    assert parsed[1] == {"version": "1.1.0", "date": "2025-12-01", "sections": {"Changed": ["x"]}}


async def test_notices_and_license(client):
    data = (await client.get("/api/meta/notices")).json()
    comps = {c["name"]: c["license"] for c in data["components"]}
    assert comps["ANTA"] == "Apache-2.0" and comps["containerlab"] == "BSD-3-Clause"
    assert comps["React Flow (@xyflow/react)"] == "MIT" and comps["cEOS-lab"] == "Arista EULA"
    assert len(comps) == 13
    assert "All rights reserved" in data["license_text"] and "TODO" in data["license_text"]
    assert "not an Arista product" in data["trademark"]


async def test_meta_dir_override(client, monkeypatch, tmp_path):
    (tmp_path / "VERSION").write_text("9.9.9\n")
    monkeypatch.setattr(settings, "meta_dir", tmp_path)
    m = (await client.get("/api/meta")).json()
    assert m["version"] == "9.9.9" and m["changelog_latest"] is None
