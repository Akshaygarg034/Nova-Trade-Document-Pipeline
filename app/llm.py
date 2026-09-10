"""OpenAI access with cost accounting and hard stops.

Every LLM call in this system goes through `call_structured`. That single
choke point is what makes three of the brief's requirements enforceable
rather than aspirational:

  * runaway cost      -> RunBudget raises before the call is made
  * infinite retries  -> retries are capped and counted against the same budget
  * observability     -> every call emits a Span with tokens, usd, latency
"""
from __future__ import annotations

import base64
import time
from dataclasses import dataclass, field
from typing import Any, Optional, Type, TypeVar

from openai import APIStatusError, APITimeoutError, OpenAI
from pydantic import BaseModel

from app.config import settings

T = TypeVar("T", bound=BaseModel)

# USD per 1M tokens. Kept in code so cost is computed per call rather than
# guessed later; override here if your account pricing differs.
PRICING: dict[str, tuple[float, float]] = {
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-5": (1.25, 10.00),
    "gpt-5-mini": (0.25, 2.00),
    "gpt-5-nano": (0.05, 0.40),
}
_FALLBACK_PRICE = (2.00, 8.00)

MAX_ATTEMPTS = 3


class BudgetExceeded(RuntimeError):
    """Raised instead of spending more. Fails loud, never silently degrades."""


@dataclass
class Span:
    """One LLM call, recorded for tracing."""

    name: str
    model: str
    input_tokens: int
    output_tokens: int
    usd: float
    latency_ms: int
    attempts: int
    status: str
    error: Optional[str] = None


@dataclass
class RunBudget:
    """Per-document spend and call ceiling."""

    max_usd: float = field(default_factory=lambda: settings.max_usd_per_document)
    max_calls: int = field(default_factory=lambda: settings.max_llm_calls_per_run)
    usd: float = 0.0
    calls: int = 0
    spans: list[Span] = field(default_factory=list)

    def precheck(self, name: str) -> None:
        if self.calls >= self.max_calls:
            raise BudgetExceeded(f"call cap reached ({self.max_calls}) before '{name}'")
        if self.usd >= self.max_usd:
            raise BudgetExceeded(f"spend cap reached (${self.usd:.4f} >= ${self.max_usd}) before '{name}'")

    def record(self, span: Span) -> None:
        self.calls += 1
        self.usd += span.usd
        self.spans.append(span)

    @property
    def input_tokens(self) -> int:
        return sum(s.input_tokens for s in self.spans)

    @property
    def output_tokens(self) -> int:
        return sum(s.output_tokens for s in self.spans)


def price(model: str, in_tok: int, out_tok: int) -> float:
    pin, pout = PRICING.get(model, PRICING.get(model.split("-2")[0], _FALLBACK_PRICE))
    return (in_tok * pin + out_tok * pout) / 1_000_000


_client: Optional[OpenAI] = None


def client() -> OpenAI:
    global _client
    if _client is None:
        if not settings.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is not set. Copy .env.example to .env and fill it in.")
        _client = OpenAI(api_key=settings.openai_api_key, timeout=settings.llm_timeout_seconds)
    return _client


def image_part(path: str, detail: str = "high") -> dict[str, Any]:
    with open(path, "rb") as fh:
        b64 = base64.b64encode(fh.read()).decode()
    suffix = "jpeg" if path.lower().endswith((".jpg", ".jpeg")) else "png"
    return {"type": "input_image", "image_url": f"data:image/{suffix};base64,{b64}", "detail": detail}


def text_part(text: str) -> dict[str, Any]:
    return {"type": "input_text", "text": text}


def call_structured(
    *,
    name: str,
    model: str,
    instructions: str,
    content: list[dict[str, Any]],
    schema: Type[T],
    budget: RunBudget,
    max_output_tokens: int = 4096,
) -> T:
    """Single choke point for structured LLM calls. Retries are bounded."""
    budget.precheck(name)

    started = time.perf_counter()
    last_err: Optional[Exception] = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            resp = client().responses.parse(
                model=model,
                instructions=instructions,
                input=[{"role": "user", "content": content}],
                text_format=schema,
                max_output_tokens=max_output_tokens,
            )
            in_tok = resp.usage.input_tokens if resp.usage else 0
            out_tok = resp.usage.output_tokens if resp.usage else 0
            usd = price(model, in_tok, out_tok)
            budget.record(
                Span(
                    name=name,
                    model=model,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    usd=usd,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    attempts=attempt,
                    status="ok",
                )
            )
            parsed = resp.output_parsed
            if parsed is None:
                raise RuntimeError(f"model returned no parsable output (refusal or truncation) for '{name}'")
            return parsed

        except (APITimeoutError, APIStatusError) as exc:
            last_err = exc
            # Do not retry deterministic client errors; only transient ones.
            retryable = isinstance(exc, APITimeoutError) or getattr(exc, "status_code", 0) in (408, 409, 429, 500, 502, 503, 504)
            if not retryable or attempt == MAX_ATTEMPTS:
                break
            time.sleep(min(2 ** attempt, 8))
        except Exception as exc:  # schema/parse failures are not retryable
            last_err = exc
            break

    budget.record(
        Span(
            name=name,
            model=model,
            input_tokens=0,
            output_tokens=0,
            usd=0.0,
            latency_ms=int((time.perf_counter() - started) * 1000),
            attempts=attempt,
            status="error",
            error=f"{type(last_err).__name__}: {last_err}"[:400],
        )
    )
    raise RuntimeError(f"LLM call '{name}' failed after {attempt} attempt(s): {last_err}") from last_err
