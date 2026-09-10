"""Extractor Agent -- Behaviour A.

Takes any trade document (PDF or image), reads it with a vision-capable LLM,
and returns the eight canonical fields with a confidence score on each.

Two design decisions worth stating plainly:

1. Extraction is VISION-ONLY. The model never sees the PDF text layer, even
   when one exists. If it did, its quoted evidence would trivially match the
   corpus we verify against and grounding would be circular. Keeping the
   read path and the verification path independent is the whole point.

2. Escalation is per-field, not per-document. A clean page costs one call.
   Only the fields that come back weak are re-asked, at higher resolution
   with image enhancement. That is both the quality story and the cost story
   -- we do not pay 300 DPI prices for a document that was fine at 150.
"""
from __future__ import annotations

import pathlib
import time
from typing import Optional

from pydantic import BaseModel, create_model

from app.config import settings
from app.grounding import ground_field
from app.llm import RunBudget, call_structured, image_part, text_part
from app.preprocess import HIGH_DPI, enhance_image, render_pdf_page
from app.schemas import (
    CANONICAL_FIELDS,
    DocumentBundle,
    ExtractedField,
    ExtractionOutput,
    GroundedField,
    RawExtraction,
    Verdict,
)

MAX_PAGES_PER_CALL = 4

INSTRUCTIONS = """You extract structured data from international trade documents \
(Bills of Lading, Commercial Invoices, Packing Lists, Certificates of Origin).

Return the eight requested fields. For each one:

1. COPY THE VALUE EXACTLY AS PRINTED. Do not translate, reformat, expand
   abbreviations, or normalise capitalisation. If the page says
   "Acme Electronics Mfg Pte", return that, not "Acme Electronics
   Manufacturing Pte Ltd".

2. ABSENCE IS A VALID AND COMMON ANSWER. Many Bills of Lading carry no
   Incoterm and no invoice number -- those live on the Commercial Invoice.
   If a field is genuinely not printed on this document, set
   status="not_found" and value=null. Reporting a field as absent is
   CORRECT behaviour and is scored as a success. Never infer, never guess
   from context, never carry a value over from a similar document.

3. NEVER TAKE A VALUE FROM PRE-PRINTED TEXT. Ignore the form's own field
   labels, its terms and conditions, its legal boilerplate, and its carrier
   liability clauses. Only report data that was filled into the form for
   THIS shipment. In particular, three-letter sequences occurring inside
   longer English words in the terms and conditions are not Incoterms.

4. EVIDENCE IS MANDATORY when status="found". `evidence_quote` must be a
   short contiguous span of text (roughly 4-15 words) copied character for
   character off the page, and it must contain the value you reported. It
   is checked against the document independently. A quote you invented, or
   assembled from separated parts of the page, will be detected.

5. REPORT IDENTIFIERS COMPLETE. Reference numbers include their alphabetic
   prefix. If the page reads "INV NO. INV-2026-08841", the invoice number is
   "INV-2026-08841" -- not "2026-08841" and not "INV NO. INV-2026-08841".
   Take the whole token, and only that token.

6. CALIBRATE CONFIDENCE HONESTLY. Use 0.95+ only when the text is crisp and
   unambiguously labelled. Use 0.5-0.7 when characters are unclear, the
   field is inferred from an unlabelled position, or the scan is degraded.
   Use below 0.5 when you are guessing. An honest low score is far more
   useful to us than a confident wrong one.

Field notes:
- consignee_name: the CONSIGNEE (the receiving party). Not the shipper, not
  the notify party, not the delivery agent. Name only, without the address.
- hs_code: Harmonized System tariff code, 6-10 digits, often written inside
  the goods description as "HS CODE: 8542.31" or "HS 8542.31".
- port_of_loading / port_of_discharge: copy the full printed form including
  any UN/LOCODE in brackets, e.g. "SINGAPORE (SGSIN)".
- incoterms: an Incoterms 2020 code only (EXW FCA CPT CIP DAP DPU DDP FAS
  FOB CFR CIF). Frequently absent from a Bill of Lading.
- description_of_goods: the cargo description, excluding carton counts,
  HS codes and freight terms that share the same box.
- gross_weight: include the unit as printed, e.g. "12,450.00 KGS".
- invoice_number: the commercial invoice reference. On a B/L this often
  appears under Marks & Numbers or Export References, or not at all."""

RETRY_PREAMBLE = """This is a SECOND PASS over the same document. The fields \
below were unreadable or unverifiable on the first pass. The image you are \
being given now is higher resolution and has been deskewed and contrast \
enhanced.

Look again, carefully, at ONLY these fields. If a field is still not legible \
or genuinely is not on the page, say status="not_found" -- do not invent a \
value to fill the gap. A second honest "not_found" is the right answer."""


def _subset_schema(fields: list[str]) -> type[BaseModel]:
    """Build a strict schema containing only the fields we are re-asking for."""
    return create_model(
        "PartialExtraction",
        **{f: (ExtractedField, ...) for f in fields},
        __base__=BaseModel,
    )


def _page_parts(bundle: DocumentBundle, image_paths: Optional[list[str]] = None) -> list[dict]:
    paths = image_paths or [p.image_path for p in bundle.pages][:MAX_PAGES_PER_CALL]
    parts: list[dict] = [
        text_part(
            f"Document: {bundle.filename} ({len(paths)} page image(s) follow). "
            "Extract the eight fields."
        )
    ]
    for i, path in enumerate(paths, start=1):
        parts.append(text_part(f"--- page {i} ---"))
        parts.append(image_part(path))
    return parts


def _transcribe_for_grounding(bundle: DocumentBundle, model: str, budget: RunBudget) -> str:
    """Last-resort corpus when there is no text layer and no OCR available.

    Weaker than OCR because it shares a model family with the extraction it
    is checking, so callers record text_source="llm_transcript" and the
    grounding layer discounts it accordingly.
    """
    Transcript = create_model(
        "Transcript", text=(str, ...), __base__=BaseModel
    )
    out = call_structured(
        name="grounding_transcript",
        model=model,
        instructions=(
            "Transcribe every piece of text visible in this document image, "
            "verbatim, preserving the original wording and numbers. Do not "
            "summarise, correct, or interpret anything."
        ),
        content=_page_parts(bundle),
        schema=Transcript,
        budget=budget,
        max_output_tokens=6000,
    )
    return out.text


def _enhanced_images(bundle: DocumentBundle) -> list[str]:
    """Re-render at high DPI and enhance, for the Tier-2 retry."""
    out: list[str] = []
    for page in bundle.pages[:MAX_PAGES_PER_CALL]:
        base = pathlib.Path(page.image_path)
        hi = base.with_name(f"{base.stem}-hi.png")
        if not hi.exists():
            if bundle.is_native_pdf:
                render_pdf_page(bundle.source_path, page.index - 1, HIGH_DPI, str(hi))
            else:
                hi = base
        enhanced = base.with_name(f"{base.stem}-enhanced.png")
        out.append(enhance_image(str(hi), str(enhanced)))
    return out


def extract(
    bundle: DocumentBundle,
    *,
    model: Optional[str] = None,
    budget: Optional[RunBudget] = None,
    allow_retry: bool = True,
) -> ExtractionOutput:
    started = time.perf_counter()
    model = model or settings.extractor_model
    budget = budget or RunBudget()
    warnings: list[str] = []

    # --- corpus for independent verification (never shown to the extractor)
    corpus = bundle.corpus
    text_source = bundle.pages[0].text_source if bundle.pages else "none"
    if not corpus.strip():
        warnings.append("no text layer or OCR; falling back to LLM transcription for grounding")
        try:
            corpus = _transcribe_for_grounding(bundle, model, budget)
            text_source = "llm_transcript"
        except Exception as exc:
            warnings.append(f"transcription fallback failed: {exc}")
            text_source = "none"

    # --- Tier 1: one vision call over the whole document
    raw = call_structured(
        name="extract_pass1",
        model=model,
        instructions=INSTRUCTIONS,
        content=_page_parts(bundle),
        schema=RawExtraction,
        budget=budget,
    )

    grounded: dict[str, GroundedField] = {
        name: ground_field(name, getattr(raw, name), corpus, text_source)
        for name in CANONICAL_FIELDS
    }

    # --- Tier 2: re-ask only the fields that came back weak
    weak = [
        n
        for n, f in grounded.items()
        if f.verdict == Verdict.UNCERTAIN
        or (f.verdict == Verdict.NOT_FOUND and f.model_confidence < 0.5)
    ]
    if weak and allow_retry:
        try:
            images = _enhanced_images(bundle)
            schema = _subset_schema(weak)
            parts = [text_part(RETRY_PREAMBLE), text_part("Fields to re-read: " + ", ".join(weak))]
            parts += _page_parts(bundle, image_paths=images)[1:]

            retry = call_structured(
                name="extract_pass2_targeted",
                model=model,
                instructions=INSTRUCTIONS,
                content=parts,
                schema=schema,
                budget=budget,
            )
            for name in weak:
                before = grounded[name]
                after = ground_field(name, getattr(retry, name), corpus, text_source)
                grounded[name] = _reconcile(before, after)
        except Exception as exc:
            warnings.append(f"targeted re-extraction skipped: {exc}")

    return ExtractionOutput(
        doc_id=bundle.doc_id,
        filename=bundle.filename,
        model=model,
        fields=[grounded[n] for n in CANONICAL_FIELDS],
        page_count=len(bundle.pages),
        text_source=text_source,
        usd_cost=round(budget.usd, 6),
        latency_ms=int((time.perf_counter() - started) * 1000),
        warnings=warnings,
    )


def _reconcile(first: GroundedField, second: GroundedField) -> GroundedField:
    """Merge a first-pass and second-pass reading of the same field.

    Agreement across two independent looks is real evidence, so we allow a
    modest confidence bump. Disagreement is the opposite: two passes that
    read different values mean we do not know the answer, and the field is
    forced to UNCERTAIN no matter how confident either pass was.
    """
    a, b = (first.value or "").strip().upper(), (second.value or "").strip().upper()

    if a and b and a != b:
        loser, winner = (first, second) if second.final_confidence >= first.final_confidence else (second, first)
        merged = winner.model_copy(deep=True)
        merged.verdict = Verdict.UNCERTAIN
        merged.final_confidence = round(min(winner.final_confidence, 0.45), 3)
        merged.flags = list(dict.fromkeys(merged.flags + ["reextraction_disagreement"]))
        merged.reasoning = (
            f"{merged.reasoning} [pass 1 read '{first.value}', pass 2 read '{second.value}'; "
            "surfaced for human review]"
        ).strip()
        return merged

    if a and b and a == b:
        merged = (second if second.final_confidence >= first.final_confidence else first).model_copy(deep=True)
        merged.final_confidence = round(min(1.0, merged.final_confidence * 1.10), 3)
        merged.flags = list(dict.fromkeys(merged.flags + ["confirmed_by_reextraction"]))
        if merged.grounded and merged.format_valid is not False:
            if merged.final_confidence >= settings.auto_approve_min_confidence:
                merged.verdict = Verdict.FOUND
        return merged

    # One pass found nothing: prefer whichever actually produced grounded evidence.
    if second.value and not first.value:
        return second
    return first
