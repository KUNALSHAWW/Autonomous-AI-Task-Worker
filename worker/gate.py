"""Hard guardrails that sit between the agent and the outside world.

Every state-changing request (browser form posts and API calls alike) passes
through `Gate.check_write`. The model cannot argue its way past it: rules are
evaluated in code against the actual request payload. The gate also keeps the
side-effect ledger used for duplicate protection and for the final report.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import re
from dataclasses import dataclass, field
import posixpath
from urllib.parse import unquote, urlparse, urlunparse

from .environment import Environment, origin

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


@dataclass
class Decision:
    action: str  # allow | deny | approval | dry_run
    message: str = ""
    rule: dict | None = None
    fingerprint: str = ""


@dataclass
class Write:
    id: int
    step: int
    method: str
    url: str
    fields: dict
    fingerprint: str
    status: int | None = None
    outcome: str = "pending"  # pending | ok | client_error | server_error | blocked | failed
    seq: int = 0              # position in the gate's event order (reads and writes)


def _num(v) -> float | None:
    if v is None:
        return None
    s = re.sub(r"[^\d.\-]", "", str(v).replace(",", ""))
    try:
        return float(s)
    except ValueError:
        return None


def _norm_fields(fields: dict) -> dict:
    return {str(k).strip().lower(): (str(v).strip() if not isinstance(v, (dict, list)) else json.dumps(v, sort_keys=True))
            for k, v in fields.items()}


def canonical(url: str) -> str:
    """Decode and normalise a URL the way the server will see it, so rules can't be dodged with
    percent-encoding (/app/%61dmin), doubled slashes or dot segments."""
    p = urlparse(url)
    path = p.path or "/"
    for _ in range(3):  # double-encoded paths
        dec = unquote(path)
        if dec == path:
            break
        path = dec
    trailing = path.endswith("/")
    path = re.sub(r"/{2,}", "/", path)
    path = posixpath.normpath(path) if path not in ("", "/") else "/"
    if trailing and not path.endswith("/"):
        path += "/"
    return urlunparse((p.scheme.lower(), p.netloc.lower(), path, p.params, p.query, ""))


def fingerprint(method: str, url: str, fields: dict) -> str:
    p = urlparse(canonical(url))
    payload = json.dumps(_norm_fields(fields), sort_keys=True)
    return hashlib.sha1(f"{method.upper()} {p.path} {payload}".encode()).hexdigest()[:16]


class Gate:
    def __init__(self, env: Environment, dry_run: bool = False):
        self.env = env
        self.dry_run = dry_run
        self.read_only = False
        self.info_only = False  # set when intake decides the task only asks for information
        self.grants: list[tuple[str, str]] = []  # (rule id, approval key)
        self.denials: list[tuple[str, str]] = []
        self.writes: list[Write] = []
        self.blocked: list[dict] = []
        self.last_read_at: dict[str, int] = {}  # app path prefix -> event seq of last read
        self.step = 0
        self._seq = 0

    def _tick(self) -> int:
        self._seq += 1
        return self._seq

    # ------------------------------------------------------------------ navigation rules
    def check_navigation(self, url: str) -> Decision:
        if url.startswith(("data:", "about:", "blob:")):
            return Decision("allow")
        url = canonical(url)
        o = origin(url)
        if self.env.allowed_origins and o not in self.env.allowed_origins:
            return Decision("deny", f"Navigation outside the allowed systems is blocked ({o}).")
        path = urlparse(url).path
        for d in self.env.denied_paths:
            if path == d.rstrip("/") or path.startswith(d.rstrip("/") + "/"):
                return Decision("deny", f"Access to {d} is not permitted for the assistant.")
        return Decision("allow")

    def note_read(self, url: str) -> None:
        self.last_read_at[self._app_key(canonical(url))] = self._tick()

    def _app_key(self, url: str) -> str:
        """Which application a URL belongs to: the manifest app with the longest matching URL prefix."""
        best = ""
        for app in self.env.apps:
            base = app.get("url", "").rstrip("/")
            if base and url.startswith(base) and len(base) > len(best):
                best = base
        return best or origin(url)

    # ------------------------------------------------------------------ writes
    def _rule_applies(self, rule: dict, method: str, url: str, fields: dict) -> tuple[bool, str]:
        m = rule.get("match", {})
        if m.get("methods") and method.upper() not in [x.upper() for x in m["methods"]]:
            return False, ""
        if m.get("url") and not fnmatch.fnmatch(canonical(url), m["url"]):
            return False, ""
        cond = rule.get("when")
        if not cond:
            return True, ""
        pat = re.compile(cond["field"], re.I)
        hits = {k: v for k, v in fields.items() if pat.search(str(k))}
        if "present" in cond:
            present = any(str(v).strip() for v in hits.values())
            return present == bool(cond["present"]), ";".join(f"{k}={v}" for k, v in hits.items())
        if "gte" in cond:
            for k, v in hits.items():
                n = _num(v)
                if n is not None and n >= float(cond["gte"]):
                    return True, f"{k}={v}"
            return False, ""
        if "not_contains_only" in cond:
            dom = cond["not_contains_only"].lower()
            for k, v in hits.items():
                addrs = [a.strip().lower() for a in re.split(r"[;,\s]+", str(v)) if "@" in a]
                outside = [a for a in addrs if not a.endswith(dom)]
                if outside:
                    return True, f"{k}={', '.join(outside)}"
            return False, ""
        return True, ""

    def is_sign_in(self, fields: dict) -> bool:
        """A request that carries a vault credential is a sign-in, not a change to business data."""
        secrets = {s["value"] for s in self.env.vault.secrets.values() if s.get("value")}
        return any(str(v) in secrets for v in fields.values()) and len(fields) <= 6

    def check_write(self, method: str, url: str, fields: dict) -> Decision:
        url = canonical(url)
        nav = self.check_navigation(url)
        if nav.action == "deny":
            return nav
        if self.is_sign_in(fields):
            return Decision("allow", "sign-in", fingerprint="sign-in")
        fp = fingerprint(method, url, fields)
        if self.read_only:
            return Decision("deny", "Read-only mode: state-changing requests are not allowed during verification.",
                            fingerprint=fp)
        # Duplicate protection: an identical write needs a fresh look at the app first.
        prior = [w for w in self.writes if w.fingerprint == fp and w.outcome != "blocked"]
        if prior:
            last = prior[-1]
            looked = self.last_read_at.get(self._app_key(url), 0) > last.seq
            if last.outcome == "ok" and not looked:
                return Decision("deny", (
                    f"Duplicate guard: this exact submission already succeeded at step {last.step} "
                    f"(HTTP {last.status}). Repeating it could create a duplicate record. Look at the current state "
                    f"of the system first; if the record is really missing you may submit again."), fingerprint=fp)
            if last.outcome in ("server_error", "failed", "pending") and not looked:
                return Decision("deny", (
                    f"Duplicate guard: this exact submission was sent at step {last.step} and the result was "
                    f"ambiguous ({last.outcome}{', HTTP ' + str(last.status) if last.status else ''}). It may have "
                    f"been applied anyway. Check the system for the record before retrying."), fingerprint=fp)
        for rule in self.env.policy:
            applies, detail = self._rule_applies(rule, method, url, fields)
            if not applies or rule.get("action") != "require_approval":
                continue
            key = f"{rule['id']}:{detail}"
            if (rule["id"], key) in self.denials:
                return Decision("deny", f"The user declined this action ({rule['id']}). Do not attempt it again.",
                                rule, fp)
            if (rule["id"], key) not in self.grants:
                return Decision("approval", f"Policy '{rule['id']}': {rule.get('description', '')} [{detail}]",
                                {**rule, "_key": key}, fp)
        if self.info_only:
            rule = {"id": "information_only_task", "description": "The task was understood as a question, but this "
                    "action would change data.", "_key": f"information_only_task:{fp}"}
            if ("information_only_task", rule["_key"]) in self.denials:
                return Decision("deny", "The user declined this change. Do not attempt it again.", rule, fp)
            if ("information_only_task", rule["_key"]) not in self.grants:
                return Decision("approval", "Information-only task: this action would change data.", rule, fp)
        if self.dry_run:
            return Decision("dry_run", "Dry run: the request was recorded but not sent.", fingerprint=fp)
        return Decision("allow", fingerprint=fp)

    def grant(self, rule_id: str, key: str) -> None:
        self.grants.append((rule_id, key))

    def deny(self, rule_id: str, key: str) -> None:
        self.denials.append((rule_id, key))

    def record_write(self, method: str, url: str, fields: dict, fp: str) -> Write:
        scrub = self.env.vault.scrub
        clean = {scrub(str(k)): scrub(str(v)) for k, v in fields.items()}
        w = Write(len(self.writes) + 1, self.step, method.upper(), scrub(url), clean, fp, seq=self._tick())
        self.writes.append(w)
        return w

    @staticmethod
    def classify(status: int | None) -> str:
        if status is None:
            return "failed"
        if status < 400:
            return "ok"
        if status >= 500:
            return "server_error"
        return "client_error"
