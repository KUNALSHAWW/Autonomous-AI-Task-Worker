"""Run manager: starts runs in the background, streams their events, and routes
user replies (clarifications and approvals) back into the paused agent."""
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from .agent.engine import Agent
from .agent.state import Pending, RunState
from .config import Settings, make_chat_model, make_llm
from .environment import load_environment

AutoAnswer = Callable[[Pending, RunState], Awaitable[str | None]]


@dataclass
class RunHandle:
    state: RunState
    workdir: str
    events: list = field(default_factory=list)
    subscribers: set = field(default_factory=set)
    task: asyncio.Task | None = None
    waiting: asyncio.Future | None = None
    pending: Pending | None = None


class RunManager:
    def __init__(self, settings: Settings | None = None, llm_factory=None, auto_answer: AutoAnswer | None = None,
                 chat_model_factory=None):
        self.settings = settings or Settings()
        self.llm_factory = llm_factory or (lambda: make_llm(self.settings))
        # LangChain model for the deep-agent investigator; scripted test runs pass none.
        self.chat_model_factory = chat_model_factory or (
            (lambda: make_chat_model(self.settings)) if llm_factory is None else (lambda: None))
        self.auto_answer = auto_answer
        self.runs: dict[str, RunHandle] = {}
        # Each run owns a Chromium; cap how many run at once (small hosts: 1). Extra runs queue.
        self.slots = asyncio.Semaphore(int(os.environ.get("MAX_CONCURRENT_RUNS", "2")))
        os.makedirs(self.settings.runs_dir, exist_ok=True)

    # ------------------------------------------------------------------ lifecycle
    def create(self, task: str, dry_run: bool = False) -> RunHandle:
        state = RunState(task=task.strip(), dry_run=dry_run)
        workdir = os.path.join(self.settings.runs_dir, state.id)
        os.makedirs(workdir, exist_ok=True)
        h = RunHandle(state, workdir)
        self.runs[state.id] = h
        return h

    def start(self, h: RunHandle) -> RunHandle:
        h.task = asyncio.create_task(self._run(h))
        return h

    async def run_to_end(self, task: str, dry_run: bool = False) -> RunHandle:
        h = self.create(task, dry_run)
        await self._run(h)
        return h

    async def _run(self, h: RunHandle) -> None:
        env = load_environment(self.settings.environment_file, self.settings.sandbox_url)
        if os.environ.get("EMBED_SANDBOX") == "1":
            # The worker's own API and UI share the origin with the company apps: keep the agent out of them.
            env.denied_paths += ["/api", "/docs", "/redoc", "/openapi.json", "/static", "/healthz"]
        try:
            llm = self.llm_factory()
        except Exception as e:
            h.state.status, h.state.outcome, h.state.error = "failed", "failed", str(e)
            await self.emit(h, "error", {"message": str(e)})
            await self.emit(h, "status", {"status": "failed", "outcome": "failed"})
            return
        await self.emit(h, "created", {"task": h.state.task, "dry_run": h.state.dry_run, "model": llm.name})
        if self.slots.locked():
            await self.emit(h, "note", {"message": "Waiting for a free worker slot (another task is running)."})
        async with self.slots:
            try:
                chat_model = self.chat_model_factory()
            except Exception:
                chat_model = None
            agent = Agent(h.state, llm, env, self.settings, h.workdir,
                          emit=lambda t, d: self.emit(h, t, d), ask_user=lambda p: self._ask(h, p),
                          chat_model=chat_model)
            await agent.run()

    async def emit(self, h: RunHandle, type_: str, data: dict) -> None:
        ev = {"seq": len(h.events) + 1, "ts": round(time.time(), 3), "run_id": h.state.id, "type": type_, "data": data}
        h.events.append(ev)
        with open(os.path.join(h.workdir, "events.jsonl"), "a") as f:
            f.write(json.dumps(ev, default=str) + "\n")
        for q in list(h.subscribers):
            q.put_nowait(ev)

    async def _ask(self, h: RunHandle, p: Pending) -> str:
        if self.auto_answer:
            ans = await self.auto_answer(p, h.state)
            if ans is not None:
                return ans
        loop = asyncio.get_running_loop()
        h.waiting, h.pending = loop.create_future(), p
        try:
            return await asyncio.wait_for(h.waiting, timeout=self.settings.approval_timeout_s)
        except asyncio.TimeoutError:
            return "deny (no answer from the user in time)" if p.kind == "approval" else \
                "No answer from the user. Make the safest reasonable assumption or stop."
        finally:
            h.waiting, h.pending = None, None

    # ------------------------------------------------------------------ user actions
    def reply(self, run_id: str, answer: str) -> bool:
        h = self.runs.get(run_id)
        if not h or not h.waiting or h.waiting.done():
            return False
        h.waiting.set_result(answer)
        return True

    def cancel(self, run_id: str) -> bool:
        h = self.runs.get(run_id)
        if not h or not h.task or h.task.done():
            return False
        h.task.cancel()
        return True

    def subscribe(self, run_id: str) -> asyncio.Queue | None:
        h = self.runs.get(run_id)
        if not h:
            return None
        q: asyncio.Queue = asyncio.Queue()
        h.subscribers.add(q)
        return q

    def unsubscribe(self, run_id: str, q: asyncio.Queue) -> None:
        h = self.runs.get(run_id)
        if h:
            h.subscribers.discard(q)

    def summary(self, h: RunHandle) -> dict:
        s = h.state
        return {"id": s.id, "task": s.task, "status": s.status, "phase": s.phase, "outcome": s.outcome,
                "created_at": s.created_at, "finished_at": s.finished_at, "steps": len(s.steps),
                "dry_run": s.dry_run,
                "pending": None if not s.pending else {"id": s.pending.id, "kind": s.pending.kind,
                                                       "question": s.pending.question, "options": s.pending.options}}

    def load_past_runs(self) -> list[dict]:
        """Runs from previous server processes (read-only, from their checkpoint files)."""
        out = []
        if not os.path.isdir(self.settings.runs_dir):
            return out
        for rid in os.listdir(self.settings.runs_dir):
            if rid in self.runs:
                continue
            p = os.path.join(self.settings.runs_dir, rid, "state.json")
            if os.path.exists(p):
                try:
                    with open(p) as f:
                        d = json.load(f)
                    out.append({"id": d["id"], "task": d["task"], "status": d["status"], "phase": d["phase"],
                                "outcome": d["outcome"], "created_at": d["created_at"],
                                "finished_at": d.get("finished_at"), "steps": len(d.get("steps", [])),
                                "dry_run": d.get("dry_run"), "pending": None})
                except (OSError, json.JSONDecodeError, KeyError):
                    pass
        return out
