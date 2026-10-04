"""HTTP API for the worker.

    POST /api/runs                  start a run {task, dry_run}
    GET  /api/runs                  list runs
    GET  /api/runs/{id}             full state + report
    GET  /api/runs/{id}/events      server-sent events (live), ?after=<seq> to resume
    POST /api/runs/{id}/reply       answer a pending question / approval {answer}
    POST /api/runs/{id}/cancel      stop a run
    GET  /api/runs/{id}/files/...   screenshots and downloaded files (evidence)
    /acme/...                       reverse proxy to the sandbox company apps

The sandbox is proxied so a single public port can show both the worker and the
systems it operates.
"""
from __future__ import annotations

import asyncio
import json
import os

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import Settings
from .service import RunManager

settings = Settings()
manager = RunManager(settings)
app = FastAPI(title="Autonomous AI Task Worker", version="1.0",
              description="Give it a task in plain language; it works through real apps and proves the result.")

EXAMPLES = [
    "Find the latest invoice from {email_vendor}, extract the amount and due date, enter it into our ERP and tell me once it is done.",
    "{portal_vendor} stopped emailing invoices. Get their newest invoice from their portal and record it in the ERP.",
    "Enter the newest {big_vendor} invoice into the ERP.",
    "We got a registration form from a new supplier, {new_vendor}. Set them up as a vendor in the ERP.",
    "Go through the AP inbox and deal with anything urgent about vendor bank details.",
    "Record the latest invoice from Northwind in the ERP.",
    "Which of our open bills are due in the next 14 days? Email the list to the finance manager.",
]


class RunIn(BaseModel):
    task: str = Field(..., min_length=3, max_length=4000)
    dry_run: bool = False


class ReplyIn(BaseModel):
    answer: str = Field(..., min_length=1, max_length=2000)


WEB_DIR = os.path.join(os.path.dirname(__file__), "web")
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


@app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
def index():
    with open(os.path.join(WEB_DIR, "index.html")) as f:
        return HTMLResponse(f.read())


@app.get("/api/health")
async def health():
    ok = True
    try:
        async with httpx.AsyncClient(timeout=3) as c:
            ok = (await c.get(f"{settings.sandbox_url}/healthz")).status_code == 200
    except httpx.HTTPError:
        ok = False
    base, key, model = settings.resolved()
    return {"ok": True, "sandbox": ok, "provider": settings.provider, "model": model,
            "llm_configured": settings.llm_configured}


@app.get("/api/examples")
async def examples():
    try:
        async with httpx.AsyncClient(timeout=3) as c:
            truth = (await c.get(f"{settings.sandbox_url}/acme/admin/state",
                                 headers={"x-admin-token": os.environ.get("SANDBOX_ADMIN_TOKEN", "")})).json()["truth"]
        v = truth["vendors"]
        names = {"email_vendor": v["email"], "portal_vendor": v["portal"], "big_vendor": v["big"], "new_vendor": v["new"]}
    except Exception:
        names = {"email_vendor": "Globex Corporation", "portal_vendor": "a supplier", "big_vendor": "a supplier",
                 "new_vendor": "a new supplier"}
    return [e.format(**names) for e in EXAMPLES]


CHAOS_HELP = {
    "transient_503": "First save fails with 503 and is not stored",
    "ambiguous_502": "First save is stored but answers 502",
    "lying_ui": "First save shows success but stores nothing",
    "modal": "A what's-new dialog blocks the ERP",
    "session_expiry": "ERP sign-in expires mid task",
    "slow": "ERP pages respond slowly",
}
APPS = [{"name": "Mail", "path": "/acme/mail/"}, {"name": "ERP", "path": "/acme/erp/"},
        {"name": "Supplier portal", "path": "/acme/portal/"}, {"name": "Drive", "path": "/acme/drive/"}]


def _admin_headers() -> dict:
    return {"x-admin-token": os.environ.get("SANDBOX_ADMIN_TOKEN", "")}


@app.get("/api/sandbox")
async def sandbox_info():
    """World settings for the demo controls. Ground truth is deliberately not included."""
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            st = (await c.get(f"{settings.sandbox_url}/acme/admin/state", headers=_admin_headers())).json()
        chaos, seed = st.get("chaos") or {}, st["truth"]["seed"]
    except Exception:
        chaos, seed = {}, None
    return {"seed": seed, "chaos": {k: bool(chaos.get(k)) for k in CHAOS_HELP}, "chaos_help": CHAOS_HELP, "apps": APPS}


class ResetIn(BaseModel):
    seed: int = Field(7, ge=0, le=1_000_000)
    chaos: dict[str, bool] = {}


@app.post("/api/sandbox/reset")
async def sandbox_reset(body: ResetIn):
    if any(h.task and not h.task.done() for h in manager.runs.values()):
        raise HTTPException(409, "a task is running; wait for it to finish before rebuilding the company")
    chaos = {k: (1 if k in ("transient_503", "ambiguous_502", "lying_ui") else True)
             for k, on in body.chaos.items() if on and k in CHAOS_HELP}
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(f"{settings.sandbox_url}/acme/admin/reset", json={"seed": body.seed, "chaos": chaos},
                         headers=_admin_headers())
    if r.status_code != 200:
        raise HTTPException(502, "could not rebuild the sandbox")
    return {"ok": True, "seed": body.seed, "chaos": {k: k in chaos for k in CHAOS_HELP}}


@app.get("/api/graph")
async def graph_diagram():
    """The LangGraph structure of the worker, as Mermaid."""
    from langgraph.graph import END, START, StateGraph

    from .agent.engine import GraphState
    g = StateGraph(GraphState)
    for n in ("intake", "act", "human", "verify", "report"):
        g.add_node(n, lambda st: st)
    g.add_edge(START, "intake")
    g.add_conditional_edges("intake", lambda st: st["route"], {"act": "act", "human": "human"})
    g.add_conditional_edges("act", lambda st: st["route"], {"act": "act", "human": "human", "verify": "verify"})
    g.add_conditional_edges("human", lambda st: st["route"], {"act": "act", "human": "human"})
    g.add_conditional_edges("verify", lambda st: st["route"], {"act": "act", "report": "report"})
    g.add_edge("report", END)
    return {"mermaid": g.compile().get_graph().draw_mermaid()}


@app.post("/api/runs", status_code=201)
async def create_run(body: RunIn):
    h = manager.start(manager.create(body.task, body.dry_run))
    return manager.summary(h)


@app.get("/api/runs")
async def list_runs():
    live = [manager.summary(h) for h in manager.runs.values()]
    return sorted(live + manager.load_past_runs(), key=lambda r: r["created_at"], reverse=True)


def _handle(run_id: str):
    h = manager.runs.get(run_id)
    if not h:
        raise HTTPException(404, "run not found")
    return h


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str):
    h = manager.runs.get(run_id)
    if h:
        return {**manager.summary(h), "state": h.state.to_dict(), "report": h.state.report}
    p = os.path.join(settings.runs_dir, os.path.basename(run_id), "state.json")
    if os.path.exists(p):
        with open(p) as f:
            d = json.load(f)
        return {"id": d["id"], "status": d["status"], "outcome": d["outcome"], "state": d, "report": d.get("report")}
    raise HTTPException(404, "run not found")


@app.get("/api/runs/{run_id}/events")
async def run_events(run_id: str, request: Request, after: int = 0):
    h = manager.runs.get(run_id)
    if h is None:
        # A run from an earlier server process: replay its event log from disk.
        path = os.path.join(settings.runs_dir, os.path.basename(run_id), "events.jsonl")
        if not os.path.exists(path):
            raise HTTPException(404, "run not found")

        async def replay():
            with open(path) as f:
                for line in f:
                    ev = json.loads(line)
                    if ev["seq"] > after:
                        yield f"id: {ev['seq']}\nevent: {ev['type']}\ndata: {line.strip()}\n\n"
            yield "event: end\ndata: {}\n\n"

        return StreamingResponse(replay(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    async def stream():
        q = manager.subscribe(run_id)
        try:
            last = after
            for ev in list(h.events):
                if ev["seq"] > last:
                    last = ev["seq"]
                    yield f"id: {ev['seq']}\nevent: {ev['type']}\ndata: {json.dumps(ev, default=str)}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                if h.task and h.task.done() and q.empty():
                    yield "event: end\ndata: {}\n\n"
                    break
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                if ev["seq"] <= last:
                    continue
                last = ev["seq"]
                yield f"id: {ev['seq']}\nevent: {ev['type']}\ndata: {json.dumps(ev, default=str)}\n\n"
        finally:
            manager.unsubscribe(run_id, q)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/runs/{run_id}/reply")
async def reply(run_id: str, body: ReplyIn):
    _handle(run_id)
    if not manager.reply(run_id, body.answer):
        raise HTTPException(409, "this run is not waiting for an answer")
    return {"ok": True}


@app.post("/api/runs/{run_id}/cancel")
async def cancel(run_id: str):
    _handle(run_id)
    return {"ok": manager.cancel(run_id)}


@app.get("/api/runs/{run_id}/files/{path:path}")
async def run_file(run_id: str, path: str):
    base = os.path.realpath(os.path.join(settings.runs_dir, os.path.basename(run_id)))
    full = os.path.realpath(os.path.join(base, path))
    if not full.startswith(base + os.sep) or not os.path.isfile(full) or os.path.basename(full) == "state.json":
        raise HTTPException(404, "file not found")
    return FileResponse(full)


@app.get("/healthz")
def healthz():
    return {"ok": True}


# ---------------------------------------------------------------------- sandbox
# EMBED_SANDBOX=1 serves the company apps from this process (saves memory on small
# hosts). Otherwise the sandbox is a separate process and /acme is reverse proxied.
EMBED_SANDBOX = os.environ.get("EMBED_SANDBOX", "0") == "1"
if EMBED_SANDBOX:
    import secrets as _secrets
    # The company apps share this origin, so the admin page must never be open by default.
    os.environ.setdefault("SANDBOX_ADMIN_TOKEN", _secrets.token_hex(12))
    from sandbox import app as sandbox_app

    app.include_router(sandbox_app.r)

    @app.on_event("startup")
    def _seed_sandbox():
        sandbox_app._startup()

_proxy = httpx.AsyncClient(base_url=settings.sandbox_url, timeout=30, follow_redirects=False)
HOP = {"connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade", "content-length",
       "content-encoding", "host"}


async def sandbox_proxy(path: str, request: Request):
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP}
    try:
        r = await _proxy.request(request.method, f"/acme/{path}", params=request.query_params,
                                 content=await request.body(), headers=headers)
    except httpx.HTTPError:
        return JSONResponse({"error": "sandbox is not reachable"}, status_code=502)
    out = Response(r.content, status_code=r.status_code,
                   headers={k: v for k, v in r.headers.items() if k.lower() not in HOP and k.lower() != "set-cookie"})
    for c in r.headers.get_list("set-cookie"):
        out.headers.append("set-cookie", c)
    return out


if not EMBED_SANDBOX:
    app.add_api_route("/acme/{path:path}", sandbox_proxy, methods=["GET", "POST"], include_in_schema=False)
