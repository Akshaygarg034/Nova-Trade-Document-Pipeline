# Technical Write-up · Nova Trade Document Pipeline

```
                    ┌────── LangGraph state machine ──────┐
   PDF / image ─────┼─ ingest ─ extract ─ validate ─ route┼─▶ done
                    └────┬────────┬─────────┬────────┬────┘
                         │  checkpoint saved after each step
        ┌────────────────▼────────▼─────────▼────────▼──────────┐
        │  SQLite: shipments · documents · runs                 │
        │          extracted_fields · validations · decisions   │
        │          agent_spans ← one row per model call:        │
        │                       tokens, cost, time, retries     │
        └──────────┬──────────────────────────┬─────────────────┘
              FastAPI + SSE            plain English → SQL (read-only)
                   ▼                          ▼
          React operator screen     "how many were flagged?"

   inside `extract`:  200 DPI ─▶ vision model ─▶ what it said ─┐
                      text layer / OCR ─▶ our own copy ────────┼▶ checks
                      weak fields ─▶ 300 DPI ─▶ ask again ─────┘
```

**Where state lives.** Three places. The LangGraph checkpoint file lets the
pipeline restart from the last finished step. The application tables hold the
results so they can be queried. The reading cache remembers what the vision
model said for a given document and model.

The third exists because of something we measured. LangGraph only saves a
step's output when the step *finishes*, so a crash *inside* extraction re-runs
the whole step. We saw it: two extraction calls, double cost, one document.
With the cache, a crash and restart costs **$0.01063** against **$0.01064**.

---

## Three failure modes we actually hit

**1 · The right text from the wrong box.** The extractor reported
`AMSTERDAM, NETHERLANDS` as the Port of Discharge, at 0.85 confidence, and it
passed every check. It was not making it up: Amsterdam really is printed on
that page, in the *Place of Delivery* box. Every text check passes and always
will, because our checks answer "is this text on the page?" and can never
answer "is it in the *right* box?" Only the customer rule caught it. Note the
knock-on effect: had that been the only problem, the amendment email would have
told the supplier to fix a port that was already correct.

*A fix that did not work.* I asked the model which box caption each value came
from. It answered `"PORT OF DISCHARGE"` — the caption it was *looking for*, not
the box it read. It also flagged a correct value on a clean document, where
Incoterms legitimately sit inside the goods description box, costing two
approvals. What catches it today is the two readings disagreeing; what would
fix it properly is matching values to labels by position, since PyMuPDF gives
word coordinates and RapidOCR gives boxes.

**2 · Fuzzy matching hides near-miss errors.** Fuzzy matching exists to cope
with scanner noise, and it copes with real errors just as happily, because a
one-character mistake and scanner noise look identical to it.

| What the model said | What the page says | Similarity | Before the fix |
|---|---|---|---|
| `2026-08841` | `INV-2026-08841` | 1.00 | approved — a shortened value is part of the full one |
| `12,960 KGS` | `12,980 KGS` | ~0.9 | approved |
| `SHANGHAI (CHSHA)` | `(CNSHA)` | ~0.96 | approved |
| `Acne Electronics…` | `Acme…` | ~0.97 | approved, **and it matched the rule** |

Four fixes: reference numbers must match a **complete token** on the page;
runs of three or more **digits must appear exactly**; **snapping to the page**,
where the model finds the value and the document decides the spelling, which
can only move a value closer to what is printed so a real supplier typo still
fails its rule; and **OCR trust dropped 0.92 → 0.80**, because a check is only
as good as the copy of the page behind it. Before: 3 wrong values approved on
the poor scan. After: **0 escaped errors**, everything approved correct (16/16).

**3 · Short codes matching inside longer words.** The Incoterm check looked for
`CIF` and found it — inside the word **SPECIFICALLY**, on a document with no
Incoterm at all. The most dangerous kind of made-up answer here, because the
"evidence" is real text from the real page. Fixed by matching whole words only
for short codes. That document is now a permanent test case.

---

## Tracing one shipment

Every model call writes a row to `agent_spans`: run id, step, call name, model,
tokens, cost, time, retries, status, error. Every stage writes under the same
run id, so tracing one shipment across 50 customers is one join:

```sql
SELECT s.node, s.name, s.model, s.latency_ms, s.usd, s.status
  FROM agent_spans s JOIN runs r USING (run_id)
 WHERE r.shipment_id = 'SHP-2287' ORDER BY s.id;
```

`runs.stage` holds the last step that finished, so a failed run says where it
died. Cost is added from those rows, not a counter in memory — a restarted run
reported $0.00101 against a real $0.01064, because the in-memory total resets
while the rows survive.

**A dashboard, in priority order.** Escaped error rate per customer: approvals
a human later overturned, the only number that can hurt a customer. Then
touchless and uncertain rates per customer over time, because a jump in
uncertainty usually means a supplier changed their form — fixable in a day, and
invisible in an accuracy average. Then cost per document by model and step, and
50th/95th percentile time per step. Missing for production: OpenTelemetry, so a
trace spans several services rather than one SQLite file, and Langfuse for
replaying prompts.

---

## Cost and speed

| Step | Calls per doc | Average cost | Share | Time |
|---|---|---|---|---|
| Extract | 1–2 | $0.0098 | **90%** | 7–13 s |
| Route | 1–2 | $0.0004 | 8% | 2–5 s |
| Validate | 0–1 | $0.0002 | 2% | 0–2.4 s |
| OCR (scans only) | — | free | — | **~30 s per page** |

A clean document costs **$0.0106** and takes 8–12 s; the poor scan costs
**$0.0202** and takes 28 s. At 1,000 documents a day that is $11–20 a day, well
below the salary cost it offsets.

**Where cost would blow up:** page count, not document count — a Bill of Lading
is 1–3 pages, a contract is 40. And one supplier with a bad scanner sends every
field to a second reading, quietly doubling that customer's bill. Holding it
down: `MAX_USD_PER_DOCUMENT` ($0.25) and `MAX_LLM_CALLS_PER_RUN` (12) are
checked before every call and stop the run rather than cutting corners; every
call goes through one place in the code, so those limits are enforceable; and
escalation is per field, so a clean page costs exactly one call. The next thing
to try is choosing the model by difficulty, probably ~5× cheaper on clean
documents, but that needs a bigger golden set first.

**The slowest step is OCR, and it is not on the critical path — but it runs as
if it were.** OCR produces our copy of the page text, which is only needed
*after* extraction returns, yet it runs first during preprocessing. Running it
alongside the vision call would save ~30 s on every scanned document for the
price of one thread. That is the biggest speed win available and **it is not
done**.

---

## What I would do differently with a week

**Match values to labels by position**, closing failure mode 1 properly.
**Build a proper golden set** of 100+ documents across several carrier forms —
24 readings finds the bugs above and is nowhere near enough to claim the
confidence scores are calibrated, and it blocks every other improvement.
**Choose the model by difficulty.** **Move OCR off the critical path.**

Three structural things I would change, having built it once:

- **Cache what the model said, never what you concluded.** I got this wrong
  first time. Caching the finished result made every improvement to our
  checking invisible on documents already seen — *including in production* — so
  tightening a rule quietly did nothing for exactly the documents most likely
  to be reprocessed.
- **Keep "is the value real" separate from "is the quote tidy".** I mixed those
  up twice, and both times correct fields were marked uncertain, so a human
  spent attention on nothing.
- **Record the rule version with each decision.** Rules change underneath
  stored results, and a dispute needs the rules as they were.

**And not more prompt tuning.** Every lasting improvement came from
deterministic checks around the model, not better instructions to it. Both
prompt fixes that appeared to work either broke at a different image resolution
or were ignored. The model is good at reading pixels. The code should decide
what to believe.
