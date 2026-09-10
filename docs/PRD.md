# PRD · Multi-Agent Trade Document Pipeline

**Nova · Full-Stack AI Engineer DAW · Part 1**

> Every number here is measured — from `evals/run_eval.py` and the `agent_spans`
> table — not estimated.

---

## 1 · Understanding Nova

### What is Nova, and what can't traditional SaaS do?

Nova is GoComet's move from selling software that helps people do logistics work to
software that does the work and is measured on the result.

Traditional enterprise SaaS keeps its business logic identical for every customer —
that uniformity is where the margin comes from. Anything customer-specific gets
pushed back onto the user as configuration, or onto a services team. In logistics
that's fatal, because the customer-specific part *is* the work. Every CG team
validates against rules that exist only in an experienced operator's head: this
customer needs Rotterdam not Antwerp, these three HS codes are pre-classified, this
consignee name must match the customs registration exactly. No dashboard captures
that. So software handles the tidy 20% and hands the messy 80% back to a human,
which is exactly backwards.

LLM agents change the economics. Encoding idiosyncratic per-customer rules is now
cheap enough that software can absorb the exception handling instead of returning
it. Nova is the platform built on that bet: a generic engine — workflow
orchestrator, agent orchestrator, app builder, data layer — where all customer
specificity lives in configuration, and agents run against governed context with
tenant isolation, audit trails and cost controls rather than ad-hoc prompting.

*(197 words)*

### What is the FDE model and why does GoComet use it for Nova?

A Forward Deployed Engineer does discovery, design, build and deployment as one
person, sitting with the client instead of behind a spec.

Nova needs it because of what the platform is: a generic engine plus per-client
configuration. That configuration can't be gathered conventionally, because nobody
can write a spec for knowledge they hold tacitly. Ask a CG operator what rules they
apply and you'll get five. Watch them work for a day and you'll find forty —
including "if it's this supplier I always check the weight twice," which they'd
never think to mention because to them it isn't a rule, it's just what you do.

An engineer extracts that faster than a PM, because they can build it in the room
and ask "like this?" The feedback loop collapses from weeks to minutes, and each
answer gets tested against something real rather than someone's recollection.

There's a second reason: Nova is early. The engine's abstractions are still being
decided. Whoever configures the first ten clients discovers which things genuinely
vary and which only appear to — and that has to flow straight back into the
platform. GoComet's AOE pairing, one engineer and one client partner with no layers
between, exists to keep that loop short.

*(199 words)*

### What does "System of Outcomes" mean?

Three generations, distinguished by what the software is accountable for.

A **System of Record** stores truth — an ERP, a TMS. Data goes in, stays correct,
comes out as reports. Its value is accuracy of the record; humans do all the work.
Priced per seat.

A **System of Engagement** helps people collaborate on that data: dashboards,
queues, inboxes. It makes work easier to coordinate, but the work is still entirely
human. It makes a CG operator's day more organised without removing a single field
they have to read.

A **System of Outcomes** performs the work and is measured on the result. Not "we
stored your 400 documents" and not "here's a nice queue," but "validation cycle time
went from three days to four hours and 78% of documents cleared untouched." Its
value is the outcome, so the natural pricing is per outcome, not per seat.

The real distinction is accountability. A System of Record is wrong if the data is
wrong. A System of Outcomes is wrong if the *shipment is delayed* — even when every
stored field was technically correct. That's harder to sell and harder to build, and
it's why trust and evidence matter more here than features.

*(198 words)*

---

## 2 · Problem statement

SU emails documents. CG opens every attachment, reads every field, checks each against
what that customer requires, and types an amendment email. SU fixes, resubmits. Two to
four cycles per shipment is normal.

| # | Failure mode | Cost |
|---|---|---|
| 1 | Rules live in operators' heads | New hires err for weeks; leave cover is unreliable |
| 2 | Every field read manually, every time (~8 fields × 3–4 docs) | Attention spent on the 80% that's fine |
| 3 | **Partial amendments** — CG finds two problems, resubmit reveals a third | Each round trip adds 4–24h and demurrage |
| 4 | No queue visibility | Load can't be balanced; escalation is reactive |
| 5 | No audit trail — approval lives in a mailbox | Disputes six months later are unanswerable |
| 6 | Throughput capped by headcount | Volume growth needs linear hiring |

**#3 is the one to design against.** The loop repeats not because the first review was
careless but because it was *partial*. Checking every field against every rule in one
pass attacks cycle count directly, which is where the hours go.

**First five minutes.** The operator uploads a document they know is wrong and within
30 seconds sees: all eight fields with found-vs-required; the three problems flagged
without the five healthy fields competing for attention; the exact source text on
clicking a flagged field; a draft amendment listing all three correctly, close enough
that they think *"I'd have written roughly that"*; and a field the agent couldn't read
saying so rather than guessing.

The five-minute test isn't "is it accurate" — it's **"does it show its work?"** Someone
accountable for a customs filing won't trust a number without provenance. That is what
converts a sceptic.

---

## 3 · Users and jobs to be done

**Priya — CG validation operator.** Four years in freight documentation, ~40
shipments/week across six customers, measured on shipments cleared without a customs
issue. The work is repetitive rather than hard. She fears approving something wrong far
more than she minds the tedium — a missed HS code is her name on the file. *Cares
about:* not being blamed, and not re-reading the same document four times.

**Marco — SU shipping coordinator.** Emails documents onward across many customers
whose rules he half-remembers, and considers the job done once sent. Amendment requests
arrive days later, out of context and incomplete, so he fixes two things and gets a
third request. *Cares about:* being told everything that is wrong, once, specifically
enough to act without a phone call.

1. When **a document set arrives**, I want to **see every field checked against this
   customer's rules in one pass**, so that **I send one amendment instead of finding
   problems across three rounds**.
2. When **the agent reports a value**, I want to **see the exact text it read and
   where**, so that **I can accept it in two seconds instead of reopening the PDF**.
3. When **a scan is too poor to read a field**, I want to **be told explicitly**, so
   that **I check the two fields that need me, not all eight**.
4. When **a discrepancy is found**, I want **a draft stating field, found, expected
   and why it matters**, so that **I send it with one edit rather than writing it**.
5. When **I take over an account**, I want to **read that customer's rules as a
   file**, so that **I'm not making a new hire's mistakes for three weeks**.
6. *(Marco)* When **an amendment arrives**, I want **every problem listed with mine
   versus required**, so that **I fix everything in one resubmission**.

---

## 4 · Agent architecture

**Why three.** There are exactly three questions here, each with different evidence and
a different failure mode:

| Agent | Question | Evidence | Fails by |
|---|---|---|---|
| **Extractor** | What does this document say? | pixels, page text | misreading |
| **Validator** | Is what it says acceptable? | customer rule set | wrong rules |
| **Router** | What happens next? | validation state | wrong policy |

**Why not one prompt.** The decisive argument is a real failure. On our degraded scan
the extractor returned `AMSTERDAM, NETHERLANDS` for Port of Discharge at 0.85, *fully
grounded* — that text genuinely is on the page, in the *Place of Delivery* box. No
self-check finds it: every question about the document passes. Only the customer rule
("Acme clears at Rotterdam") catches it. One prompt has nowhere separate for that
knowledge to live, and nowhere separate for it to fire.

Three further consequences we rely on: each stage is **evaluated independently**; each
uses **the right tool** (forcing vision, arithmetic and policy through one model means
paying vision prices to compare two strings); and **rules change weekly while
extraction logic doesn't**, so rules can be edited without re-testing the model path.

**Why not five.** Preprocessing and confidence scoring aren't agents — they are
deterministic functions with no judgement to exercise. Making them agents adds a
handoff and a failure point to buy nothing.

| | Input | Output |
|---|---|---|
| Extractor | page images (vision only) | 8 fields: value, verdict, fused confidence, evidence quote, page, flags |
| *Verification layer* | fields + independent page text | grounded fields — **code, not a model** |
| Validator | grounded fields + rule YAML | per-field match/mismatch/uncertain with found, expected, severity, reason |
| Router | validation state | decision + machine-readable policy reasons + draft email |

Roughly **planner / executor / verifier**, except the verifier isn't an agent — it's a
deterministic layer around the executor, which is the point.

**How they talk.** Structured handoff via typed Pydantic models, one direction, no
shared mutable state. The Validator cannot see page images; the Router cannot see
extraction internals. That forces each stage's output to be complete enough to act on —
the same property that makes the UI and the audit trail possible.

**How state survives a crash.** Two mechanisms, because one is not enough and we
measured that: LangGraph checkpoints each node on return, and the vision call is
separately memoised on `(document bytes, model)`. LangGraph only checkpoints a node
that *returns*, so a crash *inside* `extract` re-runs it — we saw double cost on one
document. With both, crash-and-resume costs **$0.01063** against **$0.01064**
uninterrupted. Each node also commits to SQLite before the next begins, so an
unfinished run stays queryable. The cache stores the **raw reading, not the verified
result**; caching the finished output made verification improvements invisible on
already-processed documents. Detail in the technical write-up.

---

## 5 · LLM and tooling choices

| Job | Model | Why |
|---|---|---|
| Extraction (vision) | `gpt-4.1` | Only stage where quality converts into fewer human touches. 90% of spend |
| Entity-name equivalence | `gpt-4.1-mini` | One bounded judgement, ~200 tokens |
| Router draft + rationale | `gpt-4.1-mini` | Input already structured; no vision |
| NL → SQL | `gpt-4.1-mini` | Small schema, few-shot |
| Rules, grounding, decisions | **no model** | Deterministic, free, identical every run |

Extraction is $0.0098 and ~9s; everything else together is $0.0006 and ~5s. Given that
split, extraction is the only optimisation that matters. We chose the cheapest model
that cleared the bar rather than the strongest available — at 100% accuracy on clean
documents a larger model has nothing to buy.

**Fallback for a bad document** — escalating *per field*, not per document:

```
Tier 0  digital PDF text layer                    free, exact, no model
Tier 1  render 200 DPI → vision                   normal path, one call
Tier 2  weak fields only: 300 DPI, deskew, re-ask
Tier 3  two passes disagree → UNCERTAIN           never averaged
Tier 4  still unreadable → null, surfaced to a human
```

200 DPI is measured, not chosen: at 150 the model read `INV-2026-08841` as
`INV:2026-08841` because the hyphen didn't resolve.

**Orchestration: LangGraph** — for durable execution, not agent abstractions, of which
we use none. The pipeline is a fixed DAG with no dynamic tool-calling; what we need is
checkpointing, resumability, and somewhere clean to hang the human-in-the-loop
interrupt Part 2 will require.

**Structured output** (strict JSON schema) for everything machine-consumed; the
extractor's schema names all eight fields so the model cannot invent keys. We
deliberately **avoid tool-calling loops** — the extractor is a single-shot transform
with no agency to exercise. And we avoid a model wherever the answer is deterministic:
the routing decision is a pure function precisely because "should this customs document
be approved" must not vary between runs.

---

## 6 · Trust, failure handling and evals

Confidence shown to an operator is **not** the model's self-report:

```
final = model_confidence × grounding × format × corpus_trust × citation
        (evidence located on the page · ICC/HS format rules ·
         pdf_text 1.00 | ocr 0.80 | llm_transcript 0.70)
```

Four mechanisms, each added after a specific observed failure (forensics in the
write-up):

- **Mandatory evidence** — every `found` field carries a verbatim quote we re-locate in
  an independent corpus. Extraction is **vision-only**, so the check is not circular.
- **Word-boundary matching for short codes** — `CIF` matched inside **SPECIFICALLY**.
- **Token alignment and exact digits** — `2026-08841` scored 1.00 against
  `INV-2026-08841`; a truncation genuinely is a substring. Numbers get exactness.
- **Snap-to-source** — the model *locates* a value; the document decides how it *reads*.

`not_found` is a first-class correct answer, scored separately (2/2, 0 hallucinated).
Many B/Ls carry no Incoterm; inventing one is worse than misreading a port.

**Low confidence.** Nothing below 0.85 is auto-approved, and uncertainty propagates: an
unverified field is `UNCERTAIN` even if the rule would pass, though the record still
shows what the rule *would* have said. At the Router, **unread fields outrank confirmed
mismatches** — a document with both goes to review, not to an amendment email, because
drafting from data we could not read risks asking the supplier to fix the wrong thing.

**Loops and runaway cost.** `MAX_USD_PER_DOCUMENT` ($0.25) and `MAX_LLM_CALLS_PER_RUN`
(12) checked *before* each call, raising rather than degrading; every call goes through
one choke point, so these are enforceable rather than aspirational. Retries capped at 3,
transient errors only. Re-extraction happens once, on weak fields only.

**Offline eval** (`evals/run_eval.py`). The headline metric is deliberately not accuracy
but **escaped errors** — values wrong *and* auto-approved:

| | Clean | Degraded | All |
|---|---|---|---|
| Field accuracy | 100% | 62.5% | 87.5% |
| Auto-approve accuracy | 100% | 100% | **100%** (16/16) |
| **Escaped errors** | **0** | **0** | **0** |
| Surfaced-error recall | — | 100% (3/3) | 100% (3/3) |
| Confidence separation | — | +0.14 | **+0.42** |

Plus seven offline unit suites that make no LLM calls, encoding every failure above as a
regression test. *Caveat:* 24 readings across 3 documents — direction right, sample
nowhere near enough to claim calibration.

**Online metric** — *escaped-error rate*: of fields auto-approved, the share a human
later overturns. Measurable from day one, since the UI records every operator edit.

---

## 7 · Metrics and success criteria

> **North star: touchless validation rate** — the percentage of shipment document sets
> reaching a *correct* CG decision with zero human field edits.

Correctness is inside the definition, so it can't be gamed by approving more.

| Metric | Type | Target | Why |
|---|---|---|---|
| Escaped-error rate | quality | **0** | Only metric that can hurt a customer; guardrail on the north star |
| Surfaced-error recall | quality | ≥95% | Of what we get wrong, how much we flag |
| Median cycle time, email → decision | business | −50% | What CG is paid to shorten |
| Amendment cycles per shipment | business | 2–4 → ≤1.5 | Attacks the loop, not keystrokes |
| Draft-edit distance | product | ≤20% words changed | Whether the draft is sendable or a rewrite |
| Uncertain-field rate by template | health | trended | A spike means a supplier changed their form |
| Cost per document | health | <$0.05 | Must stay far under the labour it replaces |
| p95 end-to-end latency | health | <60s | The tail is what operators feel |

Draft-edit distance is the one I would fight for: every other metric can look healthy
while CG quietly rewrites every email — at which point we have automated nothing and
added a step.

**Go** (all must hold): zero escaped errors reaching the customer; touchless ≥40% on
clean digital documents; median cycle time −50% vs the prior fortnight; ≥80% of drafts
sent with ≤20% words changed; cost <$0.05/doc; and the operator keeps using it in week
two without being asked.

**No-Go** (any): a wrong value reaches a customer via auto-approval; CG rewrites more
than half the drafts; touchless <20%; or operators start ignoring uncertainty flags —
the worst outcome, because it manufactures false confidence.

---

## 8 · What's next

1. **Multi-document shipments and cross-document consistency** (4d). A shipment is B/L
   *plus* invoice *plus* packing list, and the highest-value check is not per-document —
   it is whether consignee and HS code agree *across* all three. No single document can
   fail that check, so a human always does it today.
2. **A real golden set, 100+ documents across several templates** (3d). Everything in §6
   rests on 24 readings. Without it I cannot defend the 0.85 threshold, safely try a
   cheaper extraction model, or tell template drift from regression. It unblocks the
   others, which is why it outranks them.
3. **Geometric provenance** (2d) — match values to captions by coordinate, closing the
   one surviving error class. I tried asking the model which box it read from; it
   reports the caption it was *looking for*, so self-report fails.
4. **Rule-set versioning per decision** (1d). Rules change underneath stored outcomes; a
   dispute needs the rules *as they were*.

**Why not a nicer UI or more prompt engineering.** The UI is adequate, and Part 2 will
reshape it around the real CG workflow anyway. Prompt engineering has the worst track
record of anything I tried: every durable improvement came from deterministic
verification *around* the model, while both prompt-level fixes that appeared to work
either regressed at a different render resolution or were ignored outright. The model is
good at reading pixels. The code should decide what to believe.
