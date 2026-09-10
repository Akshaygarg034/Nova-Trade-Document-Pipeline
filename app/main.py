"""FastAPI backend for the operator UI -- Behaviour E.

The pipeline is synchronous and takes 10-30 seconds, which is far too long
to block an HTTP request. So an upload starts the run on a worker thread and
returns immediately; the browser follows progress over SSE.

Progress is read from `runs.stage` in the database rather than piped out of
the graph through a queue. That is deliberate: the stage column is written
and committed by each node as it finishes, so it survives a server restart
and is the same value a second browser tab (or a support engineer running
SQL) would see. One source of truth beats two.
"""
from __future__ import annotations

import asyncio
import glob
import json
import pathlib
import shutil
import uuid
from typing import Any, Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import store
from app.config import ROOT, settings
from app.graph import run_pipeline
from app.llm import RunBudget
from app.nlq import UnsafeQuery, ask

app = FastAPI(title="Nova Trade Document Pipeline", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

UPLOADS = settings.data_dir / "uploads"
UPLOADS.mkdir(parents=True, exist_ok=True)

STAGES = ["ingest", "extract", "validate", "route", "completed"]


# ------------------------------------------------------------------ models
class AskRequest(BaseModel):
    question: str


class SendRequest(BaseModel):
    body: Optional[str] = None
    subject: Optional[str] = None


# ------------------------------------------------------------------ runs
def _execute(run_id: str, path: str, customer: str, shipment_id: str) -> None:
    """Worker-thread entry point. Failures are recorded, never swallowed."""
    try:
        run_pipeline(path, customer=customer, shipment_id=shipment_id, run_id=run_id)
    except Exception as exc:  # already persisted as status='failed' by the graph
        print(f"[run {run_id}] failed: {type(exc).__name__}: {exc}")


@app.post("/api/runs")
async def create_run(
    background: BackgroundTasks,
    file: UploadFile,
    customer: str = "acme_electronics",
    shipment_id: Optional[str] = None,
) -> dict[str, str]:
    if not file.filename:
        raise HTTPException(400, "no filename")

    suffix = pathlib.Path(file.filename).suffix.lower()
    if suffix not in {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp", ".bmp"}:
        raise HTTPException(400, f"unsupported file type: {suffix}")

    run_id = f"run-{uuid.uuid4().hex[:10]}"

    # Keep the operator's own filename, in a per-run folder to avoid
    # collisions. Saving as "<run_id>.pdf" put the internal run id into the
    # supplier-facing email subject, which is precisely what document_ref
    # exists to prevent.
    safe_name = pathlib.Path(file.filename).name
    run_dir = UPLOADS / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    dest = run_dir / safe_name
    with dest.open("wb") as fh:
        shutil.copyfileobj(file.file, fh)

    shipment_id = shipment_id or pathlib.Path(file.filename).stem[:24]
    store.init()
    background.add_task(_execute, run_id, str(dest), customer, shipment_id)
    return {"run_id": run_id, "filename": file.filename, "shipment_id": shipment_id}


@app.get("/api/runs")
def list_runs(limit: int = 50) -> list[dict[str, Any]]:
    store.init()
    return store.recent_runs(limit)


@app.get("/api/runs/{run_id}")
def get_run(run_id: str) -> dict[str, Any]:
    row = store.fetch_run(run_id)
    if row is None:
        raise HTTPException(404, f"no run {run_id}")
    return _shape(row)


def _shape(row: dict[str, Any]) -> dict[str, Any]:
    """Join extraction and validation into one row per field, for the table."""
    validations = {v["name"]: v for v in row["validations"]}
    fields = []
    for f in row["fields"]:
        v = validations.get(f["name"], {})
        fields.append({
            **f,
            "flags": json.loads(f["flags"] or "[]"),
            "grounded": bool(f["grounded"]),
            "status": v.get("status"),
            "expected": v.get("expected"),
            "severity": v.get("severity"),
            "message": v.get("message"),
            "why": v.get("why"),
            "provisional_status": v.get("provisional_status"),
        })

    order = [
        "consignee_name", "hs_code", "port_of_loading", "port_of_discharge",
        "incoterms", "description_of_goods", "gross_weight", "invoice_number",
    ]
    fields.sort(key=lambda f: order.index(f["name"]) if f["name"] in order else 99)

    decision = row["decision"]
    if decision:
        decision = {
            **decision,
            "policy_reasons": json.loads(decision["policy_reasons"] or "[]"),
            "warnings": json.loads(decision["warnings"] or "[]"),
        }

    return {
        "run_id": row["run_id"],
        "doc_id": row["doc_id"],
        "shipment_id": row["shipment_id"],
        "status": row["status"],
        "stage": row["stage"],
        "usd_cost": row["usd_cost"],
        "latency_ms": row["latency_ms"],
        "error": row["error"],
        "fields": fields,
        "decision": decision,
        "spans": row["spans"],
    }


@app.get("/api/runs/{run_id}/stream")
async def stream_run(run_id: str) -> StreamingResponse:
    """Server-sent events reporting stage transitions until the run settles."""

    async def events():
        last = None
        for _ in range(600):  # ~5 minutes at 500ms, then give up
            row = store.fetch_run(run_id)
            if row is None:
                yield f"data: {json.dumps({'status': 'pending'})}\n\n"
            else:
                stage = row["stage"] if row["status"] != "completed" else "completed"
                payload = {
                    "status": row["status"],
                    "stage": stage,
                    "progress": STAGES.index(stage) + 1 if stage in STAGES else 0,
                    "total": len(STAGES),
                }
                if payload != last:
                    yield f"data: {json.dumps(payload)}\n\n"
                    last = payload
                if row["status"] in ("completed", "failed"):
                    yield f"data: {json.dumps({'status': row['status'], 'done': True})}\n\n"
                    return
            await asyncio.sleep(0.5)
        yield f"data: {json.dumps({'status': 'timeout', 'done': True})}\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/documents/{doc_id}/page")
def page_image(doc_id: str, n: int = 1) -> FileResponse:
    """The rendered page, so the operator can see the document beside the fields."""
    matches = sorted(glob.glob(str(settings.render_dir / doc_id / f"page-{n}*.png")))
    matches = [m for m in matches if "-hi" not in m and "-enhanced" not in m] or matches
    if not matches:
        raise HTTPException(404, "page image not found")
    return FileResponse(matches[0], media_type="image/png")


@app.post("/api/runs/{run_id}/send")
def mark_sent(run_id: str, req: SendRequest) -> dict[str, Any]:
    """Record that a HUMAN sent the draft. Nothing here talks to a mail server.

    The agent never sends. This endpoint exists to log the operator's action
    and store whatever edits they made, which is the audit trail for "who
    approved this and what did they actually send".
    """
    conn = store.connect()
    try:
        row = conn.execute("SELECT run_id FROM decisions WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise HTTPException(404, f"no decision for run {run_id}")
        conn.execute(
            """UPDATE decisions
                  SET email_sent=1,
                      draft_body=COALESCE(?, draft_body),
                      draft_subject=COALESCE(?, draft_subject)
                WHERE run_id=?""",
            (req.body, req.subject, run_id),
        )
        conn.commit()
    finally:
        conn.close()
    return {"run_id": run_id, "email_sent": True}


@app.post("/api/ask")
def ask_question(req: AskRequest) -> dict[str, Any]:
    if not req.question.strip():
        raise HTTPException(400, "empty question")
    try:
        result = ask(req.question, budget=RunBudget(max_calls=6, max_usd=0.10))
    except UnsafeQuery as exc:
        raise HTTPException(400, f"query blocked: {exc}") from exc
    except FileNotFoundError as exc:
        raise HTTPException(409, str(exc)) from exc
    return result.model_dump()


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "extractor_model": settings.extractor_model,
        "cheap_model": settings.cheap_model,
        "db": str(settings.db_path),
        "key_configured": bool(settings.openai_api_key),
    }


# Serve the built UI when it exists, so one process runs everything.
_dist = ROOT / "ui" / "dist"
if _dist.exists():
    app.mount("/", StaticFiles(directory=str(_dist), html=True), name="ui")
