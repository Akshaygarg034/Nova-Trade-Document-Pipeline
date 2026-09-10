"""The data contract for the whole pipeline.

Two layers, deliberately separated:

  RawExtraction   - exactly what the vision model returned. Never trusted.
  GroundedField   - raw extraction AFTER independent verification, carrying a
                    fused confidence and an audit trail of why.

Keeping them apart is what makes the confidence score mean something: the
model's self-report is one input to the final number, not the number itself.
"""
from __future__ import annotations

from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field

# The eight fields the brief requires.
CANONICAL_FIELDS: list[str] = [
    "consignee_name",
    "hs_code",
    "port_of_loading",
    "port_of_discharge",
    "incoterms",
    "description_of_goods",
    "gross_weight",
    "invoice_number",
]

FIELD_LABELS: dict[str, str] = {
    "consignee_name": "Consignee name",
    "hs_code": "HS code",
    "port_of_loading": "Port of loading",
    "port_of_discharge": "Port of discharge",
    "incoterms": "Incoterms",
    "description_of_goods": "Description of goods",
    "gross_weight": "Gross weight",
    "invoice_number": "Invoice number",
}


# --------------------------------------------------------------------------
# Layer 1: what the model said (strict structured output schema)
# --------------------------------------------------------------------------
class ExtractedField(BaseModel):
    """One field as reported by the vision model.

    `evidence_quote` is mandatory whenever status is "found": it must be text
    copied verbatim off the page. That is what makes independent verification
    possible downstream -- a model can misstate its confidence, but it cannot
    fake a quote we re-check against the document ourselves.
    """

    value: Optional[str] = Field(description="The field value exactly as printed, or null if absent.")
    status: Literal["found", "not_found"]
    model_confidence: float = Field(description="Model's own certainty, 0.0-1.0.")
    evidence_quote: Optional[str] = Field(
        description="Verbatim text copied from the document containing this value. Null if not_found."
    )
    evidence_page: Optional[int] = Field(description="1-indexed page the quote came from. Null if not_found.")
    reasoning: str = Field(description="One short sentence on how this was identified.")


class RawExtraction(BaseModel):
    """Strict schema handed to the model. Named fields, so it cannot invent keys."""

    consignee_name: ExtractedField
    hs_code: ExtractedField
    port_of_loading: ExtractedField
    port_of_discharge: ExtractedField
    incoterms: ExtractedField
    description_of_goods: ExtractedField
    gross_weight: ExtractedField
    invoice_number: ExtractedField


# --------------------------------------------------------------------------
# Layer 2: what we believe after verification
# --------------------------------------------------------------------------
class Verdict(str, Enum):
    FOUND = "found"          # grounded and confident
    UNCERTAIN = "uncertain"  # present but unverified / low confidence -> must surface
    NOT_FOUND = "not_found"  # genuinely absent from the document


class GroundedField(BaseModel):
    name: str
    label: str
    value: Optional[str]
    verdict: Verdict

    final_confidence: float
    model_confidence: float
    grounding_score: float = Field(description="0-1 fuzzy match of the quote against page text.")
    grounded: bool = Field(description="Was the quote actually located in the document?")

    format_valid: Optional[bool] = Field(default=None, description="None when no format rule applies.")
    format_note: Optional[str] = None

    evidence_quote: Optional[str] = None
    evidence_page: Optional[int] = None
    reasoning: str = ""
    flags: list[str] = Field(default_factory=list, description="Machine-readable audit trail.")


class PageAsset(BaseModel):
    """One rendered page plus the independent text corpus used for grounding."""

    index: int  # 1-indexed
    image_path: str
    width: int
    height: int
    text: str = ""
    text_source: Literal["pdf_text", "ocr", "llm_transcript", "none"] = "none"


class DocumentBundle(BaseModel):
    doc_id: str
    source_path: str
    filename: str
    is_native_pdf: bool
    pages: list[PageAsset]

    @property
    def corpus(self) -> str:
        return "\n".join(p.text for p in self.pages)


class MatchStatus(str, Enum):
    MATCH = "match"
    MISMATCH = "mismatch"
    UNCERTAIN = "uncertain"
    NOT_APPLICABLE = "not_applicable"


Severity = Literal["blocking", "warning", "info"]


class FieldValidation(BaseModel):
    """One field checked against one customer rule."""

    name: str
    label: str
    status: MatchStatus
    found: Optional[str]
    expected: Optional[str] = Field(description="What the rule required, rendered for a human.")
    rule: str
    severity: Severity
    message: str
    why: str = Field(default="", description="The business reason the rule exists.")

    # Carried through from extraction so the UI can show one row per field.
    extraction_confidence: float = 0.0
    extraction_verdict: Verdict = Verdict.NOT_FOUND
    evidence_quote: Optional[str] = None
    evidence_page: Optional[int] = None
    flags: list[str] = Field(default_factory=list)

    # Set when the rule could be evaluated but extraction was not trusted.
    provisional_status: Optional[MatchStatus] = Field(
        default=None,
        description="What the rule would have said, had the reading been reliable.",
    )


class ValidationOutput(BaseModel):
    """Behaviour B's deliverable."""

    doc_id: str
    # Human-facing reference. doc_id is a content hash and must never appear
    # in an email to a supplier.
    document_ref: str = ""
    customer: str
    customer_name: str
    ruleset_version: str
    results: list[FieldValidation]
    usd_cost: float = 0.0
    latency_ms: int = 0

    def count(self, status: MatchStatus) -> int:
        return sum(1 for r in self.results if r.status == status)

    @property
    def blocking_mismatches(self) -> list[FieldValidation]:
        return [
            r for r in self.results
            if r.status == MatchStatus.MISMATCH and r.severity == "blocking"
        ]

    @property
    def uncertain(self) -> list[FieldValidation]:
        return [r for r in self.results if r.status == MatchStatus.UNCERTAIN]

    def by_name(self, name: str) -> Optional[FieldValidation]:
        return next((r for r in self.results if r.name == name), None)


class DecisionType(str, Enum):
    AUTO_APPROVE = "auto_approve"
    FLAG_FOR_REVIEW = "flag_for_review"
    AMENDMENT_REQUEST = "amendment_request"


class Discrepancy(BaseModel):
    """One thing the supplier has to fix, in the terms they need to hear it."""

    field: str
    label: str
    found: Optional[str]
    expected: Optional[str]
    severity: Severity
    why: str


class EmailDraft(BaseModel):
    subject: str
    body: str
    to_role: str = "SU"
    source: Literal["llm", "template"] = "llm"
    # Non-negotiable: nothing leaves this system without a human pressing send.
    requires_human_send: bool = True


class RouterOutput(BaseModel):
    """Behaviour C's deliverable."""

    doc_id: str
    decision: DecisionType

    # Deterministic, machine-readable justification. This is the audit record:
    # given the same validation input, these reasons are always identical.
    policy_reasons: list[str] = Field(default_factory=list)

    # Human-readable explanation of the same thing, for the operator.
    rationale: str = ""

    discrepancies: list[Discrepancy] = Field(default_factory=list)
    uncertain_fields: list[str] = Field(default_factory=list)
    lowest_confidence: float = 1.0

    draft_email: Optional[EmailDraft] = None
    usd_cost: float = 0.0
    latency_ms: int = 0
    warnings: list[str] = Field(default_factory=list)


class ExtractionOutput(BaseModel):
    """Behaviour A's deliverable."""

    doc_id: str
    filename: str
    model: str
    fields: list[GroundedField]
    page_count: int
    text_source: str
    usd_cost: float
    latency_ms: int
    warnings: list[str] = Field(default_factory=list)

    def by_name(self, name: str) -> Optional[GroundedField]:
        return next((f for f in self.fields if f.name == name), None)
