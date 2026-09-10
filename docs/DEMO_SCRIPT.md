# Demo video script

**Target: 2 minutes 50 seconds.** The assignment asks for 2 to 3 minutes.

The lines you speak add up to about **430 words**. At a normal speaking pace
(150 words a minute) that is roughly 2 minutes 50 seconds. Read them out as
written. If you start improvising, you will go over 3 minutes.

Everything is written in short, simple sentences so it is easy to say out loud.

---

## Before you record

**Close the `.env` file in your editor.** Your API key must not appear on
screen. Clear your terminal too, so old commands are not visible.

```bash
python -m uvicorn app.main:app --port 8000
python run_graph.py --list        # should show exactly 3 runs
```

- Open **http://127.0.0.1:8000**
- Make the browser window wide (about 1500 pixels), zoom to 110%
- One terminal open, font size 14 to 16
- Record at 1080p, with your voice

---

## Scene 1 — What this is
**0:00 to 0:15**

**Show:** the app, freshly loaded.

> "Every shipment comes with a stack of documents. Today a person reads every
> field by hand and checks it against what that customer needs. That usually
> takes two to four email rounds. This system does that work instead."

---

## Scene 2 — Run it live
**0:15 to 0:33**

**Show:** click **Upload a document**. Pick `samples/clean/SHP-1042_BOL.pdf`.
Let the steps light up: `ingest → extract → validate → route → completed`.

> "I'll upload a Bill of Lading. You can see the steps running live. A vision
> model reads the page as an image, for about one cent. All eight fields
> checked out and every rule passed, so it approved automatically."

---

## Scene 3 — What was found, and what was needed
**0:33 to 1:07**

**Show:** click **`SHP-2287_BOL.pdf`** on the left. Point at the two middle
columns.

> "Now a document with mistakes. 'Found' is what the model read from the page.
> 'Required' comes from a rules file for this customer. That is not written on
> the document anywhere. Someone who knows this customer wrote it down."

**Stop on the Incoterms row.**

> "The consignee name is wrong. The HS code is not allowed. And the Incoterm is
> missing, which is the correct answer here. Most Bills of Lading have no
> Incoterm, so making one up would be the worst mistake. The word CIF is even
> on this page, hidden inside the word SPECIFICALLY."

---

## Scene 4 — Where the confidence number comes from
**1:07 to 1:30**

**Show:** click the **Consignee name** row so it opens up.

> "I can open any field. It shows the exact words from the page, the page
> number, and why this rule exists. That reason goes into the email, so the
> supplier knows why. The model said ninety-nine percent. But a model will say
> ninety-nine percent about something it made up. So we check it ourselves,
> against the page."

---

## Scene 5 — The decision and the email
**1:30 to 1:55**

**Show:** the decision box. Open **"Why the system decided this"**. Scroll to
the draft email. Hover over **Send to supplier**.

> "The decision is plain code. No AI. The same input always gives the same
> answer, which matters if someone asks six months later why we approved a
> shipment. The email text is written by AI. Three problems, each with what the
> document says, what is needed, and why. And the agent never sends it. This
> just records that a person did."

---

## Scene 6 — When it should not guess
**1:55 to 2:25**

**Show:** click **`SHP-2287_BOL_scan.jpg`** on the left.

> "Same shipment, as a bad phone photo. Six fields came back uncertain, and
> none were approved. Look at gross weight. The model was ninety-nine percent
> sure, and it read the number wrong. The page says twelve-nine-eighty. What it
> read is nowhere on the page, so confidence dropped to about fifteen percent."

> **Note:** the model misreads that blurry digit differently on each fresh run
> — we have seen 12,960 and 12,896. The wording above works whatever is on your
> screen. Point at the row rather than reading the wrong number aloud.

**Point at the Port of discharge row.**

> "This one is my favourite. It says Amsterdam. That is not made up. Amsterdam
> really is on this page, just in the wrong box. Checking the text can never
> catch that. Only the customer rule does. That is why there are three agents,
> not one prompt."

---

## Scene 7 — Ask questions about the data
**2:25 to 2:40**

**Show:** the Ask box at the bottom. Click the suggestion *"How many shipments
were flagged for review this week?"*. Then open **Show the query and rows**.

> "Everything is saved, and you can ask questions in normal English. It shows
> the answer, plus the query and the rows, so you can check it. The database is
> read-only, so a question can never change anything."

---

## Scene 8 — Closing
**2:40 to 2:52**

> "About one cent per document, mostly the vision model. On my test set it was
> a hundred percent accurate on clean documents, and it never once approved a
> wrong value."

*(Pause for one second, then stop recording.)*

---

## Does this cover what the assignment asks for?

| The assignment asks for | Shown in |
|---|---|
| **A** Extractor — vision model, 8 fields, confidence on each | Scenes 2, 3 |
| **B** Validator — match, mismatch, uncertain, found vs expected | Scenes 3, 6 |
| **C** Router — all three outcomes, and it explains itself | Scene 2 (approve), 5 (amend), 6 (flag) |
| **D** Storage plus questions in plain English | Scene 7 |
| **E** A screen showing a real run | The whole video |
| Stopping made-up answers | Scene 3 (missing Incoterm), Scene 6 (wrong weight) |
| Showing confidence per field | Scene 4 |
| Cost awareness | Scenes 2, 8 |
| Never approving quietly | Scene 6 |
| Why three agents and not one | Scene 6 |

---

## If you need it shorter or longer

**To make it 2 minutes:** drop Scene 7, and drop the last sentence of Scene 4.
Mention the question feature in one line in Scene 8 instead. Keep everything
else — the assignment asks for it directly.

**To make it 3 minutes:** after Scene 6, switch to the terminal and run:

```bash
python run_graph.py samples/clean/SHP-2287_BOL.pdf --crash-after extract
python run_graph.py --resume <run-id>
```

> "If it crashes half way, it starts again from the last finished step. And it
> does not pay for the vision model twice. There is no extraction call at all
> in the second run."

*(Adds about 35 words, roughly 15 seconds.)*

---

## Tips for recording

- **Don't describe your mouse.** Say what it means. Never say "now I am
  clicking here".
- **Say numbers as words.** Say "twelve-nine-eighty", not "one two comma nine
  eight zero". Long numbers are hard to follow when spoken.
- **Pause for one second** after "never once approved a wrong value".
- **If you stumble, keep going.** Cut it out later. Starting again wastes more
  time.
- **Watch it once with the sound off.** If you can still follow what is
  happening from the screen alone, your pace is good.
- **Check your time as you go.** If you are past 1 minute 10 leaving Scene 3,
  speed up Scenes 4 and 5. Do not cut Scene 6 — it is the most important part.
