"""End to end: real agent loop + real Chromium + live sandbox, with a scripted model.
Checks the final state of the sandbox, not just what the agent says."""
import os
import tempfile

import pytest

from conftest import reset, state
from scripted_policy import InvoicePolicy
from worker.config import Settings
from worker.llm.scripted import ScriptedLLM
from worker.service import RunManager


def _mgr(policy, answers=None):
    s = Settings()
    s.runs_dir = tempfile.mkdtemp()
    s.max_steps = 40

    async def auto(p, st):
        return (answers or {}).get(p.kind, "approve")

    return RunManager(s, llm_factory=lambda: ScriptedLLM(policy), auto_answer=auto)


def _bills(base, number):
    return [b for b in state(base)["bills"] if b["invoice_number"] == number]


async def test_invoice_flow_verified(sandbox):
    truth = reset(sandbox, seed=21)
    vendor = truth["vendors"]["email"]
    h = await _mgr(InvoicePolicy(vendor, sandbox)).run_to_end(f"enter the latest invoice from {vendor}")
    inv = truth["latest"][vendor]
    bills = _bills(sandbox, inv["invoice_number"])
    assert h.state.outcome == "verified", (h.state.error, [(s.tool, s.ok, s.summary[:120]) for s in h.state.steps][-4:])
    assert len(bills) == 1 and bills[0]["amount"] == inv["amount"] and bills[0]["due_date"] == inv["due_date"]
    assert all(f["status"] == "match" for f in h.state.verification["fidelity"])
    assert os.path.exists(os.path.join(h.workdir, "events.jsonl"))
    assert h.state.messages_to_user


async def test_lying_ui_is_caught_and_repaired(sandbox):
    truth = reset(sandbox, seed=22, chaos={"lying_ui": 1})
    vendor = truth["vendors"]["email"]
    h = await _mgr(InvoicePolicy(vendor, sandbox)).run_to_end(f"enter the latest invoice from {vendor}")
    assert h.state.repair_rounds == 1
    assert h.state.outcome == "verified"
    assert len(_bills(sandbox, truth["latest"][vendor]["invoice_number"])) == 1


async def test_ambiguous_502_does_not_create_duplicate(sandbox):
    truth = reset(sandbox, seed=23, chaos={"ambiguous_502": 1})
    vendor = truth["vendors"]["email"]
    h = await _mgr(InvoicePolicy(vendor, sandbox)).run_to_end(f"enter the latest invoice from {vendor}")
    assert len(_bills(sandbox, truth["latest"][vendor]["invoice_number"])) == 1
    assert h.state.outcome == "verified"


@pytest.mark.parametrize("decision,expected", [("approve", 1), ("deny", 0)])
async def test_high_value_needs_approval(sandbox, decision, expected):
    truth = reset(sandbox, seed=24)
    vendor = truth["vendors"]["big"]
    h = await _mgr(InvoicePolicy(vendor, sandbox), {"approval": decision}).run_to_end(f"enter {vendor}'s invoice")
    assert h.state.approvals and h.state.approvals[0]["rule"] == "high_value_entry"
    assert len(_bills(sandbox, truth["latest"][vendor]["invoice_number"])) == expected
    assert h.state.outcome == ("verified" if decision == "approve" else "needs_attention")


async def test_dry_run_writes_nothing(sandbox):
    truth = reset(sandbox, seed=25)
    vendor = truth["vendors"]["email"]
    mgr = _mgr(InvoicePolicy(vendor, sandbox))
    h = await mgr.run_to_end(f"enter the latest invoice from {vendor}", dry_run=True)
    assert h.state.outcome == "dry_run"
    assert _bills(sandbox, truth["latest"][vendor]["invoice_number"]) == []
    assert any(w["outcome"] == "dry_run" for w in h.state.report["changes_made"])
