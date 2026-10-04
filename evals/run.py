"""Eval harness.

    python -m evals.run                         all scenarios, seed 1
    python -m evals.run --seeds 1,2,3 --only invoice_email,bec_fraud
    python -m evals.run --paraphrases           also run task paraphrases
    python -m evals.run --scripted              plumbing check with the scripted model (invoice scenarios only)

For every run it rebuilds the sandbox world from the seed, runs the worker with a
simulated user, then grades the final system state with a ground-truth checker.
Besides pass rate it reports how often the worker's own verification agreed with
ground truth, and the false-success rate (worker said "verified" but it wasn't).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time

import httpx
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from evals.checkers import CHECKERS  # noqa: E402
from worker.__main__ import start_sandbox  # noqa: E402
from worker.config import Settings  # noqa: E402
from worker.service import RunManager  # noqa: E402

HERE = os.path.dirname(__file__)


def fill(template: str, truth: dict) -> str:
    v = truth["vendors"]
    return template.format(email=v["email"], portal=v["portal"], big=v["big"], new=v["new"], bec=v["bec"],
                           twin0=truth["twins"][0], twin1=truth["twins"][1])


async def run_one(sc: dict, seed: int, task_tmpl: str, sandbox: str, settings: Settings, llm_factory=None) -> dict:
    admin = {"x-admin-token": os.environ.get("SANDBOX_ADMIN_TOKEN", "")}
    async with httpx.AsyncClient(timeout=30) as c:
        truth = (await c.post(f"{sandbox}/acme/admin/reset", json={"seed": seed, "chaos": sc.get("chaos") or {}},
                              headers=admin)).json()["truth"]
    task = fill(task_tmpl, truth)
    user = sc.get("user") or {}

    async def answer(p, state):
        if p.kind == "approval":
            return "approve" if user.get("approve", True) else "deny - not approved, do not do this"
        return fill(user.get("answer", "Use your best judgement."), truth)

    mgr = RunManager(settings, llm_factory=llm_factory, auto_answer=answer)
    t0 = time.time()
    h = await mgr.run_to_end(task)
    async with httpx.AsyncClient(timeout=30) as c:
        state = (await c.get(f"{sandbox}/acme/admin/state", headers=admin)).json()
    run = h.state.to_dict()
    ok, detail = CHECKERS[sc["check"]](state, run, **(sc.get("args") or {}))
    outcome = h.state.outcome
    claimed_success = outcome in ("verified",)
    return {
        "scenario": sc["id"], "seed": seed, "task": task, "passed": ok, "detail": detail, "outcome": outcome,
        "worker_status": (h.state.claimed or {}).get("status"), "false_success": claimed_success and not ok,
        "agreement": claimed_success == ok, "steps": len([s for s in h.state.steps if s.phase == "act"]),
        "llm_calls": h.state.usage["llm_calls"],
        "tokens": h.state.usage["prompt_tokens"] + h.state.usage["completion_tokens"],
        "questions": len(h.state.contract.clarifications), "approvals": len(h.state.approvals),
        "repair_rounds": h.state.repair_rounds, "seconds": round(time.time() - t0, 1), "run_id": h.state.id,
        "error": h.state.error,
    }


def table(rows: list[dict]) -> str:
    out = ["| scenario | seed | ground truth | worker outcome | steps | LLM calls | detail |",
           "|---|---|---|---|---|---|---|"]
    for r in rows:
        out.append(f"| {r['scenario']} | {r['seed']} | {'PASS' if r['passed'] else 'FAIL'} | {r['outcome']} | "
                   f"{r['steps']} | {r['llm_calls']} | {r['detail'][:80]} |")
    n = len(rows)
    if n:
        p = sum(r["passed"] for r in rows)
        fs = sum(r["false_success"] for r in rows)
        ag = sum(r["agreement"] for r in rows)
        out.append("")
        out.append(f"Pass rate: {p}/{n} ({100 * p / n:.0f}%).  Verifier agreement with ground truth: {ag}/{n}.  "
                   f"False successes (claimed verified but wrong): {fs}.")
    return "\n".join(out)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", default="1")
    ap.add_argument("--only", default="")
    ap.add_argument("--paraphrases", action="store_true")
    ap.add_argument("--scripted", action="store_true")
    args = ap.parse_args()

    with open(os.path.join(HERE, "scenarios.yaml")) as f:
        scenarios = yaml.safe_load(f)
    if args.only:
        keep = set(args.only.split(","))
        scenarios = [s for s in scenarios if s["id"] in keep]
    port = int(os.environ.get("SANDBOX_PORT", "8100"))
    sandbox = os.environ.setdefault("SANDBOX_URL", f"http://127.0.0.1:{port}")
    proc = start_sandbox(port)
    settings = Settings()
    settings.runs_dir = os.path.join(HERE, "..", "data", "eval_runs")

    rows = []
    try:
        for seed in [int(s) for s in args.seeds.split(",")]:
            for sc in scenarios:
                tasks = [sc["task"]] + (sc.get("paraphrases", []) if args.paraphrases else [])
                for tmpl in tasks:
                    factory = None
                    if args.scripted:
                        if sc["check"] != "bill_entered" or sc["args"]["role"] != "email":
                            continue
                        sys.path.insert(0, os.path.join(HERE, "..", "tests"))
                        from scripted_policy import InvoicePolicy
                        from worker.llm.scripted import ScriptedLLM
                        from sandbox.world import generate
                        vendor = generate(seed).truth["vendors"]["email"]
                        factory = (lambda v=vendor: ScriptedLLM(InvoicePolicy(v)))
                    r = await run_one(sc, seed, tmpl, sandbox, settings, factory)
                    rows.append(r)
                    print(f"{'PASS' if r['passed'] else 'FAIL'}  {r['scenario']:<24} seed={seed} outcome={r['outcome']:<18} "
                          f"steps={r['steps']:<3} {r['detail'][:90]}", flush=True)
    finally:
        if proc:
            proc.terminate()
    out_dir = os.path.join(HERE, "..", "data", "evals")
    os.makedirs(out_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    with open(os.path.join(out_dir, f"results-{stamp}.json"), "w") as f:
        json.dump(rows, f, indent=1)
    md = table(rows)
    with open(os.path.join(out_dir, f"results-{stamp}.md"), "w") as f:
        f.write(md + "\n")
    print("\n" + md)


if __name__ == "__main__":
    asyncio.run(main())
