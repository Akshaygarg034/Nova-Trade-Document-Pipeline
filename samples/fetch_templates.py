"""Download the blank industry B/L templates that samples are rendered onto.

These are third-party publicly-distributed blank forms, so we fetch them
rather than vendoring them into the repo. Run this once before
`build_samples.py`.
"""
from __future__ import annotations

import pathlib
import urllib.request

RAW = pathlib.Path(__file__).resolve().parent / "_raw"

TEMPLATES = {
    # Japan Shipping Exchange standard ocean B/L (fillable AcroForm, 64 fields)
    "jse_bol_sample.pdf": "https://www.jseinc.org/document/bl/shubil(a)sample.pdf",
    # DHX multimodal ocean B/L, US format (fillable AcroForm, 117 fields)
    "dhx_multimodal.pdf": "https://www.dhx.com/forms/DHX-Multimodal-Bill-of-Lading.pdf",
}


def main() -> None:
    RAW.mkdir(parents=True, exist_ok=True)
    for name, url in TEMPLATES.items():
        dest = RAW / name
        if dest.exists():
            print(f"  {name:24s} already present ({dest.stat().st_size // 1024} KB)")
            continue
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            dest.write_bytes(r.read())
        print(f"  {name:24s} downloaded ({dest.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
