"""Run Extractor -> Validator on one document.

  python run_pipeline.py samples/clean/SHP-1042_BOL.pdf
  python run_pipeline.py samples/messy/SHP-2287_BOL_scan.jpg --customer acme_electronics

Extraction results are cached on disk by content hash + model, so iterating
on downstream agents does not re-bill the vision call. --fresh forces a rerun.
"""
from __future__ import annotations

import argparse
import json
import sys

from app.agents.extractor import extract
from app.agents.validator import validate
from app.config import settings
from app.llm import RunBudget
from app.preprocess import load_document
from app.schemas import ExtractionOutput, MatchStatus, Verdict

VMARK = {
    MatchStatus.MATCH: "OK  ",
    MatchStatus.MISMATCH: "XX  ",
    MatchStatus.UNCERTAIN: "??  ",
    MatchStatus.NOT_APPLICABLE: "--  ",
}
EMARK = {Verdict.FOUND: "OK ", Verdict.UNCERTAIN: "?? ", Verdict.NOT_FOUND: "-- "}


def cached_extract(path: str, model: str | None, budget: RunBudget, fresh: bool):
    bundle = load_document(path)
    model = model or settings.extractor_model
    cache = settings.data_dir / "extract_cache" / f"{bundle.doc_id}.{model}.json"
    cache.parent.mkdir(parents=True, exist_ok=True)

    if cache.exists() and not fresh:
        print(f"(using cached extraction: {cache.name})")
        return ExtractionOutput(**json.loads(cache.read_text(encoding="utf-8")))

    out = extract(bundle, model=model, budget=budget)
    cache.write_text(out.model_dump_json(indent=2), encoding="utf-8")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--customer", default="acme_electronics")
    ap.add_argument("--model", default=None)
    ap.add_argument("--fresh", action="store_true", help="ignore the extraction cache")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    budget = RunBudget()
    ext = cached_extract(args.path, args.model, budget, args.fresh)
    val = validate(ext, args.customer, budget=budget)

    if args.json:
        print(json.dumps({"extraction": ext.model_dump(), "validation": val.model_dump()},
                         indent=2, default=str))
        return 0

    print(f"\n{'=' * 100}")
    print(f"{ext.filename}   model={ext.model}   corpus={ext.text_source}")
    print(f"customer: {val.customer_name}   ruleset {val.customer}@{val.ruleset_version}")
    print("=" * 100)
    print(f"{'':4}{'FIELD':21}{'FOUND':30}{'EXPECTED':28}{'CONF':>5}  NOTE")
    print("-" * 100)
    for r in val.results:
        found = (r.found or "(absent)").replace("\n", " ")
        found = found if len(found) <= 28 else found[:25] + "..."
        exp = (r.expected or "").replace("\n", " ")
        exp = exp if len(exp) <= 26 else exp[:23] + "..."
        sev = "!" if (r.status == MatchStatus.MISMATCH and r.severity == "blocking") else " "
        print(
            f"{VMARK[r.status]}{r.name:21}{found:30}{exp:28}"
            f"{r.extraction_confidence:5.2f}{sev} {r.message[:44]}"
        )
    print("-" * 100)
    print(
        f"match={val.count(MatchStatus.MATCH)}  "
        f"mismatch={val.count(MatchStatus.MISMATCH)}  "
        f"uncertain={val.count(MatchStatus.UNCERTAIN)}  "
        f"n/a={val.count(MatchStatus.NOT_APPLICABLE)}"
    )
    blocking = val.blocking_mismatches
    if blocking:
        print(f"\nBLOCKING ({len(blocking)}):")
        for r in blocking:
            print(f"   {r.label}: found {r.found!r}, expected {r.expected!r}")
            print(f"      why it matters: {r.why[:120]}")
    if val.uncertain:
        print(f"\nUNCERTAIN ({len(val.uncertain)}) - must not be auto-approved:")
        for r in val.uncertain:
            prov = f" [rule would say: {r.provisional_status.value}]" if r.provisional_status else ""
            print(f"   {r.label}: {r.message[:90]}{prov}")

    print(f"\ncost: extraction ${ext.usd_cost:.5f} + validation ${val.usd_cost:.5f}"
          f" = ${ext.usd_cost + val.usd_cost:.5f}")
    print(f"latency: extraction {ext.latency_ms} ms + validation {val.latency_ms} ms")
    for s in budget.spans:
        print(f"   span {s.name:26s} {s.model:16s} {s.latency_ms:6d}ms  ${s.usd:.5f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
