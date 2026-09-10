"""SQLite persistence -- Behaviour D's storage half.

Two things drive this schema:

1. Fields are rows, not JSON blobs. "Which fields most often need a human?"
   and "how many shipments were flagged this week?" should be one SELECT,
   not a JSON walk. The natural-language query layer sits directly on these
   tables, so the schema is the thing the model has to understand -- short
   names, obvious types, no surprises.

2. Every stage writes as it completes. Extraction is durable before
   validation starts. Combined with the LangGraph checkpointer this is what
   makes a crash mid-pipeline survivable: the expensive vision call is
   already on disk and is never paid for twice.

`agent_spans` is the observability table. One row per LLM call, carrying
tokens, cost, latency and retry count, joined to a run and a document. That
is how a single shipment is traced end to end.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

from app.config import settings
from app.llm import RunBudget
from app.schemas import DocumentBundle, ExtractionOutput, RouterOutput, ValidationOutput

SCHEMA = """
CREATE TABLE IF NOT EXISTS shipments (
    shipment_id TEXT PRIMARY KEY,
    customer    TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    doc_id       TEXT PRIMARY KEY,
    shipment_id  TEXT REFERENCES shipments(shipment_id),
    filename     TEXT NOT NULL,
    source_path  TEXT NOT NULL,
    doc_type     TEXT,
    page_count   INTEGER,
    text_source  TEXT,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    run_id       TEXT PRIMARY KEY,
    doc_id       TEXT REFERENCES documents(doc_id),
    shipment_id  TEXT,
    customer     TEXT,
    model        TEXT,
    status       TEXT NOT NULL,          -- running | completed | failed
    stage        TEXT,                   -- last stage that completed
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    usd_cost     REAL DEFAULT 0,
    latency_ms   INTEGER DEFAULT 0,
    error        TEXT
);

CREATE TABLE IF NOT EXISTS extracted_fields (
    run_id            TEXT NOT NULL,
    doc_id            TEXT NOT NULL,
    name              TEXT NOT NULL,
    label             TEXT,
    value             TEXT,
    verdict           TEXT,              -- found | uncertain | not_found
    final_confidence  REAL,
    model_confidence  REAL,
    grounding_score   REAL,
    grounded          INTEGER,
    format_valid      INTEGER,
    evidence_quote    TEXT,
    evidence_page     INTEGER,
    reasoning         TEXT,
    flags             TEXT,
    PRIMARY KEY (run_id, name)
);

CREATE TABLE IF NOT EXISTS validations (
    run_id             TEXT NOT NULL,
    doc_id             TEXT NOT NULL,
    name               TEXT NOT NULL,
    label              TEXT,
    status             TEXT,             -- match | mismatch | uncertain | not_applicable
    found              TEXT,
    expected           TEXT,
    rule               TEXT,
    severity           TEXT,             -- blocking | warning | info
    message            TEXT,
    why                TEXT,
    provisional_status TEXT,
    confidence         REAL,
    PRIMARY KEY (run_id, name)
);

CREATE TABLE IF NOT EXISTS decisions (
    run_id            TEXT PRIMARY KEY,
    doc_id            TEXT NOT NULL,
    shipment_id       TEXT,
    customer          TEXT,
    decision          TEXT NOT NULL,     -- auto_approve | flag_for_review | amendment_request
    rationale         TEXT,
    policy_reasons    TEXT,
    lowest_confidence REAL,
    n_mismatch        INTEGER DEFAULT 0,
    n_uncertain       INTEGER DEFAULT 0,
    draft_subject     TEXT,
    draft_body        TEXT,
    draft_source      TEXT,
    warnings          TEXT,              -- why a generated draft was rejected, etc.
    email_sent        INTEGER DEFAULT 0, -- always 0: a human sends, never the agent
    decided_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_spans (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT NOT NULL,
    doc_id        TEXT,
    node          TEXT,
    name          TEXT,
    model         TEXT,
    input_tokens  INTEGER,
    output_tokens INTEGER,
    usd           REAL,
    latency_ms    INTEGER,
    attempts      INTEGER,
    status        TEXT,
    error         TEXT,
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_decisions_decision ON decisions(decision);
CREATE INDEX IF NOT EXISTS idx_decisions_at       ON decisions(decided_at);
CREATE INDEX IF NOT EXISTS idx_fields_verdict     ON extracted_fields(verdict);
CREATE INDEX IF NOT EXISTS idx_valid_status       ON validations(status);
CREATE INDEX IF NOT EXISTS idx_spans_run          ON agent_spans(run_id);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Columns added after the first schema shipped. CREATE TABLE IF NOT EXISTS
# will not add them to an existing database, so they are applied explicitly.
MIGRATIONS = [("decisions", "warnings", "TEXT")]


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, coltype in MIGRATIONS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


def connect(readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(f"file:{settings.db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()
    return conn


def init() -> None:
    connect().close()


# ------------------------------------------------------------------ writes
def upsert_shipment(conn: sqlite3.Connection, shipment_id: str, customer: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO shipments (shipment_id, customer, created_at) VALUES (?,?,?)",
        (shipment_id, customer, now()),
    )


def upsert_document(conn: sqlite3.Connection, bundle: DocumentBundle, shipment_id: str) -> None:
    conn.execute(
        """INSERT INTO documents (doc_id, shipment_id, filename, source_path, doc_type,
                                  page_count, text_source, created_at)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(doc_id) DO UPDATE SET
             shipment_id=excluded.shipment_id, text_source=excluded.text_source""",
        (
            bundle.doc_id, shipment_id, bundle.filename, bundle.source_path,
            "BILL_OF_LADING", len(bundle.pages),
            bundle.pages[0].text_source if bundle.pages else "none", now(),
        ),
    )


def start_run(
    conn: sqlite3.Connection, run_id: str, doc_id: str, shipment_id: str, customer: str, model: str
) -> None:
    conn.execute(
        """INSERT INTO runs (run_id, doc_id, shipment_id, customer, model, status, stage, started_at)
           VALUES (?,?,?,?,?,'running','ingest',?)
           ON CONFLICT(run_id) DO UPDATE SET status='running'""",
        (run_id, doc_id, shipment_id, customer, model, now()),
    )


def mark_stage(conn: sqlite3.Connection, run_id: str, stage: str) -> None:
    conn.execute("UPDATE runs SET stage=? WHERE run_id=?", (stage, run_id))


def run_cost(conn: sqlite3.Connection, run_id: str) -> float:
    """Total spend for a run, from the durable span records.

    Not from the in-process budget: that resets when a crashed run resumes,
    which silently under-reported a resumed run as $0.00101 against an actual
    $0.01064. Spans are written as each call completes, so they survive the
    crash and are the only trustworthy source for cost.
    """
    row = conn.execute(
        "SELECT COALESCE(SUM(usd), 0) AS total FROM agent_spans WHERE run_id=?", (run_id,)
    ).fetchone()
    return float(row["total"] if row else 0.0)


def finish_run(
    conn: sqlite3.Connection, run_id: str, status: str, usd: float, latency_ms: int,
    error: Optional[str] = None,
) -> None:
    conn.execute(
        "UPDATE runs SET status=?, finished_at=?, usd_cost=?, latency_ms=?, error=? WHERE run_id=?",
        (status, now(), round(usd, 6), latency_ms, error, run_id),
    )


def save_extraction(conn: sqlite3.Connection, run_id: str, ext: ExtractionOutput) -> None:
    conn.executemany(
        """INSERT INTO extracted_fields
             (run_id, doc_id, name, label, value, verdict, final_confidence, model_confidence,
              grounding_score, grounded, format_valid, evidence_quote, evidence_page,
              reasoning, flags)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(run_id, name) DO UPDATE SET
             value=excluded.value, verdict=excluded.verdict,
             final_confidence=excluded.final_confidence, flags=excluded.flags""",
        [
            (
                run_id, ext.doc_id, f.name, f.label, f.value, f.verdict.value,
                f.final_confidence, f.model_confidence, f.grounding_score,
                int(f.grounded), None if f.format_valid is None else int(f.format_valid),
                f.evidence_quote, f.evidence_page, f.reasoning, json.dumps(f.flags),
            )
            for f in ext.fields
        ],
    )


def save_validation(conn: sqlite3.Connection, run_id: str, val: ValidationOutput) -> None:
    conn.executemany(
        """INSERT INTO validations
             (run_id, doc_id, name, label, status, found, expected, rule, severity,
              message, why, provisional_status, confidence)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(run_id, name) DO UPDATE SET
             status=excluded.status, found=excluded.found, message=excluded.message""",
        [
            (
                run_id, val.doc_id, r.name, r.label, r.status.value, r.found, r.expected,
                r.rule, r.severity, r.message, r.why,
                r.provisional_status.value if r.provisional_status else None,
                r.extraction_confidence,
            )
            for r in val.results
        ],
    )


def save_decision(
    conn: sqlite3.Connection, run_id: str, shipment_id: str, customer: str,
    dec: RouterOutput, val: ValidationOutput,
) -> None:
    from app.schemas import MatchStatus

    conn.execute(
        """INSERT INTO decisions
             (run_id, doc_id, shipment_id, customer, decision, rationale, policy_reasons,
              lowest_confidence, n_mismatch, n_uncertain, draft_subject, draft_body,
              draft_source, warnings, email_sent, decided_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?)
           ON CONFLICT(run_id) DO UPDATE SET
             decision=excluded.decision, rationale=excluded.rationale,
             decided_at=excluded.decided_at""",
        (
            run_id, dec.doc_id, shipment_id, customer, dec.decision.value, dec.rationale,
            json.dumps(dec.policy_reasons), dec.lowest_confidence,
            val.count(MatchStatus.MISMATCH), val.count(MatchStatus.UNCERTAIN),
            dec.draft_email.subject if dec.draft_email else None,
            dec.draft_email.body if dec.draft_email else None,
            dec.draft_email.source if dec.draft_email else None,
            json.dumps(dec.warnings),
            now(),
        ),
    )


def save_spans(
    conn: sqlite3.Connection, run_id: str, doc_id: str, node: str, budget: RunBudget, offset: int
) -> int:
    """Persist spans recorded since `offset`. Returns the new offset."""
    new = budget.spans[offset:]
    conn.executemany(
        """INSERT INTO agent_spans
             (run_id, doc_id, node, name, model, input_tokens, output_tokens, usd,
              latency_ms, attempts, status, error, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [
            (
                run_id, doc_id, node, s.name, s.model, s.input_tokens, s.output_tokens,
                s.usd, s.latency_ms, s.attempts, s.status, s.error, now(),
            )
            for s in new
        ],
    )
    return len(budget.spans)


# ------------------------------------------------------------------- reads
def fetch_run(run_id: str) -> Optional[dict[str, Any]]:
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["fields"] = [dict(r) for r in conn.execute(
            "SELECT * FROM extracted_fields WHERE run_id=?", (run_id,))]
        out["validations"] = [dict(r) for r in conn.execute(
            "SELECT * FROM validations WHERE run_id=?", (run_id,))]
        d = conn.execute("SELECT * FROM decisions WHERE run_id=?", (run_id,)).fetchone()
        out["decision"] = dict(d) if d else None
        out["spans"] = [dict(r) for r in conn.execute(
            "SELECT * FROM agent_spans WHERE run_id=? ORDER BY id", (run_id,))]
        return out
    finally:
        conn.close()


def recent_runs(limit: int = 50) -> list[dict[str, Any]]:
    conn = connect()
    try:
        return [dict(r) for r in conn.execute(
            """SELECT r.run_id, r.doc_id, r.shipment_id, r.status, r.stage, r.started_at,
                      r.usd_cost, r.latency_ms, d.filename, dec.decision, dec.n_mismatch,
                      dec.n_uncertain
                 FROM runs r
                 LEFT JOIN documents d  ON d.doc_id = r.doc_id
                 LEFT JOIN decisions dec ON dec.run_id = r.run_id
                ORDER BY r.started_at DESC LIMIT ?""", (limit,))]
    finally:
        conn.close()
