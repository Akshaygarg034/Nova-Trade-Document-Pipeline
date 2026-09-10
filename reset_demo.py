"""Reset to a clean, known state for a demo or a recording.

  python reset_demo.py                 # free: reuses the cached vision calls
  python reset_demo.py --cold          # [$] ~0.04: forces real API calls
  python reset_demo.py --rebuild-docs  # also regenerate the sample documents
  python reset_demo.py --check         # verify only, change nothing

Afterwards the database holds exactly three runs, one per decision path:

    SHP-1042_BOL.pdf        AUTO_APPROVE
    SHP-2287_BOL.pdf        AMENDMENT_REQUEST
    SHP-2287_BOL_scan.jpg   FLAG_FOR_REVIEW

By default the reading cache is KEPT, so this costs nothing and takes a few
seconds -- the vision call is memoised on document content. Use --cold when
you want to show genuine end-to-end timings, or to prove the pipeline works
from scratch on a reviewer's machine.

Safe to run while the API server is up: rows are deleted rather than files,
so no SQLite file lock is disturbed.
"""
from __future__ import annotations

import argparse
import pathlib
import shutil
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app import store  # noqa: E402
from app.config import settings  # noqa: E402

PYTHON = sys.executable

# (path, shipment id, expected decision)
DEMO_DOCS = [
    ("samples/clean/SHP-1042_BOL.pdf", "SHP-1042", "auto_approve"),
    ("samples/clean/SHP-2287_BOL.pdf", "SHP-2287", "amendment_request"),
    ("samples/messy/SHP-2287_BOL_scan.jpg", "SHP-2287", "flag_for_review"),
]

TABLES = ["decisions", "validations", "extracted_fields", "agent_spans",
          "runs", "documents", "shipments"]


def clear_database() -> None:
    """Empty every table. Rows, not files, so a running server is unaffected."""
    conn = store.connect()
    try:
        for table in TABLES:
            conn.execute(f"DELETE FROM {table}")
        conn.commit()
    finally:
        conn.close()
    print("  cleared all rows from nova.db")


def clear_checkpoints() -> None:
    p = pathlib.Path(str(settings.data_dir / "checkpoints.sqlite"))
    for f in [p, p.with_suffix(".sqlite-wal"), p.with_suffix(".sqlite-shm")]:
        if f.exists():
            try:
                f.unlink()
            except OSError as exc:  # held open on Windows
                print(f"  could not delete {f.name}: {exc}")
                print("  (stop the server and re-run if resume tests misbehave)")
                return
    print("  cleared LangGraph checkpoints")


def clear_uploads() -> None:
    up = settings.data_dir / "uploads"
    if up.exists():
        shutil.rmtree(up, ignore_errors=True)
    up.mkdir(parents=True, exist_ok=True)
    print("  cleared uploaded files")


def clear_reading_cache() -> None:
    """Clear everything derived from a document, for a genuine from-scratch run.

    The rendered pages and the OCR text are cached alongside the reading, so
    clearing only the reading cache would still reuse a 30-second OCR pass and
    the page renders -- which is not what "cold" should mean.
    """
    for name, note in [
        ("reading_cache", "vision calls will be paid for again"),
        ("renders", "pages re-render, and OCR re-runs (~30s for the scan)"),
    ]:
        d = settings.data_dir / name
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
        print(f"  cleared {name} -- {note}")


def rebuild_documents() -> None:
    print("\nrebuilding sample documents:")
    for script in ("samples/fetch_templates.py", "samples/build_samples.py"):
        r = subprocess.run([PYTHON, script], cwd=ROOT, capture_output=True, text=True)
        if r.returncode != 0:
            print(f"  FAILED {script}\n{r.stdout}\n{r.stderr}")
            sys.exit(1)
        for line in r.stdout.strip().splitlines():
            print("  " + line.strip())


def run_documents() -> None:
    print("\nrunning the three demo documents:")
    for path, shipment, _ in DEMO_DOCS:
        if not (ROOT / path).exists():
            print(f"  MISSING {path} -- run with --rebuild-docs")
            sys.exit(1)
        started = time.perf_counter()
        r = subprocess.run(
            [PYTHON, "run_graph.py", path, "--shipment", shipment],
            cwd=ROOT, capture_output=True, text=True,
        )
        if r.returncode != 0:
            print(f"  FAILED {path}")
            print((r.stdout + r.stderr)[-1500:])
            sys.exit(1)
        decision = next(
            (l.split()[1] for l in r.stdout.splitlines() if l.startswith("DECISION:")),
            "?",
        )
        secs = time.perf_counter() - started
        print(f"  {pathlib.Path(path).name:26s} {decision:20s} {secs:5.1f}s")


def check() -> bool:
    """Verify the database is in the expected demo state."""
    conn = store.connect()
    try:
        rows = list(conn.execute(
            """SELECT d.filename, dec.decision, r.usd_cost,
                      (SELECT COUNT(*) FROM agent_spans s WHERE s.run_id = r.run_id) calls
                 FROM runs r
                 JOIN documents d   ON d.doc_id = r.doc_id
                 LEFT JOIN decisions dec ON dec.run_id = r.run_id
                ORDER BY r.started_at"""
        ))
    finally:
        conn.close()

    print(f"\n{'DOCUMENT':28}{'DECISION':20}{'COST':>10}{'CALLS':>7}  OK")
    print("-" * 72)
    ok = len(rows) == len(DEMO_DOCS)
    expected = {pathlib.Path(p).name: d for p, _, d in DEMO_DOCS}
    seen = set()
    for r in rows:
        want = expected.get(r["filename"])
        good = want is not None and r["decision"] == want
        ok = ok and good
        seen.add(r["filename"])
        print(f"{r['filename']:28}{str(r['decision']):20}"
              f"${r['usd_cost'] or 0:9.5f}{r['calls']:7d}  {'yes' if good else 'NO'}")
    for missing in set(expected) - seen:
        ok = False
        print(f"{missing:28}{'(no run)':20}{'':10}{'':7}  NO")

    print("-" * 72)
    total = sum(r["usd_cost"] or 0 for r in rows)
    print(f"{'TOTAL':28}{'':20}${total:9.5f}")

    if ok:
        print("\nReady to demo. Open http://127.0.0.1:8000")
    else:
        print("\nNot in the expected state. Run: python reset_demo.py")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--cold", action="store_true",
                    help="clear the reading cache too, forcing real API calls (~$0.04)")
    ap.add_argument("--rebuild-docs", action="store_true",
                    help="regenerate the sample documents from the B/L templates")
    ap.add_argument("--check", action="store_true",
                    help="verify the current state without changing anything")
    args = ap.parse_args()

    if args.check:
        return 0 if check() else 1

    print("resetting demo state:")
    store.init()
    clear_database()
    clear_checkpoints()
    clear_uploads()
    if args.cold:
        clear_reading_cache()

    if args.rebuild_docs:
        rebuild_documents()

    run_documents()
    return 0 if check() else 1


if __name__ == "__main__":
    sys.exit(main())
