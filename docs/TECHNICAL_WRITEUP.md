# Technical Write-up · Nova Trade Document Pipeline

## 1. Architecture

```
                    ┌──────────── LangGraph state machine ────────────┐
                    │   SqliteSaver checkpoint after every node       │
  PDF / image ──────┼──▶ ingest ─▶ extract ─▶ validate ─▶ route ─▶ ⏹  │
   (upload or       │      │          │          │          │        │
    file path)      └──────┼──────────┼──────────┼──────────┼────────┘
                           │          │          │          │
              ┌────────────▼──────────▼──────────▼──────────▼───────┐
              │                    SQLite (nova.db)                 │
              │  shipments · documents · runs                       │
              │  extracted_fields · validations · decisions         │
              │  agent_spans   ← one row per LLM call: tokens,      │
              │                  usd, latency, retries, status      │
              └────────┬──────────────────────────────┬─────────────┘
                       │                              │
              FastAPI + SSE                    NL→SQL agent
                       │                       (mode=ro connection)
                       ▼                              ▼
              React operator UI              "how many were flagged?"

  Inside `extract`:
     page render (200 DPI) ──▶ vision LLM ──▶ raw reading  ─┐
     text layer / OCR ─────────────────────────▶ corpus ────┼─▶ grounding
                                                            │   ├ quote found?
     weak fields only ──▶ 300 DPI + deskew ──▶ re-read ─────┘   ├ format valid?
                                                                ├ digits exact?
                                                                └ corpus trust
```

**Where state lives.** Two independent mechanisms, because they do different jobs.
The LangGraph checkpointer (`data/checkpoints.sqlite`) makes the *graph* resumable at
node boundaries. The application tables (`data/nova.db`) make the *result* queryable
and are written by each node as it completes, before the next begins. A third piece,
the reading cache (`data/reading_cache/`), memoises the expensive vision call keyed on
document bytes + model.

That third piece exists because of a measured failure. Checkpointing alone does **not**
give you "never pay for extraction twice": LangGraph checkpoints a node's output only
when the node *returns*, so a crash *inside* `extract` re-runs the whole node. We
observed exactly that — two `extract_pass1` spans, double cost, one document. With the
reading cache, a crash-and-resume now costs $0.01063 against $0.01064 for an
uninterrupted run.

**Why three agents.** Each answers a different question with different evidence, and
fails in a different way:

| Agent | Question | Evidence | LLM? |
|---|---|---|---|
| Extractor | What does this document say? | pixels + page text | yes, vision |
| Validator | Is what it says acceptable? | customer rule set | almost never |
| Router | What should happen next? | validation state | no (prose only) |

The clean proof they belong apart is failure mode 1 below: an error the Extractor
structurally *cannot* catch and the Validator catches trivially. Merging them into one
prompt would leave that error undetectable. Splitting further would add handoffs
without separating anything.

The Validator is deliberately **not** an LLM. Rule evaluation is arithmetic and string
comparison: it should be exact, free, instant, and identical on every run. A model is
called in exactly one place — deciding whether two company names denote the same legal
entity, when deterministic similarity lands in a genuinely ambiguous band. The Router's
*decision* is likewise a pure function; only the email prose is generated. An auditor
asking why a shipment cleared six months ago should not get an answer that depends on
a model version.

---

## 2. The three nastiest failure modes

All three were found by running the system, not by imagining it. Each has a
regression test.

### 2.1 Right text, wrong box — the one grounding cannot catch

On the degraded scan the extractor returned `AMSTERDAM, NETHERLANDS` for Port of
Discharge, at 0.85 confidence, fully grounded. It was **not** a hallucination: that
text is genuinely printed on the page, in the *Place of Delivery by On-Carrier* box.
The real Port of Discharge reads `ROTTERDAM (NLRTM)`.

Every text-based check passes, and always will. The quote exists, the format is valid,
the value is locatable. Grounding answers "is this text on the page?" — it cannot
answer "is it in the *right* box?"

The Validator caught it: the customer rule permits only Rotterdam. But note the
second-order harm — had that been the only problem, the amendment email would have
told the supplier to correct a Port of Discharge that was **already correct on their
document**. Confidently wrong advice is worse than no advice, because it burns a cycle
and credibility.

*Attempted fix that failed.* We added `source_label` to the extraction schema, asking
the model which printed caption each value came from, and checked it against expected
keywords. It reported `"PORT OF DISCHARGE"` — the caption it was *looking for*, not the
box it read from, despite an explicit instruction not to. Worse, it fired as a false
positive on a clean document: Incoterms legitimately live inside the goods-description
box on a JSE B/L (`FREIGHT TERMS: FOB SINGAPORE`), and the check penalised a correct
reading, costing two auto-approvals. We scoped it to fields with a dedicated box and
kept it as a weak signal.

*What actually works today:* two-pass disagreement. The field is re-read at 300 DPI
after deskew; the passes disagreed, so it was forced to UNCERTAIN.
*What would work properly:* geometric provenance. PyMuPDF gives word coordinates and
RapidOCR gives boxes; matching a value to its nearest caption by position is real
evidence rather than self-report. Listed under §6.

### 2.2 Fuzzy matching launders near-miss errors

Three separate incidents of the same root cause. Fuzzy similarity exists to absorb OCR
noise, but it absorbs *real errors* just as happily, because a one-character error and
scanner noise look identical to an edit-distance metric.

| Extracted | Actual page | Similarity | Outcome before fix |
|---|---|---|---|
| `2026-08841` | `INV-2026-08841` | 1.00 | approved — truncation *is* a substring |
| `12,960 KGS` | `12,980 KGS` | ~0.9 | approved |
| `SHANGHAI (CHSHA)` | `SHANGHAI (CNSHA)` | ~0.96 | approved |
| `Acne Electronics…` | `Acme Electronics…` | ~0.97 | approved, and *matched the rule* |

Four fixes, each targeting a different shape of the problem:

- **Token alignment** for identifiers — a value must equal a *whole* token on the
  page, so `2026-08841` no longer passes as part of `INV-2026-08841`.
- **Exact digit checks** — every digit run of 3+ must appear verbatim. Numbers are
  where one character changes the meaning of a shipment, so they get exactness, not
  fuzziness.
- **Snap-to-source** — the model *locates* a value; the document decides how it
  *reads*. `Acne` → `Acme`, `INV:2026-08841` → `INV-2026-08841`. Guarded so it can
  only ever move a value toward the page text: if the page really says `Acne`, that is
  what gets reported and the rule correctly fails.
- **OCR trust 0.92 → 0.80** — verification is only as good as the corpus it verifies
  against, and a corpus we had to guess at deserves less trust.

Before: 3 wrong values auto-approved on the degraded scan. After: **0 escaped errors**
across the golden set, with auto-approve accuracy at 100% (15/15).

### 2.3 Substring matches inside unrelated words

The Incoterm check searched for `CIF` in the page text and found it — inside the word
**SPECIFICALLY**, in the carrier's liability boilerplate. That document has no Incoterm
at all, which is normal for an ocean B/L.

This is the most dangerous shape of hallucination for this domain, because the
"evidence" is real text from the real page. Any naive substring grounding rubber-stamps
it. Fixed with word-boundary matching on raw text for short enum codes, plus a prompt
rule that three-letter sequences inside longer English words are not Incoterms.

The same document is now a permanent test fixture: the only correct extraction for its
Incoterm is `not_found`, and the eval scores correct-absence separately from accuracy
(currently 2/2, 0 hallucinated fields).

---

## 3. Observability — tracing one shipment across 50 customers

Every LLM call writes an `agent_spans` row: `run_id, doc_id, node, name, model,
input_tokens, output_tokens, usd, latency_ms, attempts, status, error`. Every stage
writes its domain output keyed by the same `run_id`. So one shipment is one join:

```sql
SELECT s.node, s.name, s.model, s.latency_ms, s.usd, s.status
  FROM agent_spans s JOIN runs r USING (run_id)
 WHERE r.shipment_id = 'SHP-2287' ORDER BY s.id;
```

which returns the whole chain — which model read the document, how many retries, what
each stage cost, where it stalled. `runs.stage` records the last completed node, so a
failed run says exactly where it died, and `runs.error` says why.

Cost is summed from `agent_spans`, not from an in-process counter. That distinction
was earned: a resumed run reported $0.00101 against an actual $0.01064, because the
in-memory budget resets on resume while the spans survive.

**Dashboard at 50 customers.** Four panels, in priority order:

1. **Escaped-error rate** — approvals later overturned by a human, per customer. The
   only metric that can hurt a customer, and the one that should page someone.
2. **Touchless rate and uncertain rate**, trended per customer. A sudden rise in
   uncertainty usually means a supplier changed their document template — actionable
   within a day, and invisible in an accuracy average.
3. **Cost per document** by model and node, with the escalation rate. Answers "is a
   customer sending us garbage scans?" before the bill does.
4. **p50/p95 latency by node**, since the tail is what operators experience.

What is missing for production: `OpenTelemetry` spans so the trace spans services
rather than one SQLite file, and `Langfuse` for prompt-level replay — the current
`agent_spans` records that a call happened and what it cost, but not the exact prompt
sent, so a regression cannot be replayed exactly.

---

## 4. Cost

Measured from `agent_spans`, `gpt-4.1` for extraction and `gpt-4.1-mini` elsewhere.

| Stage | Calls/doc | Avg cost | Share |
|---|---|---|---|
| Extract | 1–2 | $0.0098 | **90%** |
| Route | 1–2 | $0.0004 | 8% |
| Validate | 0–1 | $0.0002 | 2% |

Clean document **$0.0106**; degraded scan with escalation **$0.0202**. At 1,000
documents/day that is roughly **$11–20/day**, or $4–7k/year — against a CG headcount
this is not the binding constraint, which is the correct shape for the business case.

**Where it blows up.** Page count, not document count — cost is linear in page images
and B/Ls run 1–3 pages while contracts run 40. A 40-page document at high detail is
~40× a B/L. Second, the escalation ladder: a systematically bad scanner at one supplier
sends every field to a second pass and doubles that customer's bill silently.

**How it is controlled.** Hard caps enforced in code, not prompts:
`MAX_USD_PER_DOCUMENT` (default $0.25) and `MAX_LLM_CALLS_PER_RUN` (12), checked
*before* each call, raising rather than degrading. Escalation is per-field, so a clean
page costs exactly one call. Retries are capped at 3 with backoff, and only on
transient errors — a schema failure is not retried, because it will fail identically.
The reading cache means reprocessing a document is free. Validation is deterministic
code and costs nothing on a clean document.

The obvious next lever is routing extraction by document quality: run `gpt-4.1-mini`
first and escalate to `gpt-4.1` only when grounding is weak. On the clean documents
here that would likely cut extraction cost ~5× at no quality loss, but it needs a
bigger golden set to prove rather than assume.

---

## 5. Latency

| Hop | Time | Note |
|---|---|---|
| **OCR (scanned pages only)** | **~30 s/page** | slowest hop by far |
| Vision extraction | 7–13 s | 90% of the cost, ~50% of clean-doc latency |
| Targeted re-extraction | 3–10 s | only when fields come back weak |
| Validate | 0–2.4 s | 0 ms with no entity-name call |
| Route | 2–5 s | two small calls, currently sequential |
| Everything else | <100 ms | SQLite, rendering, grounding |

Clean document end-to-end **8–12 s**; degraded scan **28 s**.

**The slowest hop is OCR, and it is not on the critical path.** OCR produces the
grounding corpus, which is only needed *after* extraction returns — yet it currently
runs first, serially, during preprocessing. Running it concurrently with the vision
call would remove ~30 s from every scanned document for the cost of a thread. That is
the single highest-value latency fix and it is not done.

After that: the Router's two calls (draft + rationale) are independent and could be
concurrent, saving ~2 s; and multi-page extraction is one call over N images, which
could fan out per page. For the CG workflow none of this is urgent — the human
comparison is a 4–24 hour email cycle, so 30 s versus 10 s is not what the user feels.
It matters for throughput at volume, not for perceived responsiveness.

---

## 6. What I would do differently with a week

**Fix the one error class that survives.** Geometric provenance: use PyMuPDF word
coordinates and RapidOCR boxes to match each value to its nearest printed caption by
position. That closes §2.1 properly instead of trusting the model's self-report, and
it is the highest-value remaining correctness work.

**Build a real golden set.** 24 field readings across 3 documents is enough to catch
the bugs above and nowhere near enough to claim calibration. 100+ documents across
several carrier templates, with per-template accuracy, would turn "confidence
separation +0.38" from a promising signal into a threshold I would defend. It would
also let me tune the 0.85 approval floor against measured cost-of-error rather than
intuition.

**Model routing by difficulty.** Cheap model first, escalate on weak grounding. Likely
several-fold cost reduction, but it needs the golden set first — otherwise it is a
guess dressed as an optimisation.

**Move OCR off the critical path** and parallelise the Router's two calls.

**Structural changes I would make, having built it once:**

- *Cache the raw reading, never the verified result* — I got this wrong initially. The
  first version cached the finished `ExtractionOutput`, so every improvement to
  grounding was invisible on documents already seen, **including in production**.
  Tightening a rule silently did nothing for exactly the documents most likely to be
  reprocessed. Reading is expensive and immutable; verification is cheap and changes
  often. Only the first belongs in a cache.
- *Separate "is the value real" from "is the citation tidy" at the schema level.* I
  conflated them twice and both times it marked correct fields uncertain, spending
  human attention on nothing.
- *Version the rule set per shipment.* Today `decisions` records the outcome but rules
  can change underneath it; a dispute six months later needs the rules *as they were*.
  One column, and I would not ship to a real customer without it.

**Not doing more prompt engineering.** Every durable improvement here came from
deterministic verification around the model, not from better instructions to it. The
two prompt-level fixes that seemed to work (identifier completeness, box provenance)
both regressed under a different render resolution or were ignored outright. The model
is good at reading pixels; the code should decide what to believe.
