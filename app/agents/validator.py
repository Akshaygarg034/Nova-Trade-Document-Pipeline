"""Validator Agent -- Behaviour B.

Compares extracted fields against a customer rule set and produces a
field-by-field match / mismatch / uncertain result.

This agent is deliberately MOSTLY NOT AN LLM, and that is the design, not a
shortcut. Rule evaluation is arithmetic and string comparison: it should be
exact, free, instant, and identical on every run. An enterprise validator
that returns a different answer to the same input twice is not auditable.

An LLM is called in exactly one place -- deciding whether two company names
denote the same legal entity, when deterministic similarity lands in a band
that is genuinely ambiguous. "Acme Electronics Mfg Pte" versus "Acme
Electronics Manufacturing Pte Ltd" is a judgement call about corporate
suffixes, not a string distance problem. Everything else stays deterministic.

Uncertainty from extraction propagates. If a field was not reliably read, the
result is UNCERTAIN even when the rule would have passed -- but we still
record what the rule WOULD have said, so the operator sees the whole picture
instead of a shrug.
"""
from __future__ import annotations

import pathlib
import re
import time
from typing import Any, Optional

import yaml
from pydantic import BaseModel
from rapidfuzz import fuzz

from app.config import ROOT, settings
from app.llm import RunBudget, call_structured, text_part
from app.schemas import (
    CANONICAL_FIELDS,
    ExtractionOutput,
    FieldValidation,
    GroundedField,
    MatchStatus,
    ValidationOutput,
    Verdict,
)

RULES_DIR = ROOT / "rules"


class RuleSet(BaseModel):
    customer: str
    customer_name: str
    version: str
    description: str = ""
    fields: dict[str, dict[str, Any]]


def load_ruleset(customer: str) -> RuleSet:
    path = RULES_DIR / f"{customer}.yaml"
    if not path.exists():
        available = ", ".join(sorted(p.stem for p in RULES_DIR.glob("*.yaml"))) or "none"
        raise FileNotFoundError(f"no rule set '{customer}'. Available: {available}")
    return RuleSet(**yaml.safe_load(path.read_text(encoding="utf-8")))


# ---------------------------------------------------------------- helpers
def _norm(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


_PORT_CODE = re.compile(r"\(?\b([A-Z]{5})\b\)?")


def _port_parts(value: str) -> tuple[str, Optional[str]]:
    """Split "ROTTERDAM (NLRTM)" into ("ROTTERDAM", "NLRTM")."""
    v = (value or "").upper()
    code = None
    m = _PORT_CODE.search(v)
    if m:
        code = m.group(1)
        v = v.replace(m.group(0), " ")
    name = re.split(r"[,/]", v)[0]
    return re.sub(r"[^A-Z ]", " ", name).strip(), code


def _ports_equal(found: str, expected: str) -> bool:
    """Match on UN/LOCODE when both carry one, otherwise on city name.

    Written this way because our own degraded scan produced
    "AMSTERDAM, NETHERLANDS" where the rule expects "ROTTERDAM (NLRTM)".
    There is no code to compare, so the city name has to decide it -- and
    it must decide it as a mismatch.
    """
    fname, fcode = _port_parts(found)
    ename, ecode = _port_parts(expected)
    if fcode and ecode:
        return fcode == ecode
    if not fname or not ename:
        return False
    return fuzz.ratio(fname, ename) >= 90


# ------------------------------------------------------- LLM escalation
class EntityMatch(BaseModel):
    same_legal_entity: bool
    reason: str


ENTITY_INSTRUCTIONS = """You compare two company names from international \
trade documents and decide whether they denote the SAME legal entity.

Treat as the SAME entity:
- Punctuation, spacing, case and accent differences.
- Standard abbreviations of the identical corporate form:
  "Pte. Ltd." / "Pte Ltd" / "PTE LTD", "Limited" / "Ltd", "Company" / "Co".
- A trading name with and without a benign suffix such as "(S)" or a branch
  descriptor, where the registered name is otherwise identical.

Treat as DIFFERENT entities:
- A missing or added corporate form. "Acme Pte" and "Acme Pte Ltd" are not
  the same registered company.
- An abbreviated or dropped word in the substantive name, such as "Mfg"
  where the registered name reads "Manufacturing", when the corporate form
  also differs.
- Any difference in the distinctive part of the name.

Customs and letter-of-credit checks are unforgiving here, so when the two \
names would plausibly be treated as different registered persons by a \
customs authority, answer false. Give one short sentence of reasoning that \
names the specific difference."""


def _same_entity(found: str, expected: str, budget: RunBudget) -> tuple[bool, str, bool]:
    """Returns (equivalent, reason, used_llm)."""
    ratio = fuzz.ratio(_norm(found), _norm(expected)) / 100.0
    try:
        out = call_structured(
            name="validate_entity_name",
            model=settings.cheap_model,
            instructions=ENTITY_INSTRUCTIONS,
            content=[
                text_part(
                    f"Name on the document: {found}\n"
                    f"Name required by the customer: {expected}\n"
                    f"(String similarity: {ratio:.2f})"
                )
            ],
            schema=EntityMatch,
            budget=budget,
            max_output_tokens=300,
        )
        return out.same_legal_entity, out.reason, True
    except Exception as exc:
        # Fail closed: an unavailable model must not become an approval.
        return False, f"entity check unavailable ({type(exc).__name__}); treated as a mismatch", False


# ------------------------------------------------------------ evaluators
def _evaluate(
    name: str, value: str, rule: dict[str, Any], budget: RunBudget
) -> tuple[bool, str, str]:
    """Apply one rule. Returns (ok, expected_rendered, message)."""
    kind = rule.get("rule", "present")
    expected = rule.get("expected")

    if kind == "equals_entity":
        exp = str(expected)
        ratio = fuzz.ratio(_norm(value), _norm(exp)) / 100.0
        if ratio >= float(rule.get("fuzzy_auto_match", 0.95)):
            return True, exp, f"matches the registered name (similarity {ratio:.2f})"
        if ratio < float(rule.get("fuzzy_auto_reject", 0.55)):
            return False, exp, f"different company (similarity {ratio:.2f})"
        same, reason, _ = _same_entity(value, exp, budget)
        return same, exp, reason

    if kind == "in_list":
        options = [str(o) for o in (expected or [])]
        rendered = " or ".join(options)
        if rule.get("match_mode") == "port":
            ok = any(_ports_equal(value, o) for o in options)
        else:
            ok = any(_norm(value) == _norm(o) or _norm(o) in _norm(value) for o in options)
        return ok, rendered, ("permitted value" if ok else f"not one of the permitted values ({rendered})")

    if kind == "contains_any":
        options = [str(o) for o in (expected or [])]
        rendered = "must mention " + " or ".join(options)
        ok = any(_norm(o) in _norm(value) for o in options)
        return ok, rendered, ("commodity class identified" if ok else "does not name a recognised commodity class")

    if kind == "regex":
        pattern = str(expected)
        ok = re.search(pattern, value.strip()) is not None
        return ok, f"format {pattern}", ("format is correct" if ok else f"does not match the required format {pattern}")

    if kind == "numeric_range":
        digits = re.sub(r"[^\d.]", "", value.replace(",", ""))
        rendered = f"between {rule.get('min')} and {rule.get('max')} {rule.get('unit', '')}".strip()
        try:
            n = float(digits)
        except ValueError:
            return False, rendered, "no numeric value could be read"
        lo, hi = float(rule.get("min", 0)), float(rule.get("max", 1e12))
        ok = lo <= n <= hi
        return ok, rendered, (f"{n:g} is within range" if ok else f"{n:g} is outside {rendered}")

    return True, "", "no rule configured"


# ---------------------------------------------------------------- agent
def validate(
    extraction: ExtractionOutput,
    customer: str = "acme_electronics",
    *,
    budget: Optional[RunBudget] = None,
) -> ValidationOutput:
    started = time.perf_counter()
    budget = budget or RunBudget()
    rs = load_ruleset(customer)
    before = budget.usd

    results: list[FieldValidation] = []
    for name in CANONICAL_FIELDS:
        field = extraction.by_name(name)
        rule = rs.fields.get(name)
        if field is None:
            continue
        results.append(_validate_field(name, field, rule, budget))

    return ValidationOutput(
        doc_id=extraction.doc_id,
        document_ref=pathlib.Path(extraction.filename).stem,
        customer=rs.customer,
        customer_name=rs.customer_name,
        ruleset_version=rs.version,
        results=results,
        usd_cost=round(budget.usd - before, 6),
        latency_ms=int((time.perf_counter() - started) * 1000),
    )


def _validate_field(
    name: str, field: GroundedField, rule: Optional[dict[str, Any]], budget: RunBudget
) -> FieldValidation:
    base = dict(
        name=name,
        label=field.label,
        found=field.value,
        extraction_confidence=field.final_confidence,
        extraction_verdict=field.verdict,
        evidence_quote=field.evidence_quote,
        evidence_page=field.evidence_page,
        flags=field.flags,
    )

    if rule is None:
        return FieldValidation(
            **base,
            status=MatchStatus.NOT_APPLICABLE,
            expected=None,
            rule="none",
            severity="info",
            message="no rule configured for this customer",
        )

    severity = rule.get("severity", "warning")
    why = " ".join(str(rule.get("why", "")).split())
    kind = rule.get("rule", "present")
    required = bool(rule.get("required", False))

    # --- field genuinely absent from the document
    if field.verdict == Verdict.NOT_FOUND or not field.value:
        if required:
            return FieldValidation(
                **base,
                status=MatchStatus.MISMATCH,
                expected=_render_expected(rule),
                rule=kind,
                severity=severity,
                why=why,
                message="required by this customer but not present on the document",
            )
        return FieldValidation(
            **base,
            status=MatchStatus.NOT_APPLICABLE,
            expected=_render_expected(rule),
            rule=kind,
            severity="info",
            why=why,
            message="optional and not present",
        )

    ok, expected_rendered, message = _evaluate(name, field.value, rule, budget)

    # --- extraction was not trusted: surface it, but say what the rule thought
    if field.verdict == Verdict.UNCERTAIN:
        provisional = MatchStatus.MATCH if ok else MatchStatus.MISMATCH
        hint = "would pass the rule" if ok else f"would fail the rule: {message}"
        return FieldValidation(
            **base,
            status=MatchStatus.UNCERTAIN,
            expected=expected_rendered,
            rule=kind,
            severity=severity,
            why=why,
            message=(
                f"could not read this field reliably "
                f"(confidence {field.final_confidence:.2f}); as read it {hint}"
            ),
            provisional_status=provisional,
        )

    return FieldValidation(
        **base,
        status=MatchStatus.MATCH if ok else MatchStatus.MISMATCH,
        expected=expected_rendered,
        rule=kind,
        severity=severity,
        why=why,
        message=message,
    )


def _render_expected(rule: dict[str, Any]) -> str:
    exp = rule.get("expected")
    if isinstance(exp, list):
        return " or ".join(str(o) for o in exp)
    if exp is not None:
        return str(exp)
    if rule.get("rule") == "numeric_range":
        return f"between {rule.get('min')} and {rule.get('max')} {rule.get('unit', '')}".strip()
    return "a value"
