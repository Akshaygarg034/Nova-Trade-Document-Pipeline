# Nova · Multi-Agent Trade Document Pipeline

Takes a trade document (Bill of Lading, PDF or scan), extracts eight fields with a
vision LLM, verifies every value against the document itself, checks it against one
customer's rule set, and decides: **auto-approve**, **flag for human review**, or
**draft an amendment request**.

Everything is stored and queryable in plain English. The agent never sends anything —
a human presses send.

```
document ──▶ Extractor ──▶ Validator ──▶ Router ──▶ SQLite ──▶ UI + NL query
             vision LLM     YAML rules    decide      per-stage
             + grounding    + 1 LLM call  + draft     durable
```

---

## Setup

Requires **Python 3.11+**, **Node 18+**, and an OpenAI API key.

```bash
python -m venv .venv
.venv/Scripts/activate          # Windows
# source .venv/bin/activate     # macOS / Linux

pip install -r requirements.txt
pip install -r requirements-ocr.txt   # optional, see note below

cp .env.example .env                  # then put your key in OPENAI_API_KEY
```

Build the sample documents (downloads two real blank B/L templates, fills and
flattens them, and derives a degraded scan):

```bash
python samples/fetch_templates.py
python samples/build_samples.py
```

**About `requirements-ocr.txt`:** it installs RapidOCR, which gives scanned images an
*independent* text corpus to verify the model's quotes against. Without it the system
still runs — it falls back to asking the LLM for a verbatim transcript and discounts
confidence accordingly, because a model checking its own reading is weaker evidence.
It is ~100 MB and pure pip, no system packages.

---

## Run it

### Reset to a known demo state

```bash
python reset_demo.py            # free: reuses cached vision calls, ~35s
python reset_demo.py --check    # verify only, change nothing
python reset_demo.py --cold     # [$] ~0.05: genuine from-scratch run
```

Leaves exactly three runs in the database, one per decision path. Safe to run
while the server is up.

### The whole pipeline, on one document

```bash
python run_graph.py samples/clean/SHP-1042_BOL.pdf --shipment SHP-1042
```

Three sample documents, three different outcomes:

| Document | Outcome |
|---|---|
| `samples/clean/SHP-1042_BOL.pdf` | `AUTO_APPROVE` — every rule passes |
| `samples/clean/SHP-2287_BOL.pdf` | `AMENDMENT_REQUEST` — 3 blocking discrepancies |
| `samples/messy/SHP-2287_BOL_scan.jpg` | `FLAG_FOR_REVIEW` — degraded scan, fields unreadable |

### The UI

```bash
cd ui && npm install && npm run build && cd ..
python -m uvicorn app.main:app --port 8000 --reload
```

Open **http://127.0.0.1:8000**. Upload a document and watch the stages stream live.
Click any field row for the evidence quote, the page it came from, and the three
inputs behind its confidence score.

For frontend development, `cd ui && npm run dev` proxies to the API on :8000.

### Ask questions about stored results

```bash
python ask.py "how many shipments were flagged for review this week?"
python ask.py --demo          # seven sample questions
```

Answers show the generated SQL and the rows underneath, so they can be checked
rather than trusted.

### Crash resume

The pipeline checkpoints after every stage. To see it:

```bash
python run_graph.py samples/clean/SHP-2287_BOL.pdf --crash-after extract
python run_graph.py --resume <run-id>     # printed by the command above
```

The resumed run completes at the same total cost as an uninterrupted one — the
vision call is not paid for twice.

### Tests and eval

```bash
for t in grounding repair snap validator router nlq_guards reconcile; do python evals/test_$t.py; done
python evals/run_eval.py        # offline eval against the golden set
```

The test suites make **no LLM calls** and cost nothing. `run_eval.py` re-verifies from
cached model readings, so it is free after the first run.

---

## What it costs and how long it takes

Measured, not estimated — every LLM call is recorded in the `agent_spans` table.

| Stage | Calls/doc | Avg cost | Avg latency |
|---|---|---|---|
| Extract | 1–2 | $0.0098 | 9.2 s |
| Validate | 0–1 | $0.0002 | 2.4 s |
| Route | 1–2 | $0.0004 | 2.9 s |

| Document | Total | Latency | LLM calls |
|---|---|---|---|
| Clean B/L | **$0.0106** | 11.5 s | 2 |
| Clean B/L with discrepancies | **$0.0100** | 8.1 s | 3 |
| Degraded scan (escalation fires) | **$0.0202** | 28.0 s | 5 |

**Extraction is 90% of spend** (routing 8%, validation 2%). That is what justifies
caching it, and escalating
per-field rather than re-reading whole documents. Validation is near-free because the
rule engine is deterministic code, not a model.

Hard limits are enforced in code, not hoped for: `MAX_USD_PER_DOCUMENT`,
`MAX_LLM_CALLS_PER_RUN` and `LLM_TIMEOUT_SECONDS` in `.env`. A run that hits one
raises rather than continuing.

---

## Accuracy

From `python evals/run_eval.py` over the golden set (3 documents, 24 field readings):

| | Clean | Degraded | All |
|---|---|---|---|
| Field accuracy | 100% | 62.5% | 87.5% |
| **Auto-approve accuracy** | **100%** (15/15) | **100%** (1/1) | **100%** (16/16) |
| **Escaped errors** (wrong *and* approved) | **0** | **0** | **0** |
| Surfaced-error recall | — | 100% (3/3) | 100% (3/3) |
| Hallucinated fields | 0 | 0 | 0 |
| Confidence separation | — | +0.14 | **+0.42** |

The headline metric is deliberately not field accuracy. A pipeline that is 95%
accurate and silent about the rest is worse than one that is 87% accurate and says
so: the first ships wrong customs data, the second asks a human. Every wrong value
on the degraded scan was caught before approval.

Confidence separation is the gap between mean confidence when correct (0.86) and
when wrong (0.44). Without that gap the score would be decoration.

---

## How confidence is calculated

The number shown to the operator is **not** the model's self-report. It is:

```
final = model_confidence
      × grounding      (was the quoted evidence actually found on the page?)
      × format         (does the value satisfy the field's hard format rule?)
      × corpus trust   (pdf_text 1.00 · ocr 0.80 · llm_transcript 0.70)
      × citation       (0.90 if the quote was not verbatim)
```

Extraction is **vision-only** — the model never sees the PDF text layer. If it did,
its quotes would trivially match the corpus we check them against and grounding
would be circular.

Values are also **snapped to the source**: if the model reads `Acne` where the page
says `Acme`, the page wins. This can only ever move a value *towards* the document,
so it cannot hide a genuine supplier error.

---

## Repo map

```
app/
  graph.py        LangGraph state machine + SQLite checkpointing
  agents/
    extractor.py  vision extraction, per-field escalation, reading cache
    validator.py  YAML rule engine (deterministic) + 1 LLM call for entity names
    router.py     deterministic decision policy + generated email draft
  grounding.py    evidence verification and confidence fusion
  preprocess.py   PDF/image → page renders + independent text corpus
  store.py        SQLite schema, per-stage writes, agent_spans (observability)
  nlq.py          natural-language → SQL, read-only, guarded
  main.py         FastAPI: upload, SSE progress, query, mark-as-sent
rules/
  acme_electronics.yaml    one customer's rules — data, not code
evals/
  run_eval.py     offline eval; test_*.py are offline unit suites
samples/          real B/L templates, filled; plus a degraded scan
ui/               React operator screen
docs/             PRD and technical write-up
```

**Rules are data.** [`rules/acme_electronics.yaml`](rules/acme_electronics.yaml) holds
every threshold, permitted value and business reason. Adding a customer is adding a
file; the engine contains no customer logic. Each rule's `why` is what the Router puts
in the amendment email, so suppliers are told the reason, not the rule name.

---

## Known limitations

Honest list, all observed rather than theoretical:

1. **Right text, wrong box.** On the degraded scan the extractor read
   `AMSTERDAM, NETHERLANDS` for Port of Discharge out of the *Place of Delivery*
   field. The text genuinely is on the page, so grounding cannot catch it — only the
   customer rule does. I tried asking the model which box it read from; it reports
   the caption it was *looking for*, so the check does not work. Matching values to
   captions by coordinate would.
2. **Confidence separation is measured on 24 readings.** The direction is right and
   the mechanism is sound, but the sample is far too small to call it calibrated.
3. **One document per run.** Multi-document shipments and cross-document consistency
   (does the consignee match across B/L, Invoice and Packing List?) are not built.
   The schema models a shipment owning many documents, so it is additive.
4. **OCR is slow** — ~30 s for a full scanned page, the single slowest hop. Cached to
   disk, so it is paid once per page.
5. **The NL query layer is read-only by construction** (`mode=ro` connection plus a
   statement guard), but it has no per-tenant scoping. Multi-tenant use needs a
   customer predicate injected into every query.
