"""Independent verification of what the extractor claimed.

A model can state any confidence it likes. It cannot fake a quote that we
re-locate in the document ourselves. So the confidence we surface to the user
is not the self-report -- it is that self-report multiplied by three
independent checks:

  grounding  did the quoted evidence actually appear in the page text?
  format     does the value satisfy the hard format rule for its field?
  source     how trustworthy is the text corpus we verified against?

Two matching modes, and the difference between them matters:

  Normalised fuzzy   strips punctuation and whitespace, tolerant of OCR noise.
                     "INV-2026 08847" (OCR) == "INV-2026-08847" (truth).

  Word-boundary      exact token match on raw text, for short enum codes.
                     Without it the Incoterm "CIF" matches inside the word
                     "SPECIFICALLY" in a bill of lading legal boilerplate --
                     a real false positive we hit on our own DHX sample.
"""
from __future__ import annotations

import re
from typing import Optional

from rapidfuzz import fuzz

from app.config import settings
from app.schemas import FIELD_LABELS, ExtractedField, GroundedField, Verdict

# Fields that are short enum-like codes: verified by exact token match only.
SHORT_CODE_FIELDS = {"incoterms"}

# Identifier fields, where a value must line up with a WHOLE token on the page.
# Caught a real failure: the extractor returned invoice_number "2026-08841"
# for a document reading "INV-2026-08841". Fuzzy matching scored that 1.00,
# because the truncated value really is a substring of the true one. Only
# token alignment distinguishes "correct" from "correct prefix removed".
ID_FIELDS = {"invoice_number", "hs_code"}

# Free-text fields, scored on token overlap rather than contiguity. A goods
# description legitimately spans several form rows, so the words are all on
# the page but never in one unbroken run -- contiguous matching scored a
# perfectly correct description at 0.6 and sent it to a human for no reason.
FREETEXT_FIELDS = {"description_of_goods"}

_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9./:_-]*")

INCOTERMS_2020 = {
    "EXW", "FCA", "CPT", "CIP", "DAP", "DPU", "DDP",
    "FAS", "FOB", "CFR", "CIF",
}

# How much we trust the corpus we are checking against.
#
# OCR started at 0.92 and that was measurably wrong. On our degraded scan it
# let three incorrect values auto-approve, because fuzzy matching against a
# garbled corpus happily accepts a one-character error ("12,960" for "12,980",
# "CHSHA" for "CNSHA"). Verification is only as good as the text it verifies
# against, so a corpus we had to guess at earns markedly less trust.
SOURCE_TRUST = {"pdf_text": 1.00, "ocr": 0.80, "llm_transcript": 0.70, "none": 0.60}

# Runs of digits, and alphabetic codes, that must be present verbatim.
_SUBTOKEN_RE = re.compile(r"\d[\d.,]*\d|\d")

WEIGHT_UNITS = r"(KG|KGS|KGM|LB|LBS|MT|TON|TONNE)"


def norm(s: str) -> str:
    """Uppercase, alphanumerics only. Collapses OCR spacing and punctuation noise."""
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def token_in_text(token: str, text: str) -> bool:
    """Whole-word presence in raw text. Guards against substring false hits."""
    if not token or not text:
        return False
    return re.search(rf"\b{re.escape(token.strip().upper())}\b", text.upper()) is not None


_LABEL_NOISE = re.compile(
    r"^(INV(OICE)?\.?\s*(NO\.?|NUMBER|#)?|REF(ERENCE)?S?\.?|HS\s*(CODE)?\.?"
    r"|P\.?\s?O\.?\s*(NO\.?)?|B/?L\s*(NO\.?)?)[:\s.#-]*",
    re.IGNORECASE,
)


def repair_identifier(value: str, corpus: str) -> tuple[str, bool]:
    """Strip field-label text the model swept into an identifier value.

    Observed repeatedly: for a page reading "INV NO. INV-2026-08841" the
    extractor returns either "INV NO. INV-2026-08841" (label included) or
    "2026-08841" (prefix dropped), depending on render resolution.

    The repair is only ACCEPTED if the result matches a whole token that is
    actually on the page. That guard is what stops this from becoming a way
    to launder a wrong value into a plausible one -- stripping the real
    prefix off "INV-2026-08841" yields "2026-08841", which aligns with
    nothing, so the original is kept and the field stays suspect.
    """
    v = value.strip()

    stripped = _LABEL_NOISE.sub("", v).strip(" :.#-")
    if stripped and stripped != v and token_aligned(stripped, corpus):
        return stripped, True

    # Otherwise: exactly one digit-bearing token in the value that also
    # appears as a whole token on the page.
    candidates = [
        t for t in _TOKEN_RE.findall(v) if re.search(r"\d", t) and token_aligned(t, corpus)
    ]
    if len(set(candidates)) == 1 and candidates[0] != v:
        return candidates[0], True

    return v, False


def unverified_digits(value: str, corpus: str) -> list[str]:
    """Digit runs in `value` that do not appear anywhere on the page.

    Fuzzy matching exists to absorb OCR noise, but it also absorbs real
    errors: "12,960" scores 0.9+ against a page reading "12,980". Numbers
    are where a single character flips the meaning of a shipment, so they
    get an exact check rather than a fuzzy one.
    """
    if not value or not corpus:
        return []
    corpus_tokens = {norm(t) for t in _TOKEN_RE.findall(corpus)}
    corpus_digits = norm(corpus)

    missing: list[str] = []
    for run in _SUBTOKEN_RE.findall(value.upper()):
        n = norm(run)
        if len(n) < 3:
            continue  # too short to be discriminating
        if n in corpus_tokens or any(n in t for t in corpus_tokens) or n in corpus_digits:
            continue
        missing.append(run)
    return missing


def token_aligned(value: str, corpus: str) -> bool:
    """Does `value` equal a complete identifier token on the page?

    Compares normalised forms, so "INV-2026-08841" still matches an OCR
    reading of "INV-2026 08841". But a truncation like "2026-08841" matches
    no whole token and is correctly rejected.
    """
    target = norm(value)
    if not target:
        return False
    return any(norm(t) == target for t in _TOKEN_RE.findall(corpus))


# -------------------------------------------------------------------- format
def check_format(name: str, value: Optional[str]) -> tuple[Optional[bool], Optional[str]]:
    """Deterministic per-field validity. Returns (valid, note); None means no rule."""
    if value is None or not value.strip():
        return None, None
    v = value.strip()

    if name == "incoterms":
        head = re.split(r"[\s,;/]", v.upper())[0]
        if head in INCOTERMS_2020:
            return True, None
        return False, f"{head} is not an Incoterms 2020 code"

    if name == "hs_code":
        digits = re.sub(r"\D", "", v)
        if not re.fullmatch(r"[\d.\s-]+", v):
            return False, "HS code contains non-numeric characters"
        if not 6 <= len(digits) <= 10:
            return False, f"HS code has {len(digits)} digits, expected 6-10"
        return True, None

    if name == "gross_weight":
        if not re.search(r"\d", v):
            return False, "no numeric component"
        if not re.search(WEIGHT_UNITS, v.upper()):
            return False, "no recognised weight unit"
        return True, None

    if name == "invoice_number":
        if not re.search(r"\d", v):
            return False, "invoice number contains no digits"
        if len(v) < 4:
            return False, "implausibly short invoice number"
        return True, None

    if name in ("port_of_loading", "port_of_discharge"):
        if not re.search(r"[A-Za-z]{3}", v):
            return False, "no recognisable port name"
        return True, None

    if name == "consignee_name":
        if len(v) < 3 or not re.search(r"[A-Za-z]{2}", v):
            return False, "implausible consignee name"
        return True, None

    return None, None


# ----------------------------------------------------------------- grounding
def score_grounding(
    name: str,
    value: Optional[str],
    quote: Optional[str],
    corpus: str,
    text_source: str = "pdf_text",
) -> tuple[float, bool, list[str]]:
    """Locate the quoted evidence in the document. Returns (score, grounded, flags)."""
    flags: list[str] = []

    if not corpus.strip():
        return 0.0, False, ["no_text_corpus"]
    if not quote or not quote.strip():
        return 0.0, False, ["no_evidence_quote"]

    nv, nq, nc = norm(value or ""), norm(quote), norm(corpus)

    # 1. The quote must actually contain the value it is offered as evidence for.
    if nv and nv not in nq:
        flags.append("quote_does_not_contain_value")

    # 2. The quote must be locatable in the document text.
    quote_score = fuzz.partial_ratio(nq, nc) / 100.0 if nq else 0.0

    # 3. The value itself must be locatable, independent of the quote.
    if not nv:
        value_score = 0.0
    elif name in FREETEXT_FIELDS:
        # Word-level overlap, order- and gap-insensitive.
        value_score = fuzz.token_set_ratio(
            re.sub(r"[^A-Z0-9 ]", " ", (value or "").upper()),
            re.sub(r"[^A-Z0-9 ]", " ", corpus.upper()),
        ) / 100.0
    else:
        value_score = fuzz.partial_ratio(nv, nc) / 100.0

    # 4. Short enum codes: demand an exact whole-word hit on raw text.
    if name in SHORT_CODE_FIELDS and value:
        head = re.split(r"[\s,;/]", value.strip().upper())[0]
        if not token_in_text(head, corpus):
            flags.append("code_not_present_as_whole_word")
            return 0.0, False, flags

    # These are two different questions and conflating them is a mistake we
    # made first time round:
    #   value_score  is the value really on the page?      -> hallucination check
    #   quote_score  is the citation verbatim?             -> evidence quality
    # Models legitimately stitch a distant label onto a value ("CONSIGNEE" +
    # the name, skipping the boilerplate between them). That should dent
    # confidence, not zero out a field whose value is genuinely present.
    # 5. Identifiers must line up with a whole token, not sit inside one.
    #    Only enforced hard against a reliable corpus: OCR routinely splits
    #    tokens, so on a noisy corpus this is a hint, not a verdict.
    #    Absence is the more specific diagnosis, so it is reported first.
    if name in ID_FIELDS and value and not token_aligned(value, corpus):
        if value_score < 0.60:
            flags.append("value_absent_from_document")
            return round(value_score, 3), False, flags
        if text_source == "pdf_text":
            flags.append("partial_token_match")
            return round(min(value_score, 0.50), 3), False, flags
        flags.append("token_alignment_unverified")

    # `grounded` is a statement about the VALUE only: is it really on the page?
    # Citation tidiness is tracked separately and costs a small confidence
    # penalty in fusion. Conflating the two made us mark a correct, perfectly
    # located "ROTTERDAM (NLRTM)" as uncertain purely because the model quoted
    # the field label alongside the value -- a human touch spent on nothing.
    score = value_score if nv else quote_score
    citation_ok = quote_score >= 0.75

    grounded = score >= 0.85 and "quote_does_not_contain_value" not in flags

    # Numbers get an exact check regardless of how well the text fuzzy-matched.
    if name not in FREETEXT_FIELDS and value:
        missing = unverified_digits(value, corpus)
        if missing:
            flags.append("digits_not_on_page:" + ",".join(missing[:3]))
            grounded = False
            score = min(score, 0.50)

    if not citation_ok:
        flags.append("evidence_citation_not_verbatim")
    if value_score < 0.60:
        flags.append("value_absent_from_document")
    elif not grounded:
        flags.append("weak_grounding")

    return round(score, 3), grounded, flags


def grounding_factor(score: float, grounded: bool) -> float:
    if grounded:
        return 1.0
    if score >= 0.75:
        return 0.75
    if score >= 0.60:
        return 0.50
    return 0.20


# -------------------------------------------------------------------- fusion
def ground_field(
    name: str, raw: ExtractedField, corpus: str, text_source: str
) -> GroundedField:
    """Fuse the extractor claim with independent evidence into one verdict."""
    model_conf = max(0.0, min(1.0, float(raw.model_confidence or 0.0)))

    # Absence claim: nothing to ground. Correctly reporting a missing field is
    # a success, not a failure -- the validator decides whether it matters.
    if raw.status == "not_found" or raw.value is None or not str(raw.value).strip():
        return GroundedField(
            name=name,
            label=FIELD_LABELS.get(name, name),
            value=None,
            verdict=Verdict.NOT_FOUND,
            final_confidence=round(model_conf, 3),
            model_confidence=round(model_conf, 3),
            grounding_score=0.0,
            grounded=False,
            format_valid=None,
            evidence_quote=None,
            evidence_page=None,
            reasoning=raw.reasoning or "",
            flags=["reported_absent"],
        )

    value = str(raw.value).strip()

    repair_flags: list[str] = []
    if name in ID_FIELDS and corpus.strip():
        repaired, changed = repair_identifier(value, corpus)
        if changed:
            repair_flags.append("identifier_repaired")
            value = repaired

    score, grounded, flags = score_grounding(
        name, value, raw.evidence_quote, corpus, text_source
    )
    flags = repair_flags + flags

    fmt_valid, fmt_note = check_format(name, value)
    if fmt_valid is False:
        flags = flags + ["format_invalid"]

    trust = SOURCE_TRUST.get(text_source, 0.6)
    if text_source == "none":
        flags = flags + ["unverifiable_no_corpus"]

    final = (
        model_conf
        * grounding_factor(score, grounded)
        * (0.40 if fmt_valid is False else 1.0)
        * (0.90 if "evidence_citation_not_verbatim" in flags else 1.0)
        * trust
    )

    # A confident claim we cannot find anywhere is the classic hallucination
    # signature. Name it explicitly so the UI and the router can act on it.
    if "value_absent_from_document" in flags and model_conf >= 0.7:
        flags = flags + ["possible_hallucination"]
        final = min(final, 0.25)

    verdict = (
        Verdict.FOUND
        if final >= settings.auto_approve_min_confidence and grounded and fmt_valid is not False
        else Verdict.UNCERTAIN
    )

    return GroundedField(
        name=name,
        label=FIELD_LABELS.get(name, name),
        value=value,
        verdict=verdict,
        final_confidence=round(min(1.0, final), 3),
        model_confidence=round(model_conf, 3),
        grounding_score=score,
        grounded=grounded,
        format_valid=fmt_valid,
        format_note=fmt_note,
        evidence_quote=raw.evidence_quote,
        evidence_page=raw.evidence_page,
        reasoning=raw.reasoning or "",
        flags=flags,
    )
