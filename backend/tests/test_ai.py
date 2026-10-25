"""v0.10.0: AI lab designer — providers (request shapes), per-user link, shared default gating, prompt → validated
proposal → apply, clarifying question round-trip, repair round, key hygiene. The provider HTTP call is faked."""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from avdlab.ai import designer, providers
from avdlab.core.config import settings
from avdlab.core.db import AILink, AppSetting, AuditLog, Secret, SessionLocal

KEY = "sk-ant-TESTKEY-0123456789abcdef"
ACME = [{"name": "ACME", "mac_vrf_vni_base": 10000, "vrfs": [{"name": "ACME_PROD", "vrf_vni": 10, "svis": [
    {"id": 110, "name": "APP", "enabled": True, "ip_address_virtual": "10.1.110.1/24"},
    {"id": 120, "name": "DB", "enabled": True, "ip_address_virtual": "10.1.120.1/24"}]}]}]
SERVER_OP = {"op": "add_endpoint", "name": "srv-ai-1", "adapters": [
    {"switches": ["dc1-leaf1a", "dc1-leaf1b"], "switch_ports": ["Ethernet7", "Ethernet7"], "mode": "trunk",
     "vlans": "110,120", "port_channel": {"mode": "active"}}]}


class FakeHTTP:
    """Replaces providers.http_request: records (method, url, headers, body), replays scripted responses."""

    def __init__(self, *responses: tuple[int, Any]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, method, url, headers, body=None, timeout=None):
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "body": body})
        assert self.responses, f"unexpected provider call {method} {url}"
        return self.responses.pop(0)


def anthropic_tool(name: str, args: dict) -> tuple[int, dict]:
    return 200, {"id": "msg_1", "type": "message", "role": "assistant", "stop_reason": "tool_use",
                 "content": [{"type": "text", "text": "Here is my plan."},
                             {"type": "tool_use", "id": "toolu_1", "name": name, "input": args}]}


def openai_tool(name: str, args: dict) -> tuple[int, dict]:
    return 200, {"choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}}]}


@pytest.fixture
def fake(monkeypatch):
    def install(*responses: tuple[int, Any]) -> FakeHTTP:
        f = FakeHTTP(*responses)
        monkeypatch.setattr(providers, "http_request", f)
        return f

    return install


@pytest_asyncio.fixture(autouse=True)
async def _clean_ai():
    from avdlab.core.db import init_db

    await init_db()  # pure tests do not use `client`
    designer.reset_rate_limit()
    yield
    async with SessionLocal() as s:
        await s.execute(delete(AILink))
        await s.execute(delete(AppSetting).where(AppSetting.key.like("ai_%")))
        await s.execute(delete(Secret).where(Secret.name.like("ai_key_%")))
        await s.execute(delete(Secret).where(Secret.name == "ai_default_api_key"))
        await s.commit()


async def _lab(client) -> dict:
    r = await client.post("/api/labs", json={"name": f"ai-{uuid.uuid4().hex[:6]}", "template": "single-dc-l3ls", "knobs": {}})
    assert r.status_code == 201, r.text
    return r.json()


async def _link(client, provider: str = "anthropic", **kw) -> dict:
    body = {"provider": provider, "model": kw.pop("model", "claude-sonnet-4-5"), "api_key": kw.pop("api_key", KEY), **kw}
    r = await client.put("/api/ai/link", json=body)
    assert r.status_code == 200, r.text
    return r.json()


# ------------------------------------------------------------------------------------------------ provider shapes
async def test_anthropic_request_shape(fake):
    f = fake(anthropic_tool("ask_user", {"question": "How many spines?"}))
    p = providers.make_provider("anthropic", "", "claude-sonnet-4-5", KEY)
    tools = [{"name": "ask_user", "description": "d", "input_schema": {"type": "object", "properties": {}}}]
    out = await p.tool_call("SYS", [{"role": "user", "content": "hi"}], tools, "any")
    assert out["tool_calls"] == [{"name": "ask_user", "arguments": {"question": "How many spines?"}}]
    assert out["text"] == "Here is my plan."
    call = f.calls[0]
    assert call["method"] == "POST" and call["url"] == "https://api.anthropic.com/v1/messages"
    assert call["headers"]["x-api-key"] == KEY and call["headers"]["anthropic-version"] == "2023-06-01"
    body = call["body"]
    assert body["model"] == "claude-sonnet-4-5" and body["system"] == "SYS" and body["max_tokens"] > 0
    assert body["messages"] == [{"role": "user", "content": "hi"}]
    assert body["tools"] == [{"name": "ask_user", "description": "d", "input_schema": {"type": "object", "properties": {}}}]
    assert body["tool_choice"] == {"type": "any"}
    assert p.request_body("s", [], tools, "ask_user")["tool_choice"] == {"type": "tool", "name": "ask_user"}


async def test_openai_request_shape_ollama_and_azure(fake):
    f = fake(openai_tool("ask_user", {"question": "q?"}), (200, {"data": [{"id": "llama3.1"}]}))
    p = providers.make_provider("openai", "http://ollama.local:11434", "llama3.1", "")
    tools = [{"name": "ask_user", "description": "d", "input_schema": {"type": "object"}}]
    out = await p.tool_call("SYS", [{"role": "user", "content": "hi"}], tools, "any")
    assert out["tool_calls"] == [{"name": "ask_user", "arguments": {"question": "q?"}}]
    call = f.calls[0]
    assert call["url"] == "http://ollama.local:11434/v1/chat/completions"
    assert "authorization" not in call["headers"]  # no key → no auth header (Ollama)
    body = call["body"]
    assert body["model"] == "llama3.1"
    assert body["messages"][0] == {"role": "system", "content": "SYS"} and body["messages"][1] == {"role": "user", "content": "hi"}
    assert body["tools"] == [{"type": "function", "function": {"name": "ask_user", "description": "d", "parameters": {"type": "object"}}}]
    assert body["tool_choice"] == "required"
    assert (await p.probe())["models"] == ["llama3.1"] and f.calls[1]["url"] == "http://ollama.local:11434/v1/models"

    o = providers.make_provider("openai", "", "gpt-4o", "sk-openai-abcdefghijkl")
    assert o.url("chat/completions") == "https://api.openai.com/v1/chat/completions"
    assert o.headers()["authorization"] == "Bearer sk-openai-abcdefghijkl"
    az = providers.make_provider("openai", "https://res1.openai.azure.com/openai/deployments/gpt4o", "gpt-4o", "azkey123")
    assert az.url("chat/completions") == ("https://res1.openai.azure.com/openai/deployments/gpt4o/chat/completions"
                                          "?api-version=2024-10-21")
    assert az.headers()["api-key"] == "azkey123" and "authorization" not in az.headers()
    assert az.request_body("s", [], tools, "ask_user")["tool_choice"] == {"type": "function", "function": {"name": "ask_user"}}


def test_tool_schema_generated_from_template():
    tpl = designer.template_for("single-dc-l3ls")
    tools = designer.tools(tpl, lab_mode=True, l3ls=True)
    props = tools[0]["input_schema"]["properties"]
    knobs = props["knobs"]["properties"]
    assert knobs["spine_count"]["type"] == "integer" and knobs["spine_count"]["minimum"] == 1 and knobs["spine_count"]["maximum"] == 4
    assert "device_password" not in knobs  # secret knobs are never offered to the model
    assert "Edited as YAML" in knobs["tenants"]["description"] or "tenants" in knobs["tenants"]["description"]
    ops = {o["properties"]["op"]["const"] for o in props["topology_ops"]["items"]["anyOf"]}
    assert ops == {"add_endpoint", "update_endpoint", "remove_endpoint", "set_uplink", "add_leaf_pair",
                   "remove_last_leaf_pair", "set_l2leaf_per_pair", "set_spine_count"}
    assert "topology_ops" not in designer.tools(tpl, lab_mode=False, l3ls=True)[0]["input_schema"]["properties"]


# ------------------------------------------------------------------------------------------------ link
async def test_link_crud_key_write_only(client):
    v = (await client.get("/api/ai/link")).json()
    assert v["linked"] is False and v["effective"] is None
    v = await _link(client)
    assert v["linked"] and v["link"]["has_api_key"] and v["link"]["provider"] == "anthropic"
    assert KEY not in json.dumps(v)
    async with SessionLocal() as s:
        sec = (await s.execute(select(Secret).where(Secret.name.like("ai_key_%")))).scalar_one()
        assert KEY not in sec.ciphertext
    assert KEY not in (await client.get("/api/secrets")).text
    # update without api_key keeps the key; "" removes it
    v = (await client.put("/api/ai/link", json={"provider": "openai", "base_url": "http://ollama:11434/v1", "model": "llama3.1"})).json()
    assert v["link"]["has_api_key"] is True and v["link"]["provider"] == "openai"
    v = (await client.put("/api/ai/link", json={"provider": "openai", "model": "llama3.1", "api_key": ""})).json()
    assert v["link"]["has_api_key"] is False
    r = await client.put("/api/ai/link", json={"provider": "gemini", "model": "x"})
    assert r.status_code == 422
    r = await client.put("/api/ai/link", json={"provider": "openai", "model": "x", "base_url": "ftp://nope"})
    assert r.status_code == 422
    assert (await client.delete("/api/ai/link")).status_code == 204
    assert (await client.get("/api/ai/link")).json()["linked"] is False


async def test_link_test_probe(client, fake):
    await _link(client)
    f = fake((200, {"data": [{"id": "claude-sonnet-4-5"}, {"id": "claude-opus-4-1"}]}))
    r = (await client.post("/api/ai/link/test", json={})).json()
    assert r == {"ok": True, "models": ["claude-sonnet-4-5", "claude-opus-4-1"], "model_listed": True, "error": None}
    assert f.calls[0]["url"] == "https://api.anthropic.com/v1/models" and f.calls[0]["headers"]["x-api-key"] == KEY
    fake((401, {"type": "error", "error": {"type": "authentication_error", "message": f"invalid x-api-key {KEY}"}}))
    r = (await client.post("/api/ai/link/test", json={})).json()
    assert r["ok"] is False and "401" in r["error"] and KEY not in r["error"]


# ------------------------------------------------------------------------------------------------ design → apply
async def test_prompt_to_proposal_dry_run_and_apply(client, fake):
    lab = await _lab(client)
    await _link(client)
    args = {"summary": "3 leaf pairs, tenant ACME with VLANs 110/120 and one MLAG server",
            "assumptions": ["VRF ACME_PROD with VNI 10"], "knobs": {"leaf_pairs": 3, "tenants": ACME},
            "topology_ops": [SERVER_OP]}
    f = fake(anthropic_tool("propose_lab_changes", args))
    r = await client.post("/api/ai/design", json={"lab_id": lab["id"], "prompt": "3 leaf pairs, tenant ACME with VLANs 110/120, one server on leaf pair 1"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["kind"] == "proposal" and out["source"] == "user" and out["repaired"] is False
    assert out["assumptions"] == ["VRF ACME_PROD with VNI 10"]
    assert out["knobs"]["leaf_pairs"] == 3 and out["knobs"]["tenants"][0]["name"] == "ACME"
    assert {c["key"]: (c["old"], c["new"]) for c in out["knob_changes"]}["leaf_pairs"] == (2, 3)
    assert out["topology_ops"] == [SERVER_OP]
    dry = out["dry_run"]
    assert dry["valid"] is True and dry["violations"] == []
    assert "group_vars/CONNECTED_ENDPOINTS.yml" in dry["changed_files"] and "group_vars/DC1_L3_LEAVES.yml" in dry["changed_files"]
    assert "srv-ai-1" in dry["diff"] and "ACME" in dry["diff"]
    assert "dc1-leaf3a" in {n["name"] for n in dry["graph"]["nodes"]}
    # the system prompt carries the template knobs, current values and the topology summary — never the password
    body = f.calls[0]["body"]
    assert "spine_count" in body["system"] and "dc1-leaf1a" in body["system"] and "free_ports" in body["system"]
    assert "arista" not in json.dumps(body["system"]).split("device_password")[1][:80]
    assert {t["name"] for t in body["tools"]} == {"propose_lab_changes", "ask_user"}
    # nothing changed yet
    assert (await client.get(f"/api/labs/{lab['id']}")).json()["knobs"].get("leaf_pairs", 2) == 2

    r = await client.post("/api/ai/design/apply", json={"lab_id": lab["id"], "knobs": out["knobs"], "topology_ops": out["topology_ops"]})
    assert r.status_code == 200, r.text
    res = r.json()
    knobs = res["lab"]["knobs"]
    assert knobs["leaf_pairs"] == 3 and knobs["tenants"][0]["name"] == "ACME"
    assert any(s["name"] == "srv-ai-1" for s in knobs["servers"])
    assert res["topology"]["commit"]
    hist = (await client.get(f"/api/labs/{lab['id']}/history")).json()
    msgs = " | ".join(str(h.get("message") or h.get("subject") or h) for h in hist)
    assert "knobs:" in msgs and "topology edit" in msgs
    async with SessionLocal() as s:
        ev = (await s.execute(select(AuditLog).where(AuditLog.event == "ai.apply"))).scalars().all()
        assert any(lab["name"] in (e.detail or "") for e in ev)


async def test_question_round_trip(client, fake):
    lab = await _lab(client)
    await _link(client)
    f = fake(anthropic_tool("ask_user", {"question": "Which leaf pair should the server connect to?"}))
    out = (await client.post("/api/ai/design", json={"lab_id": lab["id"], "prompt": "add a server"})).json()
    assert out["kind"] == "question" and out["question"].startswith("Which leaf pair")
    f = fake(anthropic_tool("propose_lab_changes", {"summary": "server on pair 1", "knobs": {}, "topology_ops": [SERVER_OP]}))
    history = [{"role": "user", "content": "add a server"}, {"role": "assistant", "content": out["assistant_message"]}]
    out = (await client.post("/api/ai/design", json={"lab_id": lab["id"], "prompt": "pair 1", "history": history})).json()
    assert out["kind"] == "proposal" and out["dry_run"]["valid"], out
    msgs = f.calls[0]["body"]["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    assert msgs[1]["content"].startswith("Which leaf pair") and msgs[2]["content"] == "pair 1"


async def test_invalid_proposal_gets_one_repair_round(client, fake):
    lab = await _lab(client)
    await _link(client)
    f = fake(anthropic_tool("propose_lab_changes", {"summary": "9 spines", "knobs": {"spine_count": 9}}),
             anthropic_tool("propose_lab_changes", {"summary": "3 leaf pairs instead", "knobs": {"leaf_pairs": 3}}))
    out = (await client.post("/api/ai/design", json={"lab_id": lab["id"], "prompt": "9 spines"})).json()
    assert len(f.calls) == 2
    repair = f.calls[1]["body"]["messages"][-1]["content"]
    assert "FAILED" in repair and "must be <= 4" in repair
    assert f.calls[1]["body"]["messages"][-2]["role"] == "assistant"
    assert out["kind"] == "proposal" and out["repaired"] is True and out["dry_run"]["valid"] and out["knobs"] == {"leaf_pairs": 3}, out["dry_run"]["violations"]

    # still invalid after the repair round → returned with violations, no third call
    f = fake(anthropic_tool("propose_lab_changes", {"summary": "x", "knobs": {"spine_count": 9}}),
             anthropic_tool("propose_lab_changes", {"summary": "y", "knobs": {"no_such_knob": 1}, "topology_ops": [{"op": "add_endpoint", "name": "s", "adapters": [{"switches": ["nope"], "switch_ports": ["Ethernet1"]}]}]}))
    out = (await client.post("/api/ai/design", json={"lab_id": lab["id"], "prompt": "9 spines"})).json()
    assert len(f.calls) == 2 and out["dry_run"]["valid"] is False and out["dry_run"]["violations"]
    # apply refuses an invalid plan and changes nothing
    r = await client.post("/api/ai/design/apply", json={"lab_id": lab["id"], "knobs": {"spine_count": 9}})
    assert r.status_code == 422 and "must be <= 4" in r.text
    assert (await client.get(f"/api/labs/{lab['id']}")).json()["knobs"].get("spine_count", 2) == 2


async def test_new_lab_template_mode_returns_knobs(client, fake):
    await _link(client, "openai", base_url="http://ollama.local:11434/v1", model="llama3.1", api_key="")
    # a pool that is not a CIDR fails knob validation → repair round → the model drops it
    # (0.10.0: 3+ spines no longer collide with the default server ports — avdlab shifts them)
    f = fake(openai_tool("propose_lab_changes", {"summary": "bad pool", "knobs": {"pools": {"loopback": "not-a-cidr"}}}),
             openai_tool("propose_lab_changes", {
        "summary": "2 spines, 4 leaf pairs, CVaaS", "knobs": {"leaf_pairs": 4,
                                                             "cloudvision": {"mode": "cvaas"}, "device_password": "x"},
        "topology_ops": [{"op": "set_l2leaf_per_pair", "n": 0}]}))
    r = await client.post("/api/ai/design", json={"template": "single-dc-l3ls", "prompt": "3 spines 4 leaf pairs cvaas",
                                                  "current_knobs": {"dc_name": "dc1"}})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["kind"] == "proposal" and out["dry_run"]["valid"] and out["repaired"], out["dry_run"]
    assert "FAILED" in f.calls[1]["body"]["messages"][-1]["content"]
    assert "pools" not in out["knobs"] and out["knobs"]["leaf_pairs"] == 4 and out["knobs"]["l2leaf_per_pair"] == 0
    assert out["knobs"]["cloudvision"]["mode"] == "cvaas" and "device_password" not in out["knobs"]
    assert any("device_password" in a for a in out["assumptions"])
    assert out["topology_ops"] == [] and out["dry_run"]["nodes"] == 10
    assert f.calls[0]["url"] == "http://ollama.local:11434/v1/chat/completions"
    assert "NEW LAB" in f.calls[0]["body"]["messages"][0]["content"]


# ------------------------------------------------------------------------------------------------ hygiene / gating
async def test_key_never_in_logs_or_responses(client, fake, caplog):
    caplog.set_level(logging.DEBUG)
    lab = await _lab(client)
    await _link(client)
    fake((401, {"type": "error", "error": {"type": "authentication_error", "message": f"invalid x-api-key: {KEY}"}}))
    r = await client.post("/api/ai/design", json={"lab_id": lab["id"], "prompt": "hello"})
    assert r.status_code == 502 and "401" in r.text
    assert KEY not in r.text and KEY not in caplog.text
    fake(anthropic_tool("ask_user", {"question": "q?"}))
    r = await client.post("/api/ai/design", json={"lab_id": lab["id"], "prompt": "hello"})
    assert r.status_code == 200 and KEY not in r.text
    for path in ("/api/ai/link", "/api/secrets", "/api/users/audit?limit=50"):
        assert KEY not in (await client.get(path)).text
    assert KEY not in caplog.text
    assert providers.mask(f"x {KEY} y", KEY) == "x **** y"
    assert "sk-proj-abcdefghij123" not in providers.mask("Bearer sk-proj-abcdefghij123")


async def test_shared_default_gating(client, fake):
    lab = await _lab(client)
    r = await client.post("/api/ai/design", json={"lab_id": lab["id"], "prompt": "hi"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "no_ai_link"
    r = await client.put("/api/admin/ai", json={"ai_default_provider": "openai", "ai_default_base_url": "http://vllm:8000/v1",
                                                "ai_default_model": "qwen2.5", "ai_default_api_key": "shared-secret-key-1"})
    assert r.status_code == 200, r.text
    v = r.json()
    assert v["has_ai_default_api_key"] is True and v["ai_shared_allowed"] is False and "shared-secret-key-1" not in r.text
    # configured but not allowed → still no model for users without a link
    assert (await client.get("/api/ai/link")).json()["effective"] is None
    assert (await client.post("/api/ai/design", json={"lab_id": lab["id"], "prompt": "hi"})).status_code == 409
    await client.put("/api/admin/ai", json={"ai_shared_allowed": True})
    v = (await client.get("/api/ai/link")).json()
    assert v["effective"] == "shared" and v["shared"] == {"available": True, "provider": "openai", "model": "qwen2.5"}
    f = fake(openai_tool("ask_user", {"question": "q?"}))
    out = (await client.post("/api/ai/design", json={"lab_id": lab["id"], "prompt": "hi"})).json()
    assert out["source"] == "shared" and out["kind"] == "question"
    assert f.calls[0]["url"] == "http://vllm:8000/v1/chat/completions"
    assert f.calls[0]["headers"]["authorization"] == "Bearer shared-secret-key-1"
    # an own link wins over the shared default
    await _link(client)
    f = fake(anthropic_tool("ask_user", {"question": "q?"}))
    assert (await client.post("/api/ai/design", json={"lab_id": lab["id"], "prompt": "hi"})).json()["source"] == "user"
    assert f.calls[0]["url"].startswith("https://api.anthropic.com")
    assert (await client.put("/api/admin/ai", json={"ai_default_provider": "gemini"})).status_code == 422
    assert (await client.put("/api/admin/ai", json={"bogus": 1})).status_code == 422


async def test_roles_and_rate_limit(client, fake, monkeypatch):
    from tests.test_auth import GOOD, as_user, mk_user

    lab = await _lab(client)
    viewer, _ = await mk_user(client, "viewer")
    async with as_user(viewer, GOOD) as c:
        assert (await c.get("/api/ai/link")).status_code == 200
        assert (await c.post("/api/ai/design/apply", json={"lab_id": lab["id"], "knobs": {"spine_count": 3}})).status_code == 403
        assert (await c.post("/api/ai/design", json={"lab_id": lab["id"], "prompt": "x"})).status_code == 403
        assert (await c.get("/api/admin/ai")).status_code == 403
    operator, _ = await mk_user(client, "operator")
    async with as_user(operator, GOOD) as c:
        assert (await c.get("/api/admin/ai")).status_code == 403
        assert (await c.put("/api/ai/link", json={"provider": "anthropic", "model": "m", "api_key": "op-key-123456"})).status_code == 200
        fake(anthropic_tool("ask_user", {"question": "q?"}))
        assert (await c.post("/api/ai/design", json={"lab_id": lab["id"], "prompt": "x"})).status_code == 200
        monkeypatch.setattr(settings, "ai_rate_limit", 1)
        assert (await c.post("/api/ai/design", json={"lab_id": lab["id"], "prompt": "x"})).status_code == 429


async def test_lenient_text_json_from_local_models():
    name, args = designer.pick_call({"text": 'Sure! {"knobs": {"spine_count": 3}, "summary": "s"}', "tool_calls": []})
    assert name == "propose_lab_changes" and args["knobs"] == {"spine_count": 3}
    assert designer.pick_call({"text": "just prose", "tool_calls": []}) == (None, {})


async def test_design_from_scratch_picks_template(client, fake):
    """0.10.0: no lab_id/template → a choose_template round, then the template-mode design; response carries the choice."""
    await _link(client)
    f = fake(anthropic_tool("choose_template", {"template": "single-dc-l3ls", "reason": "generic EVPN spine/leaf", "lab_name": "ACME DC Lab!"}),
             anthropic_tool("propose_lab_changes", {"summary": "3 leaf pairs", "knobs": {"leaf_pairs": 3}}))
    r = await client.post("/api/ai/design", json={"prompt": "an EVPN fabric with 3 leaf pairs for ACME"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["template"] == "single-dc-l3ls" and out["template_reason"] == "generic EVPN spine/leaf" and out["lab_name"] == "acme-dc-lab"
    assert out["kind"] == "proposal" and out["knobs"] == {"leaf_pairs": 3} and out["dry_run"]["valid"]
    first = f.calls[0]["body"]
    assert first["tool_choice"] == {"type": "tool", "name": "choose_template"}
    names = first["tools"][0]["input_schema"]["properties"]["template"]["enum"]
    assert "single-dc-l3ls" in names and "dual-dc-l3ls" in names and "custom" not in names
    assert "Available templates" in first["system"]

    # model does not call the tool → safe default, no error
    f = fake((200, {"id": "m", "type": "message", "role": "assistant", "content": [{"type": "text", "text": "hmm"}]}),
             anthropic_tool("propose_lab_changes", {"summary": "defaults", "knobs": {}}))
    out = (await client.post("/api/ai/design", json={"prompt": "something"})).json()
    assert out["template"] == "single-dc-l3ls" and out["lab_name"] == "ai-lab"
