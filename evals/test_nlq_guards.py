"""Adversarial checks on the natural-language query layer. No LLM calls.

A model writing SQL against a live database is the last place to trust a
prompt, so the guards are tested against the things a bad generation (or a
prompt injection carried in a document) would actually produce.
"""
from __future__ import annotations

import pathlib
import sqlite3
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from app.config import settings  # noqa: E402
from app.nlq import UnsafeQuery, guard_sql, render_rows, verify_answer  # noqa: E402

failures = 0


def check(label: str, got, want) -> None:
    global failures
    ok = got == want
    failures += 0 if ok else 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}\n         got={got!r}")


def blocked(sql: str) -> str | bool:
    try:
        guard_sql(sql)
        return False
    except UnsafeQuery as exc:
        return str(exc)


print("=== legitimate queries pass, and get a LIMIT ===")
check("plain select gains a LIMIT",
      guard_sql("SELECT decision FROM decisions").endswith("LIMIT 200"), True)
check("an explicit LIMIT is respected",
      guard_sql("SELECT decision FROM decisions LIMIT 5").endswith("LIMIT 5"), True)
check("a CTE is allowed",
      guard_sql("WITH x AS (SELECT 1 AS n) SELECT n FROM x").startswith("WITH"), True)
check("a trailing semicolon is tolerated",
      guard_sql("SELECT 1;").startswith("SELECT"), True)

print("\n=== writes and DDL are rejected ===")
for sql in [
    "DELETE FROM decisions",
    "DROP TABLE decisions",
    "UPDATE decisions SET decision='auto_approve'",
    "INSERT INTO decisions (run_id) VALUES ('x')",
    "ALTER TABLE runs ADD COLUMN x TEXT",
    "PRAGMA table_info(runs)",
    "ATTACH DATABASE '/tmp/evil.db' AS evil",
]:
    check(f"blocked: {sql[:44]}", bool(blocked(sql)), True)

print("\n=== statement stacking is rejected ===")
check("select then delete",
      bool(blocked("SELECT 1; DELETE FROM decisions")), True)
check("select then drop, with trailing semicolon",
      bool(blocked("SELECT 1; DROP TABLE runs;")), True)

print("\n=== a value that merely looks like a keyword is fine ===")
# 'amendment_request' contains no keyword, but a naive scanner tripping on
# words inside string literals would be a constant annoyance. Verify that
# literals are excluded from keyword scanning.
check("literal containing CREATE is not blocked",
      blocked("SELECT run_id FROM decisions WHERE rationale = 'we did not CREATE this'"), False)
check("literal containing DELETE is not blocked",
      blocked("SELECT run_id FROM decisions WHERE rationale = 'DELETE was requested'"), False)

print("\n=== the read-only connection is the real backstop ===")
# Even if every guard above were bypassed, the connection cannot write.
if settings.db_path.exists():
    conn = sqlite3.connect(f"file:{settings.db_path}?mode=ro", uri=True)
    try:
        conn.execute("DELETE FROM decisions")
        check("a write on the read-only connection", "succeeded (BAD)", "raised")
    except sqlite3.OperationalError as exc:
        check("a write on the read-only connection raises", "readonly" in str(exc).lower(), True)
    finally:
        conn.close()
else:
    print("  (no database yet - run the pipeline first to exercise this)")

print("\n=== answers are grounded in the rows returned ===")
rows = [["flag_for_review", 3]]
check("a figure present in the rows is accepted",
      verify_answer("3 shipments were flagged for review.", rows), [])
check("an invented figure is caught",
      bool(verify_answer("7 shipments were flagged for review.", rows)), True)
check("3.0 and 3 are treated as the same number",
      verify_answer("There were 3 flagged.", [["x", 3.0]]), [])
check("an empty result set imposes no constraint",
      verify_answer("Nothing matched.", []), [])

# A number inside an identifier is not a claim about the data. Without this,
# "gpt-4.1" was read as the figure 4.1 and every cost answer was rejected.
check("a version number inside a model name is not treated as a figure",
      verify_answer("gpt-4.1 cost 0.029668 and gpt-4.1-mini cost 0.0020736.",
                    [["gpt-4.1", 0.029668], ["gpt-4.1-mini", 0.0020736]]), [])

# Rounding for readability is honest; inventing a figure is not.
check("rounding a returned value to stated precision is accepted",
      verify_answer("Total spend was about 0.03.", [["gpt-4.1", 0.029668]]), [])
check("a figure that no rounding produces is still caught",
      bool(verify_answer("Total spend was 0.09.", [["gpt-4.1", 0.029668]])), True)
check("an invented count is caught even alongside a real one",
      bool(verify_answer("3 approved and 7 flagged.", [["approved", 3]])), True)

print("\n=== deterministic fallback rendering ===")
check("empty results render plainly", render_rows(["n"], []), "No records matched that question.")
check("a single scalar renders plainly", render_rows(["total"], [[42]]), "total: 42")

print(f"\n{'ALL PASS' if failures == 0 else str(failures) + ' FAILURE(S)'}")
sys.exit(1 if failures else 0)
