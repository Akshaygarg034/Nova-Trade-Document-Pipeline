"""Checks for the deterministic identifier-repair step. No LLM calls."""
from __future__ import annotations

import pathlib
import sys

import pymupdf

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from app.grounding import repair_identifier, token_aligned  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
corpus = pymupdf.open(ROOT / "samples/clean/SHP-1042_BOL.pdf")[0].get_text()

failures = 0


def check(label: str, got, want) -> None:
    global failures
    ok = got == want
    failures += 0 if ok else 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}\n         got={got!r}")


# The page reads: "INV NO. INV-2026-08841"
print("=== repairs the two failure shapes we actually observed ===")
check(
    "label swept into value is stripped",
    repair_identifier("INV NO. INV-2026-08841", corpus),
    ("INV-2026-08841", True),
)
check(
    "already-correct value is left alone",
    repair_identifier("INV-2026-08841", corpus),
    ("INV-2026-08841", False),
)

print("\n=== the guard: repair cannot launder a wrong value ===")
# Stripping the real "INV-" prefix yields something aligned with no token,
# so the repair must be refused rather than silently truncating.
check(
    "truncation is not produced by the repair",
    repair_identifier("2026-08841", corpus),
    ("2026-08841", False),
)
check("truncation still fails alignment", token_aligned("2026-08841", corpus), False)
check(
    "a value absent from the page is untouched",
    repair_identifier("INV-9999-00000", corpus),
    ("INV-9999-00000", False),
)

print("\n=== hs_code ===")
check("HS label stripped", repair_identifier("HS CODE: 8542.31", corpus), ("8542.31", True))
check("bare HS code untouched", repair_identifier("8542.31", corpus), ("8542.31", False))

print(f"\n{'ALL PASS' if failures == 0 else str(failures) + ' FAILURE(S)'}")
sys.exit(1 if failures else 0)
