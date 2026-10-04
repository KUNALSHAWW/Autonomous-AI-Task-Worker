"""LangChain chat models (ChatOllama by default) behind the worker's LLM interface.

The act loop talks to `chat(...)`; the deep agent used for verification needs the
raw LangChain `BaseChatModel`, which `make_chat_model` also provides.
"""
from __future__ import annotations

import asyncio
import json
import random

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from .base import LLMError, LLMReply, ToolCall, ToolSpec, tool_call_from_text, tools_prompt

OLLAMA_CLOUD = "https://ollama.com"
OLLAMA_LOCAL = "http://localhost:11434"


def think_setting(model: str):
    """How much the model should think before each step (OLLAMA_THINK).

    Every agent step is a model call, so thinking time is paid dozens of times per task.
    Default is off. gpt-oss cannot switch thinking off, so "off" maps to its lowest level.
    OLLAMA_THINK=default keeps the model's own behaviour; low/medium/high/true also work.
    """
    import os
    v = os.environ.get("OLLAMA_THINK", "false").strip().lower()
    if v in ("", "default", "none"):
        return None
    if v in ("low", "medium", "high"):
        return v if "gpt-oss" in model else True
    on = v in ("1", "true", "yes", "on")
    if "gpt-oss" in model:
        return "medium" if on else "low"
    return on


def make_ollama(model: str, api_key: str = "", base_url: str = "", temperature: float = 0.1, num_ctx: int | None = None):
    """ChatOllama for Ollama Cloud (with an API key) or a local Ollama server."""
    from langchain_ollama import ChatOllama

    base = base_url or (OLLAMA_CLOUD if api_key else OLLAMA_LOCAL)
    kwargs: dict = {"model": model, "base_url": base, "temperature": temperature}
    think = think_setting(model)
    if think is not None:
        kwargs["reasoning"] = think
    if num_ctx:
        kwargs["num_ctx"] = num_ctx
    if api_key:
        kwargs["client_kwargs"] = {"headers": {"Authorization": f"Bearer {api_key}"}, "timeout": 180}
    return ChatOllama(**kwargs)


def _to_lc(messages: list[dict]):
    out = []
    for m in messages:
        role, content = m["role"], m["content"]
        if role == "system":
            out.append(SystemMessage(content))
        elif role == "assistant":
            out.append(AIMessage(content))
        else:
            out.append(HumanMessage(content))
    return out


class LangChainLLM:
    """Adapter: any LangChain BaseChatModel -> the worker's `chat` interface."""

    def __init__(self, model, name: str, max_retries: int = 4, native_tools: bool = True):
        self.native_tools = native_tools
        self.model = model
        self.name = name
        self.max_retries = max_retries

    async def chat(self, messages, tools: list[ToolSpec] | None = None, json_mode: bool = False,
                   temperature: float = 0.1, max_tokens: int = 1200, purpose: str = "") -> LLMReply:
        runnable = self.model
        if tools and self.native_tools:
            runnable = self.model.bind_tools([t.openai() for t in tools])
        elif tools:
            messages = [{"role": "system", "content": tools_prompt(tools)}] + list(messages)
        elif json_mode:
            runnable = self.model.bind(format="json")
        last = ""
        for attempt in range(self.max_retries + 1):
            try:
                msg = await runnable.ainvoke(_to_lc(messages))
                break
            except Exception as e:  # provider errors come in many shapes
                last = f"{type(e).__name__}: {str(e)[:300]}"
                if "think" in last.lower() and getattr(self.model, "reasoning", None) is not None:
                    # model does not support the thinking switch: use its default from now on
                    self.model = self.model.model_copy(update={"reasoning": None})
                    return await self.chat(messages, tools, json_mode, temperature, max_tokens, purpose)
                if tools and self.native_tools and "support" in last.lower() and "tool" in last.lower():
                    # e.g. "model does not support tools": fall back to text tool calls for good
                    self.native_tools = False
                    return await self.chat(messages, tools, json_mode, temperature, max_tokens, purpose)
                if any(s in last.lower() for s in ("unauthorized", "401", "403", "not found", "404")):
                    raise LLMError(last) from e
                await asyncio.sleep(min(2 ** attempt + random.random(), 30))
        else:
            raise LLMError(f"gave up after {self.max_retries + 1} attempts: {last}")
        text = msg.content if isinstance(msg.content, str) else "".join(
            p.get("text", "") for p in msg.content if isinstance(p, dict))
        call = None
        for tc in getattr(msg, "tool_calls", None) or []:
            args = tc.get("args") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {"__malformed__": args}
            call = ToolCall(tc.get("name", ""), args)
            break
        if call is None and tools:
            call = tool_call_from_text(text, {t.name for t in tools})
        um = getattr(msg, "usage_metadata", None) or {}
        usage = {"prompt_tokens": um.get("input_tokens", 0), "completion_tokens": um.get("output_tokens", 0)}
        return LLMReply(text=text, tool_call=call, usage=usage)
