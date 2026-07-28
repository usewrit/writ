"""Dataset output formats (`?format=`) — the serialization boundary shared by
every surface that hands a flattened extracted-data table back to a caller.

This module exists as TWO BYTE-IDENTICAL COPIES, one per deployment, and a twin
test fails the build the moment they drift. Edit one, copy it verbatim to the
other; never edit a single copy on its own. (The same guard pins
services/extracted_data_table.py, for the same reason.)

Why a service and not a router helper: formats are served from several
independent call sites — a public datasets API, the application's own data
export, and the self-host export route. One implementation behind all of them is
what stops a format being "available over here but not over there" again, which
is exactly the drift this file was extracted to end.

The renderers themselves live in services/extracted_data_table.py (also a twin).
This module owns only the HTTP framing: validation, media types, download
filenames, and the security headers a render must carry.

SECURITY — `markdown`/`html` echo SCRAPED THIRD-PARTY content back from OUR
origin, which makes them a stored-XSS surface. Three layers defend it:
  1. The renderer escapes raw HTML (markdown-it ``html=False``) and allowlists
     link/image URL schemes — see extracted_data_table.py::_md_to_html.
  2. ``X-Content-Type-Options: nosniff``, so a text/markdown body can never be
     sniffed into HTML.
  3. ``Content-Security-Policy: default-src 'none'`` — set HERE, per response,
     rather than inherited from a global middleware. That distinction matters:
     the cloud's global CSP is already ``default-src 'none'``, but the
     coordinator serves the SPA from the SAME origin and so must run a global
     ``default-src 'self'; script-src 'self'`` — which would NOT block execution
     for a render. Stamping the strict policy on the response itself makes the
     guarantee a property of this code path instead of a property of whichever
     deployment happens to be serving it.
"""

from __future__ import annotations

import json
from typing import Optional

from fastapi import HTTPException
from fastapi.responses import Response

from services import extracted_data_table as edt

#: Every format a dataset read can be served as. `json` is the documented
#: envelope; markdown/html are CONTENT-AWARE (a document-shaped dataset — crawl
#: pages carry a `markdown` column — renders as documents, anything else as a
#: table).
DATASET_FORMATS = ("json", "csv", "markdown", "html")

_FORMAT_MEDIA = {
    "json": "application/json",
    "csv": "text/csv; charset=utf-8",
    "markdown": "text/markdown; charset=utf-8",
    "html": "text/html; charset=utf-8",
}

_FORMAT_EXT = {"json": "json", "csv": "csv", "markdown": "md", "html": "html"}

#: Stamped on every render. See the module docstring for why the CSP is set per
#: response rather than left to the global security-header middleware.
RENDER_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": (
        "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
}


def norm_format(fmt: Optional[str], *, default: str = "json") -> str:
    """Validate + normalize `?format=`. Unknown values 400 rather than silently
    falling back, so a typo is never mistaken for a successful response in the
    caller's default format.

    ``default`` is the format used when the parameter is absent/empty — `json`
    for the read APIs (which have a documented envelope), `csv` for the download
    routes (whose long-standing default is a spreadsheet)."""
    f = (fmt or default).strip().lower()
    if f not in DATASET_FORMATS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported format '{fmt}'. Use one of: {', '.join(DATASET_FORMATS)}",
        )
    return f


def render_body(
    fmt: str,
    columns: list[str],
    rows: list[dict],
    *,
    title: Optional[str] = None,
    lineage: bool = False,
) -> str:
    """Serialize a (columns, rows) table to `fmt` as text. Split out from
    `render_dataset` so a caller that needs the body without an HTTP envelope
    (the AI concierge folds datasets into a prompt this way) shares exactly the
    serialization the API serves."""
    if fmt == "csv":
        return edt.to_csv(columns, rows, lineage=lineage)
    if fmt == "markdown":
        return edt.to_markdown(columns, rows, lineage=lineage, title=title)
    if fmt == "html":
        return edt.to_html(columns, rows, lineage=lineage, title=title)
    return json.dumps(
        edt.to_json_records(columns, rows, lineage=lineage),
        ensure_ascii=False, default=str, indent=2,
    )


def render_dataset(
    fmt: str,
    columns: list[str],
    rows: list[dict],
    *,
    title: Optional[str] = None,
    lineage: bool = False,
    filename: Optional[str] = None,
) -> Response:
    """Serialize a (columns, rows) table in `fmt` as a ready Response, carrying
    the media type and the security headers the format requires. When
    ``filename`` is set (WITHOUT an extension — this picks the right one) the
    response is framed as a download, which is what the export routes want."""
    headers = dict(RENDER_HEADERS)
    if filename:
        headers["Content-Disposition"] = f'attachment; filename="{filename}.{_FORMAT_EXT[fmt]}"'
    return Response(
        content=render_body(fmt, columns, rows, title=title, lineage=lineage),
        media_type=_FORMAT_MEDIA[fmt],
        headers=headers,
    )


def search_table(payload: dict) -> tuple[list[str], list[dict]]:
    """Re-shape a ``dataset_search.search_records`` payload into the
    (columns, rows) the renderers consume, so `?format=` works on search exactly
    as it does on records. Delegates to the table service so this surface and the
    AI concierge's answer-from-datasets fold reshape search hits identically."""
    return edt.search_results_to_table(payload)


def format_help(default: str = "json") -> str:
    """The `?format=` OpenAPI description, so every route documents the same
    list and a newly added format shows up everywhere at once."""
    others = [f for f in DATASET_FORMATS if f != default]
    return f"{default} (default) | " + " | ".join(others)


__all__ = [
    "DATASET_FORMATS",
    "RENDER_HEADERS",
    "format_help",
    "norm_format",
    "render_body",
    "render_dataset",
    "search_table",
]
