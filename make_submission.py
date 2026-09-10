"""Build the submission folder: print-ready HTML for the docs that must be PDFs.

  python make_submission.py

Writes ./submission/ containing:
    PRD.html                  open in a browser, Ctrl+P, Save as PDF
    TECHNICAL_WRITEUP.html    same
    SAMPLE_QUERIES.html       same (optional extra for the reviewer)
    CHECKLIST.md              what to attach where

The assignment wants the PRD and the technical write-up as PDF or Google Doc.
Markdown pasted into Google Docs loses its tables, so print-to-PDF from a
browser is the reliable route: tables, code blocks and page breaks all survive.

`submission/` is gitignored -- it is derived output, not source.
"""
from __future__ import annotations

import pathlib
import sys

try:
    import markdown
except ImportError:
    sys.exit("pip install markdown")

ROOT = pathlib.Path(__file__).resolve().parent
OUT = ROOT / "submission"

DOCS = [
    ("docs/PRD.md", "PRD.html", "PRD — Multi-Agent Trade Document Pipeline"),
    ("docs/TECHNICAL_WRITEUP.md", "TECHNICAL_WRITEUP.html",
     "Technical Write-up — Nova Trade Document Pipeline"),
    ("docs/SAMPLE_QUERIES.md", "SAMPLE_QUERIES.html",
     "Sample queries against stored output"),
]

CSS = """
@page { size: A4; margin: 13mm 13mm; }
* { box-sizing: border-box; }
body {
  font: 9.6pt/1.32 -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  color: #1a1a1a; margin: 0; padding: 0;
}
h1 { font-size: 17pt; margin: 0 0 3pt; letter-spacing: -0.2pt; }
h2 { font-size: 11.8pt; margin: 9pt 0 3pt; padding-bottom: 2pt;
     border-bottom: 1px solid #d8dde3; }
h3 { font-size: 10.3pt; margin: 7pt 0 2pt; }
h4 { font-size: 10pt; margin: 7pt 0 2pt; color: #444; }
p, li { margin: 0 0 3pt; }
ul, ol { margin: 0 0 5pt; padding-left: 16pt; }
li { margin-bottom: 2pt; }
strong { font-weight: 650; }
a { color: #1f5fa8; text-decoration: none; }
hr { border: 0; border-top: 1px solid #e3e7ec; margin: 7pt 0; }

code {
  font: 8.6pt ui-monospace, "SF Mono", Consolas, monospace;
  background: #f4f6f8; padding: 0 2pt; border-radius: 2pt;
}
pre {
  background: #f7f9fb; border: 1px solid #e3e7ec; border-radius: 3pt;
  padding: 5pt 7pt; margin: 5pt 0; overflow-x: auto;
  page-break-inside: avoid;
}
pre code { background: none; padding: 0; font-size: 7.5pt; line-height: 1.3; }

table {
  border-collapse: collapse; width: 100%; margin: 5pt 0 8pt;
  font-size: 8.4pt; page-break-inside: avoid;
}
th, td { border: 1px solid #dde2e8; padding: 2pt 3.5pt; text-align: left;
         vertical-align: top; }
th { background: #f4f6f8; font-weight: 650; }

blockquote {
  margin: 5pt 0; padding: 4pt 8pt; border-left: 2.5pt solid #b9c4d0;
  background: #f8fafc; color: #333;
}
blockquote p:last-child { margin-bottom: 0; }

h1, h2, h3 { page-break-after: avoid; }
"""

TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>{title}</title><style>{css}</style></head>
<body>{body}</body></html>
"""


def build() -> None:
    OUT.mkdir(exist_ok=True)
    md = markdown.Markdown(extensions=["tables", "fenced_code", "sane_lists", "toc"])

    for src, dest, title in DOCS:
        path = ROOT / src
        if not path.exists():
            print(f"  SKIP {src} (missing)")
            continue
        md.reset()
        html = md.convert(path.read_text(encoding="utf-8"))
        (OUT / dest).write_text(
            TEMPLATE.format(title=title, css=CSS, body=html), encoding="utf-8"
        )
        words = len(path.read_text(encoding="utf-8").split())
        print(f"  {dest:26s} from {src:32s} ({words} words)")

    (OUT / "CHECKLIST.md").write_text(CHECKLIST, encoding="utf-8")
    print(f"  {'CHECKLIST.md':26s} what to attach where")
    print(f"\nOpen the .html files in Chrome, then Ctrl+P -> 'Save as PDF'.")
    print(f"Folder: {OUT}")


CHECKLIST = """# Submission checklist

## 1. Export two documents to PDF

Open each in Chrome, press **Ctrl+P**, choose **Save as PDF**:

- `submission/PRD.html`               -> `PRD.pdf`
- `submission/TECHNICAL_WRITEUP.html` -> `Technical-Writeup.pdf`

In the print dialog: A4, Default margins, **Background graphics ON** (so table
headers and code blocks keep their shading). Turn headers/footers off.

`SAMPLE_QUERIES.html` is optional — the queries also live in the repo as
markdown, which is enough.

## 2. Zip the code

From the folder **above** `nova-trade-docs`:

```bash
git -C nova-trade-docs archive --format=zip -o nova-trade-docs.zip HEAD
```

`git archive` only includes tracked files, so it automatically leaves out
`.env`, `.venv/`, `node_modules/`, `data/` and the caches. **Never zip the
folder manually** — you would ship your API key.

If you push to GitHub instead, make it private or public as you prefer, and
confirm `.env` is absent before pushing:

```bash
git -C nova-trade-docs ls-files | grep -c "^\\.env$"    # must print 0
```

## 3. What to send

| Item the brief asks for | What you send |
|---|---|
| A single repo or zipped project, runnable | `nova-trade-docs.zip` or a repo link |
| README with setup and run instructions | inside the repo (`README.md`) |
| PRD as PDF or Google Doc | `PRD.pdf` |
| Technical write-up as PDF or Google Doc | `Technical-Writeup.pdf` |
| At least 2 sample docs, one clean one messy | inside the repo (`samples/`) — 3 included |
| 2–3 minute demo video | your recording (upload, share the link) |
| Sample queries run against stored output | inside the repo (`docs/SAMPLE_QUERIES.md`) |

## 4. Before you hit send

- [ ] `.env` is NOT in the zip
- [ ] `git status` is clean
- [ ] Demo video plays, audio is audible, no API key visible on screen
- [ ] PRD Section 1 reads in your own voice
- [ ] Video link is set to "anyone with the link can view"
"""


if __name__ == "__main__":
    build()
