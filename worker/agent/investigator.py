"""Read-only verification investigator built with LangChain Deep Agents.

Where it is used: only for success criteria that cannot be turned into a simple
code-run probe (for example "did the worker give the right answer to a
question?"). That is an open-ended investigation: look around several systems,
keep notes, maybe split the work. Deep Agents gives that for free (a todo
planner, a scratch file system for notes, sub-agents), so the investigation can
go deeper than a fixed loop without us writing the plumbing.

Where it is NOT used: the main worker loop. That loop changes real systems, so
it stays a tightly controlled LangGraph graph with code guardrails around every
step.

Guarantees kept regardless of how the deep agent behaves:
  * it only gets read tools, and the gate is in read-only mode anyway
  * a "pass" only counts if its evidence quote appears verbatim in something it
    actually observed during this investigation
"""
from __future__ import annotations

import json
import re

from langchain_core.tools import StructuredTool

from ..tools.toolbox import READ_TOOLS, TOOL_SPECS
from . import prompts
from .state import StepRecord

INVESTIGATOR_PROMPT = """You are an independent, skeptical verifier with READ-ONLY access to company systems.
Your job: decide whether ONE success criterion is really met, by looking at the systems yourself.
The worker's own claims are not evidence.

{environment}

Rules:
- Use the read tools to look. Any attempt to change data is blocked.
- Keep it short: plan with the todo tool only if you need more than 3 lookups.
- When you know the answer, call `submit_verdict` exactly once with passed=true/false and an
  `evidence` quote copied EXACTLY from a page, file or API response you observed.
- If you cannot find evidence, passed=false."""


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", str(s)).strip().lower()


def _schema(spec_params: dict) -> dict:
    return {"type": "object", "title": "args", "properties": spec_params.get("properties", {}),
            "required": spec_params.get("required", [])}


async def investigate(agent, criterion: dict, instructions: str, max_steps: int = 14) -> dict:
    """Run a deep agent on one criterion. Returns {type, status, detail}."""
    from deepagents import create_deep_agent  # imported lazily: heavy, only needed here

    s = agent.state
    start_obs = len(s.observations)
    verdict: dict = {}

    def make_tool(name: str):
        spec = TOOL_SPECS[name]

        async def run(**kwargs):
            n = len(s.steps) + 1
            res = await agent.tools.run(name, kwargs, n)
            s.steps.append(StepRecord(n, name, {k: v for k, v in kwargs.items() if k != "reason"},
                                      str(kwargs.get("reason", "")), res.ok, res.error_kind,
                                      res.output.split("\n")[0][:200], "", res.screenshot, phase="verify"))
            await agent.emit("step", {**s.steps[-1].__dict__, "screenshot": None, "observation": res.output[:2000]})
            return res.output

        return StructuredTool(name=name, description=spec.description, args_schema=_schema(spec.parameters),
                              coroutine=run)

    async def submit_verdict(passed: bool, evidence: str, explanation: str = "") -> str:
        if not verdict:
            verdict.update({"passed": bool(passed), "evidence": str(evidence), "explanation": str(explanation)})
        return "Verdict recorded. Stop now."

    verdict_tool = StructuredTool(
        name="submit_verdict", description="Record the final verdict for the criterion.", coroutine=submit_verdict,
        args_schema={"type": "object", "title": "verdict", "properties": {
            "passed": {"type": "boolean"}, "evidence": {"type": "string"}, "explanation": {"type": "string"}},
            "required": ["passed", "evidence"]})

    deep = create_deep_agent(
        model=agent.chat_model,
        tools=[make_tool(n) for n in READ_TOOLS] + [verdict_tool],
        system_prompt=INVESTIGATOR_PROMPT.format(environment=agent.env_text),
        name="verification-investigator",
    )
    task = prompts.JUDGE_TURN.format(
        criterion=criterion["text"], instructions=instructions,
        facts=json.dumps({k: f.value for k, f in s.facts.items()}),
        claimed=json.dumps(s.claimed or {})[:800], history="(none yet)", observation="(start by looking)")
    await agent.emit("note", {"message": f"Deep-agent investigator checking: {criterion['text'][:120]}"})
    try:
        result = await deep.ainvoke({"messages": [{"role": "user", "content": task}]},
                                    {"recursion_limit": max_steps * 3})
        for m in result.get("messages", []):
            um = getattr(m, "usage_metadata", None)
            if um:
                agent.add_usage(um)
    except Exception as e:  # recursion limit, provider errors: never crash verification
        if not verdict:
            return {"type": "investigator", "status": "unknown", "detail": f"investigator stopped: {type(e).__name__}"}
    if not verdict:
        return {"type": "investigator", "status": "unknown", "detail": "investigator finished without a verdict"}
    ev = _norm(verdict["evidence"])
    grounded = len(ev) >= 3 and any(ev in _norm(o.text) for o in s.observations[start_obs:])
    if verdict["passed"] and not grounded:
        return {"type": "investigator", "status": "unknown",
                "detail": "investigator said pass, but its evidence quote is not in anything it observed"}
    return {"type": "investigator", "status": "pass" if verdict["passed"] else "fail",
            "detail": (verdict["explanation"][:200] + " | evidence: " + verdict["evidence"][:200]).strip(" |")}
