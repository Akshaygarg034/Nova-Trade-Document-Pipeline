# PRD · Multi-Agent Trade Document Pipeline

**Nova · Full-Stack AI Engineer DAW · Part 1**

> Every number here is measured, not estimated. They come from
> `evals/run_eval.py` and the `agent_spans` table in the running system.

---

## 1 · Understanding Nova

### What is Nova, and what can't traditional SaaS do?

Nova is GoComet moving from software that helps people do the work, to
software that does the work and is judged on the result.

Normal enterprise software has to behave the same way for every customer.
That sameness is where the profit comes from. Anything specific to one
customer gets pushed back onto the user as settings, or handed to a services
team.

In logistics that falls apart, because the customer-specific part *is* the
work. Every cargo team checks documents against rules that live in one
experienced person's head. This customer clears customs at Rotterdam, not
Antwerp. These three HS codes are pre-approved. This consignee name must match
the customs record exactly. No dashboard holds any of that. So the software
handles the easy 20% and hands the messy 80% back to a human, which is
backwards.

AI agents change the maths. Writing down odd, customer-specific rules is now
cheap enough that the software can absorb the exceptions instead of returning
them. Nova is built on that idea: one generic engine, everything
customer-specific kept in config, and agents that run with real controls —
tenant isolation, audit trails and cost limits.

*(195 words)*

### What is the FDE model and why does GoComet use it for Nova?

A Forward Deployed Engineer does the discovery, the design, the build and the
deployment. One person, sitting with the client, instead of working from a
written spec.

Nova needs this because of what it is: a generic engine plus per-client
config. You cannot gather that config the usual way, because nobody can write
a spec for knowledge they hold without thinking about it. Ask a cargo operator
what rules they follow and you get five. Watch them work for a day and you
find forty. One will be "if it's this supplier I always check the weight
twice" — which they would never think to mention, because to them it is not a
rule, it is just how the job is done.

An engineer gets that out faster than a PM, because they can build it in the
room and ask "like this?". The loop drops from weeks to minutes, and every
answer gets tested against something real.

There is a second reason. Nova is early, so the engine's shape is still being
decided. Whoever sets up the first ten clients finds out which things really
vary between customers and which only look like they do. That has to feed
straight back into the platform.

*(197 words)*

### What does "System of Outcomes" mean?

Three generations of software, told apart by what the software is responsible
for.

A **System of Record** stores the truth. An ERP, a TMS. Data goes in, stays
correct, comes out as reports. Its value is an accurate record. People still
do all the work. Sold per seat.

A **System of Engagement** helps people work together on that data.
Dashboards, queues, inboxes. It makes work easier to organise, but the work is
still fully human. It makes a cargo operator's day tidier without removing a
single field they have to read.

A **System of Outcomes** does the work and is judged on the result. Not "we
stored your 400 documents", and not "here is a tidy queue", but "checking time
dropped from three days to four hours, and 78% of documents cleared without
anyone touching them". The value is the result, so charging per result makes
more sense than per seat.

The real difference is responsibility. A System of Record is wrong when the
data is wrong. A System of Outcomes is wrong when the shipment is late, even
if every stored field was correct. That is harder to build and harder to sell.
It is also why trust and evidence matter more here than features.

*(198 words)*

---

## 2 · The problem

A supplier emails documents. The cargo team reads every field of every
attachment, checks each one against what that customer needs, and types an
email listing what is wrong. The supplier fixes it and resends. Two to four
rounds per shipment is normal.

| # | What goes wrong | What it costs |
|---|---|---|
| 1 | The rules only exist in people's heads | New hires make mistakes for weeks; leave cover is unreliable |
| 2 | Every field read by hand, every time (8 fields × 3–4 docs) | Attention spent on the 80% that is fine |
| 3 | **Half-finished amendments** — two problems found, a third appears after the fix | Each extra round adds 4 to 24 hours, and demurrage builds |
| 4 | Nobody can see the queue | Work cannot be balanced; problems found late |
| 5 | No audit trail — the approval sits in a mailbox | A dispute six months later cannot be answered |
| 6 | Throughput limited by headcount | More volume means more hiring |

**Number 3 is the one to design against.** The loop repeats not because the
first review was careless, but because it was incomplete. Checking every field
against every rule in one pass attacks the number of rounds, and the rounds
are where the hours go.

**The first five minutes.** The operator uploads a document they already know
is wrong. Within 30 seconds they see all eight fields with found next to
required; the three problems highlighted without the five healthy fields
competing for attention; the exact text from the page when they click a
flagged field; a draft email listing all three problems correctly; and any
field the system could not read saying so instead of guessing.

The five-minute test is not "is it accurate". It is **"does it show its
work?"** Nobody accountable for a customs filing trusts a number without
seeing where it came from.

---

## 3 · Users and jobs to be done

**Priya — cargo control operator.** Four years in freight documentation, about
40 shipments a week across six customers, judged on shipments that clear
customs cleanly. She is far more afraid of approving something wrong than she
is bothered by the repetition, because a missed HS code has her name on it.
*Wants:* not to be blamed, and not to read the same document four times.

**Marco — supplier shipping coordinator.** Sends documents to many customers
whose rules he half-remembers, and considers the job done once the email is
sent. Amendment requests arrive days later, incomplete, so he fixes two things
and gets a third request. *Wants:* to be told everything that is wrong, once,
clearly enough to fix without a phone call.

1. When **a set of documents arrives**, I want to **see every field checked
   against this customer's rules in one go**, so that **I send one amendment
   instead of finding problems across three rounds**.
2. When **the system reports a value**, I want to **see the exact text it read
   and where**, so that **I can accept it in two seconds instead of reopening
   the PDF**.
3. When **a scan is too poor to read a field**, I want to **be told plainly**,
   so that **I check the two fields that need me, not all eight**.
4. When **a problem is found**, I want **a draft saying the field, what was
   found, what is required and why it matters**, so that **I send it after one
   edit instead of writing it**.
5. When **I take over an account**, I want to **read that customer's rules as
   a file**, so that **I am not making a new starter's mistakes for weeks**.
6. *(Marco)* When **an amendment arrives**, I want **every problem listed with
   mine next to what is required**, so that **I fix everything in one go**.

---

## 4 · Agent architecture

Three different questions, each needing different evidence and each going
wrong in a different way.

| Agent | Question | Looks at | Fails by |
|---|---|---|---|
| **Extractor** | What does this document say? | page image | misreading |
| **Validator** | Is what it says acceptable? | the customer's rules | wrong rules |
| **Router** | What happens next? | the validation result | wrong policy |

**Why not one big prompt.** The strongest argument is something that actually
happened. On our poor-quality scan the extractor reported
`AMSTERDAM, NETHERLANDS` as the Port of Discharge, at 0.85 confidence, and it
passed every check we had. It was not making it up — Amsterdam really is
printed on that page, in the *Place of Delivery* box. No self-checking finds
that, because every question you can ask about the document comes back fine.
Only the customer's rule that Acme clears at Rotterdam catches it, and in one
big prompt there is nowhere for that rule to live.

The split also lets each stage be measured on its own and use the right tool.
Pushing vision, arithmetic and policy through one model means paying vision
prices to compare two strings. And rules change weekly while extraction logic
does not, so rules can be edited without retesting the model path.

**Why not five.** A preprocessing agent and a confidence agent are not agents.
They are plain functions with no judgement to make, so making them agents buys
a handover and another failure point.

| | Takes in | Gives back |
|---|---|---|
| Extractor | page images only | 8 fields: value, verdict, confidence, quoted evidence, page, flags |
| *Verification layer* | those fields + our own copy of the page text | verified fields. **Code, not a model** |
| Validator | verified fields + the rule file | per field: match / mismatch / uncertain, with found, required, severity, reason |
| Router | the validation result | a decision, the rules behind it, and a draft email |

That is roughly planner / executor / verifier, except the verifier is not an
agent. It is deterministic code wrapped around the executor.

**How they talk.** Each stage hands the next a typed Pydantic object, one
direction only, no shared editable state. The Validator cannot see the page
images; the Router cannot see inside extraction. That forces each stage to
produce output complete enough to act on alone, which is what makes the UI and
the audit trail possible.

**How state survives a crash.** Two mechanisms, because one is not enough and
we found that out by testing. LangGraph checkpoints each step when it finishes.
Separately, the vision call is remembered against the document's content and
the model name.

The second exists because the first has a gap: LangGraph only saves a step's
output when the step *finishes*, so a crash *inside* extraction re-runs the
whole step. We saw exactly that — two extraction calls, double cost, one
document. With both, a crash and restart costs **$0.01063** against
**$0.01064**. Each step also writes to SQLite before the next starts, so an
unfinished run can still be inspected.

The cache stores **what the model said, not what we concluded**. Caching the
finished result made every improvement to our checking invisible on documents
already seen, including in production.

---

## 5 · Models and tools

| Job | Model | Why |
|---|---|---|
| Extraction (vision) | `gpt-4.1` | The only place better quality means fewer human touches. 90% of spend |
| Company name comparison | `gpt-4.1-mini` | One small judgement call, ~200 tokens |
| Draft email + explanation | `gpt-4.1-mini` | Input already structured, no images |
| Plain English to SQL | `gpt-4.1-mini` | Small schema, a few examples |
| Rules, verification, decisions | **no model** | Deterministic, free, same every time |

Extraction costs $0.0098 and takes ~9 s; everything else together costs
$0.0006 and takes ~5 s. Because the split is that uneven, extraction is the
only thing worth optimising. We picked the cheapest model that was good enough
rather than the strongest available — at 100% accuracy on clean documents, a
bigger model has nothing left to win.

**When the document is poor quality**, the system escalates per field, not per
document:

```
Step 0  digital PDF with a text layer       free, exact, no model
Step 1  render at 200 DPI, read with vision the normal path, one call
Step 2  weak fields only: 300 DPI, straighten, contrast, ask again
Step 3  the two readings disagree           mark UNCERTAIN, never average
Step 4  still unreadable                    report null, send to a human
```

200 DPI is measured, not guessed. At 150 DPI the model read `INV-2026-08841`
as `INV:2026-08841`, because the hyphen did not come out clearly.

**Why LangGraph:** for durable execution, not its agent features, which we do
not use. The pipeline is a fixed sequence with no dynamic tool calling; what we
need is checkpointing, resuming, and a clean place to pause for the human
approval Part 2 requires.

**Structured output** (strict JSON schemas) for everything a machine reads. The
extraction schema names all eight fields, so the model cannot invent one or
drop one. We avoid **tool-calling loops** — the extractor does one job in one
pass and has no decisions to make. We also avoid a model wherever the answer is
fixed: the routing decision is plain code precisely because "should this customs
document be approved" must not change between runs.

---

## 6 · Trust, failure handling and testing

The confidence number an operator sees is **not** what the model claimed:

```
final = the model's own confidence
      × did we find the quoted text on the page
      × does the value pass its format rule
      × how much we trust our copy of the page
        (PDF text 1.00 · OCR 0.80 · model transcript 0.70)
```

Four mechanisms do the real work. Each was added after a failure we saw; the
details are in the technical write-up.

- **Evidence is required.** Every found field must quote text from the page,
  which we then look for ourselves in a separate copy of the page text.
  Extraction is **vision-only** — the model never sees that text — so the check
  is not the model agreeing with itself.
- **Whole-word matching for short codes.** The Incoterm "CIF" was matched
  inside the word **SPECIFICALLY** in the small print.
- **Whole-token and exact-digit matching.** `2026-08841` scored 1.00 against
  `INV-2026-08841`, because a shortened value is part of the full one.
- **Snapping to the page.** The model finds the value; the document decides the
  spelling. `Acne` becomes `Acme`. This can only move a value closer to what is
  printed, so it cannot hide a real supplier mistake.

Reporting a field as absent is a correct answer in its own right, scored
separately (2 of 2, 0 invented fields). Most Bills of Lading have no Incoterm,
and inventing one is worse than misreading a port.

**Low confidence.** Nothing below 0.85 is approved automatically, and
uncertainty carries forward: an unverified field is marked uncertain even if
the rule would have passed, though the record still shows what the rule *would*
have said. At the Router, unreadable fields outrank confirmed mistakes — a
document with both goes to a human, because writing an amendment from data we
could not read risks telling the supplier to fix the wrong thing.

**Runaway cost and loops.** `MAX_USD_PER_DOCUMENT` ($0.25) and
`MAX_LLM_CALLS_PER_RUN` (12) are checked before every call and stop the run
rather than cutting corners. Every call goes through one place in the code, so
those limits are enforceable. Retries are capped at 3, temporary errors only.
Re-reading happens once, on weak fields only.

**Offline test** (`evals/run_eval.py`) runs against a golden set where we know
every correct answer. The headline number is deliberately not accuracy. It is
**escaped errors**: values that were wrong *and* approved automatically.

| | Clean | Poor scan | All |
|---|---|---|---|
| Field accuracy | 100% | 62.5% | 87.5% |
| Correct when it approved | 100% | 100% | **100%** (16/16) |
| **Escaped errors** | **0** | **0** | **0** |
| Wrong values it caught | — | 100% (3/3) | 100% (3/3) |
| Confidence gap | — | +0.14 | **+0.42** |

Seven more offline suites make no model calls at all; each locks in a bug we
hit. The confidence gap is the difference between average confidence when the
system is right (0.86) and wrong (0.44) — without it the number is decoration.

**Honest limit:** 24 field readings across 3 documents. The direction is right
and the mechanisms are sound, but that is nowhere near enough to claim the
confidence scores are calibrated.

**Online metric — escaped error rate:** of the fields approved automatically,
what percentage a human later changes. Measurable from day one, because the UI
records every operator edit against the stored decision.

---

## 7 · Metrics and success criteria

> **North star: touchless rate** — the percentage of shipment document sets
> that reach a *correct* decision with no human editing any field.

Correctness is inside the definition, so it cannot be gamed by approving more.

| Metric | Type | Target | Why this one |
|---|---|---|---|
| Escaped error rate | quality | **0** | The only number that can hurt a customer. Guards the north star |
| Wrong values caught | quality | ≥95% | Of what we get wrong, how much we flag |
| Median time, email to decision | business | −50% | What the cargo team is paid to shorten |
| Rounds per shipment | business | 2–4 → ≤1.5 | Attacks the loop, not the typing |
| How much the draft is edited | product | ≤20% of words | Is the draft usable, or a rewrite |
| Uncertain rate per form type | health | tracked | A jump means a supplier changed their form |
| Cost per document | health | <$0.05 | Must stay well below the labour it replaces |
| 95th percentile time | health | <60s | The slow tail is what operators feel |

The draft-editing metric is the one I would fight for. Every other number can
look healthy while the cargo team quietly rewrites every email — at which point
we have automated nothing and added a step.

**Go** (all): zero escaped errors reaching the customer; touchless rate 40%+ on
clean digital documents; median time halved against the previous fortnight; 80%+
of drafts sent with ≤20% of words changed; cost under $0.05 a document; and the
operator still chooses to use it in week two.

**No-go** (any): a wrong value reaches a customer through automatic approval;
the team rewrites more than half the drafts; touchless rate under 20%; or
operators start ignoring the uncertainty flags. The last is worst, because it
creates false confidence.

---

## 8 · What I would build next

1. **Multi-document shipments and cross-document checks** (4 days). A shipment
   is a Bill of Lading *plus* an invoice *plus* a packing list, and the most
   valuable check is whether consignee and HS code agree *across* all three. No
   single document can fail that check, so a human always does it.
2. **A proper golden set: 100+ documents across several carrier forms**
   (3 days). Everything in section 6 rests on 24 readings. Without more data I
   cannot defend the 0.85 threshold, try a cheaper model, or tell a form change
   from a real regression. It unblocks the rest, so it comes first.
3. **Matching values to labels by position** (2 days). Closes the one error we
   still cannot catch: right text, wrong box. Asking the model which box it read
   from does not work — it reports the box it was *looking for*.
4. **Recording the rule version with each decision** (1 day). Rules change
   underneath stored results, and a dispute needs the rules as they were.

**Why not a nicer UI or more prompt tuning.** The UI is good enough, and Part 2
will reshape it around the real cargo workflow anyway. Prompt tuning has the
worst track record of anything I tried: every lasting improvement came from
deterministic checks *around* the model, while both prompt fixes that appeared
to work either broke at a different image resolution or were ignored. The model
is good at reading pixels. The code should decide what to believe.
