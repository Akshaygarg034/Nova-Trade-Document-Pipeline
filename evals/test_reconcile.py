"""Checks for merging a first-pass and second-pass reading. No LLM calls.

Two passes that differ are only *genuinely* ambiguous when both of them
passed verification. If exactly one is grounded on the page, that is not a
tie -- the other reading failed its own checks.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from app.agents.extractor import _readings_agree, _reconcile  # noqa: E402
from app.schemas import GroundedField, Verdict  # noqa: E402

failures = 0


def check(label: str, got, want) -> None:
    global failures
    ok = got == want
    failures += 0 if ok else 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}\n         got={got!r}")


def gf(name, value, *, grounded, conf, verdict=Verdict.FOUND):
    return GroundedField(
        name=name, label=name, value=value, verdict=verdict,
        final_confidence=conf, model_confidence=0.98,
        grounding_score=1.0 if grounded else 0.5, grounded=grounded,
        format_valid=True,
    )


print("=== one reading verified, one not: trust the verified one ===")
# Observed on a clean document: pass 1 kept the field label, pass 2 dropped
# the prefix. Only one survived token alignment. Calling that a disagreement
# pushed a correct, fully grounded value down to 0.45 and lost auto-approve.
merged = _reconcile(
    gf("invoice_number", "INV-2026-08841", grounded=True, conf=0.97),
    gf("invoice_number", "2026-08841", grounded=False, conf=0.45, verdict=Verdict.UNCERTAIN),
)
check("grounded reading wins", (merged.value, merged.verdict), ("INV-2026-08841", Verdict.FOUND))
check("resolution is recorded in the audit trail",
      "reextraction_resolved_by_grounding" in merged.flags, True)

# Order must not matter.
merged = _reconcile(
    gf("invoice_number", "2026-08841", grounded=False, conf=0.45, verdict=Verdict.UNCERTAIN),
    gf("invoice_number", "INV-2026-08841", grounded=True, conf=0.97),
)
check("order does not matter", merged.value, "INV-2026-08841")

print("\n=== both verified but differing: still genuinely ambiguous ===")
merged = _reconcile(
    gf("port_of_discharge", "ROTTERDAM (NLRTM)", grounded=True, conf=0.95),
    gf("port_of_discharge", "AMSTERDAM (NLAMS)", grounded=True, conf=0.93),
)
check("two grounded conflicting readings -> UNCERTAIN", merged.verdict, Verdict.UNCERTAIN)
check("confidence is capped", merged.final_confidence <= 0.45, True)
check("disagreement flagged", "reextraction_disagreement" in merged.flags, True)

print("\n=== neither verified: also ambiguous, not a free pass ===")
merged = _reconcile(
    gf("gross_weight", "12,960 KGS", grounded=False, conf=0.20, verdict=Verdict.UNCERTAIN),
    gf("gross_weight", "12,940 KGS", grounded=False, conf=0.18, verdict=Verdict.UNCERTAIN),
)
check("neither grounded -> UNCERTAIN", merged.verdict, Verdict.UNCERTAIN)

print("\n=== agreement earns a modest bump ===")
merged = _reconcile(
    gf("consignee_name", "Acme Electronics Mfg Pte", grounded=True, conf=0.88),
    gf("consignee_name", "Acme Electronics Mfg Pte", grounded=True, conf=0.88),
)
check("agreeing readings are confirmed", "confirmed_by_reextraction" in merged.flags, True)
check("confidence rises but stays <= 1.0", merged.final_confidence <= 1.0, True)

print("\n=== agreement is judged per field type ===")
# Prose legitimately varies between passes; an identifier does not.
check("free text with the same words agrees",
      _readings_agree("description_of_goods",
                      "480 CARTONS OF ELECTRONIC INTEGRATED CIRCUITS",
                      "ELECTRONIC INTEGRATED CIRCUITS"), True)
check("identifiers must match exactly",
      _readings_agree("invoice_number", "INV-2026-08841", "INV-2026-08842"), False)

print(f"\n{'ALL PASS' if failures == 0 else str(failures) + ' FAILURE(S)'}")
sys.exit(1 if failures else 0)
