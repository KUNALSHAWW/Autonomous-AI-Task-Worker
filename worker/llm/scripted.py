"""Deterministic stand-in model for tests and offline replays.

A script is a callable `(purpose, messages, tools) -> reply` where reply is a
dict (tool call or JSON object), a string, or an LLMReply. This lets tests
drive the real agent loop, real browser and real sandbox while pinning the
model's decisions.
"""
from __future__ import annotations

import json
from typing import Callable

from .base import LLMReply, ToolCall


class ScriptedLLM:
    name = "scripted"

    def __init__(self, script: Callable):
        self.script = script
        self.calls: list[dict] = []

    async def chat(self, messages, tools=None, json_mode=False, temperature=0.1, max_tokens=1200,
                   purpose: str = "") -> LLMReply:
        self.calls.append({"purpose": purpose, "messages": messages})
        out = self.script(purpose, messages, tools)
        if isinstance(out, LLMReply):
            return out
        if isinstance(out, dict) and "tool" in out and tools:
            return LLMReply(text="", tool_call=ToolCall(out["tool"], out.get("args", {})))
        if isinstance(out, (dict, list)):
            return LLMReply(text=json.dumps(out))
        return LLMReply(text=str(out))
