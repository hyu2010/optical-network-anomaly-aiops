"""LLM provider over an OpenAI-compatible endpoint (doc 05 §6).

DeepSeek, a local vLLM and Ollama all speak the OpenAI chat API, so one
client (`langchain_openai.ChatOpenAI` with `base_url`) serves every profile.
Messages are plain dicts:
    {"role": "system"|"user"|"assistant"|"tool", "content": str,
     "tool_calls": [{"id", "name", "args"}], "tool_call_id": str}
The API key is held only inside the client object and never logged.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from .cache import CassetteMiss, LLMCache, cache_key

log = logging.getLogger("diag_agent.llm")


class LLMUnavailable(RuntimeError):
    pass


def _to_lc(messages: list[dict]):
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
    out = []
    for m in messages:
        r = m["role"]
        if r == "system":
            out.append(SystemMessage(content=m["content"]))
        elif r == "user":
            out.append(HumanMessage(content=m["content"]))
        elif r == "assistant":
            tcs = [{"id": t["id"], "name": t["name"], "args": t.get("args", {}), "type": "tool_call"}
                   for t in m.get("tool_calls") or []]
            out.append(AIMessage(content=m.get("content") or "", tool_calls=tcs))
        elif r == "tool":
            out.append(ToolMessage(content=m["content"], tool_call_id=m["tool_call_id"]))
    return out


class OpenAICompatProvider:
    """Concrete `LLMProvider`."""

    def __init__(self, settings, cache: LLMCache | None = None):
        self.settings = settings
        self.cache = cache
        self._clients: dict[str, Any] = {}
        self.calls = 0
        self.tokens = 0
        prof = settings.profile_for("proposer")
        self.profile_name = prof.name
        self.model = prof.model

    def available(self, role: str) -> bool:
        prof = self.settings.profile_for(role)
        if prof.enabled and prof.api_key:
            return True
        return bool(self.cache is not None and self.cache.mode == "read_only" and prof.name != "none" and prof.model)

    def _client(self, role: str):
        if role in self._clients:
            return self._clients[role]
        prof = self.settings.profile_for(role)
        if not prof.enabled:
            raise LLMUnavailable(f"LLM profile {prof.name!r} is not configured for role {role}")
        from langchain_openai import ChatOpenAI
        params = self.settings.role_params(role)
        kw: dict[str, Any] = {"model": prof.model, "base_url": prof.base_url, "api_key": prof.api_key or "none",
                              "temperature": params.get("temperature", 0.2), "max_tokens": params.get("max_tokens", 1500),
                              "timeout": prof.timeout_s, "max_retries": prof.max_retries}
        if prof.extra_body:
            kw["extra_body"] = prof.extra_body
        self._clients[role] = ChatOpenAI(**kw)
        return self._clients[role]

    def chat(self, role: str, messages: list[dict], *, tools: list[dict] | None = None,
             json_mode: bool = False) -> dict:
        prof = self.settings.profile_for(role)
        params = self.settings.role_params(role)
        request = {"model": prof.model, "role": role, "messages": messages, "tools": tools or [],
                   "json_mode": json_mode, "temperature": params.get("temperature"),
                   "max_tokens": params.get("max_tokens")}
        key = cache_key(request)
        if self.cache is not None:
            hit = self.cache.get(key)
            if hit is not None:
                return dict(hit, cached=True)
            if self.cache.mode == "read_only":
                raise CassetteMiss(f"cassette miss for role {role}")
        client = self._client(role)
        if tools:
            client = client.bind_tools(tools)
        if json_mode and not tools:
            if prof.structured_output in ("json_mode", "json_schema"):
                client = client.bind(response_format={"type": "json_object"})
        t0 = time.time()
        try:
            msg = client.invoke(_to_lc(messages))
        except Exception as e:  # network / auth / rate limit: the line abstains
            raise LLMUnavailable(f"{type(e).__name__}: {str(e)[:200]}") from None
        usage = getattr(msg, "usage_metadata", None) or {}
        out = {"content": msg.content if isinstance(msg.content, str) else json.dumps(msg.content),
               "tool_calls": [{"id": tc.get("id") or f"call_{i}", "name": tc["name"], "args": tc.get("args") or {}}
                              for i, tc in enumerate(getattr(msg, "tool_calls", None) or [])],
               "usage": {"input": int(usage.get("input_tokens", 0)), "output": int(usage.get("output_tokens", 0))},
               "latency_s": round(time.time() - t0, 3), "model": prof.model}
        self.calls += 1
        self.tokens += out["usage"]["input"] + out["usage"]["output"]
        log.info("llm %s %s: %d tool calls, %d tokens, %.1fs", role, prof.model, len(out["tool_calls"]),
                 out["usage"]["input"] + out["usage"]["output"], out["latency_s"])
        if self.cache is not None:
            self.cache.put(key, prof.model, role, request, out)
        return dict(out, cached=False)


class NoLLM:
    profile_name = "none"
    model = ""
    calls = 0
    tokens = 0

    def available(self, role: str) -> bool:
        return False

    def chat(self, role, messages, *, tools=None, json_mode=False) -> dict:
        raise LLMUnavailable("LLM disabled (profile none)")


def parse_json(text: str) -> Any:
    """Parse a JSON object from model output, tolerating fences and prose around it."""
    s = (text or "").strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    start, end = s.find("{"), s.rfind("}")
    if start >= 0 and end > start:
        return json.loads(s[start:end + 1])
    raise ValueError("no JSON object in model output")
