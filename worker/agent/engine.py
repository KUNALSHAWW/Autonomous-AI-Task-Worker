"""The worker's control loop as a LangGraph state graph.

    START -> intake -> act <-> act (one tool call per step)
               |       |  \
               |       |   human  (LangGraph interrupt: clarification or approval, then resume)
               v       v
             human   verify -> act (failed checks, bounded repair rounds)
                       |
                     report -> END

Why LangGraph: explicit nodes and conditional edges, a checkpointer per run
(thread_id = run id), and `interrupt()` for human-in-the-loop, so a run can stop
at a question and continue exactly where it left off when the answer arrives.

The graph state only carries routing (`route`, `pending`). The rich run state
(contract, facts, steps, observations) lives in RunState, which is written to
disk after every node, and is what the API, the UI and the verifier read.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
import traceback
from typing import Any, Awaitable, Callable, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from ..environment import Environment
from ..gate import Gate
from ..llm.base import LLMError, coerce_args, extract_json, validate_args
from ..tools.browser import BrowserSession
from ..tools.toolbox import ACT_TOOLS, TOOL_SPECS, Toolbox, ToolResult
from . import prompts
from .state import ChecklistItem, Contract, Pending, RunState, StepRecord
from .verify import Verifier

Emit = Callable[[str, dict], Awaitable[None]]
AskUser = Callable[[Pending], Awaitable[str]]
CONTENT_TOOLS = {"browser_goto", "browser_click", "browser_fill_form", "browser_read", "read_file", "http_request"}


YES = {"approve", "approved", "yes", "y", "ok", "okay", "confirm", "confirmed", "go", "proceed", "allow"}
NO = {"no", "not", "don't", "dont", "deny", "denied", "reject", "rejected", "stop", "never", "cancel"}


def is_approval(answer: str) -> bool:
    """Approve only on an explicit yes, and never when the answer also contains a no."""
    words = re.findall(r"[a-z']+", answer.lower())
    return bool(words) and words[0] in YES and not (set(words) & NO)


class GraphState(TypedDict, total=False):
    route: str             # which node runs next
    pending: dict | None   # a question waiting for the human, consumed by the `human` node


class Agent:
    def __init__(self, state: RunState, llm, env: Environment, settings, workdir: str, emit: Emit,
                 ask_user: AskUser, chat_model=None):
        self.state = state
        self.llm = llm
        self.chat_model = chat_model  # LangChain model for the deep-agent investigator (optional)
        self.env = env
        self.settings = settings
        self.workdir = workdir
        self.emit = emit
        self.ask_user_cb = ask_user
        self.gate = Gate(env, dry_run=state.dry_run)
        self.browser = BrowserSession(self.gate, env.vault, workdir, headless=settings.headless)
        self.tools = Toolbox(state, env, self.gate, self.browser, workdir, settings.observation_chars,
                             on_notify=lambda m: self.emit("notify", {"message": m}))
        self.env_text = env.describe()
        self.last_output = "(nothing yet: start by deciding where to look)"
        self.last_content = ""
        self.started = time.time()
        self.stalls = 0
        self.bad_calls = 0
        self.graph = self.build_graph()

    # ================================================================== graph
    def build_graph(self):
        g = StateGraph(GraphState)
        g.add_node("intake", self.n_intake)
        g.add_node("act", self.n_act)
        g.add_node("human", self.n_human)
        g.add_node("verify", self.n_verify)
        g.add_node("report", self.n_report)
        g.add_edge(START, "intake")
        route = lambda st: st["route"]
        g.add_conditional_edges("intake", route, {"act": "act", "human": "human"})
        g.add_conditional_edges("act", route, {"act": "act", "human": "human", "verify": "verify"})
        g.add_conditional_edges("human", route, {"act": "act", "human": "human"})
        g.add_conditional_edges("verify", route, {"act": "act", "report": "report"})
        g.add_edge("report", END)
        return g.compile(checkpointer=MemorySaver())

    def mermaid(self) -> str:
        return self.graph.get_graph().draw_mermaid()

    async def run(self) -> RunState:
        s = self.state
        s.status = "running"
        await self.emit("status", {"status": s.status})
        cfg = {"configurable": {"thread_id": s.id}, "recursion_limit": self.settings.max_steps * 3 + 200}
        try:
            await self.browser.start()
            inp: Any = {"route": "intake", "pending": None}
            while True:
                out = await self.graph.ainvoke(inp, cfg)
                interrupts = out.get("__interrupt__") if isinstance(out, dict) else None
                if not interrupts:
                    break
                payload = interrupts[0].value
                answer = await self.pause(Pending(**payload["question"]))
                inp = Command(resume=answer)
        except asyncio.CancelledError:
            s.status, s.outcome = "stopped", s.outcome or "stopped"
            raise
        except LLMError as e:
            s.status, s.outcome, s.error = "failed", "failed", f"Model provider error: {e}"
            await self.emit("error", {"message": s.error})
        except Exception as e:  # last line of defence: never lose the trace
            s.status, s.outcome, s.error = "failed", "failed", f"{type(e).__name__}: {e}"
            await self.emit("error", {"message": s.error, "traceback": traceback.format_exc()[-2000:]})
        finally:
            s.finished_at = time.time()
            if s.status == "running":
                s.status = "done"
            if s.report is None:
                try:
                    from .report import build_report
                    s.report = build_report(s, self.gate)
                except Exception:
                    pass
            self.checkpoint()
            await self.emit("status", {"status": s.status, "outcome": s.outcome})
            await self.browser.close()
        return s

    async def enter(self, phase: str) -> None:
        if self.state.phase != phase:
            self.state.phase = phase
            await self.emit("phase", {"phase": phase})

    def checkpoint(self) -> None:
        path = os.path.join(self.workdir, "state.json")
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.state.to_dict(), f, indent=1, default=str)
        os.replace(tmp, path)

    async def llm_call(self, messages, tools=None, json_mode=False, purpose="", max_tokens=1200):
        reply = await self.llm.chat(messages, tools=tools, json_mode=json_mode, purpose=purpose, max_tokens=max_tokens)
        self.add_usage(reply.usage)
        return reply

    def add_usage(self, usage: dict) -> None:
        u = self.state.usage
        u["llm_calls"] += 1
        u["prompt_tokens"] += int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        u["completion_tokens"] += int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)

    async def json_call(self, prompt: str, purpose: str, retries: int = 2) -> dict:
        messages = [{"role": "user", "content": prompt}]
        last_err = ""
        for _ in range(retries + 1):
            reply = await self.llm_call(messages, json_mode=True, purpose=purpose, max_tokens=4000)
            try:
                obj = extract_json(reply.text)
                if isinstance(obj, dict):
                    return obj
                last_err = "expected a JSON object"
            except ValueError as e:
                last_err = str(e)
            messages = messages + [{"role": "assistant", "content": reply.text or "(empty)"},
                                   {"role": "user", "content": f"That was not valid JSON ({last_err}). "
                                                               f"Reply with ONLY the JSON object."}]
        raise LLMError(f"{purpose}: model did not return valid JSON ({last_err})")

    async def pause(self, pending: Pending) -> str:
        """Called by the driver when the graph is interrupted: surface the question, wait for the answer."""
        s = self.state
        s.pending, s.status = pending, "waiting_for_user"
        self.checkpoint()
        await self.emit("question", {"id": pending.id, "kind": pending.kind, "question": pending.question,
                                     "options": pending.options, "context": pending.context})
        answer = await self.ask_user_cb(pending)
        s.pending, s.status = None, "running"
        await self.emit("answer", {"id": pending.id, "answer": answer})
        return answer

    # ================================================================== node: intake
    async def n_intake(self, st: GraphState) -> GraphState:
        await self.enter("intake")
        s = self.state
        prompt = prompts.INTAKE.format(environment=self.env_text) + f"\n\nUser request:\n{s.task}"
        data = await self.json_call(prompt, "intake")
        c = Contract(
            goal=str(data.get("goal") or s.task),
            deliverables=[str(x) for x in data.get("deliverables") or []],
            success_criteria=[{"id": str(x.get("id") or f"s{i + 1}"), "text": str(x.get("text") or x)}
                              if isinstance(x, dict) else {"id": f"s{i + 1}", "text": str(x)}
                              for i, x in enumerate(data.get("success_criteria") or [])],
            facts_needed=[{"key": str(x.get("key")), "description": str(x.get("description", ""))}
                          if isinstance(x, dict) else {"key": str(x), "description": ""}
                          for x in data.get("facts_needed") or []],
            assumptions=[str(x) for x in data.get("assumptions") or []],
            constraints=[str(x) for x in data.get("constraints") or []],
            questions=[str(x) for x in data.get("blocking_questions") or []],
            checklist=[ChecklistItem(f"c{i + 1}", str(x)) for i, x in enumerate(data.get("checklist") or [])],
            read_only_task=bool(data.get("read_only")),
        )
        if not c.checklist:
            c.checklist = [ChecklistItem("c1", c.goal)]
        s.contract = c
        self.gate.info_only = c.read_only_task
        await self.emit("contract", {"contract": s.to_dict()["contract"]})
        self.checkpoint()
        if c.questions:
            return {"route": "human", "pending": self._question("intake", c.questions[0], "clarification",
                                                                extra={"remaining": c.questions[1:]})}
        return {"route": "act", "pending": None}

    def _question(self, source: str, text: str, kind: str, options=None, context=None, extra=None) -> dict:
        n = len(self.state.contract.clarifications) + len(self.state.approvals) + 1
        q = {"id": f"{source}{n}", "kind": kind, "question": text, "options": options or [],
             "context": context or {}}
        return {"source": source, "question": q, **(extra or {})}

    # ================================================================== node: human (interrupt)
    async def n_human(self, st: GraphState) -> GraphState:
        pend = st["pending"]
        answer = interrupt(pend)  # the run stops here; the driver resumes it with the user's answer
        s = self.state
        src = pend["source"]
        q = pend["question"]["question"]
        if src == "approval":
            self._apply_approval(pend, str(answer))
            return {"route": "act", "pending": None}
        s.contract.clarifications.append({"question": q, "answer": str(answer)})
        if src == "intake" and pend.get("remaining"):
            rest = pend["remaining"]
            return {"route": "human", "pending": self._question("intake", rest[0], "clarification",
                                                                extra={"remaining": rest[1:]})}
        if src == "ask_user":
            self.last_output = f"The user answered your question: {answer}"
        return {"route": "act", "pending": None}

    def _apply_approval(self, pend: dict, answer: str) -> None:
        s = self.state
        rule = pend["rule"]
        approved = is_approval(answer)
        s.approvals.append({"rule": rule.get("id"), "action": pend["action"], "approved": approved,
                            "answer": answer, "step": len(s.steps)})
        if approved:
            self.gate.grant(rule["id"], rule["_key"])
            self.last_output = (pend["output"] + f"\n\nSYSTEM: The user APPROVED this action ({rule.get('id')}). "
                                                 "Repeat exactly the same submission now; it will go through.")
        else:
            self.gate.deny(rule["id"], rule["_key"])
            self.last_output = (pend["output"] + f"\n\nSYSTEM: The user DENIED this action ({rule.get('id')}). "
                                                 f"User said: {answer}. Do not attempt it again. Continue with "
                                                 "whatever else is possible and explain this in your final summary.")

    # ================================================================== node: act (one step)
    def _turn_prompt(self) -> str:
        s, c = self.state, self.state.contract
        checklist = "\n".join(f"[{'x' if it.done else ' '}] {it.id}: {it.text}" for it in c.checklist)
        facts_needed = "\n".join(
            f"- {f['key']}: {f['description']} {'(saved)' if f['key'] in s.facts else '(MISSING)'}"
            for f in c.facts_needed) or "(none specified)"
        facts = "\n".join(f"- {k} = {f.value}   [source: {f.source}]" for k, f in s.facts.items()) or "(empty)"
        clar = ""
        if c.clarifications:
            clar = "\nUSER CLARIFICATIONS:\n" + "\n".join(f"- Q: {x['question']} A: {x['answer']}"
                                                          for x in c.clarifications) + "\n"
        notes = ""
        if s.notes:
            notes = "\nSUPERVISOR NOTES (most recent last, take them seriously):\n" + "\n".join(
                f"- {n}" for n in s.notes[-5:]) + "\n"
        act_steps = [st for st in s.steps if st.phase == "act"]
        hist = "\n".join(
            f"{st.n}. {st.tool}({self._short_args(st.args)}) -> {'ok' if st.ok else 'FAILED ' + (st.error_kind or '')}: "
            f"{st.summary[:160]}" for st in act_steps[-14:]) or "(no steps yet)"
        if len(act_steps) > 14:
            hist = f"({len(act_steps) - 14} earlier steps omitted)\n" + hist
        return prompts.ACT_TURN.format(
            task=s.task, goal=c.goal, clarifications=clar, checklist=checklist, facts_needed=facts_needed,
            facts=facts, notes=notes, history=hist, step=len(act_steps) + 1, max_steps=self.settings.max_steps,
            observation=self.last_output)

    @staticmethod
    def _short_args(args: dict) -> str:
        a = {k: v for k, v in args.items() if k != "reason"}
        txt = json.dumps(a, ensure_ascii=False)
        return txt[:140] + ("..." if len(txt) > 140 else "")

    async def n_act(self, st: GraphState) -> GraphState:
        await self.enter("act")
        s = self.state
        act_steps = [x for x in s.steps if x.phase == "act"]
        if len(act_steps) >= self.settings.max_steps or time.time() - self.started > self.settings.max_minutes * 60:
            s.notes.append("Budget exhausted.")
            s.claimed = {"status": "incomplete", "summary": "Stopped: step or time budget exhausted before the "
                                                            "task was finished.", "result": {}}
            await self.emit("note", {"message": "Budget exhausted, stopping."})
            return {"route": "verify", "pending": None}
        messages = [{"role": "system", "content": prompts.ACT_SYSTEM.format(environment=self.env_text)},
                    {"role": "user", "content": self._turn_prompt()}]
        reply = await self.llm_call(messages, tools=[TOOL_SPECS[n] for n in ACT_TOOLS], purpose="act",
                                    max_tokens=4000)
        call = reply.tool_call
        if call is None:
            err = "You must call exactly one tool. Reply with a tool call."
        elif call.name not in TOOL_SPECS:
            err = f"Unknown tool '{call.name}'."
        elif "__malformed__" in call.args:
            err = "Your tool arguments were not valid JSON."
        else:
            call.args = coerce_args(TOOL_SPECS[call.name].parameters, call.args)
            err = validate_args(TOOL_SPECS[call.name].parameters, call.args)
        if err:
            self.bad_calls += 1
            self.last_output = f"ERROR: {err} Your previous output: {(reply.text or '')[:300]}"
            await self.emit("note", {"message": f"Invalid model action: {err}"})
            if self.bad_calls >= 4:
                raise LLMError("the model repeatedly failed to produce a valid tool call")
            return {"route": "act", "pending": None}
        self.bad_calls = 0
        return await self.execute(call.name, call.args)

    async def execute(self, name: str, args: dict) -> GraphState:
        s = self.state
        n = len(s.steps) + 1
        reason = str(args.get("reason", ""))
        await self.emit("step_started", {"n": n, "tool": name, "args": self._public_args(args), "reason": reason})
        t0 = time.time()
        res: ToolResult = await self.tools.run(name, args, n)
        rec = StepRecord(n, name, self._public_args(args), reason, res.ok, res.error_kind,
                         res.output.split("\n")[0][:300], self.browser.page.url if self.browser.page else "",
                         res.screenshot, round(time.time() - t0, 2))
        s.steps.append(rec)
        if name in CONTENT_TOOLS:
            self.last_content = res.output
            self.last_output = res.output
        else:
            # Memory/plan/notify results are short; keep the page or document in view so the
            # model does not have to re-open it to continue working with it.
            self.last_output = res.output + (
                "\n\nThe most recent page/document you looked at (still current):\n" + self.last_content
                if self.last_content else "")
        await self.emit("step", {**rec.__dict__, "screenshot": os.path.basename(rec.screenshot) if rec.screenshot
                                 else None, "observation": res.output[:3000]})
        if res.finish:
            s.claimed = res.finish
            await self.emit("claimed", res.finish)
            self.checkpoint()
            return {"route": "verify", "pending": None}
        self.guard(rec)
        self.checkpoint()
        if res.pause:
            p = res.pause
            return {"route": "human", "pending": self._question("ask_user", p.question, p.kind, p.options)}
        if res.approval:
            rule = res.approval["rule"] or {}
            payload = self._short_args(args)
            text = (f"Approval needed. Company policy '{rule.get('id')}' applies: {rule.get('description', '')}\n"
                    f"The worker wants to: {res.approval['message']}\nAction: {payload}\nApprove this action?")
            return {"route": "human", "pending": self._question(
                "approval", text, "approval", ["approve", "deny"], {"rule": rule.get("id"), "action": payload},
                extra={"rule": rule, "action": payload, "output": res.output})}
        return {"route": "act", "pending": None}

    @staticmethod
    def _public_args(args: dict) -> dict:
        return {k: v for k, v in args.items() if k != "reason"}

    # ------------------------------------------------------------------ guard (pure code)
    def guard(self, rec: StepRecord) -> None:
        s = self.state
        act = [st for st in s.steps if st.phase == "act"]
        note = None
        if rec.error_kind in ("ambiguous_write", "server_error") and rec.tool in ("browser_fill_form", "browser_click",
                                                                                  "http_request"):
            note = ("The last submission hit a server error. It may or may not have been saved. Check the system "
                    "for the record before submitting again.")
        sig = lambda st: hashlib.md5(f"{st.tool}{json.dumps(st.args, sort_keys=True)}".encode()).hexdigest()
        recent = act[-6:]
        same = [st for st in recent if sig(st) == sig(rec)]
        if len(same) >= 3:
            note = (f"You have now run {rec.tool} with the same arguments {len(same)} times recently. Repeating it "
                    "will not help. Call update_plan with a one-line diagnosis, then take a different approach.")
            self.stalls += 1
        fails = 0
        for st in reversed(act):
            if st.ok or st.error_kind == "needs_approval":
                break
            fails += 1
        if fails >= 3 and fails % 3 == 0:
            note = (f"{fails} actions in a row have failed (latest: {rec.error_kind}: {rec.summary[:120]}). Stop and "
                    "rethink: re-read the page, check format hints and error messages, consider another path "
                    "(search, a different page, the API). Call update_plan with a diagnosis first.")
            self.stalls += 1
        if len(act) >= 12:
            window = act[-10:]
            if len({st.url for st in window}) <= 1 and not any(f.step > window[0].n for f in s.facts.values()) \
                    and not any(st.tool == "update_plan" for st in window):
                note = ("No visible progress in the last 10 steps (same page, no new facts). Step back and "
                        "reconsider your plan.")
                self.stalls += 1
        if note:
            s.notes.append(f"(after step {rec.n}) {note}")
            asyncio.ensure_future(self.emit("note", {"message": note}))
        if self.stalls >= 4:
            self.stalls = 0
            s.notes.append("(supervisor) You appear stuck. If you cannot make progress, ask the user for help "
                           "with a specific question, or finish with status=blocked.")

    # ================================================================== node: verify
    async def n_verify(self, st: GraphState) -> GraphState:
        await self.enter("verify")
        result = await Verifier(self).run()
        s = self.state
        s.verification = result
        await self.emit("verification", result)
        self.checkpoint()
        if result["outcome"] in ("failed", "partially_verified") and result.get("repairable") \
                and s.repair_rounds < self.settings.max_repair_rounds and (s.claimed or {}).get("status") == "completed":
            s.repair_rounds += 1
            problems = "; ".join(result.get("problems", []))[:900]
            s.notes.append(f"(verification round {s.repair_rounds}) Independent verification FAILED: {problems}. "
                           "Investigate the actual state of the system and fix it, then finish again.")
            self.last_output = f"SYSTEM: Your finish was rejected by independent verification: {problems}"
            s.claimed = None
            return {"route": "act", "pending": None}
        s.outcome = result["outcome"]
        return {"route": "report", "pending": None}

    # ================================================================== node: report
    async def n_report(self, st: GraphState) -> GraphState:
        await self.enter("report")
        from .report import build_report
        self.state.report = build_report(self.state, self.gate)
        await self.emit("report", self.state.report)
        return {"route": "end", "pending": None}
