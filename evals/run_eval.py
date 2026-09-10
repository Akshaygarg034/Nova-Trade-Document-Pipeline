"""Offline eval against the golden set.

  python evals/run_eval.py            # uses cached extractions where present
  python evals/run_eval.py --fresh    # re-extracts everything

The headline number is deliberately NOT raw field accuracy. A document
pipeline that is 95% accurate and silent about the other 5% is worse than
one that is 85% accurate and says so, because the first quietly ships wrong
customs data and the second asks a human. So the metrics that matter are:

  escaped errors        a wrong value that was auto-approved. Target: zero.
                        This is the only metric that can hurt a customer.
  auto-approve accuracy of the fields we did approve, how many were right.
  surfaced-error recall of the fields we got wrong, how many did we flag.
  touchless rate        how many fields needed no human at all. The thing
                        the business is actually buying -- but meaningless
                        unless escaped errors stay at zero, so never read
                        on its own.

Calibration is reported too: mean confidence on correct versus incorrect
values. If those two numbers are close, the confidence score is decoration.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

from rapidfuzz import fuzz

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from app.agents.extractor import extract, extract_cached  # noqa: E402
from app.llm import RunBudget  # noqa: E402
from app.preprocess import load_document  # noqa: E402
from app.schemas import CANONICAL_FIELDS, Verdict  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
LABELS = ROOT / "evals" / "golden" / "labels.json"

# Free-text fields are compared on token overlap; everything else must match
# once punctuation and case are normalised.
FUZZY_FIELDS = {"description_of_goods"}
FUZZY_THRESHOLD = 80


def norm(s: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def values_match(field: str, got: str | None, want: str | None) -> bool:
    if want is None:
        return got is None
    if got is None:
        return False
    if field in FUZZY_FIELDS:
        return fuzz.token_set_ratio(got.upper(), want.upper()) >= FUZZY_THRESHOLD
    return norm(got) == norm(want)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fresh", action="store_true")
    ap.add_argument("--model", default=None)
    args = ap.parse_args()

    labels = json.loads(LABELS.read_text(encoding="utf-8"))
    budget = RunBudget(max_calls=200, max_usd=5.0)
    rows: list[dict] = []

    for doc_key, spec in labels.items():
        bundle = load_document(ROOT / spec["path"])
        if args.fresh:
            out = extract(bundle, model=args.model, budget=budget)
        else:
            out, _ = extract_cached(bundle, model=args.model, budget=budget)

        for name in CANONICAL_FIELDS:
            f = out.by_name(name)
            truth = spec["fields"].get(name)
            correct = values_match(name, f.value, truth)
            rows.append({
                "doc": doc_key,
                "quality": spec["quality"],
                "field": name,
                "truth": truth,
                "got": f.value,
                "verdict": f.verdict.value,
                "confidence": f.final_confidence,
                "source_label": f.source_label,
                "correct": correct,
                "auto_approved": f.verdict == Verdict.FOUND,
                "flags": f.flags,
            })

    # ------------------------------------------------------------ report
    print(f"\n{'=' * 100}")
    print(f"{'DOC':16}{'FIELD':22}{'EXPECTED':24}{'GOT':24}{'CONF':>5} {'V':<10} OK")
    print("=" * 100)
    for r in rows:
        exp = str(r["truth"])[:22]
        got = str(r["got"])[:22]
        mark = "ok" if r["correct"] else "WRONG"
        print(f"{r['doc']:16}{r['field']:22}{exp:24}{got:24}"
              f"{r['confidence']:5.2f} {r['verdict']:<10} {mark}")

    def pct(n, d):
        return f"{100 * n / d:5.1f}%" if d else "    -"

    print("\n" + "=" * 100)
    for tier in ("clean", "degraded", "ALL"):
        sub = rows if tier == "ALL" else [r for r in rows if r["quality"] == tier]
        if not sub:
            continue
        total = len(sub)
        correct = sum(r["correct"] for r in sub)
        approved = [r for r in sub if r["auto_approved"]]
        approved_correct = sum(r["correct"] for r in approved)
        escaped = [r for r in approved if not r["correct"]]
        wrong = [r for r in sub if not r["correct"]]
        surfaced = [r for r in wrong if not r["auto_approved"]]

        # Absence handling, scored separately: reporting a field that is not
        # on the page as not_found is a success, and inventing one is the
        # failure the whole grounding layer exists to prevent.
        absent = [r for r in sub if r["truth"] is None]
        absent_ok = sum(1 for r in absent if r["got"] is None)
        hallucinated = [r for r in absent if r["got"] is not None]

        print(f"\n--- {tier} ({total} field readings) ---")
        print(f"  field accuracy          {pct(correct, total)}   ({correct}/{total})")
        print(f"  auto-approved           {pct(len(approved), total)}   ({len(approved)}/{total})")
        print(f"  auto-approve accuracy   {pct(approved_correct, len(approved))}   "
              f"({approved_correct}/{len(approved)})")
        print(f"  ESCAPED ERRORS          {len(escaped):5d}     <- wrong AND auto-approved")
        print(f"  surfaced-error recall   {pct(len(surfaced), len(wrong))}   "
              f"({len(surfaced)}/{len(wrong)} wrong values flagged)")
        if absent:
            print(f"  correct absences        {pct(absent_ok, len(absent))}   "
                  f"({absent_ok}/{len(absent)})")
            print(f"  hallucinated fields     {len(hallucinated):5d}")

        ok_conf = [r["confidence"] for r in sub if r["correct"]]
        bad_conf = [r["confidence"] for r in sub if not r["correct"]]
        if ok_conf and bad_conf:
            mo, mb = sum(ok_conf) / len(ok_conf), sum(bad_conf) / len(bad_conf)
            print(f"  mean confidence         {mo:.2f} when correct vs {mb:.2f} when wrong "
                  f"(separation {mo - mb:+.2f})")

        for r in escaped:
            print(f"  !! escaped: {r['doc']}/{r['field']} got {r['got']!r} "
                  f"expected {r['truth']!r} at {r['confidence']:.2f}")

    print(f"\ncost this run: ${budget.usd:.5f} over {budget.calls} LLM call(s)")

    out_path = ROOT / "evals" / "last_eval.json"
    out_path.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    print(f"per-field detail -> {out_path.relative_to(ROOT)}")

    escaped_total = sum(1 for r in rows if r["auto_approved"] and not r["correct"])
    return 1 if escaped_total else 0


if __name__ == "__main__":
    sys.exit(main())
