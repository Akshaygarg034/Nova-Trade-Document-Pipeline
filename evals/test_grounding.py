"""Offline unit checks for the grounding layer. No LLM calls, no cost.

These encode the two failure modes we actually hit while building, so a future
change that reintroduces either one fails here rather than in production.
"""
from __future__ import annotations

import glob
import pathlib
import sys

import pymupdf

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from app.grounding import check_format, score_grounding, token_in_text  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
corpus = pymupdf.open(ROOT / "samples/clean/SHP-2287_BOL.pdf")[0].get_text()

failures = 0


def check(label: str, got, want) -> None:
    global failures
    ok = got == want
    failures += 0 if ok else 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}\n         got={got!r}")


print("=== the CIF trap: Incoterm is absent, but 'CIF' hides in 'SPECIFICALLY' ===")
print("  naive substring   'CIF' in corpus :", "CIF" in corpus)
print("  word-boundary     token_in_text   :", token_in_text("CIF", corpus))
s, g, f = score_grounding("incoterms", "CIF", "SPECIFICALLY STATED BY THE SHIPPER", corpus)
check("hallucinated CIF is rejected", (g, "code_not_present_as_whole_word" in f), (False, True))

print("\n=== genuine fields ground cleanly ===")
s, g, f = score_grounding("hs_code", "8542.39", "MEMORY MODULES - HS 8542.39", corpus)
check("hs_code grounded", g, True)
s, g, f = score_grounding(
    "consignee_name",
    "Acme Electronics Mfg Pte",
    "Acme Electronics Mfg Pte\nPrins Hendrikkade 142",
    corpus,
)
check("consignee grounded on a verbatim quote", g, True)

# A stitched quote: label and value are both real but not contiguous on the
# page. The value is genuine, so this must NOT read as a hallucination -- it
# should ground on value and flag the citation instead.
s, g, f = score_grounding(
    "consignee_name", "Acme Electronics Mfg Pte", "CONSIGNEE Acme Electronics Mfg Pte", corpus
)
check(
    "stitched quote: value still verified, not read as hallucination",
    (s >= 0.85, "value_absent_from_document" in f),
    (True, False),
)

# The citation check is deliberately lenient -- partial_ratio scores the best
# matching window, so mild stitching passes. It exists to catch a wholly
# fabricated quote, which is the case that actually matters.
s, g, f = score_grounding(
    "consignee_name",
    "Acme Electronics Mfg Pte",
    "Buyer of record per attached schedule 4B, as amended",
    corpus,
)
# The value is genuinely on the page, so `grounded` stays True -- the
# extraction is correct. The bad citation is reported separately and costs a
# confidence penalty, because it degrades the audit trail, not the answer.
check(
    "fabricated quote is flagged, value still grounded",
    ("quote_does_not_contain_value" in f or "evidence_citation_not_verbatim" in f),
    True,
)

print("\n=== identifier truncation (a real miss the model made) ===")
s, g, f = score_grounding(
    "invoice_number", "2026-08847", "INV NO. INV-2026-08847", corpus, "pdf_text"
)
check("truncated invoice number rejected", ("partial_token_match" in f, g), (True, False))
s, g, f = score_grounding(
    "invoice_number", "INV-2026-08847", "EXPORT REFERENCES INV-2026-08847", corpus, "pdf_text"
)
check("complete invoice number accepted", g, True)

print("\n=== an invented value is caught ===")
s, g, f = score_grounding("invoice_number", "INV-9999-00000", "Invoice No. INV-9999-00000", corpus)
check("fake invoice flagged absent", (g, "value_absent_from_document" in f), (False, True))

print("\n=== OCR-noise tolerance on the degraded scan ===")
hits = sorted(glob.glob(str(ROOT / "data/renders/*/*.ocr.txt")))
if hits:
    ocr = pathlib.Path(hits[0]).read_text(encoding="utf-8")
    # text_source="ocr" matters: strict whole-token alignment is only enforced
    # against a reliable corpus, because OCR routinely splits tokens.
    s, g, f = score_grounding(
        "invoice_number", "INV-2026-08847", "EXPORT REFERENCES INV-2026-08847", ocr, "ocr"
    )
    check("invoice survives OCR noise (page OCRs as a space, not a hyphen)", g, True)

    # The same call against a corpus we claim is reliable must NOT pass: there
    # the split token is evidence of a misread, not of scanner noise.
    s2, g2, f2 = score_grounding(
        "invoice_number", "INV-2026-08847", "EXPORT REFERENCES INV-2026-08847", ocr, "pdf_text"
    )
    check("same value against a 'reliable' corpus is held to the strict rule", g2, False)

    s, g, f = score_grounding(
        "consignee_name", "Acme Electronics Mfg Pte", "CONSIGNEE Acme Electronics Mfg Pte", ocr, "ocr"
    )
    print(f"  consignee via OCR -> score={s} grounded={g} flags={f}")

    # A one-character numeric error must not survive fuzzy matching.
    s, g, f = score_grounding("gross_weight", "12,960 KGS", "GROSS WEIGHT 12,960 KGS", ocr, "ocr")
    check(
        "one-character weight error is caught",
        (any(x.startswith("digits_not_on_page") for x in f), g),
        (True, False),
    )
    s, g, f = score_grounding("gross_weight", "12,980 KGS", "GROSS WEIGHT 12,980 KGS", ocr, "ocr")
    check("correct weight passes", g, True)
else:
    print("  (no OCR corpus cached yet - run the pipeline on the messy sample first)")

print("\n=== format rules ===")
cases = [
    ("incoterms", "FOB", True), ("incoterms", "CIF", True), ("incoterms", "FOBB", False),
    ("hs_code", "8542.31", True), ("hs_code", "85", False), ("hs_code", "85XY.31", False),
    ("gross_weight", "12,450.00 KGS", True), ("gross_weight", "12450", False),
    ("invoice_number", "INV-2026-08841", True), ("invoice_number", "ABC", False),
]
for name, value, want in cases:
    valid, note = check_format(name, value)
    check(f"{name}={value!r}", valid, want)

print(f"\n{'ALL PASS' if failures == 0 else str(failures) + ' FAILURE(S)'}")
sys.exit(1 if failures else 0)
