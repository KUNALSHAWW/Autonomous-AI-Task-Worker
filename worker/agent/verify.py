"""Independent verification.

The worker's own "I'm done" is treated as a claim, not a fact. Three layers:

1. Source fidelity (double entry): every recorded fact is re-extracted from its
   raw source by a fresh model call that never sees the recorded value. Code
   compares the two after normalisation.
2. State probes: a fresh model call that does not see the worker's trace turns
   the success criteria into concrete read-only checks (API record lookups,
   page text checks). Code executes them against the live systems.
3. Side-effect ledger: code checks for duplicate or unresolved writes.

Only criteria that cannot be probed fall back to a read-only LLM judge, and its
evidence quote must literally appear in what it observed. Writes are blocked by
the gate for the whole verification phase.
"""
from __future__ import annotations

import json
import re

from ..llm.base import ToolSpec, coerce_args, validate_args
from ..tools.toolbox import READ_TOOLS, TOOL_SPECS
from . import normalize, prompts
from .state import StepRecord

VERDICT = ToolSpec("verdict", "Record your verdict for the criterion.", {
    "type": "object",
    "properties": {"passed": {"type": "boolean"}, "evidence": {"type": "string"}, "explanation": {"type": "string"}},
    "required": ["passed", "evidence"],
})


def _variants(value: str) -> list[str]:
    out = [str(value)]
    n = normalize.number(value)
    if n is not None:
        out += [f"{n:.2f}", f"{n:,.2f}", f"{n:g}"]
    for iso in normalize.dates(str(value)):
        from datetime import date
        d = date.fromisoformat(iso)
        out += [iso, d.strftime("%d/%m/%Y"), d.strftime("%m/%d/%Y"), d.strftime("%B %d, %Y"), d.strftime("%d %b %Y")]
    return out


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", str(s)).strip().lower()


class Verifier:
    def __init__(self, agent):
        self.a = agent
        self.s = agent.state

    async def run(self) -> dict:
        s = self.s
        claimed = s.claimed or {}
        if s.dry_run:
            return {"outcome": "dry_run", "criteria": [], "fidelity": [], "ledger": self.ledger(), "problems": [],
                    "summary": "Dry run: nothing was written, so there is nothing to verify."}
        self.a.gate.read_only = True
        try:
            fidelity = await self.fidelity()
            criteria = []
            if claimed.get("status") == "completed":
                criteria = await self.criteria()
            ledger = self.ledger()
        finally:
            self.a.gate.read_only = False

        problems = [f"fact '{f['key']}': recorded {f['recorded']!r} but the source says {f['source_value']!r}"
                    for f in fidelity if f["status"] == "mismatch"]
        problems += [f"criterion {c['id']} ({c['text']}) not met: {c['detail']}" for c in criteria if c["status"] == "fail"]
        problems += [p for p in ledger["problems"]]
        unknown = [c for c in criteria if c["status"] == "unknown"]

        status = claimed.get("status")
        if status != "completed":
            outcome = "needs_attention" if status == "blocked" else "failed"
            summary = f"The worker did not complete the task (status: {status or 'unknown'})."
        elif problems:
            outcome = "failed" if not any(c["status"] == "pass" for c in criteria) else "partially_verified"
            summary = "Independent verification found problems."
        elif unknown:
            outcome = "unverified"
            summary = "Some success criteria could not be checked automatically."
        else:
            outcome = "verified"
            summary = "All success criteria were independently confirmed against the live systems."
        return {"outcome": outcome, "summary": summary, "criteria": criteria, "fidelity": fidelity,
                "ledger": ledger, "problems": problems, "repairable": bool(problems)}

    # ------------------------------------------------------------------ 1. source fidelity
    async def fidelity(self) -> list[dict]:
        s = self.s
        needed = {f["key"]: f["description"] for f in s.contract.facts_needed}
        groups: dict[int, list] = {}
        for key, fact in s.facts.items():
            if key in needed or not needed:
                groups.setdefault(fact.obs_id, []).append(fact)
        out = []
        obs_by_id = {o.id: o for o in s.observations}
        for obs_id, facts in groups.items():
            obs = obs_by_id.get(obs_id)
            if not obs:
                continue
            fields = "\n".join(f"- {f.key}: {needed.get(f.key, '')}" for f in facts)
            try:
                data = await self.a.json_call(prompts.EXTRACT.format(fields=fields, document=obs.text[:12000]),
                                              "verify_extract")
            except Exception as e:
                out += [{"key": f.key, "recorded": f.value, "source_value": None, "status": "unknown",
                         "source": f.source, "detail": str(e)} for f in facts]
                continue
            for f in facts:
                got = data.get(f.key)
                if got in (None, "", "null"):
                    st = "unknown"
                else:
                    st = "match" if normalize.same(f.value, got) else "mismatch"
                out.append({"key": f.key, "recorded": f.value, "source_value": got, "status": st, "source": f.source,
                            "quote": f.quote})
        return out

    # ------------------------------------------------------------------ 2. criteria
    async def criteria(self) -> list[dict]:
        s = self.s
        crit = s.contract.success_criteria or [{"id": "s1", "text": s.contract.goal}]
        facts_txt = "\n".join(f"- {k} = {f.value}" for k, f in s.facts.items()) or "(none)"
        try:
            plan = await self.a.json_call(prompts.PROBES.format(
                environment=self.a.env_text, task=s.task, goal=s.contract.goal,
                criteria="\n".join(f"- {c['id']}: {c['text']}" for c in crit), facts=facts_txt), "verify_plan")
            checks = plan.get("checks") or []
        except Exception:
            checks = []
        by_crit: dict[str, list] = {c["id"]: [] for c in crit}
        for chk in checks:
            if isinstance(chk, dict) and chk.get("criterion") in by_crit:
                by_crit[chk["criterion"]].append(chk)
        results = []
        for c in crit:
            chks = by_crit[c["id"]] or [{"criterion": c["id"], "type": "judge", "instructions": c["text"]}]
            outcomes = []
            for chk in chks[:3]:
                outcomes.append(await self.run_check(chk, c))
            if any(o["status"] == "fail" for o in outcomes):
                st = "fail"
            elif outcomes and all(o["status"] == "pass" for o in outcomes):
                st = "pass"
            else:
                st = "unknown"
            detail = "; ".join(o["detail"] for o in outcomes if o["status"] != "pass")[:400] or "ok"
            results.append({"id": c["id"], "text": c["text"], "status": st, "detail": detail, "checks": outcomes})
            await self.a.emit("check", {"criterion": c["id"], "status": st, "detail": detail})
        return results

    def _subst(self, v):
        if isinstance(v, str):
            def rep(m):
                f = self.s.facts.get(m.group(1).strip())
                if f is None:
                    raise KeyError(m.group(1))
                return f.value
            return re.sub(r"\{\{\s*fact:([^}]+)\}\}", rep, v)
        if isinstance(v, list):
            return [self._subst(x) for x in v]
        if isinstance(v, dict):
            return {k: self._subst(x) for k, x in v.items()}
        return v

    async def run_check(self, chk: dict, crit: dict) -> dict:
        kind = chk.get("type")
        try:
            chk = self._subst(chk)
        except KeyError as e:
            return {"type": kind, "status": "unknown", "detail": f"check refers to unknown fact {e}"}
        try:
            if kind == "api_record":
                return await self.check_api(chk)
            if kind == "page_text":
                return await self.check_page(chk)
            if kind == "user_notified":
                msgs = self.s.messages_to_user
                return {"type": kind, "status": "pass" if msgs else "fail",
                        "detail": msgs[-1]["message"][:200] if msgs else "the user was never sent a message"}
            return await self.judge(crit, chk.get("instructions") or crit["text"])
        except Exception as e:
            return {"type": kind, "status": "unknown", "detail": f"check could not run: {type(e).__name__}: {e}"}

    async def check_api(self, chk: dict) -> dict:
        url = str(chk.get("url", ""))
        expect = chk.get("expect") or {}
        mode = chk.get("count") or "at_least_one"
        if not expect and mode != "none":
            return {"type": "api_record", "status": "unknown", "detail": "check has no expected values"}
        if self.a.gate.check_navigation(url).action == "deny":
            return {"type": "api_record", "status": "unknown", "detail": f"url not allowed: {url}"}
        headers = {k: self.a.env.vault.resolve(str(v), url) for k, v in (chk.get("headers") or {}).items()}
        if not headers:
            for app in self.a.env.apps:
                if "Bearer" in (app.get("api") or "") and url.startswith(app["url"].rstrip("/")):
                    m = re.search(r"Bearer (\{\{secret:\w+\}\})", app["api"])
                    if m:
                        headers = {"Authorization": "Bearer " + self.a.env.vault.resolve(m.group(1), url)}
        resp = await self.a.browser.context.request.get(url, headers=headers or None, timeout=15000,
                                                        fail_on_status_code=False)
        if resp.status >= 400:
            return {"type": "api_record", "status": "unknown", "detail": f"GET {url} returned HTTP {resp.status}"}
        data = json.loads(await resp.text())
        if isinstance(data, dict):
            lists = [v for v in data.values() if isinstance(v, list)]
            records = lists[0] if lists else [data]
        else:
            records = data if isinstance(data, list) else []

        def matches(rec: dict) -> bool:
            low = {str(k).lower(): v for k, v in rec.items()} if isinstance(rec, dict) else {}
            return all(str(k).lower() in low and normalize.same(low[str(k).lower()], v) for k, v in expect.items())

        hits = [r for r in records if matches(r)]
        n = len(hits)
        ok = {"exactly_one": n == 1, "at_least_one": n >= 1, "none": n == 0}.get(mode, n >= 1)
        if ok:
            detail = f"{n} matching record(s) at {url}: {json.dumps(hits[:2])[:300]}"
        elif n == 0:
            near = json.dumps(records[:3])[:300]
            detail = f"no record at {url} matches {json.dumps(expect)}; first records: {near}"
        else:
            detail = f"expected {mode} but found {n} matching records at {url} (possible duplicate)"
        return {"type": "api_record", "status": "pass" if ok else "fail", "detail": detail, "url": url}

    async def check_page(self, chk: dict) -> dict:
        url = str(chk.get("url", ""))
        res = await self.a.browser.goto(url)
        if not res.view:
            return {"type": "page_text", "status": "unknown", "detail": res.message}
        page = _norm(res.view.text)
        missing = [t for t in chk.get("contains") or [] if not any(_norm(v) in page for v in _variants(t))]
        ok = not missing
        return {"type": "page_text", "status": "pass" if ok else "fail", "url": url,
                "detail": "all expected text found" if ok else f"not found on {url}: {missing}"}

    async def judge(self, crit: dict, instructions: str, max_steps: int = 7) -> dict:
        a, s = self.a, self.s
        tools = [TOOL_SPECS[n] for n in READ_TOOLS] + [VERDICT]
        start_obs = len(s.observations)
        system = prompts.JUDGE_SYSTEM.format(environment=a.env_text)
        history, observation = [], "(nothing yet)"
        facts = json.dumps({k: f.value for k, f in s.facts.items()})
        for i in range(max_steps):
            turn = prompts.JUDGE_TURN.format(criterion=crit["text"], instructions=instructions, facts=facts,
                                             claimed=json.dumps(s.claimed or {})[:800],
                                             history="\n".join(history[-8:]) or "(none)", observation=observation)
            reply = await a.llm_call([{"role": "system", "content": system}, {"role": "user", "content": turn}],
                                     tools=tools, purpose="verify_judge", max_tokens=3000)
            call = reply.tool_call
            if call is None:
                observation = "ERROR: call a tool."
                continue
            if call.name in TOOL_SPECS or call.name == "verdict":
                spec = VERDICT if call.name == "verdict" else TOOL_SPECS[call.name]
                call.args = coerce_args(spec.parameters, call.args)
            if call.name == "verdict":
                if validate_args(VERDICT.parameters, call.args):
                    observation = "ERROR: verdict needs passed and evidence."
                    continue
                ev = _norm(call.args.get("evidence", ""))
                seen = [o for o in s.observations[start_obs:]]
                grounded = len(ev) >= 3 and any(ev in _norm(o.text) for o in seen)
                passed = bool(call.args.get("passed"))
                if passed and not grounded:
                    return {"type": "judge", "status": "unknown",
                            "detail": "judge said pass but its evidence quote was not found in what it observed"}
                return {"type": "judge", "status": "pass" if passed else "fail",
                        "detail": (call.args.get("explanation") or "")[:200] + f" | evidence: {call.args['evidence'][:200]}"}
            if call.name not in READ_TOOLS or validate_args(TOOL_SPECS[call.name].parameters, call.args):
                observation = f"ERROR: invalid tool call {call.name}."
                continue
            n = len(s.steps) + 1
            res = await a.tools.run(call.name, call.args, n)
            s.steps.append(StepRecord(n, call.name, a._public_args(call.args), str(call.args.get("reason", "")),
                                      res.ok, res.error_kind, res.output.split("\n")[0][:200], "", res.screenshot,
                                      phase="verify"))
            history.append(f"{call.name}({a._short_args(call.args)}) -> {res.output.splitlines()[0][:120]}")
            observation = res.output
        return {"type": "judge", "status": "unknown", "detail": "judge ran out of steps without a verdict"}

    # ------------------------------------------------------------------ 3. ledger
    def ledger(self) -> dict:
        writes = self.a.gate.writes
        warnings = []
        ok_fps: dict[str, list] = {}
        for w in writes:
            if w.outcome == "ok":
                ok_fps.setdefault(w.fingerprint, []).append(w)
        for fp, ws in ok_fps.items():
            if len(ws) > 1:
                warnings.append(f"the same submission to {ws[0].url} was accepted {len(ws)} times; state checks "
                                f"decide whether a duplicate exists")
        unresolved = [w for w in writes if w.outcome in ("server_error", "failed")
                      and not any(x.fingerprint == w.fingerprint and x.outcome == "ok" and x.id > w.id for x in writes)]
        return {
            "writes": [{"id": w.id, "step": w.step, "method": w.method, "url": w.url, "status": w.status,
                        "outcome": w.outcome, "fields": {k: v for k, v in w.fields.items()
                                                         if "password" not in str(k).lower()}} for w in writes],
            "unresolved_failures": len(unresolved),
            "warnings": warnings,
            "problems": [],
        }
