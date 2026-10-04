"""Loads the environment manifest (apps, secrets, network rules, policy).

The manifest is data, not code. It is the only place where knowledge about a
particular company lives, which is what lets the agent stay generic.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import urlparse

import yaml

SECRET_RE = re.compile(r"\{\{secret:([a-zA-Z0-9_]+)\}\}")


def _subst(obj, env: dict):
    if isinstance(obj, str):
        return re.sub(r"\$\{([A-Z0-9_]+)\}", lambda m: env.get(m.group(1), m.group(0)), obj)
    if isinstance(obj, list):
        return [_subst(x, env) for x in obj]
    if isinstance(obj, dict):
        return {k: _subst(v, env) for k, v in obj.items()}
    return obj


def origin(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


@dataclass
class Vault:
    """Holds secrets. Values never enter prompts, observations or logs."""
    secrets: dict = field(default_factory=dict)  # name -> {"value": str, "origins": [..]}

    def resolve(self, text: str, url: str) -> str:
        """Replace {{secret:x}} placeholders with values if `url` is an allowed origin."""
        def rep(m):
            s = self.secrets.get(m.group(1))
            if not s:
                raise KeyError(f"unknown secret '{m.group(1)}'")
            if url and s["origins"] and origin(url) not in s["origins"]:
                raise PermissionError(f"secret '{m.group(1)}' may not be used on {origin(url)}")
            return s["value"]
        return SECRET_RE.sub(rep, text)

    def scrub(self, text: str) -> str:
        for name, s in self.secrets.items():
            if s["value"] and len(s["value"]) >= 4:
                text = text.replace(s["value"], f"{{{{secret:{name}}}}}")
        return text


@dataclass
class Environment:
    name: str
    about: str
    user: dict
    apps: list[dict]
    vault: Vault
    allowed_origins: list[str]
    denied_paths: list[str]
    policy: list[dict]

    def auth_headers(self, url: str) -> dict:
        """Default credentials for an app's API, taken from the manifest's `api` notes
        (`Authorization: Bearer {{secret:x}}`). Lets API calls work without the model
        having to remember the header, and without it ever seeing the token."""
        best = None
        for app in self.apps:
            base = app.get("url", "").rstrip("/")
            if base and url.startswith(base) and (best is None or len(base) > len(best["url"].rstrip("/"))):
                best = app
        if not best:
            return {}
        m = re.search(r"Authorization:\s*Bearer\s+(\{\{secret:\w+\}\})", best.get("api") or "")
        if not m:
            return {}
        try:
            return {"Authorization": "Bearer " + self.vault.resolve(m.group(1), url)}
        except (KeyError, PermissionError):
            return {}

    def describe(self) -> str:
        """Prompt-ready description (placeholders only, no secret values)."""
        out = [f"Environment: {self.name}", self.about.strip(), f"You act on behalf of: {self.user.get('role', '')}", "",
               "Available applications:"]
        for a in self.apps:
            out.append(f"- {a['name']}: {a['url']}\n  {a.get('about', '').strip()}")
            if a.get("login"):
                creds = ", ".join(f"{k}={v}" for k, v in a["login"].items())
                out.append(f"  Login: {creds}  (type these placeholders exactly; they are filled in securely)")
            if a.get("api"):
                api = "\n".join("    " + line for line in a["api"].strip().splitlines())
                out.append(f"  API:\n{api}")
        if self.policy:
            out.append("\nHard policy rules enforced by the system (blocked actions need human approval):")
            for rule in self.policy:
                out.append(f"- {rule['id']}: {rule.get('description', '')}")
        return "\n".join(out)


def load_environment(path: str, sandbox_url: str, today: date | None = None) -> Environment:
    with open(path) as f:
        raw = yaml.safe_load(f)
    env = dict(os.environ)
    env["SANDBOX_URL"] = sandbox_url.rstrip("/")
    raw = _subst(raw, env)
    about = raw.get("about", "").replace("{today}", (today or date.today()).strftime("%A, %d %B %Y"))
    vault = Vault({
        name: {"value": os.environ.get(spec.get("env", ""), spec.get("default", "")),
               "origins": [o.rstrip("/") for o in spec.get("origins", [])]}
        for name, spec in (raw.get("secrets") or {}).items()
    })
    net = raw.get("network") or {}
    return Environment(
        name=raw.get("name", "environment"), about=about, user=raw.get("user") or {}, apps=raw.get("apps") or [],
        vault=vault, allowed_origins=[o.rstrip("/") for o in net.get("allowed_origins", [])],
        denied_paths=net.get("denied_paths", []), policy=raw.get("policy") or [],
    )
