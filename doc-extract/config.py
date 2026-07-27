"""doc-extract configuration.

A PRIVATE SERVICE. Only your `writ-agent` fleet talks to it, authenticated via
DOC_EXTRACT_SECRET. It never fetches URLs of its own — callers POST bytes they
already hold — so it has no SSRF surface.
"""
from __future__ import annotations

import os


def is_production() -> bool:
    return os.getenv("ENVIRONMENT", "production").lower() == "production"


# --- Auth --------------------------------------------------------------------
# Shared service-to-service secret. Every /extract call must present it in the
# X-Doc-Secret header (timing-safe compared in main.py). Empty in dev is allowed
# (the agents also run without a secret locally); production must set a strong
# value or the service refuses to boot (assert_production_secrets below).
DOC_EXTRACT_SECRET = os.getenv("DOC_EXTRACT_SECRET", "")

# --- Limits ------------------------------------------------------------------
# Hard ceiling on a single extraction body. A crawl page's PDF/doc is normally a
# few MB; this bounds memory + OCR cost against a hostile 500MB "document". The
# agent side also caps before forwarding, so this is defense in depth.
MAX_BYTES = int(os.getenv("DOC_EXTRACT_MAX_BYTES", str(32 * 1024 * 1024)))  # 32 MiB

# Per-request wall-clock budget for extraction (esp. OCR over many pages). The
# agent's own client timeout should be >= this.
EXTRACT_TIMEOUT_S = float(os.getenv("DOC_EXTRACT_TIMEOUT_S", "60"))

# --- OCR ---------------------------------------------------------------------
# Default OCR behaviour when the caller does not pin X-OCR-Mode:
#   auto  — OCR only pages/files with no recoverable text layer (the correct
#           default: direct extraction is exact + free when a text layer exists).
#   off   — never OCR (drop scanned/image content to empty text).
#   force — OCR every page even when a text layer exists (rarely wanted).
OCR_MODE_DEFAULT = os.getenv("DOC_EXTRACT_OCR_MODE", "auto")

# RapidOCR language set (PP-OCRv5 models). Comma-separated; empty = engine default.
OCR_LANGS = [s.strip() for s in os.getenv("DOC_EXTRACT_OCR_LANGS", "").split(",") if s.strip()]

# DPI used to rasterize a scanned PDF page before OCR. Higher = better OCR, more
# CPU/RAM. 200 is a good text-legibility/size tradeoff for typical documents.
OCR_RASTER_DPI = int(os.getenv("DOC_EXTRACT_OCR_DPI", "200"))

# A PDF page with fewer than this many extracted characters is treated as having
# no usable text layer → eligible for OCR (matches the agents' thin-page gate).
PDF_MIN_TEXT_CHARS = int(os.getenv("DOC_EXTRACT_PDF_MIN_TEXT_CHARS", "16"))

# Word-splitting tolerance as a FRACTION OF FONT SIZE. PDFs carry no word
# delimiters — a space is inferred from the horizontal gap between glyphs — so
# the threshold has to scale with type size, not be an absolute point value.
# pdfplumber's absolute 3pt default is too wide for tightly-kerned text: on the
# arXiv corpus document it ran whole sentences together
# ("Providedproperattributionisprovided"), scoring 24% word-level agreement with
# the reference extractor. At 0.15 that document scores 93% and no other
# document in the corpus moves by more than 0.2%.
PDF_WORD_X_TOLERANCE_RATIO = float(os.getenv("DOC_EXTRACT_PDF_WORD_TOLERANCE", "0.15"))

# Safety cap on how many PDF pages we will OCR in one request (rasterizing +
# OCR is the expensive path). Beyond this the remaining pages are skipped and
# meta.truncated is set, so a hostile 5000-page scan can't pin a worker.
OCR_MAX_PAGES = int(os.getenv("DOC_EXTRACT_OCR_MAX_PAGES", "100"))

# --- Engine tier -------------------------------------------------------------
# light — pdfplumber/pypdfium2 + office parsers + RapidOCR (ONNX, CPU, offline,
#         air-gap safe). The default; ships everywhere.
# rich  — additionally try `docling` (DocLayNet layout + TableFormer tables) for
#         complex-table PDFs; pulls torch, so opt-in via env. Falls back to light
#         if docling is not installed.
ENGINE_TIER = os.getenv("DOC_EXTRACT_TIER", "light").lower()

def assert_production_secrets() -> None:
    """Fail closed in production on an empty/default service secret.

    An empty DOC_EXTRACT_SECRET makes hmac.compare_digest match an empty
    caller-supplied secret, so anyone who can reach the service could push bytes
    through it. Refuse to boot under an empty or well-known dev default in prod
    (the same fail-closed rule the coordinator applies to its own secrets).
    """
    if not is_production():
        return
    insecure_defaults = {"", "dev_doc_extract_secret", "dev_gateway_secret", "dev_password"}
    if DOC_EXTRACT_SECRET in insecure_defaults:
        raise ValueError(
            "DOC_EXTRACT_SECRET must be set to a strong, non-default value in "
            "production. Generate one with: openssl rand -hex 32"
        )
