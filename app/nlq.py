"""Natural-language query over stored results -- Behaviour D's query half.

A non-engineer asks "how many shipments were flagged this week?" and gets an
answer they can check.

Safety is layered, because a model writing SQL against a production database
is exactly the place not to rely on a prompt behaving:

  1. Prompt        asks for a single read-only SELECT.
  2. Static guard  rejects anything that is not one statement starting with
                   SELECT or WITH, and rejects write/DDL/PRAGMA keywords.
  3. Read-only DB  the connection is opened mode=ro, so even a query that
                   defeats the guard physically cannot write. This is the
                   backstop that actually matters -- the first two are
                   convenience, this one is the guarantee.
  4. Row cap       a LIMIT is injected if the model did not write one.

Grounding matters as much as safety. The answer is phrased by a model, but
every number in it is checked against the rows actually returned; an
unsupported figure falls back to a plain rendering of the result set. And
the SQL and rows are always shown, so the answer can be verified rather
than trusted.

The schema handed to the model is introspected from the live database, so it
cannot drift out of date the way a hardcoded description would.
"""
from __future__ import annotations

import re
import sqlite3
import time
from typing import Any, Optional

from pydantic import BaseModel

from app import store
from app.config import settings
from app.llm import RunBudget, call_structured, text_part

MAX_ROWS = 200

FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|ATTACH|DETACH|"
    r"PRAGMA|VACUUM|REINDEX|TRIGGER|GRANT|REVOKE)\b",
    re.IGNORECASE,
)

# Tables the query layer is allowed to see.
EXPOSED_TABLES = [
    "shipments", "documents", "runs", "extracted_fields",
    "validations", "decisions", "agent_spans",
]


class UnsafeQuery(ValueError):
    """The generated SQL failed a guard and was not executed."""


class GeneratedSQL(BaseModel):
    sql: str
    reasoning: str


class Answer(BaseModel):
    answer: str


class QueryResult(BaseModel):
    question: str
    sql: str
    columns: list[str]
    rows: list[list[Any]]
    answer: str
    row_count: int
    usd_cost: float = 0.0
    latency_ms: int = 0
    warnings: list[str] = []


# ------------------------------------------------------------------ schema
def describe_schema() -> str:
    """Introspect the live database so the prompt can never go stale."""
    conn = store.connect()
    try:
        lines: list[str] = []
        for table in EXPOSED_TABLES:
            cols = list(conn.execute(f"PRAGMA table_info({table})"))
            if not cols:
                continue
            rendered = ", ".join(f"{c['name']} {c['type'] or 'TEXT'}" for c in cols)
            lines.append(f"{table}({rendered})")
        return "\n".join(lines)
    finally:
        conn.close()


SQL_INSTRUCTIONS = """You translate questions about a trade-document \
validation system into a single SQLite SELECT statement.

Rules:
- Exactly one statement. It must start with SELECT or WITH. No semicolons.
- Read only. Never write, alter, or use PRAGMA.
- Prefer explicit column lists over SELECT *.
- Return the columns needed to answer the question and nothing more.

Domain notes:
- decisions.decision is one of: auto_approve, flag_for_review,
  amendment_request. "Flagged" or "needs review" means flag_for_review.
  "Needs fixing" or "sent back" means amendment_request.
- validations.status is one of: match, mismatch, uncertain, not_applicable.
  "Uncertain" means the field could not be read reliably.
- validations.severity is blocking, warning or info.
- extracted_fields.verdict is found, uncertain or not_found, and
  final_confidence is 0..1 after verification.
- agent_spans holds one row per LLM call, with usd and latency_ms. Use it
  for questions about cost or speed.
- Timestamps are ISO-8601 UTC strings, so date(decided_at) and
  datetime('now') both work. "This week" means the last 7 days."""

ANSWER_INSTRUCTIONS = """Answer the question in one or two plain sentences, \
using ONLY the rows given to you.

- Every number you state must appear in the rows. Do not compute new
  figures, do not estimate, do not round into a different number.
- If the result set is empty, say plainly that nothing matched. Do not
  speculate about why.
- No preamble, no restating the question, no offers of further help.
- Write for a logistics operations person, not an engineer. Do not mention
  SQL, tables, or columns."""


# ------------------------------------------------------------------ guards
def guard_sql(sql: str, max_rows: int = MAX_ROWS) -> str:
    """Reject anything that is not a single read-only SELECT. Force a LIMIT."""
    cleaned = sql.strip().rstrip(";").strip()
    if not cleaned:
        raise UnsafeQuery("empty query")

    if ";" in cleaned:
        raise UnsafeQuery("multiple statements are not allowed")

    if not re.match(r"^\s*(SELECT|WITH)\b", cleaned, re.IGNORECASE):
        raise UnsafeQuery("query must start with SELECT or WITH")

    # Strip string literals before keyword scanning, so a value like
    # 'amendment_request' can never trip the DDL check.
    without_literals = re.sub(r"'[^']*'", "''", cleaned)
    hit = FORBIDDEN.search(without_literals)
    if hit:
        raise UnsafeQuery(f"forbidden keyword: {hit.group(0).upper()}")

    if not re.search(r"\bLIMIT\s+\d+\s*$", cleaned, re.IGNORECASE):
        cleaned = f"{cleaned} LIMIT {max_rows}"
    return cleaned


def run_readonly(sql: str) -> tuple[list[str], list[list[Any]]]:
    """Execute against a mode=ro connection: writes are impossible here."""
    if not settings.db_path.exists():
        raise FileNotFoundError(
            f"no database at {settings.db_path}. Run the pipeline on a document first."
        )
    conn = sqlite3.connect(f"file:{settings.db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(sql)
        rows = cur.fetchmany(MAX_ROWS)
        columns = [d[0] for d in cur.description] if cur.description else []
        return columns, [list(r) for r in rows]
    finally:
        conn.close()


# A standalone number: not glued to letters, hyphens, or a decimal
# continuation. Two boundary bugs found while testing this:
#   without the lookbehind, "gpt-4.1" yields the figure 4.1 and every cost
#   answer gets rejected;
#   with "." blanket-excluded in the lookahead, a sentence-ending period
#   ("spend was 0.09.") matched nothing at all, so the number went unchecked
#   entirely -- a guard that silently passes is worse than no guard.
_STANDALONE_NUMBER = re.compile(r"(?<![\w.-])\d+(?:\.\d+)?(?![\w-])(?!\.\d)")


def verify_answer(answer: str, rows: list[list[Any]]) -> list[str]:
    """Every number stated in the answer must be supported by the rows.

    Supported means either present verbatim, or reproducible by rounding a
    returned value to the precision the answer used. Rounding 0.029668 to
    "0.03" is honest phrasing; claiming 7 when the row says 3 is not.
    """
    if not rows:
        return []

    literals: set[str] = set()
    numbers: list[float] = []
    for row in rows:
        for cell in row:
            if cell is None:
                continue
            text = str(cell)
            literals.add(text)
            literals.add(text.replace(",", ""))
            try:
                value = float(text.replace(",", ""))
            except (TypeError, ValueError):
                continue
            numbers.append(value)
            literals.add(str(int(value)) if value.is_integer() else str(value))

    problems: list[str] = []
    for token in _STANDALONE_NUMBER.findall(answer):
        if token in literals:
            continue
        try:
            stated = float(token)
        except ValueError:
            continue
        if any(stated == v for v in numbers):
            continue
        # Rounding to the precision the answer chose must reproduce it.
        decimals = len(token.split(".")[1]) if "." in token else 0
        if any(round(v, decimals) == stated for v in numbers):
            continue
        problems.append(f"answer cites {token}, which is not supported by the result set")
    return problems


def render_rows(columns: list[str], rows: list[list[Any]], limit: int = 5) -> str:
    """Deterministic fallback phrasing, used when a generated answer fails."""
    if not rows:
        return "No records matched that question."
    if len(rows) == 1 and len(columns) == 1:
        return f"{columns[0]}: {rows[0][0]}"
    head = ["; ".join(f"{c}={v}" for c, v in zip(columns, r)) for r in rows[:limit]]
    more = "" if len(rows) <= limit else f" (and {len(rows) - limit} more)"
    return f"{len(rows)} row(s): " + " | ".join(head) + more


# ------------------------------------------------------------------- agent
def ask(question: str, *, budget: Optional[RunBudget] = None) -> QueryResult:
    started = time.perf_counter()
    budget = budget or RunBudget()
    before = budget.usd
    warnings: list[str] = []

    generated = call_structured(
        name="nlq_generate_sql",
        model=settings.cheap_model,
        instructions=SQL_INSTRUCTIONS,
        content=[
            text_part(
                f"Schema:\n{describe_schema()}\n\n"
                f"Current UTC time: {store.now()}\n\n"
                f"Question: {question}"
            )
        ],
        schema=GeneratedSQL,
        budget=budget,
        max_output_tokens=600,
    )

    sql = guard_sql(generated.sql)
    columns, rows = run_readonly(sql)

    try:
        phrased = call_structured(
            name="nlq_answer",
            model=settings.cheap_model,
            instructions=ANSWER_INSTRUCTIONS,
            content=[
                text_part(
                    f"Question: {question}\n\n"
                    f"Columns: {columns}\n"
                    f"Rows ({len(rows)}): {rows[:40]}"
                )
            ],
            schema=Answer,
            budget=budget,
            max_output_tokens=300,
        )
        answer = phrased.answer.strip()
        problems = verify_answer(answer, rows)
        if problems:
            warnings += problems + ["generated answer rejected; showing the result set"]
            answer = render_rows(columns, rows)
    except Exception as exc:
        warnings.append(f"answer phrasing failed ({type(exc).__name__}); showing the result set")
        answer = render_rows(columns, rows)

    return QueryResult(
        question=question,
        sql=sql,
        columns=columns,
        rows=rows,
        answer=answer,
        row_count=len(rows),
        usd_cost=round(budget.usd - before, 6),
        latency_ms=int((time.perf_counter() - started) * 1000),
        warnings=warnings,
    )
