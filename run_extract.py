"""Run the Extractor Agent on one document and print the result.

  python run_extract.py samples/clean/SHP-1042_BOL.pdf
  python run_extract.py samples/messy/SHP-2287_BOL_scan.jpg --model gpt-4.1-mini
"""
from __future__ import annotations

import argparse
import json
import sys

from app.agents.extractor import extract
from app.llm import RunBudget
from app.preprocess import load_document
from app.schemas import Verdict

MARK = {Verdict.FOUND: "OK ", Verdict.UNCERTAIN: "?? ", Verdict.NOT_FOUND: "-- "}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--model", default=None)
    ap.add_argument("--dpi", type=int, default=None)
    ap.add_argument("--no-retry", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    bundle = load_document(args.path, dpi=args.dpi) if args.dpi else load_document(args.path)
    budget = RunBudget()
    out = extract(bundle, model=args.model, budget=budget, allow_retry=not args.no_retry)

    if args.json:
        print(out.model_dump_json(indent=2))
        return 0

    print(f"\n{'=' * 92}")
    print(f"{out.filename}   model={out.model}   pages={out.page_count}   corpus={out.text_source}")
    print("=" * 92)
    print(f"{'':3}{'FIELD':22}{'VALUE':34}{'CONF':>6}{'MODEL':>7}{'GRND':>6}  FLAGS")
    print("-" * 92)
    for f in out.fields:
        val = (f.value or "(not found)")
        val = val if len(val) <= 32 else val[:29] + "..."
        val = val.replace("\n", " ")
        print(
            f"{MARK[f.verdict]}{f.name:22}{val:34}"
            f"{f.final_confidence:6.2f}{f.model_confidence:7.2f}{f.grounding_score:6.2f}  "
            f"{','.join(f.flags) if f.flags else ''}"
        )
    print("-" * 92)

    found = sum(1 for f in out.fields if f.verdict == Verdict.FOUND)
    unc = sum(1 for f in out.fields if f.verdict == Verdict.UNCERTAIN)
    nf = sum(1 for f in out.fields if f.verdict == Verdict.NOT_FOUND)
    print(f"found={found}  uncertain={unc}  not_found={nf}")
    print(f"cost=${out.usd_cost:.5f}  latency={out.latency_ms} ms  llm_calls={budget.calls}")
    print(f"tokens: in={budget.input_tokens} out={budget.output_tokens}")
    for s in budget.spans:
        print(f"   span {s.name:26s} {s.model:14s} {s.latency_ms:6d}ms  ${s.usd:.5f}  {s.status}")
    for w in out.warnings:
        print(f"   warning: {w}")

    print("\nevidence:")
    for f in out.fields:
        if f.evidence_quote:
            q = f.evidence_quote.replace("\n", " ")
            print(f"   {f.name:22} <- {q[:70]!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
