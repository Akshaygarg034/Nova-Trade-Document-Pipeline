"""Checks for the Router's decision policy and draft verification.

No LLM calls: `decide()` is a pure function and that is the whole point. If
this suite ever needs a network connection, the decision has stopped being
auditable.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from app.agents.router import _template_email, _verify_draft, decide  # noqa: E402
from app.schemas import (  # noqa: E402
    DecisionType,
    Discrepancy,
    FieldValidation,
    MatchStatus,
    ValidationOutput,
    Verdict,
)

failures = 0


def check(label: str, got, want) -> None:
    global failures
    ok = got == want
    failures += 0 if ok else 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}\n         got={got!r}")


def fv(name, status, severity="blocking", conf=0.95, found="X", expected="Y"):
    return FieldValidation(
        name=name, label=name.replace("_", " ").capitalize(), status=status,
        found=found, expected=expected, rule="in_list", severity=severity,
        message="test", extraction_confidence=conf,
        extraction_verdict=Verdict.FOUND if status != MatchStatus.UNCERTAIN else Verdict.UNCERTAIN,
    )


def vo(results):
    return ValidationOutput(
        doc_id="d1", document_ref="SHP-TEST", customer="acme_electronics",
        customer_name="Acme Electronics Manufacturing Pte Ltd",
        ruleset_version="test", results=results,
    )


print("=== the three outcomes ===")
d, r = decide(vo([fv("a", MatchStatus.MATCH), fv("b", MatchStatus.MATCH)]))
check("all match, high confidence -> auto approve", d, DecisionType.AUTO_APPROVE)

d, r = decide(vo([fv("a", MatchStatus.MATCH), fv("b", MatchStatus.MISMATCH)]))
check("a blocking mismatch -> amendment", d, DecisionType.AMENDMENT_REQUEST)

d, r = decide(vo([fv("a", MatchStatus.MATCH), fv("b", MatchStatus.UNCERTAIN)]))
check("an unreadable field -> human review", d, DecisionType.FLAG_FOR_REVIEW)

print("\n=== the precedence that protects the supplier ===")
# Both present. Review must win: an amendment built partly on data we could
# not read risks asking the supplier to fix the wrong thing, costing a cycle.
d, r = decide(vo([fv("a", MatchStatus.MISMATCH), fv("b", MatchStatus.UNCERTAIN)]))
check("uncertainty outranks a confirmed mismatch", d, DecisionType.FLAG_FOR_REVIEW)
check(
    "but the confirmed mismatch is still reported to the operator",
    any("confirmed blocking discrepancy" in x for x in r),
    True,
)

print("\n=== no silent approvals ===")
d, r = decide(vo([fv("a", MatchStatus.MATCH, conf=0.50), fv("b", MatchStatus.MATCH)]))
check("all rules pass but confidence under floor -> review", d, DecisionType.FLAG_FOR_REVIEW)

d, r = decide(vo([fv("a", MatchStatus.NOT_APPLICABLE), fv("b", MatchStatus.NOT_APPLICABLE)]))
check("no rules applied -> review, never approve by default", d, DecisionType.FLAG_FOR_REVIEW)

d, r = decide(vo([fv("a", MatchStatus.MATCH), fv("b", MatchStatus.MISMATCH, severity="warning")]))
check("a warning-level mismatch still requires action", d, DecisionType.AMENDMENT_REQUEST)

print("\n=== determinism ===")
sample = vo([fv("a", MatchStatus.MISMATCH), fv("b", MatchStatus.MATCH)])
runs = {(d.value, tuple(r)) for d, r in (decide(sample) for _ in range(50))}
check("50 evaluations produce one outcome, reasons included", len(runs), 1)

print("\n=== draft verification rejects bad generations ===")
discs = [
    Discrepancy(field="hs_code", label="HS code", found="8542.39", expected="8542.31",
                severity="blocking", why="pre-classified tariff line"),
    Discrepancy(field="incoterms", label="Incoterms", found=None, expected="FOB or CIF",
                severity="blocking", why="liability undefined"),
]
good = ("The HS code shown is 8542.39 but 8542.31 is required for this pre-classified "
        "tariff line. Incoterms are absent; FOB or CIF must be stated so that liability "
        "in transit is defined. Please resubmit corrected documents at your earliest.")
check("a complete draft passes", _verify_draft(good, discs), [])

omits = ("The HS code shown is 8542.39 but 8542.31 is required for this pre-classified "
         "tariff line. Please resubmit the corrected document at your earliest convenience "
         "so the shipment is not held at the port of entry.")
check("a draft omitting a discrepancy is rejected",
      any("omits" in p for p in _verify_draft(omits, discs)), True)

approves = good + " The document is otherwise approved."
check("a draft implying approval is rejected",
      any("approval language" in p for p in _verify_draft(approves, discs)), True)

check("an implausibly short draft is rejected",
      any("short" in p for p in _verify_draft("Fix the HS code and Incoterms.", discs)), True)

print("\n=== the template fallback is always sendable ===")
t = _template_email(vo([]), discs, ["Gross weight"])
body = t.body
check("template names every discrepancy", all(d.label in body for d in discs), True)
check("template includes each required value", "FOB or CIF" in body, True)
check("template surfaces illegible fields", "Gross weight" in body, True)
check("template uses the human-facing reference", "SHP-TEST" in body, True)
check("template leaks no content hash", "d1" not in t.subject, True)
check("template passes its own verification", _verify_draft(body, discs), [])

print(f"\n{'ALL PASS' if failures == 0 else str(failures) + ' FAILURE(S)'}")
sys.exit(1 if failures else 0)
