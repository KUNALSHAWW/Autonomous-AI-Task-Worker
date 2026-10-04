"""Native Anthropic Messages API client (no SDK, plain httpx)."""
from __future__ import annotations

import asyncio
import random

import httpx

from .base import LLMError, LLMReply, ToolCall, ToolSpec, tool_call_from_text


class AnthropicLLM:
    def __init__(self, api_key: str, model: str, base_url: str = "https://api.anthropic.com", timeout: float = 120.0):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.name = f"{model} @ anthropic"
        self._client = httpx.AsyncClient(timeout=timeout)

    async def chat(self, messages, tools: list[ToolSpec] | None = None, json_mode: bool = False,
                   temperature: float = 0.1, max_tokens: int = 1200, purpose: str = "") -> LLMReply:
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        convo = [{"role": m["role"], "content": m["content"]} for m in messages if m["role"] != "system"]
        body: dict = {"model": self.model, "max_tokens": max_tokens, "temperature": temperature,
                      "system": system, "messages": convo}
        if tools:
            body["tools"] = [{"name": t.name, "description": t.description, "input_schema": t.parameters}
                             for t in tools]
            body["tool_choice"] = {"type": "any"}
        headers = {"x-api-key": self.api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
        last = ""
        for attempt in range(6):
            try:
                resp = await self._client.post(f"{self.base_url}/v1/messages", json=body, headers=headers)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last = str(e)
            else:
                if resp.status_code == 200:
                    data = resp.json()
                    text, call = "", None
                    for block in data.get("content", []):
                        if block.get("type") == "text":
                            text += block.get("text", "")
                        elif block.get("type") == "tool_use" and call is None:
                            call = ToolCall(block["name"], block.get("input") or {})
                    if call is None and tools:
                        call = tool_call_from_text(text, {t.name for t in tools})
                    return LLMReply(text=text, tool_call=call, usage=data.get("usage") or {})
                last = f"HTTP {resp.status_code}: {resp.text[:400]}"
                if resp.status_code not in (408, 429, 500, 502, 503, 504, 529):
                    raise LLMError(last)
            await asyncio.sleep(min(2 ** attempt + random.random(), 30))
        raise LLMError(f"gave up: {last}")
