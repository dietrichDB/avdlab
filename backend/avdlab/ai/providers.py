"""LLM providers for the AI lab designer — plain httpx, no SDKs.

    make_provider(provider, base_url, model, api_key) → Provider
    await p.tool_call(system, messages, tools, tool_choice) → {"text": str, "tool_calls": [{"name", "arguments"}]}
    await p.probe() → {"ok": True, "models": [...]}   (raises ProviderError)

`tools` use a provider-neutral shape `[{name, description, input_schema}]`; `messages` are `[{role: user|assistant,
content: str}]`; `tool_choice` is "auto" | "any" (must call a tool) | a tool name.

* `anthropic` — Messages API `POST {base}/v1/messages` (`x-api-key`, `anthropic-version: 2023-06-01`), base default
  `https://api.anthropic.com`.
* `openai` — Chat Completions `POST {base}/chat/completions` with `tools`/`tool_choice`, base default
  `https://api.openai.com/v1`. Works for OpenAI, Ollama (`http://host:11434/v1`), LM Studio (`http://host:1234/v1`),
  vLLM and Azure OpenAI (`https://<res>.openai.azure.com/openai/deployments/<deployment>` → `api-key` header and an
  `api-version` query parameter).

API keys are only ever put in request headers. Every error message is passed through `mask()` so a key echoed by a
provider (or a proxy) never reaches a log line or an HTTP response.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from ..core.config import settings

log = logging.getLogger("avdlab.ai")

PROVIDERS = ("anthropic", "openai")
ANTHROPIC_URL = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"
OPENAI_URL = "https://api.openai.com/v1"
AZURE_API_VERSION = "2024-10-21"
MAX_TOKENS = 4096


class ProviderError(Exception):
    """Provider/network failure; `str()` is safe to show (keys masked)."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def mask(text: str, *secrets: str | None) -> str:
    """Replace every secret (and anything that looks like an API key) in `text`."""
    out = str(text)
    for s in secrets:
        if s and len(s) >= 4:
            out = out.replace(s, "****")
    # belt and braces: well-known key shapes (sk-…, sk-ant-…, Bearer tokens)
    out = re.sub(r"sk-[A-Za-z0-9_\-]{8,}", "sk-****", out)
    out = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{8,}", r"\1****", out)
    return out


async def http_request(method: str, url: str, headers: dict[str, str], body: dict[str, Any] | None = None,
                       timeout: float | None = None) -> tuple[int, Any]:
    """The only network call of this module (tests monkeypatch it). Returns (status, parsed JSON or text)."""
    async with httpx.AsyncClient(timeout=timeout or settings.ai_timeout) as client:
        r = await client.request(method, url, headers=headers, json=body)
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, r.text


def _error_text(data: Any) -> str:
    if isinstance(data, dict):
        err = data.get("error", data)
        if isinstance(err, dict):
            return str(err.get("message") or err.get("type") or json.dumps(err)[:300])
        return str(err)[:300]
    return str(data)[:300]


class Provider:
    name = "base"

    def __init__(self, base_url: str, model: str, api_key: str | None) -> None:
        self.base_url = (base_url or "").strip().rstrip("/")
        self.model = model
        self.api_key = api_key or ""

    def headers(self) -> dict[str, str]:
        raise NotImplementedError

    async def _call(self, method: str, url: str, body: dict[str, Any] | None = None) -> Any:
        try:
            status, data = await http_request(method, url, self.headers(), body)
        except httpx.TimeoutException:
            raise ProviderError(f"{self.name}: timeout after {settings.ai_timeout:.0f} s ({_host(url)})") from None
        except httpx.HTTPError as exc:
            raise ProviderError(mask(f"{self.name}: cannot reach {_host(url)}: {exc}", self.api_key)) from None
        if status >= 400:
            log.info("ai provider %s returned HTTP %s", self.name, status)
            raise ProviderError(mask(f"{self.name} HTTP {status}: {_error_text(data)}", self.api_key), status)
        return data

    async def tool_call(self, system: str, messages: list[dict[str, str]], tools: list[dict[str, Any]],
                        tool_choice: str = "auto") -> dict[str, Any]:
        raise NotImplementedError

    async def probe(self) -> dict[str, Any]:
        raise NotImplementedError


def _host(url: str) -> str:
    return urlsplit(url).netloc or url


class AnthropicProvider(Provider):
    name = "anthropic"

    def __init__(self, base_url: str, model: str, api_key: str | None) -> None:
        super().__init__(base_url or ANTHROPIC_URL, model, api_key)
        if self.base_url.endswith("/v1"):
            self.base_url = self.base_url[:-3]

    def headers(self) -> dict[str, str]:
        return {"x-api-key": self.api_key, "anthropic-version": ANTHROPIC_VERSION, "content-type": "application/json"}

    def request_body(self, system: str, messages: list[dict[str, str]], tools: list[dict[str, Any]],
                     tool_choice: str) -> dict[str, Any]:
        if tool_choice == "any":
            choice: dict[str, Any] = {"type": "any"}
        elif tool_choice == "auto":
            choice = {"type": "auto"}
        else:
            choice = {"type": "tool", "name": tool_choice}
        return {
            "model": self.model, "max_tokens": MAX_TOKENS, "system": system,
            "messages": [{"role": m["role"], "content": m["content"]} for m in messages],
            "tools": [{"name": t["name"], "description": t["description"], "input_schema": t["input_schema"]} for t in tools],
            "tool_choice": choice,
        }

    async def tool_call(self, system: str, messages: list[dict[str, str]], tools: list[dict[str, Any]],
                        tool_choice: str = "auto") -> dict[str, Any]:
        data = await self._call("POST", f"{self.base_url}/v1/messages", self.request_body(system, messages, tools, tool_choice))
        text: list[str] = []
        calls: list[dict[str, Any]] = []
        for block in (data or {}).get("content") or [] if isinstance(data, dict) else []:
            if block.get("type") == "text":
                text.append(str(block.get("text") or ""))
            elif block.get("type") == "tool_use":
                calls.append({"name": block.get("name"), "arguments": block.get("input") or {}})
        return {"text": "\n".join(t for t in text if t), "tool_calls": calls}

    async def probe(self) -> dict[str, Any]:
        data = await self._call("GET", f"{self.base_url}/v1/models")
        models = [m.get("id") for m in (data or {}).get("data") or [] if isinstance(m, dict)] if isinstance(data, dict) else []
        return {"ok": True, "models": [m for m in models if m]}


class OpenAIProvider(Provider):
    """OpenAI Chat Completions and everything that speaks it (Ollama, LM Studio, vLLM, Azure OpenAI)."""

    name = "openai"

    def __init__(self, base_url: str, model: str, api_key: str | None) -> None:
        super().__init__(base_url or OPENAI_URL, model, api_key)
        parts = urlsplit(self.base_url)
        self.azure = parts.netloc.endswith(".openai.azure.com") or parts.netloc.endswith(".azure-api.net")
        path = parts.path.rstrip("/")
        if not path and not self.azure:
            path = "/v1"  # http://host:11434 → http://host:11434/v1
        query = dict(parse_qsl(parts.query))
        if self.azure:
            query.setdefault("api-version", AZURE_API_VERSION)
        self._parts = (parts.scheme, parts.netloc, path, urlencode(query))

    def url(self, suffix: str) -> str:
        scheme, netloc, path, query = self._parts
        return urlunsplit((scheme, netloc, f"{path}/{suffix}", query, ""))

    def headers(self) -> dict[str, str]:
        h = {"content-type": "application/json"}
        if self.api_key:
            if self.azure:
                h["api-key"] = self.api_key
            else:
                h["authorization"] = f"Bearer {self.api_key}"
        return h

    def request_body(self, system: str, messages: list[dict[str, str]], tools: list[dict[str, Any]],
                     tool_choice: str) -> dict[str, Any]:
        if tool_choice == "any":
            choice: Any = "required"
        elif tool_choice == "auto":
            choice = "auto"
        else:
            choice = {"type": "function", "function": {"name": tool_choice}}
        return {
            "model": self.model, "temperature": 0.2, "max_tokens": MAX_TOKENS,
            "messages": [{"role": "system", "content": system}]
                        + [{"role": m["role"], "content": m["content"]} for m in messages],
            "tools": [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                        "parameters": t["input_schema"]}} for t in tools],
            "tool_choice": choice,
        }

    async def tool_call(self, system: str, messages: list[dict[str, str]], tools: list[dict[str, Any]],
                        tool_choice: str = "auto") -> dict[str, Any]:
        data = await self._call("POST", self.url("chat/completions"), self.request_body(system, messages, tools, tool_choice))
        choices = (data or {}).get("choices") if isinstance(data, dict) else None
        msg = (choices[0] or {}).get("message") or {} if choices else {}
        calls: list[dict[str, Any]] = []
        for tc in msg.get("tool_calls") or []:
            fn = (tc or {}).get("function") or {}
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args or "{}")
                except ValueError:
                    args = {"_unparsed": args[:2000]}
            calls.append({"name": fn.get("name"), "arguments": args if isinstance(args, dict) else {}})
        text = msg.get("content") or ""
        return {"text": text if isinstance(text, str) else json.dumps(text), "tool_calls": calls}

    async def probe(self) -> dict[str, Any]:
        if self.azure:  # deployments have no /models listing on the data plane: a one-token completion instead
            await self._call("POST", self.url("chat/completions"),
                             {"messages": [{"role": "user", "content": "ping"}], "max_tokens": 1})
            return {"ok": True, "models": [self.model]}
        data = await self._call("GET", self.url("models"))
        models = [m.get("id") for m in (data or {}).get("data") or [] if isinstance(m, dict)] if isinstance(data, dict) else []
        return {"ok": True, "models": [m for m in models if m]}


def make_provider(provider: str, base_url: str, model: str, api_key: str | None) -> Provider:
    if provider == "anthropic":
        return AnthropicProvider(base_url, model, api_key)
    if provider == "openai":
        return OpenAIProvider(base_url, model, api_key)
    raise ProviderError(f"unknown provider {provider!r}; supported: {list(PROVIDERS)}")
