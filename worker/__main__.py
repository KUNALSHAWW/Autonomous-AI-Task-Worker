"""Command line entry point.

    python -m worker serve                 start sandbox + API (http://localhost:8000)
    python -m worker run "your task"       run one task in the terminal
    python -m worker run "..." --dry-run   plan and rehearse without writing anything
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time

import httpx


def _wait_for(url: str, seconds: float = 20) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        try:
            if httpx.get(url, timeout=1).status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.3)
    return False


def start_sandbox(port: int) -> subprocess.Popen | None:
    url = f"http://127.0.0.1:{port}/healthz"
    try:
        if httpx.get(url, timeout=1).status_code == 200:
            return None  # already running
    except httpx.HTTPError:
        pass
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "sandbox.app:app", "--host", "127.0.0.1",
                             "--port", str(port), "--log-level", "warning"])
    if not _wait_for(url):
        proc.terminate()
        raise SystemExit("sandbox did not start")
    return proc


def cmd_serve(args) -> None:
    import uvicorn

    port = int(os.environ.get("SANDBOX_PORT", "8100"))
    os.environ.setdefault("SANDBOX_URL", f"http://127.0.0.1:{port}")
    proc = start_sandbox(port)
    try:
        uvicorn.run("worker.api:app", host=args.host, port=args.port, log_level="info")
    finally:
        if proc:
            proc.terminate()


COLORS = {"step": "\033[36m", "question": "\033[33m", "note": "\033[35m", "error": "\033[31m", "check": "\033[32m"}
RESET = "\033[0m"


def cmd_run(args) -> None:
    from .agent.state import Pending
    from .config import Settings
    from .service import RunManager

    port = int(os.environ.get("SANDBOX_PORT", "8100"))
    os.environ.setdefault("SANDBOX_URL", f"http://127.0.0.1:{port}")
    proc = start_sandbox(port)

    async def answer(p: Pending, state) -> str:
        if args.auto_approve and p.kind == "approval":
            return "approve"
        print(f"\n{COLORS['question']}?? {p.question}{RESET}")
        if p.options:
            print(f"   options: {', '.join(p.options)}")
        return await asyncio.get_running_loop().run_in_executor(None, lambda: input("   your answer> "))

    async def main():
        mgr = RunManager(Settings(), auto_answer=answer)
        h = mgr.create(args.task, args.dry_run)
        q = mgr.subscribe(h.state.id)

        async def printer():
            while True:
                ev = await q.get()
                t, d = ev["type"], ev["data"]
                c = COLORS.get(t, "")
                if t == "step":
                    mark = "ok " if d["ok"] else "ERR"
                    print(f"{c}[{d['n']:>2}] {mark} {d['tool']}{RESET} {d['reason'][:90]}\n      -> {d['summary'][:150]}")
                elif t == "contract":
                    con = d["contract"]
                    print(f"GOAL: {con['goal']}")
                    for it in con["checklist"]:
                        print(f"  - {it['text']}")
                elif t in ("note", "error"):
                    print(f"{c}!! {d['message']}{RESET}")
                elif t == "notify":
                    print(f"\033[1m>> message to you: {d['message']}{RESET}")
                elif t == "check":
                    print(f"{c}verify {d['criterion']}: {d['status']} {d['detail'][:140]}{RESET}")
                elif t == "phase":
                    print(f"--- {d['phase']} ---")

        pt = asyncio.create_task(printer())
        await mgr._run(h)
        await asyncio.sleep(0.2)
        pt.cancel()
        r = h.state.report or {}
        print("\n" + "=" * 70)
        print(f"{r.get('headline')}\n{r.get('summary')}")
        print(json.dumps({"result": r.get("result"), "verification": r.get("verification", {}).get("criteria"),
                          "stats": r.get("stats")}, indent=1))
        print(f"Trace and evidence: {h.workdir}")

    try:
        asyncio.run(main())
    finally:
        if proc:
            proc.terminate()


def main() -> None:
    ap = argparse.ArgumentParser(prog="worker")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    s.set_defaults(fn=cmd_serve)
    r = sub.add_parser("run")
    r.add_argument("task")
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--auto-approve", action="store_true", help="approve policy-gated actions without asking")
    r.set_defaults(fn=cmd_run)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
