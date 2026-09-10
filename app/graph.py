"""The pipeline as a LangGraph state machine.

  ingest -> extract -> validate -> route -> finalise

Why a graph rather than four function calls in a row: durability. Every node
checkpoints to SQLite when it completes, so a crash mid-pipeline resumes at
the node that failed instead of restarting. That matters because the cost is
not evenly distributed -- extraction is ~95% of the spend and ~90% of the
latency, and re-running it because the router timed out is money set on fire.

Each node also commits its own domain output to the application tables. The
checkpointer makes the graph resumable; the tables make the result queryable.
Those are different jobs and it is worth keeping both.

`crash_after` exists to prove the property rather than assert it: it raises
after a named node so the resume path can be demonstrated on a real run.
"""
from __future__ import annotations

import time
import uuid
from typing import Any, Optional, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from app import store
from app.agents.extractor import extract_cached
from app.agents.router import route as run_route
from app.agents.validator import validate as run_validate
from app.config import settings
from app.llm import BudgetExceeded, RunBudget
from app.preprocess import load_document


class PipelineState(TypedDict, total=False):
    # inputs
    run_id: str
    source_path: str
    shipment_id: str
    customer: str
    model: Optional[str]
    crash_after: Optional[str]

    # carried between nodes (serialised into the checkpoint)
    doc_id: str
    filename: str
    extraction: dict[str, Any]
    validation: dict[str, Any]
    decision: dict[str, Any]

    # bookkeeping
    stage: str
    started_ms: float
    span_offset: int
    usd: float
    error: Optional[str]


def _maybe_crash(state: PipelineState, node: str) -> None:
    if state.get("crash_after") == node:
        raise RuntimeError(f"simulated crash after '{node}' (--crash-after)")


# ------------------------------------------------------------------ nodes
def node_ingest(state: PipelineState) -> dict[str, Any]:
    bundle = load_document(state["source_path"])
    conn = store.connect()
    try:
        store.upsert_shipment(conn, state["shipment_id"], state["customer"])
        store.upsert_document(conn, bundle, state["shipment_id"])
        store.start_run(
            conn, state["run_id"], bundle.doc_id, state["shipment_id"],
            state["customer"], state.get("model") or settings.extractor_model,
        )
        conn.commit()
    finally:
        conn.close()

    _maybe_crash(state, "ingest")
    return {
        "doc_id": bundle.doc_id,
        "filename": bundle.filename,
        "stage": "ingest",
        "started_ms": time.perf_counter(),
        "span_offset": 0,
        "usd": 0.0,
    }


def node_extract(state: PipelineState) -> dict[str, Any]:
    budget = RunBudget()
    budget.usd = state.get("usd", 0.0)  # carry spend across a resume

    bundle = load_document(state["source_path"])
    ext, was_cached = extract_cached(bundle, model=state.get("model"), budget=budget)

    conn = store.connect()
    try:
        store.save_extraction(conn, state["run_id"], ext)
        offset = store.save_spans(
            conn, state["run_id"], ext.doc_id, "extract", budget, state.get("span_offset", 0)
        )
        store.mark_stage(conn, state["run_id"], "extract")
        conn.commit()
    finally:
        conn.close()

    _maybe_crash(state, "extract")
    return {
        "extraction": ext.model_dump(mode="json"),
        "stage": "extract",
        "span_offset": offset,
        "usd": budget.usd,
    }


def node_validate(state: PipelineState) -> dict[str, Any]:
    from app.schemas import ExtractionOutput

    budget = RunBudget()
    budget.usd = state.get("usd", 0.0)

    ext = ExtractionOutput(**state["extraction"])
    val = run_validate(ext, state["customer"], budget=budget)

    conn = store.connect()
    try:
        store.save_validation(conn, state["run_id"], val)
        offset = store.save_spans(conn, state["run_id"], ext.doc_id, "validate", budget, 0)
        store.mark_stage(conn, state["run_id"], "validate")
        conn.commit()
    finally:
        conn.close()

    _maybe_crash(state, "validate")
    return {
        "validation": val.model_dump(mode="json"),
        "stage": "validate",
        "span_offset": state.get("span_offset", 0) + offset,
        "usd": budget.usd,
    }


def node_route(state: PipelineState) -> dict[str, Any]:
    from app.schemas import ValidationOutput

    budget = RunBudget()
    budget.usd = state.get("usd", 0.0)

    val = ValidationOutput(**state["validation"])
    dec = run_route(val, budget=budget)

    conn = store.connect()
    try:
        store.save_decision(conn, state["run_id"], state["shipment_id"], state["customer"], dec, val)
        offset = store.save_spans(conn, state["run_id"], dec.doc_id, "route", budget, 0)
        store.mark_stage(conn, state["run_id"], "route")
        conn.commit()
    finally:
        conn.close()

    _maybe_crash(state, "route")
    return {
        "decision": dec.model_dump(mode="json"),
        "stage": "route",
        "span_offset": state.get("span_offset", 0) + offset,
        "usd": budget.usd,
    }


def node_finalise(state: PipelineState) -> dict[str, Any]:
    latency = int((time.perf_counter() - state.get("started_ms", time.perf_counter())) * 1000)
    conn = store.connect()
    try:
        # Cost comes from the span records, not the carried budget: a resumed
        # run starts a fresh budget and would otherwise report near zero.
        store.finish_run(
            conn, state["run_id"], "completed", store.run_cost(conn, state["run_id"]), latency
        )
        conn.commit()
    finally:
        conn.close()
    return {"stage": "completed"}


# ------------------------------------------------------------------ graph
def build_graph(checkpointer: Any):
    g = StateGraph(PipelineState)
    g.add_node("ingest", node_ingest)
    g.add_node("extract", node_extract)
    g.add_node("validate", node_validate)
    g.add_node("route", node_route)
    g.add_node("finalise", node_finalise)

    g.add_edge(START, "ingest")
    g.add_edge("ingest", "extract")
    g.add_edge("extract", "validate")
    g.add_edge("validate", "route")
    g.add_edge("route", "finalise")
    g.add_edge("finalise", END)

    return g.compile(checkpointer=checkpointer)


def checkpoint_path() -> str:
    return str(settings.data_dir / "checkpoints.sqlite")


def run_pipeline(
    source_path: str,
    *,
    customer: str = "acme_electronics",
    shipment_id: Optional[str] = None,
    model: Optional[str] = None,
    run_id: Optional[str] = None,
    crash_after: Optional[str] = None,
) -> dict[str, Any]:
    """Execute (or resume) one document through the graph.

    Passing an existing `run_id` resumes that thread: nodes that already
    completed are replayed from the checkpoint rather than re-executed, so a
    resume after a crash in `route` costs nothing in vision tokens.
    """
    store.init()
    run_id = run_id or f"run-{uuid.uuid4().hex[:10]}"
    shipment_id = shipment_id or f"SHP-{run_id[-6:].upper()}"
    config = {"configurable": {"thread_id": run_id}}

    with SqliteSaver.from_conn_string(checkpoint_path()) as cp:
        graph = build_graph(cp)
        state: PipelineState = {
            "run_id": run_id,
            "source_path": source_path,
            "shipment_id": shipment_id,
            "customer": customer,
            "model": model,
            "crash_after": crash_after,
        }
        try:
            final = graph.invoke(state, config=config)
        except (BudgetExceeded, Exception) as exc:  # noqa: B014 - explicit for clarity
            snapshot = graph.get_state(config)
            done = (snapshot.values or {}).get("stage", "none")
            conn = store.connect()
            try:
                store.finish_run(
                    conn, run_id, "failed", store.run_cost(conn, run_id), 0,
                    f"{type(exc).__name__}: {exc}",
                )
                conn.commit()
            finally:
                conn.close()
            raise RuntimeError(
                f"run {run_id} failed after stage '{done}': {exc}. "
                f"Resume with --run-id {run_id}"
            ) from exc

    final["run_id"] = run_id
    final["shipment_id"] = shipment_id
    return final


def resume(run_id: str) -> dict[str, Any]:
    """Continue a previously failed run from its last checkpoint."""
    with SqliteSaver.from_conn_string(checkpoint_path()) as cp:
        graph = build_graph(cp)
        config = {"configurable": {"thread_id": run_id}}
        snapshot = graph.get_state(config)
        if not snapshot.values:
            raise ValueError(f"no checkpoint found for run {run_id}")

        # Clear the crash trigger so the resume actually proceeds.
        graph.update_state(config, {"crash_after": None})
        final = graph.invoke(None, config=config)

    final["run_id"] = run_id
    return final
