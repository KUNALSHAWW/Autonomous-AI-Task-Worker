"""Regression tests for issues found in an adversarial review: policy bypasses, secret leaks,
self-grounding and silent mis-selection. Each one runs against the live sandbox."""
import tempfile

from conftest import reset, state
from worker.agent.engine import is_approval
from worker.agent.state import RunState
from worker.config import Settings
from worker.environment import load_environment
from worker.gate import Gate, canonical
from worker.tools.browser import BrowserSession
from worker.tools.toolbox import Toolbox


def test_canonical_url_defeats_encoding_tricks():
    assert canonical("http://h/acme/%61dmin/state") == "http://h/acme/admin/state"
    assert canonical("http://h/acme//erp/./x/../api/%2562ills") == "http://h/acme/erp/api/bills"


def test_approval_parsing_is_strict():
    assert is_approval("approve") and is_approval("Yes, go ahead")
    assert not is_approval("yikes, no") and not is_approval("you shouldn't") and not is_approval("deny")


async def _box(base, dry_run=False):
    env = load_environment(Settings().environment_file, base)
    gate = Gate(env, dry_run=dry_run)
    st = RunState(task="t")
    wd = tempfile.mkdtemp()
    b = BrowserSession(gate, env.vault, wd)
    await b.start()
    return st, gate, b, Toolbox(st, env, gate, b, wd)


async def test_admin_unreachable_by_encoding_or_redirect(sandbox):
    reset(sandbox, seed=41)
    st, gate, b, tb = await _box(sandbox)
    try:
        r = await tb.run("browser_goto", {"url": f"{sandbox}/acme/%61dmin/"}, 1)
        assert "BLOCKED" in r.output and "truth" not in r.output.lower()
        r = await tb.run("http_request", {"method": "GET", "url": f"{sandbox}/acme/%61dmin/state"}, 2)
        assert r.error_kind == "blocked"
        # open redirect through the login form's next= parameter
        await tb.run("browser_goto", {"url": f"{sandbox}/acme/erp/login?next=/acme/admin/"}, 3)
        r = await tb.run("browser_fill_form", {"fields": {"Username": "{{secret:erp_username}}",
                                                         "Password": "{{secret:erp_password}}"}, "submit": True}, 4)
        from urllib.parse import urlparse
        assert "BLOCKED" in r.output and not urlparse(b.page.url).path.startswith("/acme/admin")
    finally:
        await b.close()


async def test_encoded_api_path_still_needs_approval(sandbox):
    reset(sandbox, seed=42)
    st, gate, b, tb = await _box(sandbox)
    try:
        r = await tb.run("http_request", {
            "method": "POST", "url": f"{sandbox}/acme/erp/api/%62ills",
            "headers": {"Authorization": "Bearer {{secret:erp_api_token}}"},
            "body": {"vendor": "x", "invoice_number": "Z1", "invoice_date": "2026-01-01",
                     "due_date": "2026-01-02", "amount": "99999.00"}}, 1)
        assert r.error_kind == "needs_approval"
        assert not [x for x in state(sandbox)["bills"] if x["invoice_number"] == "Z1"]
    finally:
        await b.close()


async def test_sign_in_allowed_in_dry_run_and_secrets_never_leak(sandbox):
    reset(sandbox, seed=43)
    st, gate, b, tb = await _box(sandbox, dry_run=True)
    try:
        await tb.run("browser_goto", {"url": f"{sandbox}/acme/erp/"}, 1)
        r = await tb.run("browser_fill_form", {"fields": {"Username": "{{secret:erp_username}}",
                                                         "Password": "{{secret:erp_password}}"}, "submit": True}, 2)
        assert "/acme/erp/login" not in b.page.url, r.output  # signed in although nothing else may be written
        assert gate.writes == []  # a sign-in is not a data change
        await tb.run("http_request", {"method": "GET", "url": "http://10.255.255.1/x",
                                      "headers": {"Authorization": "Bearer {{secret:erp_api_token}}"}}, 3)
        everything = str([o.text for o in st.observations]) + str([w.fields for w in gate.writes])
        for s in gate.env.vault.secrets.values():
            assert s["value"] not in everything
    finally:
        await b.close()


async def test_typed_values_are_not_evidence_and_ambiguous_options_are_reported(sandbox):
    truth = reset(sandbox, seed=44)
    st, gate, b, tb = await _box(sandbox)
    try:
        await tb.run("browser_goto", {"url": f"{sandbox}/acme/erp/bills/new"}, 1)
        await tb.run("browser_fill_form", {"fields": {"Username": "{{secret:erp_username}}",
                                                     "Password": "{{secret:erp_password}}"}, "submit": True}, 2)
        r = await tb.run("browser_fill_form", {"fields": {"Vendor": truth["twins"][0].split()[0]}}, 3)
        assert "ambiguous" in r.output
        await tb.run("browser_fill_form", {"fields": {"Memo": "7777.13 typed by me"}}, 4)
        r = await tb.run("remember", {"facts": {"x": "7777.13"}, "source": "form", "quote": "7777.13 typed by me"}, 5)
        assert r.error_kind == "ungrounded"
    finally:
        await b.close()


async def test_api_calls_get_the_manifest_credentials_automatically(sandbox):
    reset(sandbox, seed=45)
    st, gate, b, tb = await _box(sandbox)
    try:
        r = await tb.run("http_request", {"method": "GET", "url": f"{sandbox}/acme/erp/api/bills"}, 1)
        assert r.ok and "HTTP 200" in r.output, r.output  # no Authorization header given by the model
        assert "erp_tok_" not in r.output  # and the token itself never shows up
    finally:
        await b.close()


async def test_api_file_downloads_are_saved_for_read_file(sandbox):
    reset(sandbox, seed=46)
    st, gate, b, tb = await _box(sandbox)
    try:
        msgs = await tb.run("http_request", {"method": "GET", "url": f"{sandbox}/acme/mail/api/messages"}, 1)
        import json, re
        url = re.search(r'"url": "(/acme/mail/att/[^"]+\.pdf)"', msgs.output).group(1)
        r = await tb.run("http_request", {"method": "GET", "url": sandbox + url}, 2)
        assert r.ok and "Saved the file as files/" in r.output, r.output
        name = re.search(r"files/(\S+?\.pdf)", r.output).group(1)
        f = await tb.run("read_file", {"path": f"files/{name}"}, 3)
        assert f.ok and "page 1" in f.output
    finally:
        await b.close()
