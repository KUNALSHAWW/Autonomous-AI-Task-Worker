"""A deterministic stand-in for the model, used to test the real loop end to end.

It reads the same prompts a real model would see (page views, files, notes)
and reacts to them. It is intentionally simple: the point is to exercise the
browser, gate, memory grounding, verification and repair machinery for real,
not to be clever.
"""
from __future__ import annotations

import json
import re

ELEM = re.compile(r"^\*?\[(\d+)\] (\w+) \"([^\"]*)\"(.*)$", re.M)


def latest_obs(messages) -> str:
    txt = messages[-1]["content"]
    return txt.split("LATEST OBSERVATION (result of your last action):", 1)[-1]


def elements(obs: str):
    return [(m.group(1), m.group(2), m.group(3), m.group(4)) for m in ELEM.finditer(obs)]


def find(obs: str, kind: str, pattern: str):
    for n, k, label, rest in elements(obs):
        if k == kind and re.search(pattern, label, re.I):
            return n, label, rest
    for line in obs.splitlines():  # links inside table rows
        m = re.search(r"\[(\d+)\] " + kind + r" \"([^\"]*)\"", line)
        if m and re.search(pattern, m.group(2), re.I):
            return m.group(1), m.group(2), line
    return None


class InvoicePolicy:
    """Find the latest emailed invoice for a vendor, enter it, notify, finish."""

    def __init__(self, vendor: str, base: str = "http://127.0.0.1:8100"):
        self.vendor = vendor
        self.base = base.rstrip("/")
        self.stage = "start"
        self.fmt = None
        self.values: dict = {}

    def __call__(self, purpose, messages, tools):
        if purpose == "intake":
            return {
                "goal": f"Latest invoice from {self.vendor} is recorded in the system and the user is told.",
                "deliverables": ["record created", "user notified"],
                "facts_needed": [{"key": "invoice_number", "description": "supplier invoice number"},
                                 {"key": "amount", "description": "total amount payable incl. tax"},
                                 {"key": "invoice_date", "description": "invoice issue date"},
                                 {"key": "due_date", "description": "payment due date"}],
                "success_criteria": [{"id": "s1", "text": "exactly one record with the invoice number, amount and due date exists"},
                                     {"id": "s2", "text": "user was notified"}],
                "checklist": ["find latest invoice", "extract values", "enter record", "confirm", "notify user"],
                "assumptions": [], "constraints": [], "blocking_questions": [], "read_only": False,
            }
        if purpose == "verify_extract":
            doc = messages[-1]["content"]
            return self.parse_doc(doc)
        if purpose == "verify_plan":
            return {"checks": [
                {"criterion": "s1", "type": "api_record",
                 "url": self.base + "/acme/erp/api/bills?invoice_number={{fact:invoice_number}}",
                 "expect": {"invoice_number": "{{fact:invoice_number}}", "amount": "{{fact:amount}}",
                            "due_date": "{{fact:due_date}}"}, "count": "exactly_one"},
                {"criterion": "s2", "type": "user_notified"}]}
        if purpose == "act":
            return self.act(messages)
        raise AssertionError(f"unexpected purpose {purpose}")

    @staticmethod
    def parse_doc(doc: str) -> dict:
        from worker.agent.normalize import dates
        out = {}
        m = re.search(r"(?:Invoice No\.:?|Reference:|Document number:)\s*(\S+)", doc)
        out["invoice_number"] = m.group(1) if m else None
        m = re.search(r"(?:Total Due|Amount payable|Balance due)\s*\$?([\d,]+\.\d\d)", doc)
        out["amount"] = m.group(1).replace(",", "") if m else None
        m = re.search(r"(?:Invoice Date:?|Issued:|Date of issue:)\s*(\d{4}-\d{2}-\d{2}|[A-Za-z0-9 ,]+?\d{4})", doc)
        out["invoice_date"] = sorted(dates(m.group(1).strip()))[0] if m else None
        m = re.search(r"(?:Due Date:?|payable by|no later than)\s*(\d{4}-\d{2}-\d{2}|[A-Za-z0-9 ,]+?\d{4})", doc)
        out["due_date"] = sorted(dates(m.group(1).strip()))[0] if m else None
        return out

    def act(self, messages):
        obs = latest_obs(messages)
        prompt = messages[-1]["content"]
        if "verification FAILED" in prompt and self.stage == "finished":
            self.stage = "repair_check"
            return {"tool": "browser_goto", "args": {"reason": "check what is actually stored",
                                                    "url": f"/acme/erp/bills?q={self.values['invoice_number']}"}}
        st = self.stage
        if st == "start":
            self.stage = "search"
            return {"tool": "browser_goto", "args": {"reason": "search mailbox", "url": f"{self.base}/acme/mail/?q={self.vendor}"}}
        if st == "search":
            hit = find(obs, "link", r"^(?!.*reminder).*(invoice|billing document)")
            self.stage = "open_mail"
            return {"tool": "browser_click", "args": {"reason": "open newest invoice email", "element": hit[0]}}
        if st == "open_mail":
            hit = find(obs, "link", r"\.pdf$")
            self.stage = "downloaded"
            return {"tool": "browser_click", "args": {"reason": "download attachment", "element": hit[0]}}
        if st == "downloaded":
            name = re.search(r"Downloaded: (files/\S+\.pdf)", obs).group(1)
            self.stage = "read"
            return {"tool": "read_file", "args": {"reason": "read invoice", "path": name}}
        if st == "read":
            self.values = self.parse_doc(obs)
            self.doc = obs
            line = next(l for l in obs.splitlines() if re.search(r"Total Due|Amount payable|Balance due", l))
            self.stage = "remember_dates"
            return {"tool": "remember", "args": {"reason": "save amount", "source": "invoice pdf", "quote": line,
                                                 "facts": {"amount": self.values["amount"],
                                                           "invoice_number": self.values["invoice_number"]}}}
        if st == "remember_dates":
            self.stage = "remember_inv"
            line = next(l for l in self.doc.splitlines() if re.search(r"Due Date|payable by|no later than", l))
            return {"tool": "remember", "args": {"reason": "save dates", "source": "invoice pdf", "quote": line,
                                                 "facts": {"due_date": self.values["due_date"],
                                                           "invoice_date": self.values["invoice_date"]}}}
        if st == "remember_inv":
            self.stage = "check_existing"
            return {"tool": "browser_goto", "args": {"reason": "check for duplicates",
                                                    "url": f"/acme/erp/bills?q={self.values['invoice_number']}"}}
        if st in ("check_existing", "repair_check"):
            if find(obs, "password", "Password"):
                return {"tool": "browser_fill_form", "args": {"reason": "sign in", "submit": True, "fields": {
                    "Username": "{{secret:erp_username}}", "Password": "{{secret:erp_password}}"}}}
            if find(obs, "button", "Got it"):
                return {"tool": "browser_click", "args": {"reason": "dismiss dialog", "element": find(obs, "button", "Got it")[0]}}
            self.stage = "open_form"
            return {"tool": "browser_goto", "args": {"reason": "open entry form", "url": "/acme/erp/bills/new"}}
        if st == "open_form":
            if find(obs, "password", "Password"):
                return {"tool": "browser_fill_form", "args": {"reason": "sign in", "submit": True, "fields": {
                    "Username": "{{secret:erp_username}}", "Password": "{{secret:erp_password}}"}}}
            if find(obs, "button", "Got it"):
                return {"tool": "browser_click", "args": {"reason": "dismiss dialog", "element": find(obs, "button", "Got it")[0]}}
            fields = {}
            fmt = "YYYY-MM-DD"
            for n, kind, label, rest in elements(obs):
                lab = label.lower()
                m = re.search(r"(DD/MM/YYYY|MM/DD/YYYY|YYYY-MM-DD)", label)
                if m:
                    fmt = m.group(1)
                if kind == "select" and lab.startswith("vendor"):
                    fields[n] = self.vendor
                elif kind == "textbox" and re.search(r"amount|total", lab):
                    fields[n] = self.values["amount"]
                elif kind == "textbox" and re.search(r"invoice number|invoice #|reference", lab):
                    fields[n] = self.values["invoice_number"]
            for n, kind, label, rest in elements(obs):
                lab = label.lower()
                if kind == "textbox" and re.search(r"due", lab):
                    fields[n] = self.fmt_date(self.values["due_date"], fmt)
                elif kind == "textbox" and re.search(r"invoice date|bill date|document date", lab):
                    fields[n] = self.fmt_date(self.values["invoice_date"], fmt)
            self.stage = "submitted"
            return {"tool": "browser_fill_form", "args": {"reason": "fill and submit", "fields": fields, "submit": True}}
        if st == "submitted" and "user APPROVED" in obs:
            btn = find(obs, "button", "Save")
            return {"tool": "browser_click", "args": {"reason": "approved: submit again", "element": btn[0]}}
        if st == "submitted" and "user DENIED" in obs:
            self.stage = "finished"
            return {"tool": "finish", "args": {"reason": "denied", "status": "blocked",
                                               "summary": "Approval was denied, nothing was entered."}}
        if st == "submitted":
            if "HTTP status 5" in obs or "HTTP 50" in obs:
                self.stage = "check_existing"
                return {"tool": "browser_goto", "args": {"reason": "server error: check if it was saved",
                                                        "url": f"/acme/erp/bills?q={self.values['invoice_number']}"}}
            self.stage = "notify"
            return {"tool": "update_plan", "args": {"reason": "record entered", "done": ["c1", "c2", "c3", "c4"]}}
        if st == "notify":
            self.stage = "finish"
            return {"tool": "notify_user", "args": {"reason": "user asked to be told",
                                                   "message": f"Entered invoice {self.values['invoice_number']}."}}
        if st in ("finish", "finished"):
            self.stage = "finished"
            return {"tool": "finish", "args": {"reason": "done", "status": "completed", "summary": "Entered it.",
                                               "result": self.values}}
        raise AssertionError(st)

    @staticmethod
    def fmt_date(iso: str, fmt: str) -> str:
        y, m, d = iso.split("-")
        return {"YYYY-MM-DD": iso, "DD/MM/YYYY": f"{d}/{m}/{y}", "MM/DD/YYYY": f"{m}/{d}/{y}"}[fmt]


def latest_obs_all(messages):
    return latest_obs(messages).splitlines()
