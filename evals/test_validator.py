"""Checks for the deterministic half of the Validator. No LLM calls, no cost.

The entity-name comparison is the one rule that can call a model, and it is
covered separately by its threshold behaviour: values outside the ambiguous
band must resolve without any network call at all.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from app.agents.validator import _evaluate, _ports_equal, load_ruleset  # noqa: E402
from app.llm import RunBudget  # noqa: E402

failures = 0


def check(label: str, got, want) -> None:
    global failures
    ok = got == want
    failures += 0 if ok else 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}\n         got={got!r}")


rs = load_ruleset("acme_electronics")
budget = RunBudget()

print("=== port matching: the error grounding cannot catch ===")
# Right text, wrong box. "AMSTERDAM, NETHERLANDS" really is printed on the
# page (as Place of Delivery), so extraction grounds it perfectly. Only the
# customer rule knows Acme clears at Rotterdam.
check("Amsterdam is not Rotterdam", _ports_equal("AMSTERDAM, NETHERLANDS", "ROTTERDAM (NLRTM)"), False)
check("same port, code present both sides", _ports_equal("ROTTERDAM (NLRTM)", "ROTTERDAM (NLRTM)"), True)
check("same port, code missing one side", _ports_equal("ROTTERDAM", "ROTTERDAM (NLRTM)"), True)
check("code wins over name formatting", _ports_equal("Rotterdam Port (NLRTM)", "ROTTERDAM (NLRTM)"), True)
check("different port sharing a country", _ports_equal("ANTWERP (BEANR)", "ROTTERDAM (NLRTM)"), False)

print("\n=== in_list ===")
ok, exp, msg = _evaluate("hs_code", "8542.31", rs.fields["hs_code"], budget)
check("approved tariff line passes", ok, True)
ok, exp, msg = _evaluate("hs_code", "8542.39", rs.fields["hs_code"], budget)
check("unapproved tariff line fails", ok, False)

print("\n=== regex ===")
ok, _, _ = _evaluate("invoice_number", "INV-2026-08841", rs.fields["invoice_number"], budget)
check("correct invoice format", ok, True)
ok, _, _ = _evaluate("invoice_number", "2026-08841", rs.fields["invoice_number"], budget)
check("missing prefix fails", ok, False)
ok, _, _ = _evaluate("invoice_number", "INV-26-8841", rs.fields["invoice_number"], budget)
check("wrong digit counts fail", ok, False)

print("\n=== numeric_range ===")
ok, _, msg = _evaluate("gross_weight", "12,450.00 KGS", rs.fields["gross_weight"], budget)
check("weight in range", ok, True)
ok, _, msg = _evaluate("gross_weight", "31,000 KGS", rs.fields["gross_weight"], budget)
check("weight over container payload cap", ok, False)
ok, _, msg = _evaluate("gross_weight", "NOT A NUMBER", rs.fields["gross_weight"], budget)
check("unparseable weight fails", ok, False)

print("\n=== contains_any ===")
ok, _, _ = _evaluate(
    "description_of_goods", "512 CARTONS ELECTRONIC INTEGRATED CIRCUITS", rs.fields["description_of_goods"], budget
)
check("commodity class named", ok, True)
ok, _, _ = _evaluate("description_of_goods", "ASSORTED ELECTRONIC PARTS", rs.fields["description_of_goods"], budget)
check("vague description fails", ok, False)

print("\n=== entity name: thresholds resolve without a model call ===")
calls_before = budget.calls
ok, _, msg = _evaluate("consignee_name", "Acme Electronics Manufacturing Pte Ltd", rs.fields["consignee_name"], budget)
check("exact name auto-matches", (ok, budget.calls == calls_before), (True, True))

calls_before = budget.calls
ok, _, msg = _evaluate("consignee_name", "Global Freight Holdings BV", rs.fields["consignee_name"], budget)
check("plainly different name auto-rejects", (ok, budget.calls == calls_before), (False, True))

print(f"\nLLM calls made by this suite: {budget.calls} (expected 0)")
check("suite is fully offline", budget.calls, 0)

print(f"\n{'ALL PASS' if failures == 0 else str(failures) + ' FAILURE(S)'}")
sys.exit(1 if failures else 0)
