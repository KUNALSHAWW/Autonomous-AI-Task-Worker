"""Run state. Everything the agent knows lives here and is checkpointed to disk."""
from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Fact:
    key: str
    value: str
    source: str           # human readable: "file report.pdf", "page /app/items/4"
    quote: str            # verbatim text that supports the value
    obs_id: int           # observation the quote was found in (for blind re-extraction)
    step: int


@dataclass
class Observation:
    id: int
    step: int
    tool: str
    source: str
    text: str             # full text, untruncated (what the quote check runs against)


@dataclass
class StepRecord:
    n: int
    tool: str
    args: dict
    reason: str
    ok: bool
    error_kind: str | None
    summary: str
    url: str = ""
    screenshot: str | None = None
    duration_s: float = 0.0
    phase: str = "act"


@dataclass
class ChecklistItem:
    id: str
    text: str
    done: bool = False
    note: str = ""


@dataclass
class Contract:
    goal: str = ""
    deliverables: list[str] = field(default_factory=list)
    success_criteria: list[dict] = field(default_factory=list)   # {id, text}
    facts_needed: list[dict] = field(default_factory=list)       # {key, description}
    assumptions: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)           # blocking questions
    checklist: list[ChecklistItem] = field(default_factory=list)
    clarifications: list[dict] = field(default_factory=list)     # {question, answer}
    read_only_task: bool = False


@dataclass
class Pending:
    id: str
    kind: str              # clarification | approval
    question: str
    options: list[str] = field(default_factory=list)
    context: dict = field(default_factory=dict)
    asked_at: float = field(default_factory=time.time)


@dataclass
class RunState:
    task: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    status: str = "queued"     # queued | running | waiting_for_user | done | failed | stopped
    phase: str = "intake"      # intake | act | verify | report | end
    outcome: str = ""          # verified | partially_verified | unverified | failed | needs_attention | dry_run
    dry_run: bool = False
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    contract: Contract = field(default_factory=Contract)
    facts: dict[str, Fact] = field(default_factory=dict)
    observations: list[Observation] = field(default_factory=list)
    steps: list[StepRecord] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)          # supervisor / reflection notes shown to the actor
    messages_to_user: list[dict] = field(default_factory=list)
    approvals: list[dict] = field(default_factory=list)
    pending: Pending | None = None
    claimed: dict | None = None                               # what the actor said when it called finish
    verification: dict | None = None
    report: dict | None = None
    repair_rounds: int = 0
    usage: dict = field(default_factory=lambda: {"llm_calls": 0, "prompt_tokens": 0, "completion_tokens": 0})
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["observations"] = [{**o, "text": o["text"][:4000]} for o in d["observations"]]
        return d

    def add_observation(self, step: int, tool: str, source: str, text: str) -> Observation:
        o = Observation(len(self.observations) + 1, step, tool, source, text)
        self.observations.append(o)
        return o
