"""Router / Decision Agent -- Behaviour C.

Reads the Validator's output and decides one of three things: auto-approve,
flag for human review, or draft an amendment request. It explains the
decision rather than merely emitting it.

The split that matters here:

  THE DECISION IS DETERMINISTIC. A policy function maps validation state to
  an outcome. Same input, same outcome, every time, with a machine-readable
  list of the rules that fired. Nothing about "should this customs document
  be approved" belongs in a sampled token stream, and an auditor asking why
  a shipment cleared six months ago needs an answer that does not depend on
  a model version.

  THE PROSE IS GENERATED. Turning a discrepancy list into an email a freight
  operator would actually send is exactly what a language model is good at.

There is always a deterministic template email, built with no model at all.
The generated draft is used only if it passes verification: it must mention
every blocking discrepancy and must not contain approval language. If the
model is unavailable, over budget, or writes something that fails those
checks, the template ships instead. A CG operator is never left staring at
an empty compose box because an API call failed.
"""
from __future__ import annotations

import time
from typing import Optional

from pydantic import BaseModel

from app.config import settings
from app.llm import RunBudget, call_structured, text_part
from app.schemas import (
    DecisionType,
    Discrepancy,
    EmailDraft,
    MatchStatus,
    RouterOutput,
    ValidationOutput,
)


# --------------------------------------------------------------- policy
def decide(validation: ValidationOutput) -> tuple[DecisionType, list[str]]:
    """Map validation state to an outcome. Pure function, no model, no I/O."""
    reasons: list[str] = []

    uncertain = validation.uncertain
    blocking = validation.blocking_mismatches
    warnings = [
        r for r in validation.results
        if r.status == MatchStatus.MISMATCH and r.severity != "blocking"
    ]

    # Precedence is deliberate. Unread fields outrank known mismatches:
    # sending a supplier an amendment built on data we could not read risks
    # asking them to correct the wrong thing, which costs another cycle.
    if uncertain:
        reasons.append(
            f"{len(uncertain)} field(s) could not be read reliably "
            f"({', '.join(r.label for r in uncertain)})"
        )
        if blocking:
            reasons.append(
                f"{len(blocking)} confirmed blocking discrepancy also present; "
                "a draft covering those is attached for the operator"
            )
        reasons.append("policy: never auto-approve or auto-draft on unverified data")
        return DecisionType.FLAG_FOR_REVIEW, reasons

    if blocking:
        for r in blocking:
            reasons.append(f"blocking: {r.label} - {r.message}")
        return DecisionType.AMENDMENT_REQUEST, reasons

    if warnings:
        for r in warnings:
            reasons.append(f"advisory: {r.label} - {r.message}")
        return DecisionType.AMENDMENT_REQUEST, reasons

    checked = [r for r in validation.results if r.status != MatchStatus.NOT_APPLICABLE]
    if not checked:
        reasons.append("no rules applied to this document; cannot approve by default")
        return DecisionType.FLAG_FOR_REVIEW, reasons

    floor = min(r.extraction_confidence for r in checked)
    if floor < settings.auto_approve_min_confidence:
        reasons.append(
            f"all rules pass, but lowest field confidence {floor:.2f} is under the "
            f"{settings.auto_approve_min_confidence:.2f} approval floor"
        )
        return DecisionType.FLAG_FOR_REVIEW, reasons

    reasons.append(f"all {len(checked)} applicable rules matched")
    reasons.append(f"lowest field confidence {floor:.2f} clears the approval floor")
    return DecisionType.AUTO_APPROVE, reasons


# ----------------------------------------------------------- email draft
def _template_email(
    validation: ValidationOutput, discrepancies: list[Discrepancy], uncertain: list[str]
) -> EmailDraft:
    """Deterministic fallback. Always available, never pretty, always correct."""
    lines = [
        f"Reference: {validation.document_ref or validation.doc_id}",
        f"Customer: {validation.customer_name}",
        "",
        "We have reviewed the documents submitted for this shipment and cannot",
        "release them to the customer in their current form. Please amend the",
        "following and resubmit:",
        "",
    ]
    for i, d in enumerate(discrepancies, 1):
        lines += [
            f"{i}. {d.label}",
            f"   Found:    {d.found if d.found is not None else '(not present on the document)'}",
            f"   Required: {d.expected}",
            f"   Reason:   {d.why}",
            "",
        ]
    if uncertain:
        lines += [
            "In addition, the following fields were not legible on the copy we",
            "received. Please resend a clearer scan:",
            "",
        ]
        lines += [f"   - {u}" for u in uncertain]
        lines.append("")
    lines += ["Please reply with corrected documents.", "", "Regards,", "Cargo Control Group"]

    return EmailDraft(
        subject=f"Amendment required - {validation.document_ref or validation.doc_id} - {len(discrepancies)} discrepancy(ies)",
        body="\n".join(lines),
        source="template",
    )


class DraftedEmail(BaseModel):
    subject: str
    body: str


EMAIL_INSTRUCTIONS = """You write amendment emails for a Cargo Control team \
to a shipping supplier, about documents that failed validation.

Requirements:
- State every discrepancy given to you. Do not omit any, do not add any.
- For each, say what the document shows, what is required, and briefly why
  it matters commercially. The reason is supplied to you; use it, do not
  invent your own.
- Report ONLY the facts provided. Never invent a reference number, a date, a
  vessel, a container number, a deadline, or a penalty.
- Direct professional freight-industry tone. No apologising, no filler, no
  "I hope this email finds you well". The reader processes dozens of these a
  day and wants the list.
- Do not state or imply that anything has been approved or cleared.
- Under 220 words. Plain text, no markdown."""


def _verify_draft(body: str, discrepancies: list[Discrepancy]) -> list[str]:
    """Reject a draft that omits a discrepancy or implies approval."""
    problems: list[str] = []
    low = body.lower()

    for d in discrepancies:
        # Match on the distinctive words of the label, not the whole phrase.
        words = [w for w in d.label.lower().split() if len(w) > 3]
        if words and not any(w in low for w in words):
            problems.append(f"draft omits discrepancy: {d.label}")

    for phrase in ("approved", "cleared for release", "no further action", "all in order"):
        if phrase in low:
            problems.append(f"draft contains approval language: {phrase!r}")

    if len(body.split()) < 30:
        problems.append("draft is implausibly short")

    return problems


def _llm_email(
    validation: ValidationOutput,
    discrepancies: list[Discrepancy],
    uncertain: list[str],
    budget: RunBudget,
) -> tuple[Optional[EmailDraft], list[str]]:
    payload = [f"Document reference: {validation.document_ref or validation.doc_id}", f"Customer: {validation.customer_name}", "", "Discrepancies:"]
    for i, d in enumerate(discrepancies, 1):
        payload += [
            f"{i}. Field: {d.label}",
            f"   Shown on document: {d.found if d.found is not None else 'NOT PRESENT'}",
            f"   Required: {d.expected}",
            f"   Why it matters: {d.why}",
        ]
    if uncertain:
        payload += ["", "Fields illegible on the copy received (ask for a clearer scan):"]
        payload += [f"   - {u}" for u in uncertain]

    try:
        out = call_structured(
            name="router_draft_email",
            model=settings.cheap_model,
            instructions=EMAIL_INSTRUCTIONS,
            content=[text_part("\n".join(payload))],
            schema=DraftedEmail,
            budget=budget,
            max_output_tokens=900,
        )
    except Exception as exc:
        return None, [f"draft generation failed ({type(exc).__name__}); using template"]

    problems = _verify_draft(out.body, discrepancies)
    if problems:
        return None, problems + ["generated draft rejected; using template"]

    return EmailDraft(subject=out.subject.strip(), body=out.body.strip(), source="llm"), []


# ------------------------------------------------------------- rationale
class Rationale(BaseModel):
    explanation: str


RATIONALE_INSTRUCTIONS = """Explain a document-validation decision to a cargo \
operations person in two or three sentences.

You are given the decision and the exact policy reasons that produced it.
Restate them in plain language. Do not second-guess the decision, do not add
reasons that were not given, and do not hedge. If fields were unreadable, say
so directly and say what the operator should look at first."""


def _rationale(
    decision: DecisionType, reasons: list[str], validation: ValidationOutput, budget: RunBudget
) -> tuple[str, list[str]]:
    try:
        out = call_structured(
            name="router_rationale",
            model=settings.cheap_model,
            instructions=RATIONALE_INSTRUCTIONS,
            content=[
                text_part(
                    f"Decision: {decision.value}\n"
                    f"Customer: {validation.customer_name}\n"
                    "Policy reasons:\n" + "\n".join(f"- {r}" for r in reasons)
                )
            ],
            schema=Rationale,
            budget=budget,
            max_output_tokens=300,
        )
        return out.explanation.strip(), []
    except Exception as exc:
        # Deterministic fallback so the decision is never unexplained.
        return "Decision: {}. {}".format(
            decision.value.replace("_", " "), " ".join(reasons)
        ), [f"rationale generation failed ({type(exc).__name__}); using policy reasons verbatim"]


# ----------------------------------------------------------------- agent
def route(
    validation: ValidationOutput, *, budget: Optional[RunBudget] = None
) -> RouterOutput:
    started = time.perf_counter()
    budget = budget or RunBudget()
    before = budget.usd
    warnings: list[str] = []

    decision, reasons = decide(validation)

    discrepancies = [
        Discrepancy(
            field=r.name,
            label=r.label,
            found=r.found,
            expected=r.expected,
            severity=r.severity,
            why=r.why or r.message,
        )
        for r in validation.results
        if r.status == MatchStatus.MISMATCH
    ]
    uncertain = [r.label for r in validation.uncertain]

    checked = [r for r in validation.results if r.status != MatchStatus.NOT_APPLICABLE]
    floor = min((r.extraction_confidence for r in checked), default=1.0)

    draft: Optional[EmailDraft] = None
    if discrepancies or uncertain:
        # The template is built first and unconditionally: it is the floor.
        draft = _template_email(validation, discrepancies, uncertain)
        polished, problems = _llm_email(validation, discrepancies, uncertain, budget)
        warnings += problems
        if polished is not None:
            draft = polished

    rationale, rproblems = _rationale(decision, reasons, validation, budget)
    warnings += rproblems

    return RouterOutput(
        doc_id=validation.doc_id,
        decision=decision,
        policy_reasons=reasons,
        rationale=rationale,
        discrepancies=discrepancies,
        uncertain_fields=uncertain,
        lowest_confidence=round(floor, 3),
        draft_email=draft,
        usd_cost=round(budget.usd - before, 6),
        latency_ms=int((time.perf_counter() - started) * 1000),
        warnings=warnings,
    )
