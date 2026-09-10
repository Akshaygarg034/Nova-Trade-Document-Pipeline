"""Does OCR earn its keep, or would the VLM alone do?

Extraction is vision-only in both arms -- the VLM reads every field either
way. What changes is the corpus the verifier checks the VLM's claims
against:

  A  OCR corpus            independent of the extracting model
  B  LLM transcript        the same model family, asked to transcribe

Arm B is the honest alternative to "just use the VLM for everything". The
question is whether an independent verifier catches errors that a
same-model verifier waves through.

Run: python evals/ab_ocr.py
"""
from __future__ import annotations

import json
import pathlib
import re
import sys

from rapidfuzz import fuzz

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from app.config import settings  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
DOC = ROOT / "samples/messy/SHP-2287_BOL_scan.jpg"
TRUTH = json.loads((ROOT / "evals/golden/labels.json").read_text(encoding="utf-8"))[
    "SHP-2287-scan"
]["fields"]


def norm(s):
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def matches(field, got, want):
    if want is None:
        return got is None
    if got is None:
        return False
    if field == "description_of_goods":
        return fuzz.token_set_ratio(got.upper(), want.upper()) >= 80
    return norm(got) == norm(want)


def run(use_ocr: bool):
    settings.use_ocr = use_ocr
    # Reset the memoised OCR engine so the flag actually takes effect.
    import app.preprocess as pre

    pre._ocr_engine, pre._ocr_tried = None, False

    from app.agents.extractor import extract
    from app.llm import RunBudget
    from app.preprocess import load_document
    from app.schemas import Verdict

    bundle = load_document(DOC)
    budget = RunBudget(max_calls=20, max_usd=1.0)
    out = extract(bundle, budget=budget)

    rows = []
    for f in out.fields:
        truth = TRUTH.get(f.name)
        rows.append({
            "field": f.name,
            "got": f.value,
            "truth": truth,
            "correct": matches(f.name, f.value, truth),
            "approved": f.verdict == Verdict.FOUND,
            "conf": f.final_confidence,
        })
    return out, rows, budget


def summarise(tag, out, rows, budget):
    approved = [r for r in rows if r["approved"]]
    escaped = [r for r in approved if not r["correct"]]
    wrong = [r for r in rows if not r["correct"]]
    print(f"\n=== {tag} ===")
    print(f"  corpus source        {out.text_source}")
    print(f"  field accuracy       {sum(r['correct'] for r in rows)}/{len(rows)}")
    print(f"  auto-approved        {len(approved)}/{len(rows)}")
    print(f"  ESCAPED ERRORS       {len(escaped)}")
    for r in escaped:
        print(f"     !! {r['field']}: got {r['got']!r} want {r['truth']!r} at {r['conf']:.2f}")
    print(f"  wrong but surfaced   {len(wrong) - len(escaped)}/{len(wrong)}")
    print(f"  cost                 ${budget.usd:.5f} over {budget.calls} call(s)")
    print(f"  latency              {out.latency_ms} ms")
    return {"escaped": len(escaped), "approved": len(approved),
            "correct": sum(r["correct"] for r in rows), "latency": out.latency_ms}


if __name__ == "__main__":
    a_out, a_rows, a_budget = run(True)
    a = summarise("A · OCR corpus (independent verifier)", a_out, a_rows, a_budget)

    b_out, b_rows, b_budget = run(False)
    b = summarise("B · LLM transcript corpus (same model verifies itself)",
                  b_out, b_rows, b_budget)

    print("\n=== side by side ===")
    print(f"{'':22}{'A: OCR':>12}{'B: LLM':>12}")
    for k in ("correct", "approved", "escaped", "latency"):
        print(f"  {k:20}{a[k]:>12}{b[k]:>12}")

    print("\n=== per-field verdicts ===")
    print(f"{'FIELD':22}{'A conf':>8} {'A ok':>6}{'B conf':>9} {'B ok':>6}")
    for ra, rb in zip(a_rows, b_rows):
        print(f"  {ra['field']:20}{ra['conf']:8.2f} {'ok' if ra['correct'] else 'WRONG':>6}"
              f"{rb['conf']:9.2f} {'ok' if rb['correct'] else 'WRONG':>6}")
