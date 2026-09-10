"""Run the full pipeline through LangGraph, with persistence.

  python run_graph.py samples/clean/SHP-1042_BOL.pdf
  python run_graph.py samples/clean/SHP-2287_BOL.pdf --shipment SHP-2287

Crash / resume demo:
  python run_graph.py samples/clean/SHP-2287_BOL.pdf --crash-after extract
  python run_graph.py --resume <run-id>          # continues, no re-extraction
"""
from __future__ import annotations

import argparse
import sys

from app import store
from app.graph import resume, run_pipeline
from app.schemas import MatchStatus, ValidationOutput

VMARK = {"match": "OK  ", "mismatch": "XX  ", "uncertain": "??  ", "not_applicable": "--  "}


def report(run_id: str) -> None:
    row = store.fetch_run(run_id)
    if row is None:
        print(f"no run {run_id}")
        return

    print(f"\n{'=' * 96}")
    print(f"run {row['run_id']}   doc {row['doc_id']}   status={row['status']}  stage={row['stage']}")
    print("=" * 96)
    print(f"{'':4}{'FIELD':21}{'VALUE':30}{'STATUS':13}{'CONF':>5}  EVIDENCE")
    print("-" * 96)
    vals = {v["name"]: v for v in row["validations"]}
    for f in row["fields"]:
        v = vals.get(f["name"], {})
        status = v.get("status", "-")
        val = (f["value"] or "(absent)").replace("\n", " ")
        val = val if len(val) <= 28 else val[:25] + "..."
        q = (f["evidence_quote"] or "").replace("\n", " ")
        print(
            f"{VMARK.get(status, '    ')}{f['name']:21}{val:30}{status:13}"
            f"{f['final_confidence']:5.2f}  {q[:30]}"
        )
    print("-" * 96)

    d = row["decision"]
    if d:
        print(f"DECISION: {d['decision'].upper()}   "
              f"mismatches={d['n_mismatch']} uncertain={d['n_uncertain']} "
              f"floor={d['lowest_confidence']:.2f}")
        print(f"rationale: {d['rationale'][:200]}")
        if d["draft_subject"]:
            print(f"draft ({d['draft_source']}, email_sent={d['email_sent']}): {d['draft_subject']}")

    print(f"\nspans ({len(row['spans'])} LLM calls):")
    total = 0.0
    for s in row["spans"]:
        total += s["usd"] or 0
        print(f"   {s['node']:9s} {s['name']:26s} {s['model']:16s} "
              f"{s['latency_ms']:6d}ms  ${s['usd']:.5f}  {s['status']}")
    print(f"   {'TOTAL':36s} {'':16s} {row['latency_ms']:6d}ms  ${total:.5f}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path", nargs="?")
    ap.add_argument("--customer", default="acme_electronics")
    ap.add_argument("--shipment", default=None)
    ap.add_argument("--model", default=None)
    ap.add_argument("--crash-after", default=None,
                    choices=["ingest", "extract", "validate", "route"])
    ap.add_argument("--resume", default=None, metavar="RUN_ID")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    if args.list:
        store.init()
        print(f"{'RUN':16}{'DOC':34}{'STATUS':11}{'STAGE':10}{'DECISION':20}{'COST':>9}")
        for r in store.recent_runs(25):
            print(f"{r['run_id']:16}{(r['filename'] or '')[:32]:34}{r['status']:11}"
                  f"{(r['stage'] or ''):10}{(r['decision'] or '-'):20}${r['usd_cost'] or 0:.5f}")
        return 0

    if args.resume:
        print(f"resuming {args.resume} from its last checkpoint...")
        out = resume(args.resume)
        report(out["run_id"])
        return 0

    if not args.path:
        ap.error("provide a document path, --resume RUN_ID, or --list")

    try:
        out = run_pipeline(
            args.path, customer=args.customer, shipment_id=args.shipment,
            model=args.model, crash_after=args.crash_after,
        )
    except RuntimeError as exc:
        print(f"\nFAILED: {exc}")
        return 1

    report(out["run_id"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
