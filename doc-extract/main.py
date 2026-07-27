"""doc-extract — document/OCR extraction service.

A PRIVATE SERVICE: publish it only where your `writ-agent` fleet can reach it,
never on the open internet. Callers authenticate with DOC_EXTRACT_SECRET.

It turns already-fetched non-HTML bytes
(PDF / office docs / JSON / CSV / images / rendered-page screenshots) into
normalized {markdown, text, tables, records, ocr}, so a crawl gains full
document + OCR coverage without the agents having to embed heavy native
libraries. It never fetches a URL itself — the caller POSTs bytes it already
has — so it carries no SSRF surface.

Role split it enforces:
  - Direct text-layer extraction (PDF text, docx/xlsx/pptx, JSON/CSV) — exact, free.
  - OCR (RapidOCR / PP-OCRv5) — ONLY for pixels with no text layer (scans, images,
    screenshots of DOM-empty rendered pages).

Everything runs locally and offline: the OCR weights ship inside the wheel, so a
fully air-gapped install works, and nothing here phones home.
"""
import asyncio
import hmac
import logging
import os
import uuid
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.responses import JSONResponse

import config
import extractor
from engines import ocr_engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("doc-extract")

# Fail closed in production on an empty/default service secret.
config.assert_production_secrets()


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not config.DOC_EXTRACT_SECRET:
        logger.warning("DOC_EXTRACT_SECRET not set — running unauthenticated (dev only)")
    logger.info("doc-extract ready (tier=%s, ocr_default=%s)", config.ENGINE_TIER, config.OCR_MODE_DEFAULT)
    yield


app = FastAPI(title="doc-extract", version="1.0.0", lifespan=lifespan)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Never leak internals; return a generic message + reference id."""
    error_id = uuid.uuid4().hex[:12]
    logger.error("[%s] doc-extract.unhandled_error %s %s: %s",
                 error_id, request.method, request.url.path, exc, exc_info=exc)
    public = f"An unexpected error occurred. (ref: {error_id})"
    return JSONResponse(status_code=500, content={"error": "Internal Server Error",
                                                  "detail": public, "error_id": error_id})


def _verify_secret(x_doc_secret: Optional[str]) -> None:
    """Timing-safe shared-secret check (skipped only when no secret configured)."""
    if not config.DOC_EXTRACT_SECRET:
        return  # dev / unauthenticated mode
    if not x_doc_secret or not hmac.compare_digest(x_doc_secret, config.DOC_EXTRACT_SECRET):
        raise HTTPException(status_code=403, detail="Invalid doc-extract secret")


@app.get("/health")
@app.get("/healthz")
async def health():
    return {
        "status": "ok",
        "tier": config.ENGINE_TIER,
        "ocr": "ready" if ocr_engine.available() else "unavailable",
    }


@app.post("/extract")
async def extract_endpoint(
    request: Request,
    x_doc_secret: Optional[str] = Header(None, alias="X-Doc-Secret"),
    x_content_type: Optional[str] = Header(None, alias="X-Content-Type"),
    x_source_url: Optional[str] = Header("", alias="X-Source-Url"),
    x_ocr_mode: Optional[str] = Header(None, alias="X-OCR-Mode"),
):
    """Extract one document. Body is the raw bytes; type is X-Content-Type."""
    _verify_secret(x_doc_secret)

    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="empty body")
    if len(data) > config.MAX_BYTES:
        raise HTTPException(status_code=413,
                            detail=f"body exceeds {config.MAX_BYTES} bytes")

    content_type = x_content_type or request.headers.get("content-type", "")
    try:
        # CPU-bound (PDF parsing / OCR) — run off the event loop, bounded by a wall clock.
        result = await asyncio.wait_for(
            asyncio.to_thread(extractor.extract, data, content_type,
                              x_source_url or "", x_ocr_mode),
            timeout=config.EXTRACT_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="extraction timed out")
    return result


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("DOC_EXTRACT_PORT", "8092"))
    uvicorn.run(app, host="0.0.0.0", port=port)
