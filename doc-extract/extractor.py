"""Content-type router — the single place that decides which lane bytes take.

This encodes "the role of each": it sniffs the true type (declared content-type
+ magic bytes) and dispatches to exactly one engine. OCR is reachable only for
scanned-PDF pages and raw images — everything with a text layer goes to a direct
extractor. A missing optional dependency degrades that one type to an empty
result with a meta note; it never raises to the caller.
"""
from __future__ import annotations

import io
import logging
import zipfile
from typing import Optional

import config
from engines import ocr_engine, pdf_engine, office_engine, structured_engine, html_engine

logger = logging.getLogger("doc-extract.router")

_OFFICE_CT = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
}


def _base_ct(content_type: str) -> str:
    """Strip parameters/charset: 'application/json; charset=utf-8' → 'application/json'."""
    return (content_type or "").split(";", 1)[0].strip().lower()


def _zip_office_kind(data: bytes) -> Optional[str]:
    """Disambiguate a PK-zip office file by its internal layout."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = set(zf.namelist())
    except Exception:  # noqa: BLE001
        return None
    if "word/document.xml" in names:
        return "docx"
    if "xl/workbook.xml" in names:
        return "xlsx"
    if "ppt/presentation.xml" in names:
        return "pptx"
    return None


def sniff_kind(content_type: str, data: bytes, source_url: str = "") -> str:
    """Resolve the extraction kind from content-type + magic bytes + URL suffix."""
    ct = _base_ct(content_type)
    head = data[:16]
    url = (source_url or "").lower().split("?", 1)[0]

    # 1) Unambiguous magic bytes win (declared content-type is often wrong/generic).
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"\x89PNG") or head[:3] == b"\xff\xd8\xff" or head[:4] == b"GIF8" \
            or head[:2] in (b"BM",) or head[:4] in (b"II*\x00", b"MM\x00*") \
            or (head[:4] == b"RIFF" and data[8:12] == b"WEBP"):
        return "image"
    if head.startswith(b"PK\x03\x04"):
        # OpenXML office or a generic zip. Prefer content-type, then internals.
        if ct in _OFFICE_CT:
            return _OFFICE_CT[ct]
        z = _zip_office_kind(data)
        if z:
            return z

    # 2) Declared content-type.
    if ct == "application/pdf":
        return "pdf"
    if ct in _OFFICE_CT:
        return _OFFICE_CT[ct]
    if ct in ("application/json", "text/json") or ct.endswith("+json"):
        return "json"
    if ct in ("text/csv", "application/csv"):
        return "csv"
    if ct in ("text/tab-separated-values",):
        return "tsv"
    if ct.startswith("image/"):
        return "image"
    if ct in ("text/html", "application/xhtml+xml"):
        return "html"

    # 3) URL suffix fallback (some servers send octet-stream).
    for suffix, kind in ((".pdf", "pdf"), (".docx", "docx"), (".xlsx", "xlsx"),
                         (".pptx", "pptx"), (".json", "json"), (".csv", "csv"),
                         (".tsv", "tsv"), (".png", "image"), (".jpg", "image"),
                         (".jpeg", "image"), (".webp", "image"), (".tif", "image"),
                         (".tiff", "image"), (".gif", "image"), (".bmp", "image")):
        if url.endswith(suffix):
            return kind

    # 4) Text heuristics.
    if ct.startswith("text/"):
        stripped = data.lstrip()[:1]
        if stripped in (b"{", b"["):
            return "json"
        return "text"
    return "unknown"


def _image_extract(data: bytes, source_url: str, ocr_mode: str) -> dict:
    base = {
        "kind": "image", "content_kind": "image", "markdown": "", "text": "",
        "tables": [], "records": [], "ocr": None,
        "meta": {"source_url": source_url},
    }
    if ocr_mode == "off" or not ocr_engine.available():
        base["meta"]["ocr"] = "skipped"
        return base
    try:
        res = ocr_engine.ocr_image_bytes(data)
    except ocr_engine.OCRUnavailable:
        base["meta"]["ocr"] = "unavailable"
        return base
    text = res["text"]
    return {
        "kind": "image",
        "content_kind": "ocr" if text.strip() else "image",
        "markdown": text, "text": text, "tables": [], "records": [],
        "ocr": {"engine": res["engine"], "confidence": res["confidence"], "pages": 1},
        "meta": {"source_url": source_url},
    }


def _text_passthrough(data: bytes, source_url: str, kind: str) -> dict:
    text = data.decode("utf-8", errors="replace")
    return {
        "kind": kind, "content_kind": kind if kind != "unknown" else "text",
        "markdown": text, "text": text, "tables": [], "records": [], "ocr": None,
        "meta": {"source_url": source_url},
    }


def _empty(kind: str, source_url: str, note: str) -> dict:
    return {
        "kind": kind, "content_kind": kind, "markdown": "", "text": "",
        "tables": [], "records": [], "ocr": None,
        "meta": {"source_url": source_url, "note": note},
    }


def extract(data: bytes, content_type: str, source_url: str = "",
            ocr_mode: Optional[str] = None) -> dict:
    """Route bytes to the right engine and return a normalized result dict."""
    ocr_mode = (ocr_mode or config.OCR_MODE_DEFAULT).lower()
    if ocr_mode not in ("auto", "off", "force"):
        ocr_mode = "auto"
    kind = sniff_kind(content_type, data, source_url)

    try:
        if kind == "pdf":
            return pdf_engine.extract(data, source_url, ocr_mode)
        if kind in ("docx", "xlsx", "pptx"):
            return office_engine.extract(data, source_url, kind)
        if kind == "json":
            return structured_engine.extract_json(data, source_url)
        if kind == "csv":
            return structured_engine.extract_csv(data, source_url, ",")
        if kind == "tsv":
            return structured_engine.extract_csv(data, source_url, "\t")
        if kind == "image":
            return _image_extract(data, source_url, ocr_mode)
        if kind == "html":
            return html_engine.extract(data, source_url)
        if kind in ("text", "unknown"):
            return _text_passthrough(data, source_url, kind)
    except ImportError as e:
        logger.warning("engine dependency missing for kind=%s: %s", kind, e)
        return _empty(kind, source_url, f"dependency missing: {e}")
    except Exception as e:  # noqa: BLE001 — one bad doc must not 500 the crawl shard
        logger.warning("extraction failed for kind=%s url=%s: %s", kind, source_url, e)
        return _empty(kind, source_url, f"extraction error: {e}")

    return _empty(kind, source_url, "unhandled kind")
