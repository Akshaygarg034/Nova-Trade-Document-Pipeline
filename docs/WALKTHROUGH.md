# Walkthrough · test every feature

Run from the project root with the venv active. On Windows use
`.venv/Scripts/python.exe` in place of `python`.

**Almost everything below is free.** The vision call is memoised on document
content, so re-running the samples costs $0.00000. Steps that spend money are
marked **[$]** with the amount.

---

## 0 · Check the setup

```bash
python -c "from app.config import settings; print('key:', bool(settings.openai_api_key), '| model:', settings.extractor_model)"
ls samples/clean samples/messy
```

Expect `key: True` and three documents. If samples are missing:
`python samples/fetch_templates.py && python samples/build_samples.py`

---

## 1 · Behaviour A — Extractor

```bash
python run_extract.py samples/clean/SHP-1042_BOL.pdf
```

**Look for:** eight fields, each with a confidence, and an `evidence:` block at
the bottom quoting the text each value came from. `OK` = verified,
`??` = uncertain, `--` = absent.

Note `invoice_number` carries the flag `identifier_repaired` or
`snapped_to_source:...`. That is the system correcting the model's
transcription against the characters actually printed on the page.

### The hallucination probe

```bash
python run_extract.py samples/clean/SHP-2287_BOL.pdf
```

**Look for:** `incoterms  (not found)` with flag `reported_absent`.

This document has **no Incoterm at all** — normal for an ocean B/L. Inventing
one would be the worst failure in this domain, and it is tempting: the string
`CIF` genuinely appears on the page, inside the word **SPECIFICALLY** in the
carrier's liability boilerplate. Prove it:

```bash
python -c "
import pymupdf, re
t = pymupdf.open('samples/clean/SHP-2287_BOL.pdf')[0].get_text()
print('naive substring CIF found:', 'CIF' in t)
print('word-boundary CIF found  :', bool(re.search(r'\bCIF\b', t)))
print('context:', [t[m.start()-30:m.end()+10].replace(chr(10),' ') for m in re.finditer('CIF', t)])
"
```

### The degraded scan

```bash
python run_extract.py samples/messy/SHP-2287_BOL_scan.jpg
```

**Look for:** most fields `??` with flags like `digits_not_on_page:12,960`,
`reextraction_disagreement`, `weak_grounding`. The system read a bad scan and
says so instead of guessing. Two `span` lines mean the escalation ladder fired
— weak fields were re-read at 300 DPI after deskew.

---

## 2 · Behaviours B + C — Validator and Router

All three decision paths, on real documents:

```bash
python run_pipeline.py samples/clean/SHP-1042_BOL.pdf        # AUTO_APPROVE
python run_pipeline.py samples/clean/SHP-2287_BOL.pdf        # AMENDMENT_REQUEST
python run_pipeline.py samples/messy/SHP-2287_BOL_scan.jpg   # FLAG_FOR_REVIEW
```

**On the second one, look for:**
- three `BLOCKING` discrepancies with found vs expected **and** the business
  reason for each
- `why (deterministic policy)` — the machine-readable audit trail
- a draft email that names all three problems and is genuinely sendable

**On the third, look for:** `UNCERTAIN (6) - must not be auto-approved`, each
with `[rule would say: match/mismatch]`. The operator sees what the rule
*would* have concluded, rather than a shrug.

Also on the third: `port_of_discharge` reads `AMSTERDAM, NETHERLANDS` against
a required `ROTTERDAM (NLRTM)`. That text really is on the page — in the
*Place of Delivery* box. Grounding cannot catch it; the customer rule does.
This is the clearest argument for keeping extraction and validation separate.

### Rules are data, not code

```bash
cat rules/acme_electronics.yaml
```

Change `expected` under `port_of_discharge` to `AMSTERDAM (NLAMS)` and re-run
the third command — the mismatch disappears. No code touched.

---

## 3 · Behaviour D — storage and natural-language query

```bash
python ask.py "how many shipments were flagged for review this week?"
python ask.py --demo          # 7 questions   [$] ~$0.004
```

**Look for:** the answer, then the generated SQL and the rows underneath, so
it can be checked rather than trusted.

### The guards

```bash
python evals/test_nlq_guards.py
```

**Look for** `ALL PASS`, including: `DELETE`, `DROP`, `PRAGMA` and
`ATTACH DATABASE` blocked; statement stacking (`SELECT 1; DROP TABLE runs;`)
blocked; a real `DELETE` on the read-only connection raising; and a *value*
containing the word `DELETE` correctly **not** blocked.

Try a hostile question — it comes back as a read-only SELECT:

```bash
python ask.py "delete every decision from the database"
```

---

## 4 · Behaviour E — the operator UI

```bash
cd ui && npm install && npm run build && cd ..
python -m uvicorn app.main:app --port 8000
```

Open **http://127.0.0.1:8000**. Click through in this order:

1. **Sidebar** — three prior runs with their decisions and cost.
2. **Click `SHP-2287_BOL.pdf`** → decision panel (red, amendment), field table
   with found vs required, confidence meters, `blocking` badges.
3. **Click the `Consignee name` row** → the evidence drawer. This is the part
   worth dwelling on: the verbatim quote with its page, *why the rule exists*
   in business terms, and the three inputs behind the score separated —
   model self-report, grounding score, verified-on-page. Plus
   **Open page image** to see the actual document.
4. **Click `SHP-2287_BOL_scan.jpg`** → amber "Needs human review", six
   uncertain fields, plain-English explanations of each flag.
5. **Draft reply panel** → edit the text, then **Send to supplier**. It records
   that a *human* sent it (`email_sent=1`); nothing contacts a mail server.
   The agent never sends.
6. **Ask box** at the bottom → click a suggestion chip, then expand
   *Show the query and rows*.
7. **Upload a document** (top right) → watch the stage pills advance
   `ingest → extract → validate → route → completed` live over SSE. **[$]**
   ~$0.001 for a sample you have already run, ~$0.011 for a new one.

---

## 5 · Crash and resume

```bash
python run_graph.py samples/clean/SHP-2287_BOL.pdf --shipment SHP-2287 --crash-after extract
```

It fails and prints a run id. Inspect what survived:

```bash
python -c "
from app import store
r = store.fetch_run('PASTE_RUN_ID')
print('status:', r['status'], '| died after stage:', r['stage'])
print('fields already durable:', len(r['fields']))
print('validations:', len(r['validations']), '| decision:', r['decision'])
"
```

Extraction is already persisted; validation and decision are not. Now resume:

```bash
python run_graph.py --resume PASTE_RUN_ID
```

**Look for:** it completes to `AMENDMENT_REQUEST`, and the span list contains
**no `extract_pass1` at all** — only `validate` and `route`, totalling
~$0.001. Extraction was served from the reading cache, so the vision call was
not paid for again.

If you want to watch the two mechanisms separately, clear the cache first so
extraction is genuinely re-run:

```bash
rm -rf data/reading_cache        # [$] the crashed attempt now costs ~$0.01
python run_graph.py samples/clean/SHP-2287_BOL.pdf --crash-after extract
python run_graph.py --resume PASTE_RUN_ID
```

The crashed attempt pays for extraction once and records the span; the resume
adds **zero** extraction spans. That is the point: the checkpointer handles a
crash *between* nodes, the cache handles one *inside* a node, and neither
pays twice.

---

## 6 · Cost and loop guards

```bash
MAX_USD_PER_DOCUMENT=0.005 python run_pipeline.py samples/messy/SHP-2287_BOL_scan.jpg --fresh
```
**[$]** ~$0.011 — `--fresh` clears the cache to force real calls.

**Look for:**
```
router warning: draft generation failed (BudgetExceeded); using template
router warning: rationale generation failed (BudgetExceeded); using policy reasons verbatim
```

The cap fired mid-run and the Router **fell back to the deterministic template
email** rather than failing. The operator still gets a sendable draft. Note the
honest limitation: cost is unknown until a call returns, so the cap stops the
*next* call rather than predicting the current one.

Re-warm the cache afterwards so later steps are free:
```bash
python run_pipeline.py samples/messy/SHP-2287_BOL_scan.jpg
```
**[$]** ~$0.012

---

## 7 · Offline eval

```bash
python evals/run_eval.py
```

Free — it re-verifies from cached readings. **Look for:**

| | Clean | Degraded | All |
|---|---|---|---|
| Field accuracy | 100% | 62.5% | 87.5% |
| Auto-approve accuracy | 100% | 100% | 100% |
| **Escaped errors** | **0** | **0** | **0** |
| Surfaced-error recall | — | 100% | 100% |
| Confidence separation | — | +0.11 | **+0.41** |

*Escaped errors* — wrong **and** auto-approved — is the headline, not accuracy.
*Confidence separation* is the gap between mean confidence when correct and
when wrong; without it the score would be decoration.

Prove verification re-runs on cached readings for free: change
`SOURCE_TRUST["pdf_text"]` in `app/grounding.py` from `1.00` to `0.50`, re-run,
and watch auto-approvals collapse at `$0.00000`. Change it back.
(If results seem stuck, clear stale bytecode:
`find . -name __pycache__ -not -path "./.venv/*" -exec rm -rf {} +`.)

---

## 8 · Test suites — all offline, all free

```bash
for t in grounding repair snap validator router nlq_guards; do python evals/test_$t.py; done
```

Each encodes a bug found by running the system:

| Suite | What it locks down |
|---|---|
| `test_grounding` | the `CIF`-in-`SPECIFICALLY` trap; one-character numeric errors; OCR-noise tolerance |
| `test_repair` | field labels swept into identifiers, and that repair cannot launder a wrong value |
| `test_snap` | `Acne` → `Acme`, and that snapping only ever moves *toward* the document |
| `test_validator` | Amsterdam ≠ Rotterdam; that the suite makes **zero** LLM calls |
| `test_router` | 50 evaluations give one decision; drafts omitting a discrepancy are rejected |
| `test_nlq_guards` | SQL injection, statement stacking, read-only enforcement |

---

## 9 · Is OCR worth it?

```bash
python evals/ab_ocr.py
```
**[$]** ~$0.006 for the LLM-transcript arm.

Extraction is vision-only in both arms. What changes is the corpus the
verifier checks quotes against: OCR (independent of the model) versus an LLM
transcript (the same model checking its own reading). Both reach **0 escaped
errors** — OCR buys throughput and speed once cached, not correctness. Turn it
off entirely with `USE_OCR=false` in `.env`.

---

## Suggested demo-video path (~2½ min)

1. **0:00** `run_pipeline.py` on the clean B/L → `AUTO_APPROVE`, 8/8 match,
   one LLM call, ~$0.01. *"Extract, verify, validate, decide."*
2. **0:25** Same command on the discrepant B/L → three blocking discrepancies
   → read out the draft email. *"That is sendable with one edit."*
3. **0:55** Same command on the degraded scan → `FLAG_FOR_REVIEW`, six
   uncertain fields. *"It refuses to approve what it could not read."*
4. **1:20** UI → click the flagged consignee row → evidence drawer.
   *"Model self-report 0.99, grounding 1.00, verified on page — the operator
   can see why, not just what."*
5. **1:50** Point at `port_of_discharge` on the scan: Amsterdam is really on
   the page, in the wrong box. *"Grounding cannot catch that. The customer
   rule does. That is why these are separate agents."*
6. **2:10** Ask box → *"how many shipments were flagged this week?"* → expand
   the SQL. *"Grounded, not asserted."*
7. **2:25** `--crash-after extract`, then `--resume` → one extraction span.
   *"Crash-safe, and the expensive call is never paid twice."*
