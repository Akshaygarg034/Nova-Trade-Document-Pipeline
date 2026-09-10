# Technical Write-up · Nova Trade Document Pipeline

```
                 ┌─────────── LangGraph state machine ───────────┐
  PDF / image ───┼─▶ ingest ─▶ extract ─▶ validate ─▶ route ─▶ ⏹ │
                 └──────┬─────────┬──────────┬──────────┬────────┘
                        │ checkpoint + commit after each node
              ┌─────────▼─────────▼──────────▼──────────▼────────┐
              │                SQLite (nova.db)                  │
              │  shipments · documents · runs                    │
              │  extracted_fields · validations · decisions      │
              │  agent_spans ← one row per LLM call: tokens,     │
              │                usd, latency, retries, status     │
              └────────┬───────────────────────────┬─────────────┘
              FastAPI + SSE                 NL→SQL (mode=ro)
                       ▼                           ▼
              React operator UI          "how many were flagged?"

  inside `extract`:
     200 DPI render ──▶ vision LLM ──▶ raw reading ─┐
     text layer / OCR ──────────────▶ corpus ───────┼─▶ grounding
     weak fields ──▶ 300 DPI + deskew ──▶ re-read ──┘   quote? format?
                                                        digits? trust?
```

**Where state lives.** Three stores. The LangGraph checkpointer makes the *graph*
resumable at node boundaries; the application tables make the *result* queryable,
written by each node before the next begins; the reading cache memoises the vision call
on `(document bytes, model)`. The third exists because of a measured failure: LangGraph
checkpoints a node only when it *returns*, so a crash *inside* `extract` re-ran it —
two `extract_pass1` spans, double cost. With the cache, crash-and-resume costs
**$0.01063** against **$0.01064** uninterrupted.

---

## Three failure modes, all observed

**1 · Right text, wrong box — the one grounding cannot catch.** The extractor returned
`AMSTERDAM, NETHERLANDS` for Port of Discharge at 0.85, fully grounded. Not a
hallucination: that text is genuinely on the page, in the *Place of Delivery* box. Every
text check passes and always will — the quote exists, the format is valid, the value is
locatable. Grounding answers "is this text on the page?", never "is it in the *right*
box?" The Validator caught it via the customer rule. Note the second-order harm: had it
been the only problem, the amendment email would have told the supplier to fix a port
**already correct on their document**.

*A fix that failed.* I added `source_label`, asking which caption each value came from.
It reported `"PORT OF DISCHARGE"` — the caption it was *looking for*, not the box it
read from, despite an explicit instruction. It also false-positived on a clean document
(Incoterms legitimately live inside the goods-description box on a JSE B/L), costing two
auto-approvals. Now scoped to fields with a dedicated box. What catches it today is
two-pass disagreement; what would catch it properly is geometric provenance — PyMuPDF
gives word coordinates, RapidOCR gives boxes.

**2 · Fuzzy matching launders near-miss errors.** One root cause, four incidents. Fuzzy
similarity absorbs OCR noise, and absorbs real errors just as happily, because a
one-character error and scanner noise are indistinguishable to edit distance.

| Extracted | Page says | Similarity | Before |
|---|---|---|---|
| `2026-08841` | `INV-2026-08841` | 1.00 | approved — a truncation *is* a substring |
| `12,960 KGS` | `12,980 KGS` | ~0.9 | approved |
| `SHANGHAI (CHSHA)` | `(CNSHA)` | ~0.96 | approved |
| `Acne Electronics…` | `Acme…` | ~0.97 | approved, **and matched the rule** |

Fixes: **token alignment** (an identifier must equal a whole token on the page); **exact
digit checks** (every 3+ digit run must appear verbatim — numbers get exactness, not
fuzziness); **snap-to-source** (the model locates a value, the document decides how it
reads, guarded so it can only move *toward* the page, so a genuine supplier typo still
fails its rule); and **OCR trust 0.92 → 0.80**, since verification is only as good as
the corpus behind it. Before: 3 wrong values auto-approved on the degraded scan. After:
**0 escaped errors**, auto-approve accuracy 100% (16/16).

**3 · Substring matches inside unrelated words.** The Incoterm check found `CIF` in the
page text — inside the word **SPECIFICALLY**, in liability boilerplate, on a document
with no Incoterm at all. The most dangerous shape of hallucination here, because the
"evidence" is real text from the real page, so naive substring grounding rubber-stamps
it. Fixed with word-boundary matching for short enum codes. That document is now a
permanent fixture: the only correct extraction for its Incoterm is `not_found`.

---

## Observability

Every LLM call writes an `agent_spans` row (`run_id, node, name, model, tokens, usd,
latency_ms, attempts, status, error`); every stage writes under the same `run_id`.
Tracing one shipment across 50 customers is one join:

```sql
SELECT s.node, s.name, s.model, s.latency_ms, s.usd, s.status
  FROM agent_spans s JOIN runs r USING (run_id)
 WHERE r.shipment_id = 'SHP-2287' ORDER BY s.id;
```

`runs.stage` records the last completed node, so a failed run says where it died and
`runs.error` says why. Cost is summed from spans, not an in-process counter — a resumed
run reported $0.00101 against an actual $0.01064 before that fix, because the in-memory
budget resets on resume while spans survive.

**Dashboard, in priority order:** escaped-error rate per customer (the only metric that
can hurt a customer, and the one that should page someone); touchless and uncertain
rates trended per customer, since a spike in uncertainty usually means a supplier
changed their template — actionable within a day and invisible in an accuracy average;
cost per document by model and node; p50/p95 latency by node. Missing for production:
OpenTelemetry so traces span services rather than one SQLite file, and Langfuse for
prompt-level replay — spans record that a call happened and what it cost, not the exact
prompt.

---

## Cost and latency

| Stage | Calls/doc | Avg cost | Share | Latency |
|---|---|---|---|---|
| Extract | 1–2 | $0.0098 | **90%** | 7–13 s |
| Route | 1–2 | $0.0004 | 8% | 2–5 s |
| Validate | 0–1 | $0.0002 | 2% | 0–2.4 s |
| OCR (scans only) | — | free | — | **~30 s/page** |

Clean document **$0.0106** / 8–12 s; degraded scan **$0.0202** / 28 s. At 1,000 docs/day
that is ~$11–20/day, well under the headcount it offsets.

**Where cost blows up:** page count, not document count — cost is linear in page images,
and B/Ls run 1–3 pages while contracts run 40. A systematically bad scanner at one
supplier also sends every field to a second pass, silently doubling that customer's
bill. **Controls:** `MAX_USD_PER_DOCUMENT` ($0.25) and `MAX_LLM_CALLS_PER_RUN` (12)
checked *before* each call, raising rather than degrading, with every call routed
through one choke point so they are enforceable; escalation is per-field, so a clean
page costs one call; retries capped at 3, transient only. The next lever is routing by
difficulty — cheap model first, escalate on weak grounding, likely ~5× cheaper on clean
documents but needing a bigger golden set to prove.

**The slowest hop is OCR, and it is not on the critical path — but it runs as if it
were.** OCR produces the grounding corpus, needed only *after* extraction returns, yet
it runs first, serially, in preprocessing. Running it concurrently with the vision call
removes ~30 s from every scanned document for the cost of a thread. That is the
highest-value latency fix and **it is not done**. After that, the Router's two calls are
independent and could be concurrent (~2 s). None of it is urgent for the CG workflow —
the human baseline is a 4–24 hour email cycle — so it matters for throughput at volume,
not perceived responsiveness.

---

## What I would do differently with a week

**Geometric provenance** to close failure mode 1 properly rather than trusting
self-report. **A real golden set** — 100+ documents across several templates; 24
readings finds the bugs above and is nowhere near enough to claim calibration, and it
gates every other optimisation. **Model routing by difficulty.** **OCR off the critical
path.**

Three structural changes, having built it once:

- **Cache the raw reading, never the verified result.** I got this wrong first time:
  caching the finished output made every grounding improvement invisible on documents
  already seen — *including in production* — so tightening a rule silently did nothing
  for exactly the documents most likely to be reprocessed. Reading is expensive and
  immutable; verification is cheap and changes often.
- **Separate "is the value real" from "is the citation tidy" at the schema level.** I
  conflated them twice; both times correct fields were marked uncertain, spending human
  attention on nothing.
- **Version the rule set per decision.** Rules change underneath stored outcomes; a
  dispute six months later needs the rules *as they were*.

**And not more prompt engineering.** Every durable improvement came from deterministic
verification around the model, not better instructions to it. Both prompt-level fixes
that appeared to work either regressed at a different render resolution or were ignored
outright. The model is good at reading pixels; the code should decide what to believe.
