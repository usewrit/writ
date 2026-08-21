"""
Main-content HTML → markdown for INLINE (non-fleet) extraction paths.

The fleet agents extract properly (trafilatura ladder in the agent's
crawl_exec._to_markdown); the backend historically had only the regex
block-tag stripper in routers/playground.py::_html_to_markdown, whose
"first N lines" are a page's nav/header — navigation junk instead of the
article. This module mirrors the AGENT'S ladder server-side so every
inline fallback (playground fleet-miss, authed /api/crawl/scrape
fallback) returns main content:

    trafilatura (markdown, favor_recall)
      → readability-lxml (article isolation) + chrome-strip + markdownify
        → "" (caller falls back to its regex stripper — never blank-page)

Every import is optional and every step degrades: this module must never
be the reason an unauthenticated endpoint 500s. Parity note: keep the
ladder's semantics in step with pagesurveil-agent/pagesurveil_agent/
crawl_exec.py (_trafilatura_markdown/_fallback_markdown) — that file is
the reference implementation.


SELF-HOSTED EDITION. Ported from the cloud backend unchanged: the engines are
imported OPTIONALLY (missing ones simply drop out of the ladder), so a
coordinator without them still gets the chrome-stripped structural fallback
rather than the regex stripper this replaced. requirements.txt pins the same
known-good ranges the agent and doc-extract use, so a normal install gets the
full ladder.
"""
import logging
import re
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

try:  # pragma: no cover - import guard
    import trafilatura as _trafilatura
except Exception:  # noqa: BLE001
    _trafilatura = None

try:  # pragma: no cover - import guard
    from readability import Document as _ReadabilityDocument
except Exception:  # noqa: BLE001
    _ReadabilityDocument = None

try:  # pragma: no cover - import guard
    from markdownify import markdownify as _markdownify
except Exception:  # noqa: BLE001
    _markdownify = None

try:  # pragma: no cover - import guard
    from bs4 import BeautifulSoup as _BeautifulSoup
except Exception:  # noqa: BLE001
    _BeautifulSoup = None

# lxml is C-backed and linear; this cap only bounds worst-case memory. It is
# far above the keyless per-response fetch cap (4 MiB), so real playground
# inputs are never truncated here.
_MAX_HTML_CHARS = 2_000_000

# Structural chrome the readability fallback always drops. Readability's
# summary usually isolates the article already; this catches what leaks
# through (and the no-readability degrade path operates on the raw body).
_CHROME_TAGS = (
    "nav", "header", "footer", "aside", "form", "script", "style",
    "noscript", "svg", "template", "iframe", "button",
)
_ROLE_JUNK = {"navigation", "banner", "contentinfo", "search", "complementary"}

_WS_COLLAPSE_RE = re.compile(r"\n{3,}")


def extract_main_markdown(html: str, url: str) -> str:
    """Main-content markdown for a page, or "" when every ladder step misses.

    The caller keeps its own last-resort converter (the historical regex
    stripper) so a page is never a blank result — this function's contract is
    "real article content or nothing", not "always something".
    """
    html = (html or "")[:_MAX_HTML_CHARS]
    if not html.strip():
        return ""

    body = _trafilatura_markdown(html, url)
    if not body:
        body = _readability_markdown(html, url)
    # Collapse pathological blank runs so head-slicing sees real lines.
    return _WS_COLLAPSE_RE.sub("\n\n", body).strip()


def extract_title(html: str, url: str) -> Optional[str]:
    """Best-effort page title: trafilatura metadata, else None (callers keep
    their existing <title> regex as the fallback)."""
    if _trafilatura is None:
        return None
    try:
        meta = _trafilatura.extract_metadata((html or "")[:_MAX_HTML_CHARS], default_url=url)
    except Exception:  # noqa: BLE001 - defensive
        return None
    title = getattr(meta, "title", None) if meta is not None else None
    if isinstance(title, str) and title.strip():
        return title.strip()[:512]
    return None


# trafilatura's markdown drifts across releases in ways that DESTROY structure
# rather than restyle it (bisected 2026-08 against the agent's GFM fixtures):
# 1.x rendered table rows without the leading pipe ("Plan | Price |" — not a
# GFM row, so markdown consumers read it as prose), and 2.2.0's extraction can
# collapse a whole page into ONE line (headings, list items and table cells
# concatenated). Either shape is strictly worse than the readability+markdownify
# fallback, which emits real GFM deterministically — so a body carrying one of
# these signatures is rejected and the caller falls through. requirements.txt
# pins the known-good range (>=2.0,<2.2); the guard covers installs that bring
# their own trafilatura anyway.
_GFM_TABLE_ROW_RE = re.compile(r"^\s*\|.+\|\s*$", re.M)  # a "| a | b |" row
_HTML_TABLE_RE = re.compile(r"<table\b", re.I)
_HTML_TABLE_HEADER_RE = re.compile(r"<th\b|<thead\b", re.I)
_HTML_BLOCK_ELEM_RE = re.compile(r"<(?:h[1-6]|p|li|t[dh]|blockquote|pre|dl)\b", re.I)


def _engine_output_mangled(md: str, html: str, *, keep_tables: bool) -> bool:
    """True when an engine's markdown lost structure the page itself carries.

    Judged against the page's own HTML, so a page that genuinely IS one
    paragraph — or has no header-carrying data table — never trips. ``md`` must
    be non-empty and stripped."""
    if "\n" not in md and len(_HTML_BLOCK_ELEM_RE.findall(html)) >= 2:
        return True  # multi-block page came back as a single unstructured line
    if (
        keep_tables
        and _HTML_TABLE_RE.search(html)
        and _HTML_TABLE_HEADER_RE.search(html)
        and not _GFM_TABLE_ROW_RE.search(md)
    ):
        return True  # the page has a data table but no GFM row survived
    return False


def _trafilatura_markdown(html: str, url: str) -> str:
    """Trafilatura → markdown body. Mirrors the agent's call (favor_recall,
    tables/links/images/comments kept) including the TypeError retry for
    signature drift across trafilatura versions and the structure-acceptance
    guard — a mangled body returns "" so the readability leg answers instead."""
    if _trafilatura is None:
        return ""
    try:
        md = _trafilatura.extract(
            html,
            url=url,
            output_format="markdown",
            include_tables=True,
            include_links=True,
            include_images=True,
            include_comments=True,
            favor_recall=True,
        )
    except TypeError:
        try:
            md = _trafilatura.extract(
                html, url=url, output_format="markdown",
                include_tables=True, include_comments=True,
            )
        except Exception as e:  # noqa: BLE001 - defensive
            logger.debug("trafilatura extract failed for %s: %s", url, e)
            return ""
    except Exception as e:  # noqa: BLE001 - defensive
        logger.debug("trafilatura extract failed for %s: %s", url, e)
        return ""
    md = (md or "").strip()
    if md and _engine_output_mangled(md, html, keep_tables=True):
        logger.debug("trafilatura body rejected as structure-mangled for %s", url)
        return ""
    return md


def _readability_markdown(html: str, url: str) -> str:
    """readability-lxml article isolation → chrome strip → markdownify."""
    content_html = html
    if _ReadabilityDocument is not None:
        try:
            summary = _ReadabilityDocument(content_html).summary(html_partial=True)
            if summary and summary.strip():
                content_html = summary
        except Exception as e:  # noqa: BLE001 - defensive
            logger.debug("readability failed for %s: %s", url, e)
    content_html = _strip_chrome(content_html)
    if not content_html.strip():
        return ""

    if _markdownify is not None:
        try:
            md = _markdownify(
                content_html, heading_style="ATX", bullets="-",
                strip=["script", "style"],
            )
            if md and md.strip():
                return md.strip()
        except Exception as e:  # noqa: BLE001 - defensive
            logger.debug("markdownify failed for %s: %s", url, e)
    return ""


def _strip_chrome(html: str) -> str:
    """Drop structural chrome tags + ARIA landmark regions + hidden nodes.
    BeautifulSoup(lxml) when available; otherwise return the input unchanged —
    the trafilatura leg (which needs no bs4) is the primary path anyway."""
    if _BeautifulSoup is None:
        return html
    try:
        soup = _BeautifulSoup(html, "lxml")
        for tag in soup(list(_CHROME_TAGS)):
            tag.decompose()
        for el in soup.find_all(attrs={"role": True}):
            if str(el.get("role", "")).strip().lower() in _ROLE_JUNK:
                el.decompose()
        for el in soup.find_all(attrs={"aria-hidden": "true"}):
            el.decompose()
        return str(soup.body or soup)
    except Exception as e:  # noqa: BLE001 - defensive
        logger.debug("chrome strip failed: %s", e)
        return html
