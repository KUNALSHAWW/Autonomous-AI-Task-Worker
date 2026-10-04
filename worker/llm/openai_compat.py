"""Client for any OpenAI-compatible chat completions endpoint.

Covers Gemini (OpenAI-compatible endpoint), Groq, OpenRouter, Hugging Face
router, Cerebras, OpenAI, vLLM and Ollama with one small httpx client.
No SDK dependency, so the request on the wire is fully visible.
"""
from __future__ import annotations

import asyncio
import json
import random

import httpx

from .base import LLMError, LLMReply, ToolCall, ToolSpec, tool_call_from_text

RETRYABLE = {408, 409, 425, 429, 500, 502, 503, 504, 529}


class OpenAICompatLLM:
    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 90.0,
                 max_retries: int = 5, native_tools: bool = True, extra_body: dict | None = None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.name = f"{model} @ {self.base_url}"
        self.native_tools = native_tools
        self.max_retries = max_retries
        self.extra_body = extra_body or {}
        self._client = httpx.AsyncClient(timeout=timeout)

    async def chat(self, messages, tools: list[ToolSpec] | None = None, json_mode: bool = False,
                   temperature: float = 0.1, max_tokens: int = 1200, purpose: str = "") -> LLMReply:
        body: dict = {"model": self.model, "messages": messages, "temperature": temperature,
                      "max_tokens": max_tokens, **self.extra_body}
        if tools and self.native_tools:
            body["tools"] = [t.openai() for t in tools]
            body["tool_choice"] = "auto"
        elif json_mode:
            body["response_format"] = {"type": "json_object"}
        data = await self._post(body)
        try:
            msg = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"unexpected response shape: {str(data)[:300]}") from e
        text = msg.get("content") or ""
        if isinstance(text, list):  # some providers return content parts
            text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
        call = None
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            raw_args = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
            except json.JSONDecodeError:
                args = {"__malformed__": raw_args}
            call = ToolCall(fn.get("name", ""), args, raw_args if isinstance(raw_args, str) else json.dumps(raw_args))
            break  # one action per step by design
        if call is None and tools:
            call = tool_call_from_text(text, {t.name for t in tools})
        return LLMReply(text=text, tool_call=call, usage=data.get("usage") or {})

    async def _post(self, body: dict) -> dict:
        url = f"{self.base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        last = ""
        for attempt in range(self.max_retries + 1):
            try:
                resp = await self._client.post(url, json=body, headers=headers)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last = f"{type(e).__name__}: {e}"
            else:
                if resp.status_code == 200:
                    return resp.json()
                last = f"HTTP {resp.status_code}: {resp.text[:400]}"
                # Some providers reject the call when the model emits a malformed tool call.
                if resp.status_code == 400 and "tool" in resp.text.lower() and "tools" in body:
                    body = {k: v for k, v in body.items() if k not in ("tools", "tool_choice")}
                    body["messages"] = body["messages"] + [{
                        "role": "user",
                        "content": "Your previous tool call was malformed. Reply with ONLY a JSON object "
                                   '{"tool": "<name>", "args": {...}} for your next action.'}]
                    continue
                if resp.status_code == 400 and "response_format" in body:
                    body = {k: v for k, v in body.items() if k != "response_format"}
                    continue  # provider does not support JSON mode; the prompt already asks for JSON
                if resp.status_code not in RETRYABLE:
                    raise LLMError(last)
                ra = resp.headers.get("retry-after")
                if ra and ra.replace(".", "", 1).isdigit():
                    await asyncio.sleep(min(float(ra), 30))
                    continue
            await asyncio.sleep(min(2 ** attempt + random.random(), 30))
        raise LLMError(f"gave up after {self.max_retries + 1} attempts: {last}")
