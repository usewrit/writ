"""Markdown from a PDF's DECLARED logical structure.

A tagged PDF carries a structure tree (``/StructTreeRoot``) stating what each run
of text IS — H1, P, LI, TD, Artifact. That is semantics the producer asserted,
so it is the correct primary source and needs no inference at all. Measured on a
real corpus (arXiv, EU GDPR, IRS 1040/W-4, NIST CSF): 4 of 5 documents are
tagged and 3 of 5 declare usable headings, tables and lists.

Two properties make this safe rather than a happy path:

  * DEGENERACY CHECK — ``/StructTreeRoot`` existing means nothing. The EU GDPR
    PDF is "tagged" with 40 nodes across 88 pages, Span only, no headings or
    tables. Trusting mere presence would yield garbage, so the tree must be
    shown to carry usable semantics before it is used at all.
  * ARTIFACTS — running heads, folios and rules are declared ``/Artifact``.
    Dropping them reads a declaration instead of guessing at page chrome.

Falls back by raising :class:`StructureUnusable`; the caller then infers.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("doc-extract.pdf.structure")

# Structure roles we render, mapped to their markdown meaning. Roles absent from
# these sets are transparent: their text is kept, the wrapper is not rendered,
# and the nearest meaningful ancestor decides the block.
_HEADING = {"H1": 1, "H2": 2, "H3": 3, "H4": 4, "H5": 5, "H6": 6}
_LIST = {"LI", "LBody"}
_CELL = {"TD", "TH"}
_BLOCK = {"P", "Caption", "Title", "H", "TR", "Table"} | _LIST | _CELL
_DROP = {"Artifact"}

# A tree must clear BOTH bars to be trusted: enough nodes to be describing the
# document rather than decorating it, and at least one genuinely structural role.
# The corpus separates by two orders of magnitude — GDPR 0.45 nodes/page, NIST
# 27.8, IRS W-4 376 — so this is a cliff, not a tuned threshold.
_MIN_NODES_PER_PAGE = 3.0
_STRUCTURAL_ROLES = set(_HEADING) | {"P", "Table", "TD", "L", "LI", "Title", "H"}

_MAX_DEPTH = 60


class StructureUnusable(Exception):
    """No structure tree, or one that declares nothing usable."""


def _resolve(obj):
    try:
        return obj.get_object()
    except Exception:  # noqa: BLE001
        return obj


def build_mcid_map(data: bytes) -> Tuple[Dict[int, Dict[int, str]], Dict[str, int]]:
    """(page index → {mcid: role}, role histogram).

    Walks the structure tree once, recording for every marked-content id which
    structural role encloses it. Nested wrappers resolve to the most specific
    MEANINGFUL ancestor, so a Span inside an H2 is an H2, not a Span.
    """
    import io

    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    root = reader.trailer["/Root"]
    if "/StructTreeRoot" not in root:
        raise StructureUnusable("no /StructTreeRoot")

    # Page object id → index, so an element's /Pg resolves to a page.
    page_index: Dict[int, int] = {}
    for i, page in enumerate(reader.pages):
        try:
            page_index[page.indirect_reference.idnum] = i
        except Exception:  # noqa: BLE001
            pass

    mapping: Dict[int, Dict[int, str]] = defaultdict(dict)
    hist: Dict[str, int] = defaultdict(int)
    visited = set()

    def walk(node, role: Optional[str], page: Optional[int], depth: int = 0) -> None:
        if depth > _MAX_DEPTH or id(node) in visited or not hasattr(node, "get"):
            return
        visited.add(id(node))

        s = node.get("/S")
        if s is not None:
            name = str(s).lstrip("/")
            hist[name] += 1
            # Only meaningful roles displace the inherited one.
            if name in _HEADING or name in _BLOCK or name in _DROP:
                role = name

        pg = node.get("/Pg")
        if pg is not None:
            try:
                page = page_index.get(pg.idnum, page)
            except Exception:  # noqa: BLE001
                pass

        kids = node.get("/K")
        if kids is None:
            return
        if not isinstance(kids, list):
            kids = [kids]

        for kid in kids:
            if isinstance(kid, int):  # a marked-content id on the current page
                if page is not None and role is not None:
                    mapping[page][kid] = role
                continue
            kid = _resolve(kid)
            if hasattr(kid, "get") and kid.get("/Type") == "/MCR":
                num = kid.get("/MCID")
                p = page
                kid_pg = kid.get("/Pg")
                if kid_pg is not None:
                    try:
                        p = page_index.get(kid_pg.idnum, page)
                    except Exception:  # noqa: BLE001
                        pass
                if num is not None and p is not None and role is not None:
                    mapping[p][int(num)] = role
                continue
            walk(kid, role, page, depth + 1)

    walk(_resolve(root["/StructTreeRoot"]), None, None)

    n_pages = max(1, len(reader.pages))
    total = sum(hist.values())
    if total / n_pages < _MIN_NODES_PER_PAGE or not (set(hist) & _STRUCTURAL_ROLES):
        raise StructureUnusable(
            f"degenerate tree: {total} nodes over {n_pages} pages, "
            f"roles={sorted(hist)[:8]}"
        )
    return mapping, dict(hist)


def elements(page, roles: Dict[int, str]) -> List[Tuple[str, str]]:
    """[(role, text)] for one page, in reading order, artifacts dropped.

    Grouped by MARKED-CONTENT ID, not by vertical position. The mcid is the unit
    the producer declared to be one logical element, so a heading wrapping across
    two visual lines is one mcid and must stay one heading. Bucketing by ``top``
    instead split NIST's title into two ``#`` lines and shredded the IRS forms
    into single-character headings, because form text sits at many baselines
    inside a single element.
    """
    groups: Dict[tuple, List[dict]] = defaultdict(list)
    order: Dict[tuple, int] = {}

    for idx, ch in enumerate(page.chars):
        mcid = ch.get("mcid")
        role = roles.get(mcid) if mcid is not None else None
        # pdfplumber also surfaces the innermost tag directly; it catches
        # artifacts the tree walk did not reach.
        if role is None:
            role = ch.get("tag")
        if role in _DROP:
            continue
        # Rotated runs are kept apart. Form labels are commonly set vertically in
        # the margin; merged with upright text and sorted by x they come out
        # reversed and interleaved ("m r o F" for a sideways "Form").
        upright = bool(ch.get("upright", True))
        key = (("mcid", mcid, role, upright) if mcid is not None
               else ("pos", round(ch["top"] / 2.0), role, upright))
        if key not in order:
            order[key] = idx
        groups[key].append(ch)

    out: List[Tuple[str, str]] = []
    for key in sorted(groups, key=lambda k: order[k]):
        chars = groups[key]
        upright = key[3]
        # Reading order inside an element: upright text runs in lines then
        # left-to-right; rotated text runs down a column, so order x then y.
        if upright:
            chars.sort(key=lambda c: (round(c["top"] / 2.0), c["x0"]))
            band = lambda c: round(c["top"] / 2.0)  # noqa: E731
        else:
            chars.sort(key=lambda c: (round(c["x0"] / 2.0), -c["top"]))
            band = lambda c: round(c["x0"] / 2.0)  # noqa: E731

        # Segment each visual line into words, then join the lines. Word breaks
        # are inferred from glyph gaps (a PDF stores no delimiters), so this must
        # go through the same size-scaled tolerance the inference tier uses —
        # concatenating char["text"] runs whole sentences together on tightly
        # kerned type.
        from engines.pdf_infer import words_to_text

        lines: List[List[dict]] = []
        prev = None
        for c in chars:
            b = band(c)
            if prev is None or b != prev:
                lines.append([])
            lines[-1].append(c)
            prev = b

        if upright:
            text = " ".join(t for t in (words_to_text(ln) for ln in lines) if t)
        else:
            # extract_words assumes horizontal reading order and would re-sort a
            # rotated run by x, reversing it ("mroF"). These chars are already in
            # their correct visual order, so concatenate them as they stand.
            text = " ".join("".join(c["text"] for c in ln) for ln in lines)
        text = " ".join(text.split()).strip()
        if text:
            out.append((key[2], text))
    return out


def to_markdown(data: bytes, pages) -> str:
    """Tagged PDF → markdown. Raises StructureUnusable when the tree can't carry it.

    ``pages`` is an already-open sequence of pdfplumber pages, so the caller's
    single ``pdfplumber.open`` is reused rather than paying for a second parse.
    """
    mapping, hist = build_mcid_map(data)
    logger.debug("structure tree usable: %s", hist)

    blocks: List[str] = []
    para: List[str] = []
    cells: List[str] = []

    def flush_para() -> None:
        nonlocal para
        if para:
            blocks.append(" ".join(para))
            para = []

    def flush_cells() -> None:
        nonlocal cells
        if cells:
            # Declared cells with no declared row grouping: emit as a single-row
            # GFM table rather than inventing a shape the document didn't state.
            blocks.append("| " + " | ".join(cells) + " |")
            blocks.append("| " + " | ".join("---" for _ in cells) + " |")
            cells = []

    for i, page in enumerate(pages):
        for role, text in elements(page, mapping.get(i, {})):
            if role in _HEADING:
                flush_para()
                flush_cells()
                blocks.append("#" * _HEADING[role] + " " + text)
            elif role in _LIST:
                flush_para()
                flush_cells()
                blocks.append("- " + text)
            elif role in _CELL:
                flush_para()
                cells.append(text)
            else:
                flush_cells()
                para.append(text)
    flush_para()
    flush_cells()

    return "\n\n".join(b for b in blocks if b.strip()).strip()
