"""
Dragnet crawl API — start / scope / inspect / cancel distributed site crawls.

A crawl fans out across the agent fleet (see services/crawl_orchestrator). Its
extracted pages aggregate under a synthetic per-crawl workflow, so the results
are read through the existing Workflow Data API at
``/api/workflows/{data_workflow_id}/data`` (returned in each crawl's status).

Three read-only endpoints let a caller trust a crawl BEFORE starting it — none of
them creates a CrawlJob, writes the frontier, or dispatches an agent:
  * ``POST /crawl/preview`` — the scope a crawl WOULD use (derived include/exclude
    paths + a kept/dropped URL sample).
  * ``POST /crawl/map``     — the site's URLs ranked by relevance to a query, so
    the caller can hand-pick the entry set (``seed_urls``).
  * ``POST /crawl/scrape``  — one page to clean markdown, no crawl at all.

Single-owner coordinator: authentication is the coordinator's own API-key / JWT
dependency (``get_current_api_key``) and there is no per-owner scoping — every
crawl belongs to the one operator. A self-hosted crawl is free: no metering, no
allotment, no usage endpoint.
"""
import logging
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models.crawl_job import CrawlJob
from security.api_key import get_current_api_key
from security.validation import InputValidator
from services import crawl_orchestrator, crawl_targeting, domain_guard, url_policy, visual_storage
from services.brand import DRAGNET_NAME

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/crawl", tags=["crawl"])

# Same shape the orchestrator mints with ``secrets.token_urlsafe(16)``.
_SCREENSHOT_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


class StartCrawlRequest(BaseModel):
    url: str = Field(..., description="Seed URL to crawl")
    name: Optional[str] = None
    extract_mode: str = Field("markdown", description="markdown | schema")
    extract_schema: Optional[dict] = None
    render_mode: str = Field("auto", description="auto | http | browser — page render strategy")
    ocr_mode: str = Field("auto", description="auto | off | force — OCR policy for PDF/office/image docs")
    persona_id: Optional[int] = Field(None, description="Login identity for authenticated crawls")
    intent: Optional[str] = Field(None, description="Plain-English goal; derives scope + ranks the frontier by relevance")
    seed_urls: Optional[list] = Field(None, description="Hand-picked seed URLs (from /crawl/map); auto-discover when empty")
    relevance_threshold: float = Field(0.0, ge=0, le=1, description="Drop URLs scoring below this against the intent (0 = keep all)")
    include_paths: Optional[list] = None
    exclude_paths: Optional[list] = None
    # SHAPE knobs — floors only, no ceilings.
    #
    # The cloud caps these against a plan ladder (depth 20 / 50k pages / 64 shards /
    # 200 per shard). Self-hosted there is no ladder: the operator owns the fleet,
    # the bandwidth and the machine the coordinator runs on, so an upper bound here
    # would only forbid them from using hardware they already paid for. The REAL
    # ceiling is live fleet capacity — the WorkflowQueue processor dispatches a
    # shard only when an agent has a free slot, so asking for 500 concurrent shards
    # against 4 agents simply queues, it does not stampede.
    #
    # The `ge=` floors stay: they reject nonsense (a negative page budget, a
    # zero-size shard) rather than rationing anything.
    max_depth: Optional[int] = Field(None, ge=0, description="Link-discovery depth; unset lets the intent-derived depth win")
    page_budget: int = Field(1000, ge=1)
    max_concurrent_shards: Optional[int] = Field(
        None, ge=1,
        description="Concurrent shard cap; unset = a conservative default. Not capped — "
                    "your own fleet size is the real ceiling.")
    shard_size: int = Field(25, ge=1)
    delay_ms: int = Field(250, ge=0)
    respect_robots: bool = True
    same_domain: bool = True
    allow_subdomains: bool = True
    content_spec: Optional[dict] = Field(
        None,
        description="Content-selection spec applied to every page: "
                    "{preset, include_comments, exclude_selectors, include_selectors, keep}.",
    )


def _view(crawl: CrawlJob) -> dict:
    d = crawl.summary()
    d["data_workflow_id"] = crawl.workflow_id
    d["brand"] = DRAGNET_NAME
    return d


async def _guard_seed(db: AsyncSession, raw: str) -> str:
    """Normalize + SSRF/blocklist-screen a caller-supplied URL. DNS-resolving —
    these are creation/preview paths (once per request), not the frontier hot
    loop — so a public hostname that only resolves into private space is refused
    up front. Returns the normalized URL or raises 400."""
    url = (raw or "").strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    await domain_guard.ensure_loaded(db)
    verdict = await url_policy.check_url(url)
    if not verdict.allowed:
        raise HTTPException(400, verdict.message or "URL is not allowed.")
    return url


@router.post("")
async def start_crawl(
    body: StartCrawlRequest,
    db: AsyncSession = Depends(get_db),
    _api_key: dict = Depends(get_current_api_key),
):
    crawl = await crawl_orchestrator.start_crawl(
        db,
        seed_url=body.url,
        name=body.name,
        extract_mode=body.extract_mode,
        extract_schema=body.extract_schema,
        content_spec=body.content_spec,
        render_mode=body.render_mode,
        ocr_mode=body.ocr_mode,
        persona_id=body.persona_id,
        intent=body.intent,
        seed_urls=body.seed_urls,
        relevance_threshold=body.relevance_threshold,
        include_paths=body.include_paths,
        exclude_paths=body.exclude_paths,
        max_depth=body.max_depth,
        page_budget=body.page_budget,
        max_concurrent_shards=body.max_concurrent_shards,
        shard_size=body.shard_size,
        delay_ms=body.delay_ms,
        respect_robots=body.respect_robots,
        same_domain=body.same_domain,
        allow_subdomains=body.allow_subdomains,
    )
    return _view(crawl)


# --------------------------------------------------------------------------- #
# Pre-flight: preview / map / scrape (read-only, no crawl created)             #
# --------------------------------------------------------------------------- #
class PreviewCrawlRequest(BaseModel):
    url: str = Field(..., description="Seed URL to scope")
    intent: Optional[str] = Field(None, description="Plain-English goal to derive scope from")
    include_paths: Optional[list] = None
    exclude_paths: Optional[list] = None
    max_depth: Optional[int] = Field(None, ge=0)
    relevance_threshold: float = Field(0.0, ge=0, le=1)
    same_domain: bool = True
    allow_subdomains: bool = True
    # Bounded on purpose: this one sizes the RESPONSE (and the site fetch behind
    # it), so it protects the coordinator rather than rationing the operator.
    sample_limit: int = Field(60, ge=1, le=200)


@router.post("/preview")
async def preview_crawl(
    body: PreviewCrawlRequest,
    db: AsyncSession = Depends(get_db),
    _api_key: dict = Depends(get_current_api_key),
):
    """Show the scope a crawl WOULD use — derived paths + a kept/dropped URL
    sample — without creating a CrawlJob or writing the frontier. Trust the scope
    before you spend the fleet's time on it."""
    url = await _guard_seed(db, body.url)

    intent = (body.intent or "").strip() or None
    sample = await crawl_orchestrator._sample_site_urls(url, cap=body.sample_limit)

    derived: dict = {"include_paths": [], "exclude_paths": [], "max_depth": 4, "reason": ""}
    if intent and (body.include_paths is None or body.exclude_paths is None or body.max_depth is None):
        derived = await crawl_targeting.derive_scope(intent, sample, url)
    eff = crawl_targeting.merge_scope(
        derived,
        include_paths=body.include_paths,
        exclude_paths=body.exclude_paths,
        max_depth=body.max_depth,
    )

    # Transient (un-persisted) CrawlJob just to reuse the hot-path scope predicates.
    probe = CrawlJob(
        seed_url=url,
        include_paths=eff["include_paths"],
        exclude_paths=eff["exclude_paths"],
        max_depth=eff["max_depth"],
        same_domain=body.same_domain,
        allow_subdomains=body.allow_subdomains,
    )
    seed_host = crawl_orchestrator._host(url)
    seed_reg = crawl_orchestrator._registrable(seed_host)
    targeting = crawl_targeting.make_targeting(
        intent=intent or "",
        include_paths=eff["include_paths"],
        max_depth=eff["max_depth"],
        threshold=body.relevance_threshold,
    )

    items, kept = [], 0
    for u in sample:
        in_scope = (
            crawl_orchestrator._in_domain_scope(probe, u, seed_host, seed_reg)
            and crawl_orchestrator._passes_path_filters(probe, u)
        )
        hit = crawl_targeting.include_hit(targeting, u)
        score = crawl_targeting.score_url(targeting, u, "", depth=1)
        keep = in_scope and crawl_targeting.passes_threshold(targeting, score, 1, hit)
        if keep:
            kept += 1
        items.append({"url": u, "kept": keep, "score": round(score, 4)})
    # Kept first, then by score — the most relevant pages surface at the top.
    items.sort(key=lambda i: (not i["kept"], -i["score"]))

    return {
        "derived": derived,
        "effective": eff,
        "sample": items,
        "counts": {"kept": kept, "dropped": len(items) - kept, "total": len(items)},
        "brand": DRAGNET_NAME,
    }


class MapCrawlRequest(BaseModel):
    url: str = Field(..., description="Site to map")
    search: Optional[str] = Field(None, description="Rank URLs by relevance to this query")
    limit: int = Field(100, ge=1, le=500)


def _last_segment(url: str) -> Optional[str]:
    from urllib.parse import urlsplit
    seg = [s for s in (urlsplit(url).path or "").split("/") if s]
    return seg[-1] if seg else None


@router.post("/map")
async def map_crawl(
    body: MapCrawlRequest,
    db: AsyncSession = Depends(get_db),
    _api_key: dict = Depends(get_current_api_key),
):
    """List a site's URLs (sitemap + a shallow seed-page harvest), ranked by
    relevance to `search`, so the caller can hand-pick which to crawl. Creates
    nothing."""
    url = await _guard_seed(db, body.url)

    # Anchor text (from the harvest) doubles as a title + a relevance signal;
    # sitemap URLs have neither, so they rank on path tokens alone.
    text_by_url: dict = {}
    harvested = await crawl_orchestrator._harvest_seed_links(url, cap=body.limit)
    for h in harvested:
        text_by_url[h["url"]] = h.get("text") or ""
    candidates = list(dict.fromkeys(
        [h["url"] for h in harvested] + list(await crawl_orchestrator._discover_sitemap_urls(url))
    ))

    targeting = crawl_targeting.make_targeting(intent=(body.search or ""))
    scored = []
    for u in candidates:
        anchor = text_by_url.get(u, "")
        score = crawl_targeting.score_url(targeting, u, anchor, depth=1)
        title = anchor or _last_segment(u)
        scored.append({"url": u, "score": round(score, 4), "title": title})
    scored.sort(key=lambda i: -i["score"])

    return {
        "urls": scored[: body.limit],
        "count": len(scored),
        "brand": DRAGNET_NAME,
    }


class ScrapeCrawlRequest(BaseModel):
    url: str = Field(..., description="Page to scrape to clean markdown")


# Compact, dependency-free HTML → markdown-ish text. The full-fidelity pipeline
# (trafilatura / readability) lives on the AGENT, which is where a real crawl's
# pages are extracted; this control-plane path only needs to render one page's
# real content faithfully enough to read.
_SCRIPT_STYLE_RE = re.compile(
    r"<(script|style|noscript|template|svg)\b[^>]*>.*?</\1>", re.I | re.S
)
_TAG_RE = re.compile(r"<[^>]+>")
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_BLOCK_RE = re.compile(
    r"<(h1|h2|h3|h4|li|p|blockquote|pre)\b[^>]*>(.*?)</\1>", re.I | re.S
)
_WS_RE = re.compile(r"[ \t\f\v]+")


def _html_to_markdown(html: str) -> str:
    html = _SCRIPT_STYLE_RE.sub(" ", html or "")
    lines: list = []
    for m in _BLOCK_RE.finditer(html):
        tag = m.group(1).lower()
        inner = _TAG_RE.sub(" ", m.group(2) or "")
        inner = _WS_RE.sub(" ", inner).replace("\n", " ").strip()
        inner = re.sub(r"\s+", " ", inner)
        if not inner:
            continue
        if tag == "h1":
            lines.append(f"# {inner}")
        elif tag == "h2":
            lines.append(f"## {inner}")
        elif tag in ("h3", "h4"):
            lines.append(f"### {inner}")
        elif tag == "li":
            lines.append(f"- {inner}")
        elif tag == "blockquote":
            lines.append(f"> {inner}")
        else:
            lines.append(inner)
    return "\n\n".join(lines).strip()


@router.post("/scrape")
async def scrape_page(
    body: ScrapeCrawlRequest,
    db: AsyncSession = Depends(get_db),
    _api_key: dict = Depends(get_current_api_key),
):
    """Scrape ONE page to clean markdown. No crawl, no fleet dispatch, no cost —
    the single-page twin of a depth-0 crawl."""
    from services import safe_fetch

    url = await _guard_seed(db, body.url)
    try:
        resp = await safe_fetch.safe_get(url, timeout=8.0)
    except Exception:
        resp = None
    if resp is None or resp.status_code != 200:
        raise HTTPException(
            422,
            {
                "message": "Couldn't fetch that page (it may block bots, require a "
                           "login, or be down). A persona-backed crawl can reach it.",
                "code": "scrape_unreachable",
            },
        )

    title_m = _TITLE_RE.search(resp.text or "")
    title = _WS_RE.sub(" ", _TAG_RE.sub("", title_m.group(1))).strip() if title_m else None
    markdown = _html_to_markdown(resp.text or "")

    return {
        "verb": "scrape",
        "url": url,
        "title": title,
        "format": "markdown",
        "markdown": markdown,
        "counts": {
            "chars": len(markdown),
            "raw_tokens_est": len(resp.text or "") // 4,
            "clean_tokens_est": len(markdown) // 4,
        },
        "brand": DRAGNET_NAME,
    }


# --------------------------------------------------------------------------- #
# Read / cancel / delete                                                       #
# --------------------------------------------------------------------------- #
@router.get("")
async def list_crawls(
    db: AsyncSession = Depends(get_db),
    _api_key: dict = Depends(get_current_api_key),
    limit: int = Query(50, ge=1, le=200),
):
    rows = (await db.execute(
        select(CrawlJob)
        .order_by(CrawlJob.created_at.desc())
        .limit(limit)
    )).scalars().all()
    return {"crawls": [_view(c) for c in rows]}


async def _load(db: AsyncSession, crawl_id: int) -> CrawlJob:
    crawl = await db.get(CrawlJob, crawl_id)
    if not crawl:
        raise HTTPException(404, "Crawl not found")
    return crawl


@router.get("/{crawl_id}")
async def get_crawl(
    crawl_id: int,
    db: AsyncSession = Depends(get_db),
    _api_key: dict = Depends(get_current_api_key),
):
    crawl = await _load(db, crawl_id)
    return _view(crawl)


@router.get("/{crawl_id}/screenshot/{token}")
async def get_crawl_page_screenshot(
    crawl_id: int,
    token: str,
    db: AsyncSession = Depends(get_db),
    _api_key: dict = Depends(get_current_api_key),
):
    """Authenticated, same-origin proxy for a crawled page's thumbnail.

    The crawl's result rows carry only a served path (``/crawl/{id}/screenshot/
    {token}``), never the raw storage ref. The frontend loads this via <AuthImage>
    (blob-fetched with the app's bearer token). Returns the stored JPEG bytes, or
    404 (the UI falls back to the site favicon)."""
    if not _SCREENSHOT_TOKEN_RE.match(token or ""):
        raise HTTPException(404, "Not found")
    await _load(db, crawl_id)  # 404 for an unknown crawl
    raw = visual_storage.fetch_crawl_thumbnail(crawl_id, token)
    if not raw:
        raise HTTPException(404, "Not found")
    return Response(
        content=raw,
        media_type="image/jpeg",
        headers={"Cache-Control": "private, max-age=3600"},
    )


# ── Site favicon (one per crawl) ─────────────────────────────────────────────
_MAX_FAVICON_BYTES = 512 * 1024
_ICON_LINK_RE = re.compile(r'<link[^>]+rel=["\']?[^"\'>]*icon[^"\'>]*["\']?[^>]*>', re.I)
_HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.I)


def _sniff_image_type(raw: bytes) -> Optional[str]:
    """Content-type from magic bytes — and a cheap "is this actually an image?"
    gate so an HTML error page served at /favicon.ico never gets cached as one."""
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if raw[:3] == b"GIF":
        return "image/gif"
    if raw[:2] == b"\xff\xd8":
        return "image/jpeg"
    if raw[:4] == b"\x00\x00\x01\x00":
        return "image/x-icon"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    head = raw[:256].lstrip().lower()
    if head.startswith(b"<svg") or (b"<svg" in head and b"<?xml" in head):
        return "image/svg+xml"
    return None


async def _resolve_favicon_bytes(seed_url: str) -> Optional[tuple]:
    """Best-effort fetch of a site's favicon: parse the homepage's
    ``<link rel=icon>`` (accurate) then fall back to ``/favicon.ico``. Every URL is
    SSRF-guarded (the seed's origin is the crawl's own site, already vetted, but we
    re-check the homepage + the DECLARED icon URL, which is attacker-controlled),
    the fetch is size-capped + timed out, and the bytes are sniffed so only real
    images are returned/cached."""
    import httpx
    from urllib.parse import urljoin, urlparse
    try:
        parts = urlparse(seed_url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            return None
        origin = f"{parts.scheme}://{parts.netloc}"
    except Exception:  # noqa: BLE001
        return None

    candidates: list = []
    headers = {"User-Agent": "Mozilla/5.0 (compatible; ScribeFavicon/1.0)"}
    # SSRF: fetch via InputValidator.safe_fetch, which follows redirects MANUALLY
    # and re-resolves + IP-pins EVERY hop. httpx's own follow_redirects would skip
    # that per-hop check, so a homepage/favicon 30x could bounce to an internal IP.
    async with httpx.AsyncClient(timeout=5.0) as cli:
        try:
            v = await url_policy.check_url(origin)
            if v.allowed:
                r = await InputValidator.safe_fetch(
                    cli, origin, headers=headers, timeout=5.0, max_redirects=3
                )
                if r.status_code < 400 and "html" in r.headers.get("content-type", "").lower():
                    m = _ICON_LINK_RE.search(r.text or "")
                    if m:
                        h = _HREF_RE.search(m.group(0))
                        if h:
                            href = (h.group(1) or "").strip()
                            if href and not href.lower().startswith(("data:", "javascript:")):
                                candidates.append(urljoin(origin + "/", href))
        except Exception:  # noqa: BLE001
            pass
        candidates.append(f"{origin}/favicon.ico")

        for url in candidates:
            try:
                v = await url_policy.check_url(url)
                if not v.allowed:
                    continue
                r = await InputValidator.safe_fetch(
                    cli, url, headers=headers, timeout=5.0, max_redirects=3
                )
                if r.status_code >= 400:
                    continue
                raw = r.content
                if not raw or len(raw) > _MAX_FAVICON_BYTES:
                    continue
                ctype = _sniff_image_type(raw)
                if ctype is None:
                    continue  # not a real image (e.g. an HTML 404 body) — skip
                return raw, ctype
            except Exception:  # noqa: BLE001
                continue
    return None


@router.get("/{crawl_id}/favicon")
async def get_crawl_favicon(
    crawl_id: int,
    db: AsyncSession = Depends(get_db),
    _api_key: dict = Depends(get_current_api_key),
):
    """Authenticated, same-origin proxy for a crawl's SITE favicon.

    A crawl is one site, so its glyph is that site's favicon — served here (not a
    per-page value) so the crawl list, the detail header and the dataset row all
    show a reliable, CSP-safe icon instead of a cross-origin ``<img>`` that breaks.
    The bytes are resolved once (homepage ``<link rel=icon>`` → ``/favicon.ico``,
    SSRF-guarded) and cached in storage; later loads serve from there. Returns the
    image bytes, or 404 (the UI falls back to the globe glyph)."""
    crawl = await _load(db, crawl_id)
    raw = visual_storage.fetch_crawl_favicon(crawl_id)
    if raw is None:
        await domain_guard.ensure_loaded(db)
        resolved = await _resolve_favicon_bytes(crawl.seed_url)
        if resolved:
            raw, ctype = resolved
            visual_storage.store_crawl_favicon(raw, ctype, crawl_id)
    if not raw:
        raise HTTPException(404, "Not found")
    return Response(
        content=raw,
        media_type=_sniff_image_type(raw) or "image/x-icon",
        headers={"Cache-Control": "private, max-age=86400"},
    )


@router.post("/{crawl_id}/cancel")
async def cancel_crawl(
    crawl_id: int,
    db: AsyncSession = Depends(get_db),
    _api_key: dict = Depends(get_current_api_key),
):
    crawl = await _load(db, crawl_id)
    await crawl_orchestrator.cancel_crawl(db, crawl)
    await db.refresh(crawl)
    return _view(crawl)


@router.delete("/{crawl_id}", status_code=204)
async def delete_crawl(
    crawl_id: int,
    db: AsyncSession = Depends(get_db),
    _api_key: dict = Depends(get_current_api_key),
):
    """Remove a crawl and its collected dataset. Terminal crawls only — an in-flight
    crawl must be stopped first (the frontend shows Stop, then Delete)."""
    crawl = await _load(db, crawl_id)
    if crawl.status not in ("completed", "failed", "cancelled"):
        raise HTTPException(400, "Stop this crawl before removing it.")
    workflow_id = crawl.workflow_id
    await db.delete(crawl)
    # The crawl's pages live under a synthetic dataset workflow; remove it too so a
    # deleted crawl leaves no orphaned dataset (cascades its data rows + shard tasks).
    if workflow_id:
        from models.automation_workflow import AutomationWorkflow
        wf = await db.get(AutomationWorkflow, workflow_id)
        if wf:
            await db.delete(wf)
    await db.commit()
    logging.getLogger(__name__).info(f"Deleted crawl {crawl_id} (+ dataset workflow {workflow_id})")
