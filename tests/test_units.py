from datetime import date

import pytest

from sandbox.world import generate
from worker.agent import normalize
from worker.environment import Environment, Vault
from worker.gate import Gate
from worker.llm.base import extract_json, tool_call_from_text, validate_args


def test_normalize_money_and_dates():
    assert normalize.same("$4,107.41", "4107.41")
    assert normalize.same("October 10, 2026", "2026-10-10")
    assert normalize.same("10 Oct 2026", "2026-10-10")
    assert normalize.same("10/11/2026", "2026-10-11")  # ambiguous numeric dates accept both readings
    assert not normalize.same("4107.41", "4107.40")
    assert not normalize.same("B-1012", "C-1012")
    assert normalize.same("INV-2889", "inv-2889")


def test_extract_json_variants():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Sure! Here it is: {"a": {"b": "}"}} trailing') == {"a": {"b": "}"}}
    with pytest.raises(ValueError):
        extract_json("no json here")


def test_tool_call_recovered_from_text():
    tc = tool_call_from_text('I will click. {"tool": "browser_click", "args": {"element": "4"}}', {"browser_click"})
    assert tc.name == "browser_click" and tc.args == {"element": "4"}
    assert tool_call_from_text('{"tool": "rm_rf"}', {"browser_click"}) is None


def test_validate_args():
    schema = {"type": "object", "properties": {"x": {"type": "integer"}, "k": {"type": "string", "enum": ["a"]}},
              "required": ["x"]}
    assert validate_args(schema, {"x": 1}) is None
    assert "missing" in validate_args(schema, {})
    assert "type" in validate_args(schema, {"x": "1"})
    assert "one of" in validate_args(schema, {"x": 1, "k": "b"})


def _env(policy):
    return Environment("t", "", {}, [], Vault({"pw": {"value": "hunter22", "origins": ["http://ok.test"]}}),
                       ["http://ok.test"], ["/admin"], policy)


POLICY = [
    {"id": "big", "match": {"url": "*/things*"}, "when": {"field": "amount|total", "gte": 10000},
     "action": "require_approval"},
    {"id": "ext", "match": {"url": "*/send*"}, "when": {"field": "^to$", "not_contains_only": "@corp.test"},
     "action": "require_approval"},
]


def test_gate_policy_threshold_and_grant():
    g = Gate(_env(POLICY))
    assert g.check_write("POST", "http://ok.test/things/new", {"Total amount": "9,999.99"}).action == "allow"
    d = g.check_write("POST", "http://ok.test/things/new", {"Total amount": "$12,000.00"})
    assert d.action == "approval" and d.rule["id"] == "big"
    g.grant("big", d.rule["_key"])
    assert g.check_write("POST", "http://ok.test/things/new", {"Total amount": "$12,000.00"}).action == "allow"
    # an approval for one amount does not approve a different amount
    assert g.check_write("POST", "http://ok.test/things/new", {"Total amount": "50000"}).action == "approval"


def test_gate_external_email_and_navigation():
    g = Gate(_env(POLICY))
    assert g.check_write("POST", "http://ok.test/send", {"to": "a@corp.test"}).action == "allow"
    assert g.check_write("POST", "http://ok.test/send", {"to": "a@corp.test, x@evil.test"}).action == "approval"
    assert g.check_navigation("http://evil.test/").action == "deny"
    assert g.check_navigation("http://ok.test/admin/x").action == "deny"


def test_gate_duplicate_guard_requires_a_look_first():
    g = Gate(_env([]))
    d = g.check_write("POST", "http://ok.test/things/new", {"a": "1"})
    w = g.record_write("POST", "http://ok.test/things/new", {"a": "1"}, d.fingerprint)
    w.outcome, w.status = "server_error", 502
    assert g.check_write("POST", "http://ok.test/things/new", {"a": "1"}).action == "deny"
    assert g.check_write("POST", "http://ok.test/things/new", {"a": "2"}).action == "allow"  # different payload
    g.note_read("http://ok.test/things/list")
    assert g.check_write("POST", "http://ok.test/things/new", {"a": "1"}).action == "allow"


def test_gate_read_only_and_dry_run():
    g = Gate(_env([]))
    g.read_only = True
    assert g.check_write("POST", "http://ok.test/x", {}).action == "deny"
    g2 = Gate(_env([]), dry_run=True)
    assert g2.check_write("POST", "http://ok.test/x", {}).action == "dry_run"


def test_vault_resolves_only_on_allowed_origin_and_scrubs():
    v = Vault({"pw": {"value": "hunter22", "origins": ["http://ok.test"]}})
    assert v.resolve("{{secret:pw}}", "http://ok.test/login") == "hunter22"
    with pytest.raises(PermissionError):
        v.resolve("{{secret:pw}}", "http://evil.test/login")
    assert v.scrub("the password is hunter22") == "the password is {{secret:pw}}"


def test_world_is_deterministic_and_varied():
    a, b, c = generate(1, date(2026, 10, 4)), generate(1, date(2026, 10, 4)), generate(2, date(2026, 10, 4))
    assert a.truth == b.truth
    assert a.truth != c.truth
    for w in (a, c):
        big = w.truth["vendors"]["big"]
        assert float(w.truth["latest"][big]["amount"]) >= 10000
        for name, inv in w.truth["latest"].items():
            if name != big:
                assert float(inv["amount"]) < 10000
