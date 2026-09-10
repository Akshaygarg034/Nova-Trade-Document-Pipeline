"""Build demo + eval documents from REAL industry Bill of Lading templates.

Why this approach: every publicly downloadable trade document is a blank
template (filled ones contain live commercial data, so nobody publishes them).
Two of the templates we found are genuine fillable AcroForm PDFs, so we fill
the real form fields and flatten. Result: authentic industry layouts we did
not design -- dense legal boilerplate, cramped boxes, non-obvious field
placement -- with ground truth we control for the eval harness.

  clean/  digital PDFs with a real text layer  -> exercises the Tier-0 path
  messy/  degraded raster, no text layer       -> exercises the OCR/vision path

Run:  python samples/build_samples.py
"""
from __future__ import annotations

import json
import pathlib
import sys

import cv2
import numpy as np
import pymupdf

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from fixtures import SHIPMENTS  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent
RAW, CLEAN, MESSY = ROOT / "_raw", ROOT / "clean", ROOT / "messy"

# Fixture key -> AcroForm field name, per template.
FIELD_MAP = {
    "jse": {
        "bol_no": "B/L No",
        "shipper": "Shipper",
        "consignee": "Consignee",
        "notify": "Notify Party",
        "vessel": "Ocean Vessel",
        "voyage": "Voy. No",
        "pol": "Port of Loading",
        "pod": "Port of Discharge",
        "final_dest": "Final destination",
        "containers": "Marks / Numbers",
        "packages": "No. of P'kgs or Units",
        "description": "Kind of Packages or Units; Description of Goods",
        "gross_weight_display": "Gross Weight",
        "measurement": "Measurement",
        "freight": "FREIGHT & CHARGES",
        "place_date": "Place & Date of issue",
        "originals": "Number of Original B(s)/L",
        "local_vessel": "Local Vessel",
        "from_place": "From",
        "transhipment": "For transhipment to",
        "total_words": "TOTAL NUMBER OF PACKAGES OR UNITS (IN WORDS)",
        "declared_value": "Declared value USD",
        "revenue_tons": "Revenue Tons",
        "rate": "Rate",
        "per": "Per",
        "collect": "Collect",
        "ex_rate": "Ex. Rate",
        "prepaid_at": "Prepaid at",
        "total_prepaid": "Total Prepaid in Yen",
        "payable_at": "Payable at",
        "for_master": "For the Master",
    },
    "dhx": {
        "bol_no": "BILL OF LADING NO  PO NO",
        "shipper": "SHIPPER EXPORTER",
        "consignee": "CONSIGNEE",
        "notify": "NOTIFY PARTY",
        "vessel": "EXPORT CARRIER VESSEL VOY FLAG",
        "pol": "PORT OF LOADING",
        "pod": "PORT OF DISCHARGE",
        "final_dest": "PLACE OF DELIVERY BY ON CARRIER",
        "containers": "MARKS NOS CONTAINER NOS",
        "packages": "NO OF PKGS",
        "description": "DESCRIPTION OF PACKAGE AND GOODS",
        "gross_weight_display": "GROSS WEIGHT",
        "measurement": "MEASUREMENTS",
        "export_ref": "EXPORT REFERENCES",
        "place_date": "DATE SHIPPED",
        "originals": "NUMBER OF ORIGINALS",
        "booking_no": "BOOKING NO",
        "quote_no": "QUOTE NO",
        "forwarding_agent": "FORWARDING AGENT FMC NO",
        "origin_point": "POINT AND COUNTRY OF ORIGIN",
        "delivery_to": "FOR  DELIVERY TO",
        "shipper_phone": "SHIPPER PHONE NO",
        "consignee_phone": "CONSIGNEE PHONE NO",
        "notify_phone": "NOTIFY PARTY PHONE NO",
        "delivery_phone": "DELIVERY PHONE NO",
        "pre_carriage": "PRE-CARRIAGE BY",
        "place_receipt": "PLACE OF RECEIPT BY PRE-CARRIER",
        "loading_pier": "LOADING PIER TERMINAL",
        "freight_class": "FREIGHT CLASS",
        "hazmat": " H M",
        "declared_value": "DECLARED VALUE US$",
        "total_collect": "TOTAL_2",
        "issued_at": "ISSUED AT",
        "issue_date": "DATE",
        "issued_by": "BY",
        "initials": "INITIALS",
    },
}

# Templates whose named field is a single narrow row; long values must spill
# into the form's numbered continuation rows ("FOO", "FOO 1", "FOO 2", ...).
MULTIROW = {
    "dhx": {
        "description": "DESCRIPTION OF PACKAGE AND GOODS",
        "charges": "CHARGES",
        "basis": "BASIS",
        "rate": "RATE",
        "collect": "COLLECT",
    }
}

TEMPLATE_FILE = {"jse": "jse_bol_sample.pdf", "dhx": "dhx_multimodal.pdf"}
# Keep only the face of the B/L; trailing pages are boilerplate terms.
KEEP_PAGES = {"jse": [0], "dhx": [0]}


def fill(shipment_id: str, spec: dict) -> pathlib.Path:
    tpl = spec["template"]
    doc = pymupdf.open(RAW / TEMPLATE_FILE[tpl])
    doc.select(KEEP_PAGES[tpl])

    mapping = {v: k for k, v in FIELD_MAP[tpl].items()}

    # Expand any multi-line value destined for a single-row field into the
    # form's continuation rows, keyed by their real widget names.
    spill: dict[str, str] = {}
    for key, base in MULTIROW.get(tpl, {}).items():
        lines = [ln for ln in str(spec.get(key, "")).splitlines() if ln.strip()]
        for i, line in enumerate(lines):
            spill[base if i == 0 else f"{base} {i}"] = line
        if lines:
            mapping.pop(base, None)  # handled by spill, not the plain mapping

    filled = 0
    for page in doc:
        for w in page.widgets():
            if w.field_name in spill:
                value = spill[w.field_name]
            else:
                key = mapping.get(w.field_name)
                if key is None or spec.get(key) is None:
                    continue
                value = str(spec[key])
            w.field_value = value
            w.text_fontsize = 7
            w.update()
            filled += 1

    # The JSE template ships a 120pt "SAMPLE" watermark across the cargo box.
    # Remove text only -- line art stays, so the form rules survive intact.
    for page in doc:
        for rect in page.search_for("SAMPLE"):
            page.add_redact_annot(rect)
        if page.first_annot:
            page.apply_redactions(
                images=pymupdf.PDF_REDACT_IMAGE_NONE,
                graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                text=pymupdf.PDF_REDACT_TEXT_REMOVE,
            )

    doc.bake()  # widgets -> real page content, so the text layer is genuine
    out = CLEAN / f"{shipment_id}_BOL.pdf"
    doc.save(out, garbage=3, deflate=True)
    doc.close()
    print(f"  {out.name:26s} template={tpl:4s} fields_filled={filled}")
    return out


def degrade(src: pathlib.Path, shipment_id: str) -> pathlib.Path:
    """Simulate a phone photo of a faxed copy: low DPI, skew, noise, JPEG rot."""
    doc = pymupdf.open(src)
    pix = doc[0].get_pixmap(dpi=110)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.h, pix.w, pix.n)
    img = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    doc.close()

    h, w = img.shape
    m = cv2.getRotationMatrix2D((w / 2, h / 2), -2.4, 1.0)
    img = cv2.warpAffine(img, m, (w, h), borderValue=245)

    img = cv2.GaussianBlur(img, (3, 3), 0)
    img = np.clip(img.astype(np.int16) + np.random.default_rng(7).normal(0, 11, img.shape), 0, 255)
    img = img.astype(np.uint8)
    # uneven scan lighting: darken one edge
    grad = np.linspace(1.0, 0.78, w, dtype=np.float32)[None, :]
    img = np.clip(img.astype(np.float32) * grad, 0, 255).astype(np.uint8)
    img = img[: int(h * 0.985), :]  # clip the bottom edge, as a bad scan does

    out = MESSY / f"{shipment_id}_BOL_scan.jpg"
    cv2.imwrite(str(out), img, [int(cv2.IMWRITE_JPEG_QUALITY), 38])
    print(f"  {out.name:26s} {img.shape[1]}x{img.shape[0]}px  {out.stat().st_size // 1024} KB")
    return out


def main() -> None:
    for d in (CLEAN, MESSY, ROOT.parent / "evals" / "golden"):
        d.mkdir(parents=True, exist_ok=True)

    print("clean documents (real templates, flattened AcroForms):")
    built = {sid: fill(sid, spec) for sid, spec in SHIPMENTS.items()}

    print("\nmessy document (degraded raster of SHP-2287):")
    degrade(built["SHP-2287"], "SHP-2287")

    labels = {
        sid: {"doc_type": s["doc_type"], "template": s["template"], "fields": s["truth"]}
        for sid, s in SHIPMENTS.items()
    }
    labels["SHP-2287-scan"] = {
        "doc_type": "BILL_OF_LADING",
        "template": "dhx-degraded",
        "fields": SHIPMENTS["SHP-2287"]["truth"],
    }
    lp = ROOT.parent / "evals" / "golden" / "labels.json"
    lp.write_text(json.dumps(labels, indent=2), encoding="utf-8")
    print(f"\nground truth -> {lp.relative_to(ROOT.parent)}  ({len(labels)} docs)")


if __name__ == "__main__":
    main()
