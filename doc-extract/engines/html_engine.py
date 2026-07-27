"""HTML → clean markdown — the same layered pipeline the crawl agents run.

Why this lives in the extraction service too: a ``.pdf`` (or
``application/pdf``) URL often answers with HTML instead — a soft-404, a login
wall, a WAF interstitial. The PDF engine detects that and routes here rather
than emitting raw markup, so a caller gets boilerplate-stripped,
frontmatter-headed markdown identical to the agent's own HTML lane.

Layered, graceful-degrading pipeline (mirrors ``crawl_exec._to_markdown``):
    trafilatura (main content + metadata + tables)
      → readability + markdownify (main-content isolation → markdown)
        → chrome-stripped plain text (last resort — never a blank row).

Every heavy extractor is optional: a missing dependency drops that layer and the
next one runs, so the engine works in a minimal (light-tier) install.
"""
from __future__ import annotations

import logging
import re
from html import unescape
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("doc-extract.html")

# --------------------------------------------------------------------------- #
# Optional heavy extractors — degrade gracefully when unavailable.             #
# --------------------------------------------------------------------------- #
try:  # trafilatura: boilerplate-stripped main-content extraction + metadata
    import trafilatura as _trafilatura  # type: ignore
except Exception:  # pragma: no cover - optional dep
    _trafilatura = None

try:  # readability-lxml: main-content isolation (fallback engine)
    from readability import Document as _ReadabilityDocument  # type: ignore
except Exception:  # pragma: no cover - optional dep
    _ReadabilityDocument = None

try:  # markdownify: HTML -> markdown
    from markdownify import markdownify as _markdownify  # type: ignore
except Exception:  # pragma: no cover - optional dep
    _markdownify = None

try:  # beautifulsoup4 + lxml: chrome strip + text/meta helpers
    from bs4 import BeautifulSoup as _BeautifulSoup  # type: ignore
except Exception:  # pragma: no cover - optional dep
    _BeautifulSoup = None


# --------------------------------------------------------------------------- #
# Regexes / constants (ported from the agent so output matches byte-for-byte). #
# --------------------------------------------------------------------------- #
_SCRIPT_STYLE_RE = re.compile(r"<(script|style|noscript)\b[^>]*>.*?</\1>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_MULTINL_RE = re.compile(r"\n{3,}")
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.I | re.S)

_CHROME_TAGS = (
    "script", "style", "noscript", "template", "nav", "header", "footer",
    "aside", "form", "svg", "iframe", "dialog", "menu", "button",
)
_ROLE_JUNK = frozenset({
    "navigation", "banner", "contentinfo", "complementary", "search",
    "dialog", "menubar", "menu", "tablist", "toolbar",
})
_ATTR_JUNK_RE = re.compile(
    r"nav|navbar|menu|breadcrumb|pagination|pager|cookie|consent|gdpr|"
    r"advert|\bads?\b|sponsor|promo|newsletter|subscribe|signup|social|"
    r"share|sharing|related|recommend|popular|trending|sidebar|footer|"
    r"masthead|toolbar|offcanvas|drawer|modal|popup|overlay|lightbox|"
    r"skip|sr-only|visually-hidden|screen-reader|banner",
    re.I,
)
_JUNK_TEXT_SHARE = 0.4

_DATA_IMG_RE = re.compile(r"!\[[^\]]*\]\(\s*data:[^)]*\)")
_EMPTY_IMG_RE = re.compile(r"!\[\s*\]\(\s*\)")
_WS_TRANSLATE = {0x00A0: " ", 0x200B: None, 0xFEFF: None}
_DEDUPE_MIN_LEN = 40
_DEDUPE_MULTILINE_MIN_LEN = 12


# --------------------------------------------------------------------------- #
# Public API                                                                   #
# --------------------------------------------------------------------------- #
def extract(data: bytes, source_url: str, note: Optional[str] = None,
            declared: Optional[str] = None) -> dict:
    """Decode ``data`` as HTML and return the normalized result dict.

    ``note``/``declared`` let a caller (the PDF engine) record that this HTML
    arrived under a mismatched content-type (e.g. a ``.pdf`` URL) — surfaced in
    ``meta`` without changing the clean-markdown output.
    """
    html = data.decode("utf-8", errors="replace")
    title, markdown, word_count = to_markdown(html, source_url)
    meta: Dict[str, Any] = {"source_url": source_url, "words": word_count}
    if note:
        meta["note"] = note
    if declared:
        meta["declared"] = declared
    return {
        "kind": "html",
        "content_kind": "html",
        "markdown": markdown,
        "text": _visible_text(html),
        "tables": [],
        "records": [],
        "ocr": None,
        "meta": meta,
    }


def to_markdown(html: str, url: str, fetched_at: Optional[str] = None) -> Tuple[str, str, int]:
    """(title, markdown, word_count) for a page's readable main content.

    trafilatura → readability/markdownify → plain-text, normalized and prefixed
    with a YAML frontmatter header. ``word_count`` counts the body only."""
    html = html or ""

    meta = _trafilatura_meta(html, url)
    if not meta.get("title"):
        meta["title"] = _extract_title(html)
    if not meta.get("description"):
        meta["description"] = _extract_description(html)

    # Prune structural chrome BEFORE the body pass. trafilatura runs with
    # favor_recall=True — the right call for real articles, since it refuses to
    # drop content it is unsure about — but on a short page that heuristic keeps
    # the whole body, nav and footer included. Metadata was already read from the
    # untouched html above, so this only narrows what the body extractor sees.
    # (The readability fallback strips chrome itself; doing it here too is a
    # no-op for that path.)
    body_html = _strip_chrome(html)

    body = _trafilatura_markdown(body_html, url)
    if not body:
        body = _fallback_markdown(body_html, url)
    body = _normalize_markdown(body)

    title = (meta.get("title") or "")[:512]
    word_count = len(body.split())

    markdown = _frontmatter(meta, url, fetched_at)
    if body:
        markdown = f"{markdown}\n\n{body}"
    return title, markdown, word_count


# --------------------------------------------------------------------------- #
# Pipeline internals                                                           #
# --------------------------------------------------------------------------- #
def _clean_text(html_fragment: str) -> str:
    """Strip tags → collapsed plain text (last-resort, no deps)."""
    txt = _SCRIPT_STYLE_RE.sub(" ", html_fragment or "")
    txt = _TAG_RE.sub(" ", txt)
    txt = unescape(txt)
    txt = _WS_RE.sub(" ", txt)
    txt = _MULTINL_RE.sub("\n\n", txt)
    return txt.strip()


def _visible_text(html: str) -> str:
    """Best-effort visible text of a page (drives the result's ``text`` field)."""
    if _BeautifulSoup is not None:
        try:
            soup = _BeautifulSoup(html or "", "lxml")
            for tag in soup(["script", "style", "noscript"]):
                tag.decompose()
            return soup.get_text(" ", strip=True)
        except Exception:
            pass
    return _clean_text(html)


def _extract_title(html: str) -> str:
    m = _TITLE_RE.search(html or "")
    if m:
        t = _clean_text(m.group(1))
        if t:
            return t[:512]
    m = _H1_RE.search(html or "")
    if m:
        t = _clean_text(m.group(1))
        if t:
            return t[:512]
    return ""


def _extract_description(html: str) -> str:
    """Best-effort page description from <meta> tags (description / og / twitter)."""
    if _BeautifulSoup is None:
        return ""
    try:
        soup = _BeautifulSoup(html or "", "lxml")
        for attrs in (
            {"name": "description"},
            {"property": "og:description"},
            {"name": "twitter:description"},
        ):
            tag = soup.find("meta", attrs=attrs)
            content = tag.get("content") if tag else None
            if content:
                desc = _WS_RE.sub(" ", unescape(content)).strip()
                if desc:
                    return desc[:500]
    except Exception:
        pass
    return ""


def _trafilatura_meta(html: str, url: str) -> Dict[str, str]:
    """Page metadata (title/description/author/date/sitename) via trafilatura."""
    if _trafilatura is None:
        return {}
    try:
        doc = _trafilatura.extract_metadata(html or "", default_url=url)
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("trafilatura metadata failed for %s: %s", url, e)
        return {}
    if doc is None:
        return {}

    def _g(name: str) -> str:
        val = getattr(doc, name, None)
        return val.strip() if isinstance(val, str) else ""

    return {
        "title": _g("title"),
        "description": _g("description"),
        "author": _g("author"),
        "date": _g("date"),
        "sitename": _g("sitename"),
    }


def _trafilatura_markdown(html: str, url: str) -> str:
    """Trafilatura → markdown body (boilerplate-stripped, tables + headings kept)."""
    if _trafilatura is None:
        return ""
    try:
        md = _trafilatura.extract(
            html or "",
            url=url,
            output_format="markdown",
            include_tables=True,
            include_links=True,
            include_images=True,
            include_comments=False,
            favor_recall=True,
        )
    except TypeError:
        # Signature drift across trafilatura versions — retry the stable minimum.
        try:
            md = _trafilatura.extract(
                html or "", url=url, output_format="markdown", include_tables=True
            )
        except Exception as e:  # pragma: no cover - defensive
            logger.debug("trafilatura extract failed for %s: %s", url, e)
            return ""
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("trafilatura extract failed for %s: %s", url, e)
        return ""
    return (md or "").strip()


def _fallback_markdown(html: str, url: str) -> str:
    """readability (main-content isolation) → chrome strip → markdownify.

    Degrades to a plain-text strip so a page is never a blank row."""
    content_html = html or ""
    if _ReadabilityDocument is not None:
        try:
            summary = _ReadabilityDocument(content_html).summary(html_partial=True)
            if summary and summary.strip():
                content_html = summary
        except Exception as e:  # pragma: no cover - defensive
            logger.debug("readability failed for %s: %s", url, e)
    content_html = _strip_chrome(content_html)

    markdown = ""
    if _markdownify is not None:
        try:
            markdown = _markdownify(
                content_html, heading_style="ATX", bullets="-", strip=["script", "style"]
            )
        except Exception as e:  # pragma: no cover - defensive
            logger.debug("markdownify failed for %s: %s", url, e)
    if not markdown.strip():
        markdown = _clean_text(content_html)
    return markdown.strip()


def _strip_chrome(html: str) -> str:
    """Remove structural chrome (nav/header/footer/aside/forms/scripts), ARIA
    landmark regions, and class/id-flagged boilerplate. The bulk-text element is
    protected from junk-class removal. BeautifulSoup when present, else regex."""
    if _BeautifulSoup is not None:
        try:
            soup = _BeautifulSoup(html or "", "lxml")

            for tag in soup(list(_CHROME_TAGS)):
                tag.decompose()

            for el in soup.find_all(attrs={"role": True}):
                if str(el.get("role", "")).strip().lower() in _ROLE_JUNK:
                    el.decompose()
            for el in soup.find_all(attrs={"aria-hidden": "true"}):
                el.decompose()

            root = soup.body or soup
            total = len(root.get_text(" ", strip=True)) or 1
            candidates = []
            for el in soup.find_all(True):
                ident = " ".join(el.get("class") or [])
                el_id = el.get("id") or ""
                if el_id:
                    ident = f"{ident} {el_id}"
                if ident.strip() and _ATTR_JUNK_RE.search(ident):
                    candidates.append(el)
            for el in candidates:
                try:
                    if len(el.get_text(" ", strip=True)) < _JUNK_TEXT_SHARE * total:
                        el.decompose()
                except Exception:
                    pass

            body = soup.body or soup
            return str(body)
        except Exception:
            pass
    cleaned = _SCRIPT_STYLE_RE.sub(" ", html or "")
    cleaned = re.sub(
        r"<(nav|header|footer|aside|form)\b[^>]*>.*?</\1>", " ",
        cleaned, flags=re.I | re.S,
    )
    return cleaned


def _yaml_scalar(value: Any) -> str:
    """Render a value as a safely double-quoted, single-line YAML scalar."""
    text = _WS_RE.sub(" ", str(value)).replace("\n", " ").replace("\r", " ").strip()
    text = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def _frontmatter(meta: Dict[str, str], url: str, fetched_at: Optional[str]) -> str:
    """YAML frontmatter header. Only non-empty fields are emitted; ``url`` always."""
    lines = ["---"]

    def _add(key: str, value: Optional[str]) -> None:
        if value:
            lines.append(f"{key}: {_yaml_scalar(value)}")

    _add("title", meta.get("title"))
    lines.append(f"url: {_yaml_scalar(url)}")
    _add("description", meta.get("description"))
    _add("author", meta.get("author"))
    _add("published", meta.get("date"))
    _add("site", meta.get("sitename"))
    _add("crawled_at", fetched_at)
    lines.append("---")
    return "\n".join(lines)


def _normalize_markdown(md: str) -> str:
    """Tidy converter output: strip odd whitespace, drop data-URI/empty images,
    trim trailing spaces, collapse blank lines, drop duplicated blocks."""
    if not md:
        return ""
    md = md.translate(_WS_TRANSLATE)
    md = _DATA_IMG_RE.sub("", md)
    md = _EMPTY_IMG_RE.sub("", md)
    md = "\n".join(line.rstrip() for line in md.splitlines())
    md = _MULTINL_RE.sub("\n\n", md)
    md = _dedupe_blocks(md)
    return md.strip()


def _dedupe_blocks(md: str) -> str:
    """Remove exact-duplicate substantial blocks (order-preserving, first wins)."""
    blocks = re.split(r"\n{2,}", md)
    if len(blocks) < 2:
        return md
    seen: set = set()
    out: List[str] = []
    for block in blocks:
        key = _WS_RE.sub(" ", block.replace("\n", " ")).strip()
        multiline = "\n" in block.strip()
        substantial = len(key) >= _DEDUPE_MIN_LEN or (
            multiline and len(key) >= _DEDUPE_MULTILINE_MIN_LEN
        )
        if substantial:
            if key in seen:
                continue
            seen.add(key)
        out.append(block)
    return "\n\n".join(out)
