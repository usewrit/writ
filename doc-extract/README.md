# doc-extract

The document + OCR extraction service. It turns already-fetched **non-HTML**
bytes into normalized `{markdown, text, tables, records, ocr}`, which is what
gives a crawl coverage of PDFs, office documents and scanned pages instead of
silently dropping them.

It ships with the coordinator and `docker compose up` starts it. Without it a
crawl still runs — it just skips every non-HTML resource it reaches.

The split exists so your `writ-agent` fleet stays a small binary: the agents
fetch bytes and forward them here, and only this one image carries the heavy
native libraries (PDF parsing, ONNX runtime, OpenCV).

| Input | How it is read | OCR? |
|---|---|---|
| PDF with a text layer | pdfplumber → markdown + tables; `pypdf` reads a tagged PDF's declared headings | no |
| PDF scanned page | PDFium raster → RapidOCR, that page only | yes |
| `.docx` / `.xlsx` / `.pptx` | python-docx / openpyxl / python-pptx | no |
| JSON / CSV | stdlib → records | no |
| image / screenshot | RapidOCR | yes |
| HTML served from a `.pdf` URL | trafilatura → readability → markdownify | no |

Markdown from a PDF comes in two tiers. Roughly four documents in five are
*tagged*, carrying their own logical structure — those are read exactly, with no
guessing. The rest fall back to inferring headings from typography.

**OCR is RapidOCR** (ONNX PP-OCRv5): CPU-only, no torch, and the model weights
ship inside the wheel — so it works fully offline and air-gapped. OCR is the
last resort for pixels with no text layer; every text-layer format is extracted
directly, which is both exact and free.

## Privacy

This service makes **no outbound network connections**. It never fetches a URL —
callers POST bytes they already hold — and it carries no analytics or
error-reporting client. Document bytes are processed in memory and are not
written to disk.

## Security

Treat it as a private service. Bind it where only your agents can reach it and
give it a strong `DOC_EXTRACT_SECRET`; with `ENVIRONMENT=production` it refuses
to boot on an empty or well-known one. The shipped compose file publishes it on
loopback only.

Because it has no SSRF surface of its own (it never fetches), the risk it does
carry is resource exhaustion from hostile input. Three limits bound that:
`DOC_EXTRACT_MAX_BYTES` caps a single body, `DOC_EXTRACT_OCR_MAX_PAGES` caps how
many pages one request may OCR, and `DOC_EXTRACT_TIMEOUT_S` bounds the whole
extraction on a wall clock.

## API

`POST /extract` — the body is the raw bytes. Headers:

- `X-Doc-Secret` — shared secret (must equal `DOC_EXTRACT_SECRET`; the check is
  skipped only when no secret is configured, which production forbids)
- `X-Content-Type` — the fetched resource's content-type (the declared type;
  magic bytes and the URL suffix are also sniffed)
- `X-Source-Url` — the original URL (metadata only; the service never fetches it)
- `X-OCR-Mode` — `auto` (default) | `off` | `force`

Returns:

```json
{ "kind": "pdf", "content_kind": "pdf|ocr|docx|xlsx|pptx|json|csv|image|text",
  "markdown": "...", "text": "...", "tables": [...], "records": [...],
  "ocr": { "engine": "rapidocr", "confidence": 0.94, "pages": 2 } | null,
  "meta": { "source_url": "...", "pages": 3, "text_layer": true } }
```

`GET /health` → `{status, tier, ocr}`.

## Configuration

| Variable | Default | What it does |
| --- | --- | --- |
| `DOC_EXTRACT_SECRET` | — | Shared secret callers must present. **Required in production.** |
| `DOC_EXTRACT_PORT` | `8092` | Listen port. |
| `DOC_EXTRACT_MAX_BYTES` | `33554432` (32 MiB) | Hard ceiling on one request body. |
| `DOC_EXTRACT_TIMEOUT_S` | `60` | Wall-clock budget for one extraction. |
| `DOC_EXTRACT_OCR_MODE` | `auto` | `auto` \| `off` \| `force`. |
| `DOC_EXTRACT_OCR_DPI` | `200` | Raster DPI before OCR. Higher reads better, costs more. |
| `DOC_EXTRACT_OCR_MAX_PAGES` | `100` | Most pages one request may OCR. |
| `DOC_EXTRACT_TIER` | `light` | `light` ships everywhere. `rich` also tries `docling` for complex tables, which pulls torch — install it yourself. |

Agents call this service only when `DOC_EXTRACT_URL` is set on the **agent**.
Unset means non-HTML content is skipped, exactly as if the service did not
exist — a safe no-op, never an error.

## Running it

`docker compose up` already starts it. To run it directly:

```bash
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8092
```

```bash
pytest -q
```

Most tests `importorskip` their heavy dependency, so a bare checkout reports
honestly on what is actually installed rather than failing. That has a sharp
edge — a suite where everything skipped still prints green — so CI sets
`DOC_EXTRACT_REQUIRE_DEPS=1`, which turns a missing dependency into a failure.
The PDF fixtures are built from raw PDF bytes with no library at all, so the
core extraction paths run either way.

## License

AGPL-3.0-only, like the rest of this repository — see [`../LICENSE`](../LICENSE).

Every dependency here is permissively licensed, deliberately: PDFium via
`pypdfium2` (BSD-3-Clause / Apache-2.0), `pdfplumber` over `pdfminer.six` (MIT),
`pypdf` (BSD-3-Clause), and RapidOCR (Apache-2.0). PyMuPDF and `pymupdf4llm`
were removed for exactly this reason — they are AGPL-3.0, and AGPL §13 obliges a
network service to offer the corresponding source of the combined work, which is
a live constraint for anyone hosting this. Third-party notices are in
[`../THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md).
