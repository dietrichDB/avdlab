"""CloudVision helpers: targets ↔ secrets, the CV REST inventory call (fakeable), cv_deploy playbook + result parsing.

Everything that talks HTTP goes through `http_client()` so tests can swap in an `httpx.MockTransport`.
Token values only ever live in the secret store, in memory, in a 0600 env-file on the lab host during a job, and on
stdin of the SSH commands that write them — never on a command line, never in a job log (see `masking_log`).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Callable

import httpx
import yaml
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.db import CVTarget, Secret, SecretKind
from ..core.security import secret_store

CV_KINDS = ("cvaas", "onprem")
INVENTORY_PATH = "/api/resources/inventory/v1/Device/all"
STREAMING_ACTIVE = "STREAMING_STATUS_ACTIVE"
TEST_BUDGET = 15.0
RESULT_BEGIN = "AVDLAB_CV_RESULT_BEGIN"
RESULT_END = "AVDLAB_CV_RESULT_END"
MASK = "********"
LogFn = Callable[[str], None]


class CVError(RuntimeError):
    pass


# ------------------------------------------------------------------------------------------------ servers / secrets
def server_host(server: str) -> str:
    """'https://www.arista.io/' → 'www.arista.io' (cv_deploy wants the bare FQDN/IP[:port])."""
    s = server.strip()
    s = re.sub(r"^[a-z]+://", "", s, flags=re.I)
    return s.split("/", 1)[0]


def base_url(server: str) -> str:
    return f"https://{server_host(server)}"


def secret_name(target_id: str, purpose: str) -> str:
    return f"cv-target-{target_id}-{purpose}"


async def set_target_secret(session: AsyncSession, target: CVTarget, purpose: str, value: str | None,
                            meta: dict[str, Any] | None = None) -> None:
    """purpose 'token' | 'onboarding'. value None → unchanged; '' → removed; else created/replaced (encrypted)."""
    attr = "token_secret_id" if purpose == "token" else "onboarding_token_secret_id"
    if value is None:
        if meta is not None and getattr(target, attr):  # metadata-only update (e.g. expiry)
            sec = await session.get(Secret, getattr(target, attr))
            if sec is not None:
                sec.meta = {**(sec.meta or {}), **meta}
        return
    current_id = getattr(target, attr)
    sec = await session.get(Secret, current_id) if current_id else None
    if value == "":
        setattr(target, attr, None)
        if sec is not None:
            await session.delete(sec)
        return
    value = value.strip()
    if "\n" in value or "\r" in value:
        raise CVError("token must be a single line")
    full_meta = {"purpose": "service_account" if purpose == "token" else "onboarding",
                 "updated_at": datetime.now(timezone.utc).isoformat(), **(meta or {})}
    if sec is None:
        sec = Secret(name=secret_name(target.id, purpose), kind=SecretKind.cv_token,
                     ciphertext=secret_store.encrypt(value), meta=full_meta)
        existing = await secret_store.by_name(session, sec.name)
        if existing is not None:  # stale secret from a deleted target with the same id (should not happen)
            await session.delete(existing)
            await session.flush()
        session.add(sec)
        await session.flush()
        setattr(target, attr, sec.id)
    else:
        sec.ciphertext = secret_store.encrypt(value)
        sec.meta = {**(sec.meta or {}), **full_meta}


# ---- phase 4 (A): the CV "ZTP bootstrap script" (Devices → Onboard → ZTP), a write-only generic secret found by name
ZTP_SCRIPT_MAX = 256 * 1024


def ztp_script_secret_name(target_id: str) -> str:
    return secret_name(target_id, "ztp")


async def set_target_ztp_script(session: AsyncSession, target: CVTarget, value: str | None) -> None:
    """None → unchanged; '' → removed; else created/replaced (encrypted, multi-line)."""
    if value is None:
        return
    sec = await secret_store.by_name(session, ztp_script_secret_name(target.id))
    if value.strip() == "":
        if sec is not None:
            await session.delete(sec)
        return
    if len(value.encode()) > ZTP_SCRIPT_MAX:
        raise CVError(f"ZTP bootstrap script larger than {ZTP_SCRIPT_MAX // 1024} KiB")
    value = value.replace("\r\n", "\n")
    if not value.endswith("\n"):
        value += "\n"
    meta = {"purpose": "ztp_script", "target_id": target.id, "updated_at": datetime.now(timezone.utc).isoformat()}
    if sec is None:
        session.add(Secret(name=ztp_script_secret_name(target.id), kind=SecretKind.generic,
                           ciphertext=secret_store.encrypt(value), meta=meta))
        await session.flush()
    else:
        sec.ciphertext = secret_store.encrypt(value)
        sec.meta = {**(sec.meta or {}), **meta}


async def target_ztp_script_secret(session: AsyncSession, target_id: str) -> Secret | None:
    return await secret_store.by_name(session, ztp_script_secret_name(target_id))


async def target_ztp_script(session: AsyncSession, target: CVTarget) -> str | None:
    sec = await target_ztp_script_secret(session, target.id)
    return secret_store.decrypt(sec.ciphertext) if sec is not None else None


async def target_tokens(session: AsyncSession, target: CVTarget) -> tuple[str | None, str | None]:
    """(service-account token, onboarding token) plaintext."""
    tok = await secret_store.reveal(session, target.token_secret_id) if target.token_secret_id else None
    onb = (await secret_store.reveal(session, target.onboarding_token_secret_id)
           if target.onboarding_token_secret_id else None)
    return tok, onb


def masking_log(log: LogFn, secrets: list[str | None]) -> LogFn:
    values = [s for s in secrets if s]

    def _log(line: str) -> None:
        for v in values:
            line = line.replace(v, MASK)
        log(line)

    return _log


# ------------------------------------------------------------------------------------------------ HTTP (fakeable)
def http_client(*, verify: bool, timeout: float) -> httpx.AsyncClient:
    """Factory for the CV REST client — tests monkeypatch this to inject an httpx.MockTransport."""
    return httpx.AsyncClient(verify=verify, timeout=timeout)


def parse_inventory(text: str) -> list[dict[str, Any]]:
    """CV resource-API GetAll output → [{serial, hostname, streaming_status, streaming}].

    The REST gateway streams one JSON object per line (`{"result": {"value": {...}, "type": "INITIAL"}}`); a JSON array
    or a single object are also accepted."""
    items: list[Any] = []
    text = (text or "").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
        items = data if isinstance(data, list) else [data]
    except ValueError:
        for line in text.splitlines():
            line = line.strip().rstrip(",")
            if not line or line in ("[", "]"):
                continue
            try:
                items.append(json.loads(line))
            except ValueError:
                continue
    out: list[dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        if "error" in it and "result" not in it:
            raise CVError(f"CloudVision error: {it['error']}")
        value = (it.get("result") or {}).get("value") if "result" in it else it.get("value", it)
        if not isinstance(value, dict):
            continue
        serial = (value.get("key") or {}).get("deviceId") or value.get("serialNumber")
        if not serial:
            continue
        status = value.get("streamingStatus") or ""
        out.append({"serial": serial, "hostname": value.get("hostname"), "streaming_status": status,
                    "streaming": status == STREAMING_ACTIVE})
    return out


async def cv_inventory(server: str, token: str, *, verify_certs: bool = True,
                       timeout: float = TEST_BUDGET) -> list[dict[str, Any]]:
    """GET https://<server>/api/resources/inventory/v1/Device/all with the bearer token. Raises CVError."""
    try:
        async with http_client(verify=verify_certs, timeout=timeout) as client:
            resp = await client.get(base_url(server) + INVENTORY_PATH, headers={"Authorization": f"Bearer {token}",
                                                                                  "Accept": "application/json"})
    except httpx.TimeoutException as exc:
        raise CVError(f"timeout talking to {server_host(server)}") from exc
    except httpx.HTTPError as exc:
        msg = str(exc).replace(token, MASK) if token else str(exc)
        hint = " (self-signed certificate? untick 'verify certificates')" if "CERTIFICATE" in msg.upper() else ""
        raise CVError(f"cannot reach {server_host(server)}: {msg or type(exc).__name__}{hint}") from exc
    if resp.status_code in (401, 403):
        raise CVError(f"authentication failed (HTTP {resp.status_code}): service-account token invalid or expired")
    if resp.status_code >= 400:
        raise CVError(f"HTTP {resp.status_code} from {server_host(server)}: {resp.text[:200]}")
    return parse_inventory(resp.text)


async def check_target(server: str, token: str | None, *, verify_certs: bool) -> dict[str, Any]:
    if not token:
        return {"ok": False, "devices_seen": 0, "error": "no service-account token stored for this target"}
    try:
        devices = await cv_inventory(server, token, verify_certs=verify_certs, timeout=TEST_BUDGET)
    except CVError as exc:
        return {"ok": False, "devices_seen": 0, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - anything unexpected is reported, not raised
        return {"ok": False, "devices_seen": 0, "error": f"{type(exc).__name__}: {exc}".replace(token, MASK)}
    return {"ok": True, "devices_seen": len(devices), "error": None}


# ------------------------------------------------------------------------------------------------ cv_deploy playbook
CV_DEPLOY_PLAYBOOK = "playbooks/avdlab-cv-deploy.yml"


def cv_deploy_playbook(fabric_group: str, *, verify_certs: bool, submit_workspace: bool, run_change_control: bool,
                       skip_missing_devices: bool, strict_tags: bool, workspace_name: str | None) -> str:
    role_vars: dict[str, Any] = {
        "cv_server": "{{ lookup('env', 'AVDLAB_CV_SERVER') }}",
        "cv_token": "{{ lookup('env', 'AVDLAB_CV_TOKEN') }}",
        "cv_verify_certs": verify_certs,
        "cv_submit_workspace": submit_workspace,
        "cv_run_change_control": run_change_control,
        "cv_skip_missing_devices": skip_missing_devices,
        "cv_strict_tags": strict_tags,
        "cv_register_detailed_results": True,
        "cv_deploy_tmp_dir": "/tmp/avdlab_cv_deploy",  # container-local: the bind-mounted lab may not be writable
    }
    if workspace_name:
        role_vars["cv_workspace_name"] = workspace_name
    play = [{
        "name": "Deploy to CloudVision (generated by avdlab)",
        "hosts": fabric_group,
        "gather_facts": False,
        "connection": "local",
        "tasks": [{
            "name": "cv_deploy",
            "block": [{"name": "Deploy configurations and tags to CloudVision",
                       "ansible.builtin.import_role": {"name": "arista.avd.cv_deploy"}, "vars": role_vars}],
            "always": [{"name": "avdlab result",
                        "ansible.builtin.debug": {
                            "msg": RESULT_BEGIN + "{{ cv_deploy_results | default({}) | to_json }}" + RESULT_END},
                        "run_once": True}],
        }],
    }]
    return ("---\n# Generated by avdlab for each CloudVision deploy; edits are overwritten.\n"
            "# Server and token come from a 0600 env-file created on the lab host for the run.\n"
            + yaml.safe_dump(play, sort_keys=False, width=200))


def extract_result_json(output: str) -> dict[str, Any] | None:
    """JSON between the markers (ansible prints it inside a JSON string, so it may be escaped once)."""
    matches = re.findall(re.escape(RESULT_BEGIN) + r"(.*?)" + re.escape(RESULT_END), output or "", flags=re.S)
    for raw in reversed(matches):
        for candidate in (raw, None):
            try:
                if candidate is None:
                    candidate = json.loads('"' + raw + '"')  # un-escape \" and \\ once
                data = json.loads(candidate)
            except ValueError:
                continue
            if isinstance(data, dict):
                return data
    return None


def workspace_url(server: str, ws_id: str | None) -> str | None:
    return f"{base_url(server)}/cv/provisioning/workspaces?ws={ws_id}" if ws_id else None


def summarize_deploy(raw: dict[str, Any] | None, *, server: str, output: str = "", rc: int = 0) -> dict[str, Any]:
    """cv_deploy_results (cv_register_detailed_results) → {workspace, change_control, devices, errors, warnings, ...}."""
    raw = raw or {}
    ws = raw.get("workspace") or {}
    cc = raw.get("change_control") or {}
    ws_id = ws.get("id")
    devices: dict[str, dict[str, Any]] = {}
    for key, deployed in (("deployed_configs", True), ("skipped_configs", False), ("configs", None)):
        for item in raw.get(key) or []:
            dev = (item or {}).get("device") or {}
            host = dev.get("hostname")
            if not host:
                continue
            entry = devices.setdefault(host, {"hostname": host, "serial": dev.get("serial_number"),
                                              "exists_on_cv": dev.get("exists_on_cv"), "streaming": dev.get("streaming"),
                                              "deployed": None})
            for f in ("exists_on_cv", "streaming"):
                if entry[f] is None and dev.get(f) is not None:
                    entry[f] = dev.get(f)
            if entry["serial"] is None and dev.get("serial_number"):
                entry["serial"] = dev.get("serial_number")
            if deployed is not None and entry["deployed"] is not True:
                entry["deployed"] = deployed
    errors = [str(e) for e in raw.get("errors") or []]
    if not raw and rc != 0:
        errors = errors or fatal_messages(output) or [f"ansible-playbook failed (rc={rc}); see the job log"]
    return {
        "ok": rc == 0 and not raw.get("failed") and not errors,
        "workspace": {"id": ws_id, "name": ws.get("name"), "state": ws.get("state"),
                      "url": workspace_url(server, ws_id)} if ws else None,
        "change_control": {"id": cc.get("id") or ws.get("change_control_id"), "name": cc.get("name"),
                           "state": cc.get("state")} if (cc or ws.get("change_control_id")) else None,
        "devices": sorted(devices.values(), key=lambda d: d["hostname"]),
        "deployed_device_tags": len(raw.get("deployed_device_tags") or []),
        "deployed_interface_tags": len(raw.get("deployed_interface_tags") or []),
        "errors": errors,
        "warnings": [str(w) for w in raw.get("warnings") or []],
        "notes": [str(n) for n in raw.get("notes") or []],
        "ansible_rc": rc,
    }


def fatal_messages(output: str) -> list[str]:
    msgs: list[str] = []
    for line in (output or "").splitlines():
        if line.startswith(("fatal:", "ERROR!")) or "FAILED!" in line:
            m = re.search(r'"msg":\s*"((?:[^"\\]|\\.)*)"', line)
            msgs.append(m.group(1) if m else line.strip()[:300])
    return msgs[:10]
