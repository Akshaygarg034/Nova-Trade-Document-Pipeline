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

**The written thinking lives in `docs/`:**

| | |
|---|---|
| [`docs/PRD.md`](docs/PRD.md) | Problem, users, agent architecture, model choices, trust, metrics |
| [`docs/TECHNICAL_WRITEUP.md`](docs/TECHNICAL_WRITEUP.md) | Architecture diagram, failure modes, observability, cost, latency |
| [`docs/SAMPLE_QUERIES.md`](docs/SAMPLE_QUERIES.md) | Natural-language queries run against stored output, with SQL and rows |
| [`docs/WALKTHROUGH.md`](docs/WALKTHROUGH.md) | Command-by-command guide to exercising every feature |

---

## Setup

Requires **Python 3.11+**, **Node 18+**, and an OpenAI API key.

```bash
python -m venv .venv
.venv/Scripts/activate          # Windows
# source .venv/bin/activate     # macOS / Linux

pip install -r requirements.txt
pip install -r requirements-ocr.txt   # optional — see note at the bottom

cp .env.example .env                  # then set OPENAI_API_KEY
```

Build the sample documents. This downloads two real blank Bill of Lading templates,
fills them in, and derives a degraded scan from one of them:

```bash
python samples/fetch_templates.py
python samples/build_samples.py
```

---

## Quickest way to see it work

```bash
python reset_demo.py     # runs all three sample documents, ~35s

cd ui && npm install && npm run build && cd ..
python -m uvicorn app.main:app --port 8000 --reload
```

Open **http://127.0.0.1:8000**. Three runs will be in the sidebar, one per decision
path. Click a run, then click any field row to see the quote it came from, the page,
and the three inputs behind its confidence score.

`reset_demo.py` costs nothing on a second run, because the vision call is cached
against document content. Add `--cold` (~$0.05) to force genuine API calls, or
`--check` to verify state without changing anything.

---

## Running it from the command line

### One document, end to end

```bash
python run_graph.py samples/clean/SHP-1042_BOL.pdf --shipment SHP-1042
```

The three sample documents deliberately produce three different outcomes:

| Document | Outcome |
|---|---|
| `samples/clean/SHP-1042_BOL.pdf` | `AUTO_APPROVE` — every rule passes |
| `samples/clean/SHP-2287_BOL.pdf` | `AMENDMENT_REQUEST` — 3 blocking discrepancies |
| `samples/messy/SHP-2287_BOL_scan.jpg` | `FLAG_FOR_REVIEW` — degraded scan, fields unreadable |

### Ask questions about stored results

```bash
python ask.py "how many shipments were flagged for review this week?"
python ask.py --demo          # seven sample questions
```

Answers come with the generated SQL and the rows underneath, so they can be checked
rather than trusted. The connection is read-only, so a generated query cannot write.

### Crash and resume

The pipeline checkpoints after every stage:

```bash
python run_graph.py samples/clean/SHP-2287_BOL.pdf --crash-after extract
python run_graph.py --resume <run-id>     # printed by the command above
```

The resumed run costs the same total as an uninterrupted one — the vision call is not
paid for twice.

### Tests and eval

```bash
for t in grounding repair snap validator router nlq_guards reconcile; do python evals/test_$t.py; done
python evals/run_eval.py        # offline eval against the golden set
```

The test suites make **no LLM calls** and cost nothing. `run_eval.py` re-verifies from
cached model readings, so it is free after the first run. Headline result: **0 escaped
errors** — nothing wrong was ever auto-approved. Full numbers are in the PRD.

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
evals/            run_eval.py (offline eval) + test_*.py (offline unit suites)
samples/          real B/L templates, filled; plus a degraded scan
ui/               React operator screen
docs/             PRD, technical write-up, sample queries, walkthrough
```

**Rules are data.** [`rules/acme_electronics.yaml`](rules/acme_electronics.yaml) holds
every threshold, permitted value and business reason. Adding a customer means adding a
file; the engine contains no customer logic. Each rule's `why` is what the Router puts
in the amendment email, so suppliers are told the reason, not the rule name.

---

## Notes

**`requirements-ocr.txt`** installs RapidOCR, which gives scanned images an
*independent* copy of the page text to check the model's quotes against. Without it
the system still runs — it falls back to asking the LLM for a verbatim transcript and
discounts confidence accordingly, because a model checking its own reading is weaker
evidence. It is ~100 MB, pure pip, no system packages. Set `USE_OCR=false` in `.env`
to skip it entirely.

**Cost and safety limits** live in `.env`: `MAX_USD_PER_DOCUMENT`,
`MAX_LLM_CALLS_PER_RUN` and `LLM_TIMEOUT_SECONDS`. They are checked before every call
and stop the run rather than quietly degrading.

A clean document costs about **$0.01** and takes 8–12 seconds; a degraded scan costs
about **$0.02** and takes ~28 seconds. Extraction is 90% of the spend. Breakdown and
reasoning are in the technical write-up.
