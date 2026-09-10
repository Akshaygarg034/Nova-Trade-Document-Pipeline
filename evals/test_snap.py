"""Checks for snap-to-source: the document, not the model, defines how a
value reads. No LLM calls.
"""
from __future__ import annotations

import pathlib
import sys

import pymupdf

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from app.grounding import snap_to_source  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
corpus = pymupdf.open(ROOT / "samples/clean/SHP-1042_BOL.pdf")[0].get_text()

failures = 0


def check(label: str, got, want) -> None:
    global failures
    ok = got == want
    failures += 0 if ok else 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}\n         got={got!r}")


print("=== the two misreads observed on a real run ===")
# The page reads "Acme"; the model transcribed "Acne" and the rule matched it.
snapped, changed = snap_to_source("Acne Electronics Manufacturing Pte Ltd", corpus)
check("one-letter name misread is corrected from the page",
      (snapped, changed), ("Acme Electronics Manufacturing Pte Ltd", True))

# The page reads "INV-2026-08841"; the model rendered the hyphen as a colon,
# which failed the customer's format rule and would have emailed the supplier.
snapped, changed = snap_to_source("INV:2026-08841", corpus)
check("punctuation misread is corrected from the page",
      (snapped, changed), ("INV-2026-08841", True))

print("\n=== it must not fire when there is nothing to fix ===")
check("an exact value is left untouched",
      snap_to_source("SINGAPORE (SGSIN)", corpus), ("SINGAPORE (SGSIN)", False))
check("an already-correct identifier is untouched",
      snap_to_source("INV-2026-08841", corpus), ("INV-2026-08841", False))

print("\n=== it must not invent a match ===")
check("a value absent from the page is not snapped to something else",
      snap_to_source("Globex Trading GmbH", corpus)[1], False)
check("a different invoice number is not snapped onto the real one",
      snap_to_source("INV-2029-77777", corpus)[1], False)
check("a different port is not snapped",
      snap_to_source("HAMBURG (DEHAM)", corpus)[1], False)

print("\n=== direction of travel: only ever towards the document ===")
# If the page genuinely said "Acne", that is what must be reported -- the
# rule should then fail. Snapping cannot launder a real supplier error.
fake = "Consignee\nAcne Electronics Manufacturing Pte Ltd\nPrins Hendrikkade 142"
snapped, changed = snap_to_source("Acme Electronics Manufacturing Pte Ltd", fake)
check("a genuine document typo is preserved, not silently corrected",
      (snapped, changed), ("Acne Electronics Manufacturing Pte Ltd", True))

print(f"\n{'ALL PASS' if failures == 0 else str(failures) + ' FAILURE(S)'}")
sys.exit(1 if failures else 0)
