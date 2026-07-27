"""Markdown for PDFs that declare no usable structure.

The fallback behind :mod:`pdf_structure`. Roughly one document in five carries
no structure tree at all (arXiv papers, most LaTeX output, anything from a
scanner pipeline), and some carry a degenerate one, so headings and tables have
to be inferred from typography.

Inference means thresholds; that is inherent, not a shortcut. The two used here
are typographic conventions rather than values fitted to a fixture:

  * a heading is set materially larger than body text (>= 1.15x)
  * a paragraph break shows as vertical space materially larger than the
    prevailing line pitch (> 1.6x)

Both are applied over sizes measured DOCUMENT-wide. Per page, a page that simply
has no h1 on it promotes its h2 — which is exactly the bug this module was first
written with.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

import config

logger = logging.getLogger("doc-extract.pdf.infer")

_HEADING_SIZE_RATIO = 1.15
_PARA_GAP_RATIO = 1.6
_LINE_TOL = 2.5
_SIZE_QUANTUM = 0.5

# Ruled tables first (precise). The text-alignment strategy is a fallback for
# borderless tables and is held to a much higher bar — see _plausible_table.
_RULED = {"vertical_strategy": "lines", "horizontal_strategy": "lines"}
_BORDERLESS = {"vertical_strategy": "text", "horizontal_strategy": "text",
               "min_words_vertical": 2, "min_words_horizontal": 2}


def _q(v: float) -> float:
    """Quantize a font size so 11.0 and 11.04 are one size."""
    return round(v / _SIZE_QUANTUM) * _SIZE_QUANTUM


def words_to_text(chars: List[dict]) -> str:
    """Join a run of chars into text with word breaks restored.

    A PDF stores no word delimiters: a space is inferred from the gap between
    glyphs. Concatenating char["text"] therefore yields
    "Providedproperattributionisprovided" on tightly-kerned type. Delegate the
    segmentation to pdfplumber, with the tolerance scaled to the run's own font
    size (see config.PDF_WORD_X_TOLERANCE_RATIO) rather than left at the
    absolute-point default.
    """
    if not chars:
        return ""
    try:
        from pdfplumber.utils import extract_words

        sizes = sorted(c.get("size") or 0 for c in chars)
        median = sizes[len(sizes) // 2] or 0
        tol = max(0.1, median * config.PDF_WORD_X_TOLERANCE_RATIO) if median else 3.0
        words = extract_words(chars, x_tolerance=tol, keep_blank_chars=False)
        if words:
            return " ".join(w["text"] for w in words).strip()
    except Exception as e:  # noqa: BLE001 — fall back to naive concatenation
        logger.debug("word segmentation failed: %s", e)
    return "".join(c["text"] for c in chars).strip()


def _plausible_table(rows: List[list], *, strict: bool) -> bool:
    """Does this candidate look like a table rather than shredded prose?

    The text-alignment strategy will happily read a paragraph as a grid: left
    margins line up, so every line becomes a row and every word-run a cell.
    Measured on a plain two-paragraph page it produced a 13-cell "table" and
    destroyed the prose. Tables are distinguished by SHORT cells in a CONSISTENT
    grid, so require both before trusting a borderless candidate.
    """
    if not rows or len(rows) < 2:
        return False
    widths = [len(r) for r in rows]
    if max(widths) < 2:
        return False
    if not strict:
        return True

    if len(set(widths)) > 1:  # ragged row widths ⇒ not a grid
        return False
    lengths = sorted(len(str(c or "").split())
                     for r in rows for c in r if str(c or "").strip())
    if not lengths:
        return False
    # Cells carry labels and values, not sentences.
    if lengths[len(lengths) // 2] > 3 or lengths[-1] > 8:
        return False
    filled = sum(1 for r in rows for c in r if str(c or "").strip())
    return filled >= 0.7 * sum(widths)


def tables_for_page(page) -> Tuple[List[list], List[tuple]]:
    """(extracted tables, their bboxes) for one page."""
    for settings, strict in ((_RULED, False), (_BORDERLESS, True)):
        try:
            found = page.find_tables(table_settings=settings)
        except Exception as e:  # noqa: BLE001 — a bad page must not kill the doc
            logger.debug("find_tables failed (%s): %s", settings, e)
            continue
        tables, boxes = [], []
        for t in found:
            try:
                rows = t.extract()
            except Exception:  # noqa: BLE001
                continue
            if _plausible_table(rows, strict=strict):
                tables.append(rows)
                boxes.append(t.bbox)
        if tables:
            return tables, boxes
    return [], []


def gfm(rows: List[list]) -> str:
    """A table → GitHub-flavoured markdown."""
    def cell(v) -> str:
        return str(v if v is not None else "").replace("\n", " ").replace("|", r"\|").strip()

    width = max(len(r) for r in rows)
    norm = [[cell(c) for c in r] + [""] * (width - len(r)) for r in rows]
    head, body = norm[0], norm[1:]
    out = ["| " + " | ".join(head) + " |",
           "| " + " | ".join("---" for _ in head) + " |"]
    out += ["| " + " | ".join(r) + " |" for r in body]
    return "\n".join(out)


def _outside(boxes: List[tuple]):
    def pred(obj) -> bool:
        for x0, top, x1, bottom in boxes:
            if (obj["top"] >= top - 2 and obj["bottom"] <= bottom + 2
                    and obj["x0"] >= x0 - 2 and obj["x1"] <= x1 + 2):
                return False
        return True
    return pred


def _body_size(pages_chars: List[List[dict]]) -> float:
    """The document's modal font size, weighted by character count.

    Character-weighted on purpose: a document with many short headings and one
    dense paragraph must still resolve the paragraph's size as body text.
    """
    counts: Dict[float, int] = defaultdict(int)
    for chars in pages_chars:
        for c in chars:
            s = _q(c.get("size") or 0)
            if s > 0:
                counts[s] += 1
    return max(counts, key=counts.get) if counts else 0.0


def _levels(pages_chars: List[List[dict]], body: float) -> Dict[float, int]:
    """size → heading level (1 = largest), for sizes meaningfully above body."""
    sizes = {
        _q(c.get("size") or 0)
        for chars in pages_chars for c in chars
        if _q(c.get("size") or 0) >= body * _HEADING_SIZE_RATIO
    }
    return {s: i + 1 for i, s in enumerate(sorted(sizes, reverse=True))}


def _lines(chars: List[dict]) -> List[Tuple[float, float, str, float]]:
    """Chars → [(top, bottom, text, max_size)] in reading order."""
    rows: List[List[dict]] = []
    for c in sorted(chars, key=lambda c: (c["top"], c["x0"])):
        if rows and abs(c["top"] - rows[-1][0]["top"]) <= _LINE_TOL:
            rows[-1].append(c)
        else:
            rows.append([c])

    out = []
    for row in rows:
        row.sort(key=lambda c: c["x0"])
        text = words_to_text(row)
        if text:
            out.append((min(c["top"] for c in row),
                        max(c["bottom"] for c in row),
                        text,
                        _q(max(c.get("size") or 0 for c in row))))
    return out


def _paragraphs(buf: List[Tuple[float, float, str]]) -> List[str]:
    """Reflow consecutive body lines into paragraphs on vertical gaps."""
    if not buf:
        return []
    gaps = [buf[i][0] - buf[i - 1][1] for i in range(1, len(buf))]
    positive = sorted(g for g in gaps if g > 0)
    base = positive[len(positive) // 2] if positive else 0.0

    out, current = [], [buf[0][2]]
    for i in range(1, len(buf)):
        if base > 0 and (buf[i][0] - buf[i - 1][1]) > base * _PARA_GAP_RATIO:
            out.append(" ".join(current))
            current = [buf[i][2]]
        else:
            current.append(buf[i][2])
    out.append(" ".join(current))
    return [p.strip() for p in out if p.strip()]


def to_markdown(pages, page_tables: Optional[List[List[list]]] = None) -> str:
    """Untagged PDF → markdown, inferring headings from typography.

    ``pages`` is an already-open sequence of pdfplumber pages. ``page_tables``,
    when supplied, is the per-page table extraction the caller already performed,
    so tables are not found twice.
    """
    per_page = []
    pages_chars: List[List[dict]] = []

    for i, page in enumerate(pages):
        if page_tables is not None:
            tables = page_tables[i]
            # Recover the boxes only when there are tables to exclude.
            boxes = tables_for_page(page)[1] if tables else []
        else:
            tables, boxes = tables_for_page(page)
        chars = [c for c in page.chars if _outside(boxes)(c)]
        pages_chars.append(chars)
        per_page.append((chars, tables))

    body = _body_size(pages_chars)
    levels = _levels(pages_chars, body) if body else {}

    blocks: List[str] = []
    for chars, tables in per_page:
        pending: List[Tuple[float, float, str]] = []
        for top, bottom, text, size in _lines(chars):
            level = levels.get(size)
            if level:
                blocks.extend(_paragraphs(pending))
                pending = []
                blocks.append("#" * min(level, 6) + " " + text)
            else:
                pending.append((top, bottom, text))
        blocks.extend(_paragraphs(pending))
        for rows in tables:
            blocks.append(gfm(rows))

    return "\n\n".join(b for b in blocks if b.strip()).strip()
