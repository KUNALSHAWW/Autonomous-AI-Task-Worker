"""Builds the final report: what was done, how we know, and what to look at.

Pure code, no model call: the report is assembled from verified state, so it
cannot contradict the verification.
"""
from __future__ import annotations

import os
import time

HEADLINES = {
    "verified": "Done and independently verified.",
    "partially_verified": "Partly done: some checks failed. Please review.",
    "unverified": "Done, but some results could not be verified automatically. Please spot check.",
    "failed": "Not completed.",
    "needs_attention": "Stopped: needs your input or a decision.",
    "dry_run": "Dry run finished: nothing was changed. Planned writes are listed below.",
    "stopped": "Stopped by the user.",
}


def build_report(s, gate) -> dict:
    v = s.verification or {}
    outcome = s.outcome or v.get("outcome") or ("failed" if s.error else "unverified")
    claimed = s.claimed or {}
    act_steps = [st for st in s.steps if st.phase == "act"]
    failed_steps = [st for st in act_steps if not st.ok and st.error_kind not in ("needs_approval",)]
    recovered = 0
    for i, st in enumerate(act_steps):
        if not st.ok and any(x.ok for x in act_steps[i + 1:i + 4]):
            recovered += 1
    evidence = []
    shots = [st for st in s.steps if st.screenshot]
    for st in shots:
        if st.tool == "browser_fill_form" or st.phase == "verify":
            evidence.append({"step": st.n, "what": st.summary[:120], "screenshot": os.path.basename(st.screenshot)})
    last_act = [st for st in shots if st.phase == "act"]
    if last_act and all(e["step"] != last_act[-1].n for e in evidence):
        evidence.append({"step": last_act[-1].n, "what": "last screen before finishing",
                         "screenshot": os.path.basename(last_act[-1].screenshot)})
    writes = [w for w in gate.writes] if gate else []
    return {
        "run_id": s.id,
        "task": s.task,
        "outcome": outcome,
        "headline": HEADLINES.get(outcome, outcome),
        "summary": claimed.get("summary") or s.error or "No summary was produced.",
        "result": claimed.get("result") or {},
        "worker_status": claimed.get("status"),
        "verification": {
            "summary": v.get("summary"),
            "criteria": [{"id": c["id"], "text": c["text"], "status": c["status"], "detail": c["detail"]}
                         for c in v.get("criteria", [])],
            "source_checks": v.get("fidelity", []),
            "problems": v.get("problems", []),
        },
        "facts": [{"key": k, "value": f.value, "source": f.source, "quote": f.quote} for k, f in s.facts.items()],
        "changes_made": [{"step": w.step, "method": w.method, "url": w.url, "status": w.status, "outcome": w.outcome,
                          "fields": {k: val for k, val in w.fields.items() if "password" not in str(k).lower()}}
                         for w in writes],
        "approvals": s.approvals,
        "questions_asked": s.contract.clarifications,
        "messages_sent_to_user": s.messages_to_user,
        "assumptions": s.contract.assumptions,
        "evidence": evidence[-8:],
        "stats": {
            "steps": len(act_steps),
            "verification_steps": len(s.steps) - len(act_steps),
            "failed_actions": len(failed_steps),
            "recovered_failures": recovered,
            "repair_rounds": s.repair_rounds,
            "llm_calls": s.usage["llm_calls"],
            "tokens": s.usage["prompt_tokens"] + s.usage["completion_tokens"],
            "duration_s": round((s.finished_at or time.time()) - s.created_at, 1),
        },
        "error": s.error,
    }
