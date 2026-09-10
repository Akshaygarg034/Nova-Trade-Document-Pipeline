"""Ask questions about stored results in plain English.

  python ask.py "how many shipments were flagged this week?"
  python ask.py --demo          # run the sample question set
"""
from __future__ import annotations

import argparse
import sys

from app.llm import RunBudget
from app.nlq import UnsafeQuery, ask

DEMO_QUESTIONS = [
    "How many shipments were flagged for review this week?",
    "Which documents need an amendment, and how many discrepancies does each have?",
    "Which field fails validation most often?",
    "What is the average confidence for each field across all runs?",
    "How much have we spent on LLM calls in total, and on which models?",
    "Show me every field that could not be read reliably, with its document.",
    "Which document took the longest to process?",
]


def show(q: str, budget: RunBudget) -> None:
    print(f"\n{'=' * 88}")
    print(f"Q: {q}")
    print("=" * 88)
    try:
        res = ask(q, budget=budget)
    except UnsafeQuery as exc:
        print(f"  BLOCKED: {exc}")
        return
    except Exception as exc:
        print(f"  ERROR: {type(exc).__name__}: {exc}")
        return

    print(f"A: {res.answer}\n")
    print(f"   sql: {res.sql}")
    if res.rows:
        print(f"   {' | '.join(res.columns)}")
        for row in res.rows[:8]:
            print("   " + " | ".join("" if v is None else str(v) for v in row))
        if res.row_count > 8:
            print(f"   ... {res.row_count - 8} more row(s)")
    else:
        print("   (no rows)")
    for w in res.warnings:
        print(f"   warning: {w}")
    print(f"   {res.latency_ms} ms  ${res.usd_cost:.5f}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("question", nargs="*")
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()

    budget = RunBudget(max_calls=100, max_usd=1.0)

    if args.demo:
        for q in DEMO_QUESTIONS:
            show(q, budget)
        print(f"\ntotal: {budget.calls} calls, ${budget.usd:.5f}")
        return 0

    if not args.question:
        ap.error("ask a question, or pass --demo")
    show(" ".join(args.question), budget)
    return 0


if __name__ == "__main__":
    sys.exit(main())
