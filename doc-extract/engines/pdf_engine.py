"""PDF extraction — text layer first, OCR only for pages without one.

This is the crux of "the role of each": a born-digital PDF has an exact text
layer, so we read it directly at zero cost and perfect fidelity. OCR is invoked
ONLY for pages that carry no recoverable text (true scans), and only when
ocr_mode allows it.

Markdown is produced in two tiers:
  1. :mod:`pdf_structure` — the document's DECLARED logical structure (tagged
     PDFs). Exact semantics, no inference. Roughly 4 documents in 5 are tagged.
  2. :mod:`pdf_infer` — typography-based inference, for the rest.

LICENSING: deliberately free of PyMuPDF/pymupdf4llm, which are AGPL-3.0 (dual
licensed by Artifex). AGPL's section 13 obliges a network service to offer the
corresponding source of the combined work, which is a live constraint for a
hosted deployment. The stack here is permissive throughout — pypdfium2
(BSD-3-Clause/Apache-2.0, Google's PDFium), pdfplumber (MIT) over pdfminer.six
(MIT), pypdf (BSD-3-Clause) — and was benchmarked at parity on text extraction,
table extraction and the rasterize-to-OCR path that scanned documents depend on.
"""
from __future__ import annotations

import io
import logging
from typing import Any, Dict, List, Optional

import config
from engines import html_engine, ocr_engine, pdf_infer, pdf_structure

logger = logging.getLogger("doc-extract.pdf")


def _looks_like_pdf(data: bytes) -> bool:
    """A real PDF carries the ``%PDF-`` signature in its opening bytes.

    Spec-tolerant readers scan the first ~1 KiB rather than byte 0, because some
    servers prepend a BOM or a stray newline — so we look across the same window
    instead of requiring a bare prefix match.
    """
    return b"%PDF-" in data[:1024]


def _non_pdf_kind(data: bytes) -> Optional[str]:
    """Classify a ``.pdf``/``application/pdf`` response that is NOT actually a PDF.

    A URL ending in ``.pdf`` (or a server that hard-declares ``application/pdf``)
    routes here even when the body is really an HTML soft-404 / login wall / WAF
    interstitial, or a plaintext error string. Rather than let the parser throw
    and collapse the page to an empty result, we sniff the true shape so the
    caller can still use the content.

    Returns ``"html"`` or ``"text"`` when the bytes are clearly not a PDF, or
    ``None`` when they look like a PDF (or like opaque binary — leave those to
    the real parser, which fails loudly rather than silently mislabelling).
    """
    if not data or _looks_like_pdf(data):
        return None
    sample = data[:4096].lstrip(b"\xef\xbb\xbf\xff\xfe\xfe\xff\x00\r\n\t ")
    lowered = sample[:512].lower()
    if lowered.startswith(
        (b"<!doctype", b"<html", b"<?xml", b"<head", b"<body", b"<!--")
    ) or b"<html" in lowered or b"<body" in lowered:
        return "html"
    if _is_probably_text(sample):
        return "text"
    return None


def _is_probably_text(sample: bytes) -> bool:
    """Heuristic: does ``sample`` decode as (mostly) printable UTF-8 text?"""
    if not sample or b"\x00" in sample:  # a NUL byte means binary, not text
        return False
    try:
        text = sample.decode("utf-8")
    except UnicodeDecodeError:
        return False
    if not text:
        return False
    printable = sum(1 for ch in text if ch.isprintable() or ch in "\r\n\t")
    return printable / len(text) >= 0.85


def _as_text_result(data: bytes, source_url: str, kind: str) -> dict:
    """Shape a mislabeled HTML/text body into the standard result dict."""
    note = f"content-type mismatch: .pdf URL/declaration returned {kind}"
    if kind == "html":
        result = html_engine.extract(data, source_url, note=note, declared="pdf")
        result["meta"]["declared_pdf"] = True
        return result
    text = data.decode("utf-8", errors="replace")
    return {
        "kind": kind,
        "content_kind": kind,
        "markdown": text,
        "text": text,
        "tables": [],
        "records": [],
        "ocr": None,
        "meta": {"source_url": source_url, "declared_pdf": True, "note": note},
    }


def _render_png(data: bytes, index: int, dpi: int) -> Optional[bytes]:
    """Rasterize one page to PNG for OCR, via PDFium.

    Returns None rather than raising: a page that will not render must cost the
    document nothing more than that page's OCR.
    """
    try:
        import pypdfium2 as pdfium
        from PIL import Image  # noqa: F401 — bitmap.to_pil() needs Pillow
    except Exception as e:  # noqa: BLE001
        logger.warning("rasterizer unavailable: %s", e)
        return None
    doc = None
    try:
        doc = pdfium.PdfDocument(data)
        # PDFium renders at a scale factor relative to 72dpi user space.
        bitmap = doc[index].render(scale=dpi / 72.0)
        buf = io.BytesIO()
        bitmap.to_pil().save(buf, format="PNG")
        return buf.getvalue()
    except Exception as e:  # noqa: BLE001
        logger.warning("rasterize failed on page %d: %s", index, e)
        return None
    finally:
        if doc is not None:
            try:
                doc.close()
            except Exception:  # noqa: BLE001
                pass


def _markdown(data: bytes, pages, page_tables: List[List[list]]) -> str:
    """Structure tree when the document declares one, inference otherwise."""
    try:
        md = pdf_structure.to_markdown(data, pages)
        if md.strip():
            return md
        logger.debug("structure tree produced no content; inferring instead")
    except pdf_structure.StructureUnusable as e:
        logger.debug("structure tree unusable (%s); inferring instead", e)
    except Exception as e:  # noqa: BLE001 — inference must still get its turn
        logger.warning("structure tree walk failed: %s; inferring instead", e)
    try:
        return pdf_infer.to_markdown(pages, page_tables)
    except Exception as e:  # noqa: BLE001
        logger.warning("markdown inference failed: %s", e)
        return ""


def extract(data: bytes, source_url: str, ocr_mode: str) -> dict:
    """PDF bytes → the normalized result dict."""
    # A .pdf URL does not guarantee a PDF body: soft-404s, login walls and WAF
    # interstitials commonly answer with HTML/text. Detect that here and hand
    # back the real content instead of letting the parser blank the page.
    mislabeled = _non_pdf_kind(data)
    if mislabeled is not None:
        logger.info("PDF lane received %s for %s — returning as-is", mislabeled, source_url)
        return _as_text_result(data, source_url, mislabeled)

    import pdfplumber

    page_texts: List[str] = []
    tables: List[list] = []
    page_tables: List[List[list]] = []
    markdown = ""

    with pdfplumber.open(io.BytesIO(data)) as pdf:
        pages = list(pdf.pages)
        page_count = len(pages)

        for page in pages:
            try:
                # Size-scaled word tolerance: pdfplumber's absolute 3pt default
                # runs tightly-kerned text together (see config).
                page_texts.append((page.extract_text(
                    x_tolerance_ratio=config.PDF_WORD_X_TOLERANCE_RATIO,
                ) or "").strip())
            except TypeError:  # older pdfplumber without the ratio kwarg
                page_texts.append((page.extract_text() or "").strip())
            except Exception as e:  # noqa: BLE001 — one bad page, not one bad doc
                logger.warning("text extraction failed on a page: %s", e)
                page_texts.append("")
            found, _ = pdf_infer.tables_for_page(page)
            page_tables.append(found)
            tables.extend(found)

        total_chars = sum(len(t) for t in page_texts)
        has_text_layer = total_chars >= config.PDF_MIN_TEXT_CHARS

        if has_text_layer:
            markdown = _markdown(data, pages, page_tables)

    # --- Decide which pages to OCR ------------------------------------------
    ocr_pages: List[int] = []
    if ocr_mode != "off" and ocr_engine.available():
        if ocr_mode == "force":
            ocr_pages = list(range(page_count))
        else:  # auto — only pages lacking a usable text layer
            ocr_pages = [i for i, t in enumerate(page_texts)
                         if len(t) < config.PDF_MIN_TEXT_CHARS]

    truncated = False
    if len(ocr_pages) > config.OCR_MAX_PAGES:
        ocr_pages = ocr_pages[: config.OCR_MAX_PAGES]
        truncated = True

    ocr_texts: Dict[int, str] = {}
    ocr_scores: List[float] = []
    for i in ocr_pages:
        png = _render_png(data, i, config.OCR_RASTER_DPI)
        if not png:
            continue
        try:
            res = ocr_engine.ocr_image_bytes(png)
        except ocr_engine.OCRUnavailable:
            break
        except Exception as e:  # noqa: BLE001 — never let one page kill the doc
            logger.warning("OCR failed on page %d: %s", i, e)
            continue
        if res["text"].strip():
            ocr_texts[i] = res["text"]
            if res["confidence"]:
                ocr_scores.append(res["confidence"])

    # --- Assemble ------------------------------------------------------------
    if not markdown:
        markdown = "\n\n".join(t for t in page_texts if t)

    if ocr_texts:
        extra = "\n\n".join(f"<!-- page {i + 1} (ocr) -->\n{ocr_texts[i]}"
                            for i in sorted(ocr_texts))
        if has_text_layer:
            markdown = (markdown + "\n\n" + extra).strip() if markdown else extra
        else:
            markdown = "\n\n".join(ocr_texts[i] for i in sorted(ocr_texts))

    text_parts: List[str] = []
    for i, t in enumerate(page_texts):
        if t:
            text_parts.append(t)
        elif i in ocr_texts:
            text_parts.append(ocr_texts[i])
    text = "\n\n".join(text_parts).strip()

    records: List[Dict[str, Any]] = [
        {"page": i + 1, "text": (page_texts[i] or ocr_texts.get(i, "")),
         "ocr": i in ocr_texts}
        for i in range(page_count)
    ]

    # content_kind: a doc with a real text layer is "pdf"; a pure scan we had to
    # OCR is "ocr". This drives per-page billing weight downstream.
    content_kind = "pdf" if has_text_layer else ("ocr" if ocr_texts else "pdf")

    ocr_block = None
    if ocr_texts:
        ocr_block = {
            "engine": "rapidocr",
            "confidence": round(sum(ocr_scores) / len(ocr_scores), 4) if ocr_scores else 0.0,
            "pages": len(ocr_texts),
        }

    return {
        "kind": "pdf",
        "content_kind": content_kind,
        "markdown": markdown,
        "text": text,
        "tables": tables,
        "records": records,
        "ocr": ocr_block,
        "meta": {
            "source_url": source_url,
            "pages": page_count,
            "text_layer": has_text_layer,
            "truncated": truncated,
        },
    }
