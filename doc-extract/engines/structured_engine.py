"""Structured data — JSON / CSV / TSV. Parsed directly to records, never OCR.

This is already machine-structured; the right move is to surface it AS records
(so the crawl's schema/data consumers get real fields) plus a readable markdown
preview. Running OCR or HTML extraction over it would destroy the structure.
"""
from __future__ import annotations

import csv
import io
import json
import logging
from typing import Any, List

logger = logging.getLogger("doc-extract.structured")

# Cap the markdown preview so a huge dataset doesn't produce a massive table;
# the full data still rides in `records`.
_PREVIEW_ROWS = 200


def _flatten_records(obj: Any) -> List[dict]:
    """Coerce arbitrary JSON into a list of record dicts."""
    if isinstance(obj, list):
        out = []
        for item in obj:
            if isinstance(item, dict):
                out.append(item)
            else:
                out.append({"value": item})
        return out
    if isinstance(obj, dict):
        # A dict whose sole/primary value is a list of rows → use that list.
        for v in obj.values():
            if isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
                return v
        return [obj]
    return [{"value": obj}]


def _records_markdown(records: List[dict]) -> str:
    if not records:
        return ""
    # Union of keys, preserving first-seen order.
    keys: List[str] = []
    for rec in records[:_PREVIEW_ROWS]:
        for k in rec.keys():
            if k not in keys:
                keys.append(k)
    if not keys:
        return ""
    lines = ["| " + " | ".join(keys) + " |", "| " + " | ".join(["---"] * len(keys)) + " |"]
    for rec in records[:_PREVIEW_ROWS]:
        lines.append("| " + " | ".join(
            str(rec.get(k, "")).replace("\n", " ") for k in keys
        ) + " |")
    if len(records) > _PREVIEW_ROWS:
        lines.append(f"\n_({len(records) - _PREVIEW_ROWS} more rows in records)_")
    return "\n".join(lines)


def extract_json(data: bytes, source_url: str) -> dict:
    try:
        obj = json.loads(data.decode("utf-8", errors="replace"))
    except Exception as e:  # noqa: BLE001
        logger.warning("json parse failed for %s: %s", source_url, e)
        # Not valid JSON despite the content-type — surface raw text.
        text = data.decode("utf-8", errors="replace")
        return {
            "kind": "json", "content_kind": "json", "markdown": text, "text": text,
            "tables": [], "records": [], "ocr": None,
            "meta": {"source_url": source_url, "parse_error": str(e)},
        }
    records = _flatten_records(obj)
    md = _records_markdown([r for r in records if isinstance(r, dict)])
    if not md:
        md = "```json\n" + json.dumps(obj, indent=2, ensure_ascii=False)[:4000] + "\n```"
    return {
        "kind": "json", "content_kind": "json", "markdown": md,
        "text": json.dumps(obj, ensure_ascii=False), "tables": [],
        "records": records, "ocr": None,
        "meta": {"source_url": source_url, "record_count": len(records)},
    }


def extract_csv(data: bytes, source_url: str, delimiter: str = ",") -> dict:
    text = data.decode("utf-8-sig", errors="replace")
    try:
        reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
        records = [dict(row) for row in reader]
    except Exception as e:  # noqa: BLE001
        logger.warning("csv parse failed for %s: %s", source_url, e)
        records = []
    md = _records_markdown(records) or text[:4000]
    return {
        "kind": "csv" if delimiter == "," else "tsv",
        "content_kind": "csv",
        "markdown": md, "text": text, "tables": [], "records": records, "ocr": None,
        "meta": {"source_url": source_url, "record_count": len(records)},
    }
