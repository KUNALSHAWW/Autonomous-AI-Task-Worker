"""Provider-agnostic LLM interface.

The agent only ever needs two things from a model:
  * `decide(...)`: pick exactly one tool call given a prompt and a tool list
  * `complete_json(...)`: return a JSON object for a structured prompt

Everything provider-specific (wire format, retries, tool-call repair) stays
behind this interface so the agent code never changes when the model does.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol


class LLMError(Exception):
    """Raised when the provider fails in a way retries could not fix."""


@dataclass
class ToolCall:
    name: str
    args: dict
    raw: str = ""


@dataclass
class LLMReply:
    text: str = ""
    tool_call: ToolCall | None = None
    usage: dict = field(default_factory=dict)


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict  # JSON schema (object)

    def openai(self) -> dict:
        return {"type": "function", "function": {"name": self.name, "description": self.description,
                                                 "parameters": self.parameters}}


class LLM(Protocol):
    name: str

    async def chat(self, messages: list[dict], tools: list[ToolSpec] | None = None,
                   json_mode: bool = False, temperature: float = 0.1, max_tokens: int = 1200,
                   purpose: str = "") -> LLMReply: ...


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def extract_json(text: str) -> Any:
    """Best-effort JSON extraction from model text (fenced, bare, or embedded)."""
    if not text:
        raise ValueError("empty response")
    candidates = [m.group(1) for m in _FENCE.finditer(text)] + [text]
    for c in candidates:
        c = c.strip()
        try:
            return json.loads(c)
        except json.JSONDecodeError:
            pass
        # Find the first balanced {...} block.
        start = c.find("{")
        while start != -1:
            depth, in_str, esc = 0, False, False
            for i in range(start, len(c)):
                ch = c[i]
                if in_str:
                    if esc:
                        esc = False
                    elif ch == "\\":
                        esc = True
                    elif ch == '"':
                        in_str = False
                elif ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(c[start:i + 1])
                        except json.JSONDecodeError:
                            break
            start = c.find("{", start + 1)
    raise ValueError("no JSON object found in model output")


def tool_call_from_text(text: str, tool_names: set[str]) -> ToolCall | None:
    """Recover a tool call when a model answers in text instead of native tool calling."""
    try:
        obj = extract_json(text)
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    name = obj.get("tool") or obj.get("name") or obj.get("action")
    args = obj.get("args") or obj.get("arguments") or obj.get("parameters") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = {}
    if name in tool_names and isinstance(args, dict):
        return ToolCall(name, args, text)
    return None


def validate_args(schema: dict, args: dict) -> str | None:
    """Tiny JSON-schema subset validator. Returns an error message or None."""
    if not isinstance(args, dict):
        return "arguments must be a JSON object"
    props = schema.get("properties", {})
    for req in schema.get("required", []):
        if req not in args or args[req] in (None, ""):
            return f"missing required argument '{req}'"
    for k, v in args.items():
        if k not in props:
            continue
        t = props[k].get("type")
        ok = {
            "string": isinstance(v, str), "integer": isinstance(v, int) and not isinstance(v, bool),
            "number": isinstance(v, (int, float)) and not isinstance(v, bool), "boolean": isinstance(v, bool),
            "object": isinstance(v, dict), "array": isinstance(v, list),
        }.get(t, True) if t else True
        if not ok:
            return f"argument '{k}' must be of type {t}"
        enum = props[k].get("enum")
        if enum and v not in enum:
            return f"argument '{k}' must be one of {enum}"
    return None
