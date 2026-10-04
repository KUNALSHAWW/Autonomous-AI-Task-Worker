"""The worker's control loop, written as a small explicit state graph.

    intake ──> act ──> verify ──> report ──> end
      │         ▲  │      │
      │ (ask)   │  │      └── failed checks ──> act (bounded repair rounds)
      └─────────┘  └── pause for user (clarification / approval) and resume

Nodes are plain async methods that return the name of the next node. The
state is checkpointed after every node and every step, and everything the
agent does is emitted as an event (for the API, the CLI and the trace file).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import traceback
from typing import Awaitable, Callable

from ..environment import Environment
from ..gate import Gate
from ..llm.base import LLMError, coerce_args, extract_json, validate_args
from ..tools.browser import BrowserSession
from ..tools.toolbox import ACT_TOOLS, TOOL_SPECS, Toolbox, ToolResult
from . import prompts
from .state import ChecklistItem, Contract, Pending, RunState, StepRecord
from .verify import Verifier

Emit = Callable[[str, dict], Awaitable[None]]
CONTENT_TOOLS = {"browser_goto", "browser_click", "browser_fill_form", "browser_read", "read_file", "http_request"}
AskUser = Callable[[Pending], Awaitable[str]]


class Agent:
    def __init__(self, state: RunState, llm, env: Environment, settings, workdir: str, emit: Emit, ask_user: AskUser):
        self.state = state
        self.llm = llm
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

    # ================================================================== graph driver
    async def run(self) -> RunState:
        nodes = {"intake": self.intake, "act": self.act, "verify": self.verify, "report": self.report}
        s = self.state
        s.status = "running"
        await self.emit("status", {"status": s.status})
        try:
            await self.browser.start()
            node = s.phase if s.phase in nodes else "intake"
            while node != "end":
                s.phase = node
                await self.emit("phase", {"phase": node})
                node = await nodes[node]()
                self.checkpoint()
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

    def checkpoint(self) -> None:
        path = os.path.join(self.workdir, "state.json")
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.state.to_dict(), f, indent=1, default=str)
        os.replace(tmp, path)

    async def llm_call(self, messages, tools=None, json_mode=False, purpose="", max_tokens=1200):
        reply = await self.llm.chat(messages, tools=tools, json_mode=json_mode, purpose=purpose, max_tokens=max_tokens)
        u = self.state.usage
        u["llm_calls"] += 1
        u["prompt_tokens"] += int(reply.usage.get("prompt_tokens") or reply.usage.get("input_tokens") or 0)
        u["completion_tokens"] += int(reply.usage.get("completion_tokens") or reply.usage.get("output_tokens") or 0)
        return reply

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
        s = self.state
        s.pending, s.status = pending, "waiting_for_user"
        self.checkpoint()
        await self.emit("question", {"id": pending.id, "kind": pending.kind, "question": pending.question,
                                     "options": pending.options, "context": pending.context})
        answer = await self.ask_user_cb(pending)
        s.pending, s.status = None, "running"
        await self.emit("answer", {"id": pending.id, "answer": answer})
        return answer

    # ================================================================== intake
    async def intake(self) -> str:
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
        await self.emit("contract", {"contract": s.to_dict()["contract"]})
        for q in c.questions:
            ans = await self.pause(Pending(id=f"intake{len(c.clarifications) + 1}", kind="clarification", question=q))
            c.clarifications.append({"question": q, "answer": ans})
        return "act"

    # ================================================================== act
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

    async def act(self) -> str:
        s = self.state
        system = prompts.ACT_SYSTEM.format(environment=self.env_text)
        tools = [TOOL_SPECS[n] for n in ACT_TOOLS]
        bad_calls = 0
        while True:
            act_steps = [st for st in s.steps if st.phase == "act"]
            if len(act_steps) >= self.settings.max_steps or time.time() - self.started > self.settings.max_minutes * 60:
                s.notes.append("Budget exhausted.")
                s.claimed = {"status": "incomplete", "summary": "Stopped: step or time budget exhausted before the "
                                                                "task was finished.", "result": {}}
                await self.emit("note", {"message": "Budget exhausted, stopping."})
                return "verify"
            messages = [{"role": "system", "content": system}, {"role": "user", "content": self._turn_prompt()}]
            reply = await self.llm_call(messages, tools=tools, purpose="act", max_tokens=4000)
            call = reply.tool_call
            err = None
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
                bad_calls += 1
                self.last_output = f"ERROR: {err} Your previous output: {(reply.text or '')[:300]}"
                await self.emit("note", {"message": f"Invalid model action: {err}"})
                if bad_calls >= 4:
                    raise LLMError("the model repeatedly failed to produce a valid tool call")
                continue
            bad_calls = 0
            next_node = await self.execute(call.name, call.args)
            if next_node:
                return next_node

    async def execute(self, name: str, args: dict) -> str | None:
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
        await self.emit("step", {**rec.__dict__, "observation": res.output[:3000]})
        if res.finish:
            s.claimed = res.finish
            await self.emit("claimed", res.finish)
            return "verify"
        if res.pause:
            answer = await self.pause(res.pause)
            s.contract.clarifications.append({"question": res.pause.question, "answer": answer})
            self.last_output = f"The user answered your question: {answer}"
        if res.approval:
            await self.handle_approval(res, args)
        self.guard(rec)
        self.checkpoint()
        return None

    @staticmethod
    def _public_args(args: dict) -> dict:
        return {k: v for k, v in args.items() if k != "reason"}

    async def handle_approval(self, res: ToolResult, args: dict) -> None:
        rule = res.approval["rule"] or {}
        s = self.state
        payload = self._short_args(args)
        question = (f"Approval needed. Company policy '{rule.get('id')}' applies: {rule.get('description', '')}\n"
                    f"The worker wants to: {res.approval['message']}\nAction: {payload}\nApprove this action?")
        pend = Pending(id=f"approval{len(s.approvals) + 1}", kind="approval", question=question,
                       options=["approve", "deny"], context={"rule": rule.get("id"), "action": payload})
        answer = await self.pause(pend)
        approved = answer.strip().lower().startswith(("y", "approve", "ok", "go", "sure", "confirm"))
        s.approvals.append({"rule": rule.get("id"), "action": payload, "approved": approved, "answer": answer,
                            "step": len(s.steps)})
        if approved:
            self.gate.grant(rule["id"], rule["_key"])
            self.last_output = (res.output + f"\n\nSYSTEM: The user APPROVED this action ({rule.get('id')}). "
                                             "Repeat exactly the same submission now; it will go through.")
        else:
            self.gate.deny(rule["id"], rule["_key"])
            self.last_output = (res.output + f"\n\nSYSTEM: The user DENIED this action ({rule.get('id')}). "
                                             f"User said: {answer}. Do not attempt it again. Continue with whatever "
                                             "else is possible and explain this in your final summary.")

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

    # ================================================================== verify
    async def verify(self) -> str:
        v = Verifier(self)
        result = await v.run()
        s = self.state
        s.verification = result
        await self.emit("verification", result)
        if result["outcome"] in ("failed", "partially_verified") and result.get("repairable") \
                and s.repair_rounds < self.settings.max_repair_rounds and (s.claimed or {}).get("status") == "completed":
            s.repair_rounds += 1
            problems = "; ".join(result.get("problems", []))[:900]
            s.notes.append(f"(verification round {s.repair_rounds}) Independent verification FAILED: {problems}. "
                           "Investigate the actual state of the system and fix it, then finish again.")
            self.last_output = f"SYSTEM: Your finish was rejected by independent verification: {problems}"
            s.claimed = None
            return "act"
        s.outcome = result["outcome"]
        return "report"

    # ================================================================== report
    async def report(self) -> str:
        from .report import build_report
        self.state.report = build_report(self.state, self.gate)
        await self.emit("report", self.state.report)
        return "end"
