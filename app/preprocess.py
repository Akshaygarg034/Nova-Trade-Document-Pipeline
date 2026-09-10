"""Turn any PDF or image into rendered pages plus an independent text corpus.

The text corpus is the load-bearing part. It is what `grounding.py` checks the
model's quoted evidence against, so it must come from somewhere other than the
extraction call itself. Preference order:

  1. pdf_text  - the PDF's own text layer. Free, exact, fully independent.
  2. ocr       - RapidOCR, if installed. Independent of the LLM.
  3. (later)   - LLM verbatim transcription, requested by the extractor as a
                 last resort. Weaker, because it shares a model with the thing
                 being checked -- so we record text_source and discount it.
"""
from __future__ import annotations

import hashlib
import pathlib
from typing import Optional

import cv2
import numpy as np
import pymupdf

from app.config import settings
from app.schemas import DocumentBundle, PageAsset

RENDER_DPI = 200  # 150 lost thin hyphens in reference numbers; 200 reads them
HIGH_DPI = 300
MIN_TEXT_CHARS = 120  # below this a "text layer" is just form labels, not data

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}


def doc_id_for(path: pathlib.Path) -> str:
    """Content-addressed id: same bytes -> same id, so runs are cacheable."""
    h = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    return f"{path.stem}-{h}"


# --------------------------------------------------------------------- OCR
_ocr_engine = None
_ocr_tried = False


def _ocr(image_path: str) -> Optional[str]:
    """RapidOCR if available; None otherwise. Optional dependency by design.

    Cached to disk: OCR is the slowest hop in the pipeline (~30s on a full
    scanned page), so we never pay for the same page twice.
    """
    global _ocr_engine, _ocr_tried

    if not settings.use_ocr:
        return None

    cache = pathlib.Path(image_path).with_suffix(".ocr.txt")
    if cache.exists():
        return cache.read_text(encoding="utf-8") or None
    if not _ocr_tried:
        _ocr_tried = True
        try:
            from rapidocr_onnxruntime import RapidOCR

            _ocr_engine = RapidOCR()
        except Exception:
            _ocr_engine = None
    if _ocr_engine is None:
        return None
    try:
        result, _ = _ocr_engine(image_path)
        if not result:
            return None
        text = "\n".join(line[1] for line in result)
        cache.write_text(text, encoding="utf-8")
        return text
    except Exception:
        return None


# ----------------------------------------------------------------- imaging
def enhance_image(src: str, dst: str) -> str:
    """Deskew + flatten lighting + sharpen. Used on the Tier-2 retry only."""
    img = cv2.imread(src, cv2.IMREAD_GRAYSCALE)
    if img is None:
        return src

    # Estimate skew from the dominant orientation of dark (ink) pixels.
    inv = cv2.bitwise_not(img)
    thr = cv2.threshold(inv, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)[1]
    coords = np.column_stack(np.where(thr > 0))
    if len(coords) > 500:
        angle = cv2.minAreaRect(coords.astype(np.float32))[-1]
        if angle < -45:
            angle = 90 + angle
        if abs(angle) < 15:  # ignore nonsense estimates
            h, w = img.shape
            m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
            img = cv2.warpAffine(img, m, (w, h), flags=cv2.INTER_CUBIC, borderValue=255)

    # Even out scan lighting, then boost local contrast.
    bg = cv2.medianBlur(img, 31)
    img = cv2.divide(img, bg, scale=255)
    img = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(img)
    img = cv2.filter2D(img, -1, np.array([[0, -0.4, 0], [-0.4, 2.6, -0.4], [0, -0.4, 0]]))

    cv2.imwrite(dst, img)
    return dst


def render_pdf_page(pdf_path: str, page_index: int, dpi: int, out_path: str) -> tuple[int, int]:
    doc = pymupdf.open(pdf_path)
    pix = doc[page_index].get_pixmap(dpi=dpi)
    pix.save(out_path)
    doc.close()
    return pix.width, pix.height


# ------------------------------------------------------------------ loader
def load_document(path: str | pathlib.Path, dpi: int = RENDER_DPI) -> DocumentBundle:
    p = pathlib.Path(path)
    if not p.exists():
        raise FileNotFoundError(p)

    did = doc_id_for(p)
    out_dir = settings.render_dir / did
    out_dir.mkdir(parents=True, exist_ok=True)

    if p.suffix.lower() in IMAGE_SUFFIXES:
        return _load_image(p, did, out_dir)
    if p.suffix.lower() == ".pdf":
        return _load_pdf(p, did, out_dir, dpi)
    raise ValueError(f"unsupported file type: {p.suffix}")


def _load_image(p: pathlib.Path, did: str, out_dir: pathlib.Path) -> DocumentBundle:
    img = cv2.imread(str(p))
    if img is None:
        raise ValueError(f"could not decode image: {p}")
    dst = out_dir / "page-1.png"
    cv2.imwrite(str(dst), img)

    text = _ocr(str(dst))
    page = PageAsset(
        index=1,
        image_path=str(dst),
        width=img.shape[1],
        height=img.shape[0],
        text=text or "",
        text_source="ocr" if text else "none",
    )
    return DocumentBundle(
        doc_id=did, source_path=str(p), filename=p.name, is_native_pdf=False, pages=[page]
    )


def _load_pdf(p: pathlib.Path, did: str, out_dir: pathlib.Path, dpi: int) -> DocumentBundle:
    doc = pymupdf.open(p)
    pages: list[PageAsset] = []
    for i, page in enumerate(doc):
        dst = out_dir / f"page-{i + 1}@{dpi}.png"  # dpi in the name: OCR cache cannot go stale
        pix = page.get_pixmap(dpi=dpi)
        pix.save(dst)

        text = page.get_text().strip()
        source = "pdf_text"
        if len(text) < MIN_TEXT_CHARS:  # scanned PDF masquerading as digital
            ocr_text = _ocr(str(dst))
            text, source = (ocr_text, "ocr") if ocr_text else ("", "none")

        pages.append(
            PageAsset(
                index=i + 1,
                image_path=str(dst),
                width=pix.width,
                height=pix.height,
                text=text,
                text_source=source,
            )
        )
    doc.close()
    return DocumentBundle(
        doc_id=did, source_path=str(p), filename=p.name, is_native_pdf=True, pages=pages
    )
