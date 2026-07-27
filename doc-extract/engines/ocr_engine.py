"""RapidOCR wrapper — the ONLY OCR path in the service.

RapidOCR is the ONNX-Runtime port of PaddleOCR's PP-OCRv5 models: CPU-only, no
torch/PaddlePaddle, ~80MB, fully offline. That combination is what lets OCR ship
in an air-gapped OSS self-host box, which is why it's the pick over Surya/docling
(GPU/torch) or Tesseract (weaker on modern layouts).

ROLE REMINDER: OCR is the last resort for pixels with no text layer — scanned
PDF pages, raw images, and screenshots of DOM-empty rendered pages. Everything
with a real text layer (HTML, JSON, office docs, born-digital PDFs) is extracted
directly elsewhere and never reaches this module.

Supports both packagings transparently:
  - rapidocr            (v2+, `RapidOCR()(img)` → object with .txts/.scores)
  - rapidocr_onnxruntime (v1,  `RapidOCR()(img)` → (list[[box,text,score]], elapse))
"""
from __future__ import annotations

import io
import logging
import threading
from typing import List, Optional, Tuple

logger = logging.getLogger("doc-extract.ocr")

_engine = None
_api: Optional[str] = None  # "v2" | "v1"
_lock = threading.Lock()


class OCRUnavailable(RuntimeError):
    """Raised when no RapidOCR packaging is importable."""


def _load_engine():
    """Lazily construct a process-wide RapidOCR engine (thread-safe, once)."""
    global _engine, _api
    if _engine is not None:
        return _engine
    with _lock:
        if _engine is not None:
            return _engine
        # Prefer the v2 unified package, fall back to the v1 ONNX package.
        try:
            from rapidocr import RapidOCR  # type: ignore
            _engine = RapidOCR()
            _api = "v2"
            logger.info("RapidOCR engine ready (rapidocr v2)")
            return _engine
        except Exception as e_v2:  # noqa: BLE001
            try:
                from rapidocr_onnxruntime import RapidOCR  # type: ignore
                _engine = RapidOCR()
                _api = "v1"
                logger.info("RapidOCR engine ready (rapidocr_onnxruntime v1)")
                return _engine
            except Exception as e_v1:  # noqa: BLE001
                raise OCRUnavailable(
                    f"RapidOCR not installed (v2: {e_v2}; v1: {e_v1})"
                ) from e_v1


def available() -> bool:
    try:
        _load_engine()
        return True
    except Exception:  # noqa: BLE001
        return False


def _to_ndarray(data: bytes):
    """Decode image bytes → RGB numpy array (RapidOCR's most portable input)."""
    from PIL import Image  # Pillow is a hard dep of the OCR path
    import numpy as np

    with Image.open(io.BytesIO(data)) as im:
        return np.array(im.convert("RGB"))


def _normalize_result(result) -> Tuple[List[str], List[float]]:
    """Flatten either API's result into (texts, scores)."""
    texts: List[str] = []
    scores: List[float] = []
    if result is None:
        return texts, scores

    # v2: object exposing .txts / .scores (tuples), or a dict-like fallback.
    txts = getattr(result, "txts", None)
    scs = getattr(result, "scores", None)
    if txts is not None:
        for i, t in enumerate(txts):
            if t is None:
                continue
            texts.append(str(t))
            try:
                scores.append(float(scs[i]))
            except Exception:  # noqa: BLE001
                pass
        return texts, scores

    # v1: list of [box, text, score].
    try:
        for line in result:
            if not line or len(line) < 2:
                continue
            texts.append(str(line[1]))
            if len(line) >= 3:
                try:
                    scores.append(float(line[2]))
                except Exception:  # noqa: BLE001
                    pass
    except TypeError:
        pass
    return texts, scores


def ocr_image_bytes(data: bytes) -> dict:
    """OCR raw image bytes (PNG/JPEG/…).

    Returns {text, confidence, lines, engine}. Raises OCRUnavailable if the
    engine can't be loaded; the caller decides whether that's fatal.
    """
    engine = _load_engine()
    arr = _to_ndarray(data)
    raw = engine(arr)

    # v1 returns (result, elapse); v2 returns a single result object.
    if _api == "v1" and isinstance(raw, tuple):
        raw = raw[0]

    texts, scores = _normalize_result(raw)
    text = "\n".join(t for t in texts if t and t.strip())
    confidence = round(sum(scores) / len(scores), 4) if scores else 0.0
    return {
        "text": text,
        "confidence": confidence,
        "lines": len(texts),
        "engine": "rapidocr",
    }
