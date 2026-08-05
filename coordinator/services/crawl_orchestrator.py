"""
Dragnet crawl orchestrator — the distributed coordinator for a whole-site crawl.

ONE crawl discovers a site using MANY agents. It does not scrape from the
control plane; it maintains a shared URL frontier in the coordinator's
(in-process) Redis keyspace and hands the fleet *shards* (batches of URLs). Each
shard is an ordinary AutomationTask under a synthetic per-crawl workflow, so:
  - the existing WorkflowQueue processor + capacity manager spread shards across
    the whole fleet (each agent gets a different slice of the site);
  - shard results aggregate through the normal Workflow Data API + lineage dedup;
  - crawl-aware politeness is enforced *by the coordinator* (it never has more
    than `max_concurrent_shards` outstanding against a host), which is the only
    place global rate can be enforced when the agents can't see each other.

Loop (event-driven, driven by shard completion — see on_shard_complete):
  seed frontier (robots + sitemap + homepage)
    → cut shards up to the concurrency cap → dispatch across fleet
    → on each shard complete: store pages, admit newly-discovered in-scope URLs
      (most rejected by the visited set), cut fresh shards, bump counters
    → frontier empty + zero shards in flight → reconcile → done.

Admission is atomic: a URL enters the frontier only if `SADD visited` returns 1
AND it passes scope-regex + depth + robots + SSRF/blocklist. Marking visited at
admission (not completion) is what stops two agents being handed the same URL.
The frontier is a SORTED SET scored by relevance to the crawl's intent, so the
page_budget is spent on the pages the operator actually asked for (see
services/crawl_targeting); with no intent the score is shallow-first, which
reproduces a plain breadth-first sweep.

`sweep_crawls()` (called from the scheduler) is the crash-safety net: it reaps
stale shards (requeueing their URLs), rescues crawls stranded in "queued", and
reconciles in-flight counts from real task state before re-pumping.

Single-owner coordinator: no per-owner scoping. The whole DB (and the in-process
Redis keyspace) belongs to the one operator, so shard tasks and the synthetic
workflow are created without any owner column, and there is no billing, plan
ladder, or metered-AI executor in this loop — a self-hosted crawl is free.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlsplit, urljoin, urldefrag

from sqlalchemy import Integer, cast, select, func, update

from database import AsyncSessionLocal
from models.automation_task import AutomationTask
from models.automation_workflow import AutomationWorkflow
from models.crawl_job import CrawlJob
from services import url_policy, robots_guard, crawl_targeting, visual_storage
from services.brand import (
    CRAWL_WORKFLOW_TYPE,
    CRAWL_STEP_TYPE,
    CTX_CRAWL_ID,
    CTX_CRAWL_SHARD,
    CTX_CRAWL_EXTRACT,
    DRAGNET_NAME,
)
from services.monitor_coalescing import normalize_url
from services.run_events import emit_run_event
from utils.redis_client import get_redis

logger = logging.getLogger(__name__)

_KEY_TTL = 24 * 3600  # frontier/visited live at most a day
# How long a crawl may sit in "queued" before sweep_crawls assumes its detached
# seeder was lost (worker restart / dropped task) and re-kicks it. Comfortably
# longer than a healthy _seed takes to flip queued → mapping.
_SEED_RESCUE_AFTER_S = 90
# How long a shard may stay non-terminal ("running"/"assigned") before sweep
# assumes its agent dropped mid-run (or its completion frame was lost) and reaps
# it. Without this, one dead shard pins the crawl's inflight counter forever, so
# the crawl can neither dispatch more shards nor finalize. Generous enough not to
# kill a legitimately slow browser shard (25 URLs × warm render), short enough to
# unblock quickly.
_SHARD_STALE_AFTER_S = int(os.getenv("CRAWL_SHARD_STALE_AFTER_S", "300"))

# BLOCK / RATE-LIMIT recovery. A URL the host REFUSED (429/403/captcha) is not a
# dead link — it's requeued for a DIFFERENT agent/IP, up to this many times before
# we give up (so a hard-blocked URL can't loop forever). When a host blocks an
# agent outright, the crawl ADAPTIVELY backs off: longer per-page politeness delay
# + fewer parallel shards, clamped so it never seizes up entirely.
_MAX_BLOCK_RETRIES = 3
_MAX_BACKOFF_DELAY_MS = 5000

# CROSS-CRAWL PER-HOST COOLDOWN. A healthy host is never throttled — the normal
# dispatch already balances load. But politeness is a property of the HOST, not one
# crawl: when a host BLOCKS an agent, it earns a shared cooldown that makes EVERY
# crawl of that host (not just the one that hit the wall) ease off. Env-tunable.
_HOST_COOLDOWN_S = int(os.getenv("CRAWL_HOST_COOLDOWN_S", "30"))
_HOST_COOLDOWN_MAX_S = 600


def _emit_crawl_run_event(crawl: CrawlJob, event: str = "updated") -> None:
    """Nudge the unified runs feed (Live activity) that this crawl changed
    lifecycle state, so a starting/finishing crawl surfaces instantly instead of
    waiting out the feed's poll. Fire-and-forget and best-effort: realtime is only
    an accelerator over polling, so a missed emit is harmless and must never break
    the crawl path.

    The payload carries the live counters (pages done/discovered/failed, agents
    working, depth) so a subscriber — the crawl detail page's live card — can
    animate progress straight from the push, WITHOUT polling the crawl row."""
    try:
        active = max(0, (crawl.shards_dispatched or 0) - (crawl.shards_done or 0))
        asyncio.create_task(emit_run_event(
            run_type="crawl", row_id=crawl.id,
            status=crawl.status, event=event,
            extra={
                "crawl_id": crawl.id,
                "ai_session_id": crawl.ai_session_id,
                "seed_host": _host(crawl.seed_url) if crawl.seed_url else None,
                "pages_done": crawl.pages_done or 0,
                "pages_discovered": crawl.pages_discovered or 0,
                "pages_failed": crawl.pages_failed or 0,
                "pages_skipped": crawl.pages_skipped or 0,
                "agents_active": active,
                "shards_dispatched": crawl.shards_dispatched or 0,
                "shards_done": crawl.shards_done or 0,
                "current_depth": crawl.current_depth or 0,
                "page_budget": crawl.page_budget or 0,
            },
        ))
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Redis key helpers                                                           #
# --------------------------------------------------------------------------- #
def _k_frontier(cid: int) -> str:
    return f"crawl:{cid}:frontier"


def _k_visited(cid: int) -> str:
    return f"crawl:{cid}:visited"


def _k_inflight(cid: int) -> str:
    return f"crawl:{cid}:inflight"


def _k_shard_progress(cid: int) -> str:
    """Hash of ``task_id -> "<done>:<failed>"`` — what each RUNNING shard has already
    been credited toward the crawl's page counters.

    A shard is the accounting unit: `on_shard_complete` credits the whole batch when
    it returns. That is fine for the HTTP lane (sub-second pages) and useless on the
    browser lane, where 25 URLs is over a minute during which the crawl's counters
    cannot move at all — the run reads as frozen on page 1, which is what operators
    cancel. Agents now report a running tally (`task_progress`), and this hash is what
    makes crediting it IDEMPOTENT: each frame is applied as a delta over what the same
    task was already credited, and the final result subtracts the total so no page is
    ever counted twice."""
    return f"crawl:{cid}:shard_progress"


def _k_host_cooldown(reg: str) -> str:
    """A host is cooling down (recently blocked an agent) — no crawl dispatches to
    it while this key lives."""
    return f"crawl:host:{reg}:cooldown"


# In-process serialization of the per-shard completion DB phase, keyed by crawl.
# Concurrent shard completions must NOT each hold a pooled DB connection while
# contending for the same crawl row — that is self-inflicted pool exhaustion (the
# best-effort hook is skipped → counters under-count, and starved completion
# transactions strand shards in 'running'). Waiting on an asyncio.Lock is free:
# the session (and its connection) is only opened once this shard is actually
# next. The counters themselves are written as ATOMIC in-place increments, so they
# stay correct even across worker processes, which have their own lock maps.
_SHARD_PHASE_LOCKS: dict = {}


def _shard_phase_lock(crawl_id: int) -> asyncio.Lock:
    lock = _SHARD_PHASE_LOCKS.get(crawl_id)
    if lock is None:
        lock = _SHARD_PHASE_LOCKS.setdefault(crawl_id, asyncio.Lock())
    return lock


# --------------------------------------------------------------------------- #
# Scope helpers                                                               #
# --------------------------------------------------------------------------- #
def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except Exception:
        return ""


def _registrable(host: str) -> str:
    """Cheap eTLD+1 (last two labels). Good enough for scoping; multi-part
    public suffixes (co.uk) merely scope slightly wider, never leak off-site
    because the SSRF/blocklist checks still run per URL."""
    labels = (host or "").split(".")
    return ".".join(labels[-2:]) if len(labels) >= 2 else host


def _path_of(url: str) -> str:
    """Path + query for include/exclude matching. The QUERY is included because many sites encode the
    thing that distinguishes a content page from chrome in the query string, NOT the path — e.g. HN
    uses `/item?id=123` for a story and `/news?p=2` for pagination (both share a bare path). Matching
    path-only makes `^/item\\?id=` never match and `^/news$` match every `?p=` page — exactly wrong."""
    try:
        parts = urlsplit(url)
        return (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    except Exception:
        return "/"


def _in_domain_scope(crawl: CrawlJob, url: str, seed_host: str, seed_reg: str) -> bool:
    h = _host(url)
    if not h:
        return False
    if not crawl.same_domain:
        return True
    if crawl.allow_subdomains:
        return h == seed_host or h.endswith("." + seed_reg) or h == seed_reg
    return h == seed_host


def _passes_path_filters(crawl: CrawlJob, url: str) -> bool:
    """Apply the crawl's include/exclude path patterns to one URL.

    Runs on the frontier hot path — once per discovered URL — so a
    catastrophically-backtracking pattern here is amplified across the entire
    crawl. Patterns are screened by validate_regex before storage
    (crawl_targeting._valid_regexes), and executed here under the shared hard
    timeout as defence in depth for rows written before that screen existed.
    """
    from security.validation import InputValidator

    path = _path_of(url)
    inc = crawl.include_paths or []
    exc = crawl.exclude_paths or []
    for pat in exc:
        try:
            if InputValidator.safe_regex_search(pat, path, validate=False):
                return False
        except re.error:
            continue
    if inc:
        for pat in inc:
            try:
                if InputValidator.safe_regex_search(pat, path, validate=False):
                    return True
            except re.error:
                continue
        return False
    return True


# --------------------------------------------------------------------------- #
# Admission                                                                   #
# --------------------------------------------------------------------------- #
async def _admit(crawl: CrawlJob, raw_url: str, depth: int, seed_host: str, seed_reg: str,
                 *, anchor_text: str = "", targeting=None) -> bool:
    """Try to add one URL to the frontier. Returns True if newly admitted.

    Atomic dedupe via SADD; then scope + depth + robots + SSRF. A rejected URL
    stays in the visited set so it is never re-evaluated.

    Ranked frontier: the URL's relevance score against the crawl's intent (its
    path + anchor text) becomes its sorted-set score, so `_pump` dispatches the
    most-relevant pages FIRST and the page_budget flows to what the operator asked
    for. With no intent the score is shallow-first — reproducing a plain BFS. A
    below-threshold, off-topic URL is dropped here (it stays in `visited`, so it
    is never reconsidered)."""
    if not raw_url or depth > (crawl.max_depth or 0):
        return False
    url = normalize_url(raw_url)
    if not url.startswith(("http://", "https://")):
        return False

    r = get_redis()
    added = await r.sadd(_k_visited(crawl.id), url)
    if not added:
        return False  # already seen this exact URL

    if not _in_domain_scope(crawl, url, seed_host, seed_reg):
        return False
    # The entry page(s) at depth 0 are the EXPLICIT seed — always admit them so their links can be
    # discovered. Include/exclude PATH filters govern which DISCOVERED links to follow, NOT whether to
    # fetch the entry page (a user who seeds a list page but filters for detail pages must still have
    # the list page crawled, or nothing is ever found).
    if depth > 0 and not _passes_path_filters(crawl, url):
        return False

    # SSRF / blocklist. DNS-vet every newly-discovered URL so a public host that
    # resolves to a private/metadata IP (the classic DNS-alias/rebind trick) cannot
    # enter the frontier. Literal IPs are still screened synchronously inside
    # check_url; the lookup runs at most once per URL (admission is deduped by the
    # SADD visited above), and safe_fetch re-screens + pins at connect time.
    verdict = await url_policy.check_url(url)
    if not verdict.allowed:
        return False

    # Per-URL robots admission (crawl default respects robots). Fail-open inside
    # is_allowed.
    try:
        if not await robots_guard.is_allowed(url, respect_robots=bool(crawl.respect_robots)):
            return False
    except Exception:
        pass

    # Relevance-ranked admission. `targeting` is built once per seeding / shard-
    # completion pass by the caller; fall back to building it here for safety.
    if targeting is None:
        targeting = crawl_targeting.build_targeting(crawl)
    hit = crawl_targeting.include_hit(targeting, url)
    score = crawl_targeting.score_url(targeting, url, anchor_text, depth)
    if not crawl_targeting.passes_threshold(targeting, score, depth, hit):
        return False

    await r.zadd(_k_frontier(crawl.id), {json.dumps({"url": url, "depth": depth}): score})
    await r.expire(_k_frontier(crawl.id), _KEY_TTL)
    await r.expire(_k_visited(crawl.id), _KEY_TTL)
    return True


# --------------------------------------------------------------------------- #
# Seeding (robots + sitemap + homepage)                                       #
# --------------------------------------------------------------------------- #
def _session_cookie_header(auth, url: str):
    """Build a `Cookie:` header for `url` from a persona session, or None.

    DISCOVERY RUNS AUTHENTICATED TOO. Seeding (robots/sitemap/homepage harvest) and
    /crawl/map are control-plane fetches made by the COORDINATOR, not the agent — so
    without this an authenticated crawl still derived its frontier from whatever a
    logged-OUT visitor sees. On a site whose homepage is a login wall that means the
    seed harvest returns the login page's links and the crawl explores nothing.

    Cookies are matched the same way the agent's fetch lanes match them (host suffix
    on a label boundary, path prefix, Secure => https only) so every fetch path agrees
    on scope and none of them leaks a cookie off-site."""
    if not auth:
        return None
    cookies = auth.get("cookies") or []
    if not isinstance(cookies, list) or not cookies:
        return None
    from urllib.parse import urlparse
    try:
        parsed = urlparse(url)
    except Exception:
        return None
    host = (parsed.hostname or "").lower()
    if not host:
        return None
    path = parsed.path or "/"
    pairs = []
    for c in cookies:
        if not isinstance(c, dict):
            continue
        name, value = c.get("name"), c.get("value")
        if not isinstance(name, str) or not isinstance(value, str):
            continue
        domain = str(c.get("domain") or "").lstrip(".").lower()
        if not domain:
            continue
        if not (host == domain or host.endswith("." + domain)):
            continue
        cpath = str(c.get("path") or "/")
        if cpath not in ("", "/") and not path.startswith(cpath):
            continue
        if c.get("secure") and parsed.scheme != "https":
            continue
        pairs.append(f"{name}={value}")
    return "; ".join(pairs) if pairs else None


def _auth_headers_for(auth, url: str) -> dict:
    """`safe_get(headers=...)` kwargs carrying the persona cookies for `url`."""
    cookie = _session_cookie_header(auth, url)
    return {"Cookie": cookie} if cookie else {}


async def _discover_sitemap_urls(seed_url: str, *, auth=None) -> list:
    """Fetch robots.txt Sitemap: directives + /sitemap.xml and return <loc> URLs.
    Best-effort, tightly bounded — a control-plane GET of a text asset only. Never
    raises.

    SSRF: robots.txt, every ``Sitemap:`` directive, and every sitemap-index
    ``<loc>`` are ATTACKER-CONTROLLED (they come from a third-party site), so
    ALL of them are fetched through ``safe_fetch.safe_get`` — which resolves the
    host, screens every resolved IP against private/internal/metadata ranges,
    pins the connection, and refuses redirects into internal space. A hostile
    sitemap can no longer point us at 169.254.169.254 / localhost / RFC1918."""
    from services import safe_fetch

    urls: list = []
    parts = urlsplit(seed_url)
    origin = f"{parts.scheme}://{parts.netloc}"
    candidates: list = []
    try:
        # robots.txt Sitemap: lines
        try:
            rb = await safe_fetch.safe_get(
                f"{origin}/robots.txt", timeout=8.0,
                headers=_auth_headers_for(auth, f"{origin}/robots.txt") or None)
            if rb.status_code == 200:
                for line in rb.text.splitlines():
                    if line.lower().startswith("sitemap:"):
                        candidates.append(line.split(":", 1)[1].strip())
        except Exception:
            pass
        if not candidates:
            candidates.append(f"{origin}/sitemap.xml")

        seen_maps: set = set()
        # One level of sitemap-index expansion.
        for sm in candidates[:5]:
            if sm in seen_maps:
                continue
            seen_maps.add(sm)
            try:
                resp = await safe_fetch.safe_get(
                    sm, timeout=8.0, headers=_auth_headers_for(auth, sm) or None)
                if resp.status_code != 200:
                    continue
                locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", resp.text, re.I)
                # sitemap-index → each <loc> is another sitemap
                if "<sitemapindex" in resp.text.lower():
                    for child in locs[:20]:
                        if child in seen_maps:
                            continue
                        seen_maps.add(child)
                        try:
                            cr = await safe_fetch.safe_get(
                                child, timeout=8.0,
                                headers=_auth_headers_for(auth, child) or None)
                            if cr.status_code == 200:
                                urls.extend(re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", cr.text, re.I))
                        except Exception:
                            continue
                else:
                    urls.extend(locs)
            except Exception:
                continue
    except Exception as e:
        logger.debug(f"[{DRAGNET_NAME}] sitemap discovery failed for {seed_url}: {e}")
    # Dedup preserving order.
    out, seen = [], set()
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


# Anchor + title extraction for the shallow seed harvest (the coordinator has no
# bs4; a regex is enough for a URL sample + a relevance signal).
_ANCHOR_RE = re.compile(
    r'<a\b[^>]*?\bhref\s*=\s*["\']([^"\']+)["\'][^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")


async def _harvest_seed_links(seed_url: str, *, cap: int = 200, auth=None) -> list:
    """One SSRF-vetted control-plane GET of the seed page → absolute links with
    their anchor text. Best-effort, tightly bounded, never raises. Feeds both the
    scope-derivation URL sample and the /crawl/map picker."""
    from services import safe_fetch

    out: list = []
    seen: set = set()
    try:
        resp = await safe_fetch.safe_get(
            seed_url, timeout=8.0, headers=_auth_headers_for(auth, seed_url) or None)
        if resp.status_code != 200:
            return out
        html = resp.text or ""
    except Exception:
        return out

    for m in _ANCHOR_RE.finditer(html):
        href = (m.group(1) or "").strip()
        if not href or href.startswith(("#", "mailto:", "javascript:", "tel:", "data:")):
            continue
        try:
            absu = urldefrag(urljoin(str(seed_url), href))[0]
        except Exception:
            continue
        if not absu.startswith(("http://", "https://")):
            continue
        absu = normalize_url(absu)
        if absu in seen:
            continue
        seen.add(absu)
        text = _TAG_RE.sub(" ", m.group(2) or "")
        text = re.sub(r"\s+", " ", text).strip()
        out.append({"url": absu, "text": text[:120]})
        if len(out) >= cap:
            break
    return out


async def _sample_site_urls(seed_url: str, *, cap: int = 150, auth=None) -> list:
    """A sample of the site's real URLs for scope derivation: sitemap first
    (cheap, broad), topped up with a shallow seed-page harvest when the sitemap
    is thin or absent. Never raises."""
    sample: list = []
    try:
        sample = list(await _discover_sitemap_urls(seed_url, auth=auth))
    except Exception:
        sample = []
    if len(sample) < 40:
        try:
            sample += [l["url"] for l in await _harvest_seed_links(seed_url, cap=120, auth=auth)]
        except Exception:
            pass
    # Dedup preserving order, bounded.
    out, seen = [], set()
    for u in sample:
        if u not in seen:
            seen.add(u)
            out.append(u)
        if len(out) >= cap:
            break
    return out


def _session_is_usable(session) -> bool:
    """Does this persona session carry anything that can actually authenticate?

    Checks EVERY shape a session can arrive in — `cookies`, the Writ camelCase
    `localStorage`/`sessionStorage` maps, captured auth `headers`, the HTTP-lane
    `tokens` store, and Playwright's `origins[]`. The previous check accepted only
    `cookies` or `origins`; a token-auth SPA whose whole session is a localStorage
    JWT (a very common shape here) has NEITHER, so it was reported to the operator
    as an expired login no matter how fresh it was."""
    if not isinstance(session, dict):
        return False
    for key in ("cookies", "origins", "localStorage", "sessionStorage", "headers", "tokens"):
        if session.get(key):
            return True
    return False


async def _ensure_persona_session(db, crawl: CrawlJob) -> tuple:
    """Login-before-crawl guard. A crawl with a persona MUST carry a fresh warm
    session so every shard fetches logged-IN (the persona's login was established
    at link/discover time and captured as session_state). Here we only VERIFY it is
    present + unexpired before fanning out; a stale session would otherwise silently
    crawl the logged-out site and collect garbage.

    Returns (ok, error, session). ok=True → proceed (no persona, or a fresh session
    exists, returned so SEEDING can run authenticated too). ok=False → the caller
    fails the crawl with `error` so the operator re-links the login instead of
    getting a logged-out crawl."""
    if not crawl.persona_id:
        return True, None, None
    try:
        from services.persona_service import PersonaService
        persona = await PersonaService.get_owned(db, crawl.persona_id)
        if not persona or not getattr(persona, "is_active", True):
            return False, ("The login identity for this crawl is missing or inactive. "
                           "Re-link a persona for the site, then start the crawl."), None
        # load_session returns None when absent OR expired (see PersonaService).
        session = PersonaService.load_session(persona)
        if _session_is_usable(session):
            logger.info(f"[{DRAGNET_NAME}] crawl {crawl.id}: authenticated — reusing "
                        f"persona {persona.id} warm session")
            return True, None, session
        return False, ("The login session for this crawl's persona has expired. "
                       "Re-link the login and start the crawl so pages behind the "
                       "login are reachable."), None
    except Exception as e:  # noqa: BLE001 — never crash seeding on the guard
        logger.warning(f"[{DRAGNET_NAME}] persona-session guard failed for crawl {crawl.id}: {e}")
        # Fail OPEN on an unexpected guard error only when we truly can't tell —
        # better to attempt the crawl than to wrongly block it on an internal fault.
        return True, None, None


async def _seed(crawl_id: int) -> None:
    """Seed the frontier from robots + sitemap + the homepage, then pump.

    Runs as a DETACHED task (start_crawl fires it fire-and-forget), so it MUST NOT
    let an exception escape silently — that would strand the crawl in queued/mapping
    forever (it shows as a running crawl that never progresses). Any unexpected
    error fails the crawl instead. sweep_crawls re-kicks a queued crawl whose seeder
    never ran at all (worker restart between start_crawl's commit and this task)."""
    try:
        await _seed_inner(crawl_id)
    except Exception as e:  # noqa: BLE001 — a detached seeder must never strand the crawl
        logger.exception(f"[{DRAGNET_NAME}] seed failed for crawl {crawl_id}: {e}")
        try:
            async with AsyncSessionLocal() as db:
                crawl = await db.get(CrawlJob, crawl_id)
                # Only fail a crawl still pre-fanout (queued/mapping). Once it reaches
                # "crawling" the shards are the source of truth and sweep_crawls owns
                # recovery — don't clobber an in-flight crawl on a late pump error.
                if crawl and crawl.status in ("queued", "mapping"):
                    crawl.status = "failed"
                    crawl.error = f"Crawl seed failed: {e}"
                    crawl.completed_at = datetime.now(timezone.utc)
                    await db.commit()
                    _emit_crawl_run_event(crawl, "ended")
        except Exception:  # noqa: BLE001
            logger.warning(f"[{DRAGNET_NAME}] could not mark crawl {crawl_id} failed after seed error")


async def _seed_inner(crawl_id: int) -> None:
    async with AsyncSessionLocal() as db:
        crawl = await db.get(CrawlJob, crawl_id)
        if not crawl or crawl.status in ("cancelled", "stopping"):
            return

        # Login-before-crawl: verify the persona's session is fresh BEFORE fanning
        # out, so an auth'd crawl never silently runs logged-out. The session comes
        # back so the sitemap/robots discovery below runs SIGNED IN — on a
        # login-walled site an anonymous seed harvests the login page's links and
        # the crawl explores nothing.
        ok, err, auth_session = await _ensure_persona_session(db, crawl)
        if not ok:
            crawl.status = "failed"
            crawl.error = err
            crawl.completed_at = datetime.now(timezone.utc)
            await db.commit()
            _emit_crawl_run_event(crawl, "ended")
            logger.info(f"[{DRAGNET_NAME}] crawl {crawl_id} blocked pre-seed: {err}")
            return

        crawl.status = "mapping"
        crawl.started_at = datetime.now(timezone.utc)
        await db.commit()
        _emit_crawl_run_event(crawl, "started")

        seed_host = _host(crawl.seed_url)
        seed_reg = _registrable(seed_host)
        targeting = crawl_targeting.build_targeting(crawl)

        admitted = 0
        if await _admit(crawl, crawl.seed_url, 0, seed_host, seed_reg, targeting=targeting):
            admitted += 1

        budget = crawl.page_budget or 1000
        picks = crawl.seed_urls or []
        if picks:
            # Map-first: the operator curated the entry set. Seed exactly those (+
            # the seed) at depth 0 and SKIP the broad sitemap expansion — links found
            # while crawling them still fan out normally within scope/depth.
            for u in picks:
                if admitted >= budget:
                    break
                if await _admit(crawl, u, 0, seed_host, seed_reg, targeting=targeting):
                    admitted += 1
            sitemap_urls = []
        else:
            sitemap_urls = await _discover_sitemap_urls(crawl.seed_url, auth=auth_session)
            for u in sitemap_urls:
                if admitted >= budget:
                    break
                if await _admit(crawl, u, 0, seed_host, seed_reg, targeting=targeting):
                    admitted += 1

        crawl.pages_discovered = admitted
        crawl.status = "crawling"
        await db.commit()
        _emit_crawl_run_event(crawl, "updated")
        logger.info(f"[{DRAGNET_NAME}] crawl {crawl_id} seeded {admitted} URLs "
                    f"({len(picks)} picked, {len(sitemap_urls)} from sitemap)")

    await _pump(crawl_id)


# --------------------------------------------------------------------------- #
# Shard cutting / dispatch                                                     #
# --------------------------------------------------------------------------- #
async def _mint_shard_task(db, crawl: CrawlJob, batch: list) -> AutomationTask:
    """Create ONE queued crawl-shard task. Crawl concurrency is bounded by
    max_concurrent_shards, and the task is minted with
    queue_traffic_type='scheduled' (+ a low priority within that class) so the
    existing WorkflowQueue processor dispatches it on borrowed idle slots, never
    starving direct user runs.

    target_id is left NULL (not 0): the coordinator enforces SQLite foreign keys,
    so a shard task is a target-less scheduled workflow run."""
    extract = {
        "mode": crawl.extract_mode,
        "schema": crawl.extract_schema,
        "delay_ms": crawl.delay_ms,
        # Render + document-handling knobs the fleet agent's shard executor honors:
        # render_mode picks HTTP vs warm-browser per page; ocr_mode governs the
        # doc-extract sidecar path for non-HTML docs + DOM-empty renders.
        "render_mode": getattr(crawl, "render_mode", "auto") or "auto",
        "ocr_mode": getattr(crawl, "ocr_mode", "auto") or "auto",
        # Content-selection spec (which page ELEMENTS the markdown keeps).
        "content": getattr(crawl, "content_spec", None),
        # AUTHENTICATED CRAWL BOUNDARY. The persona session rides separately (as
        # config.session_state, resolved at dispatch); this is the registrable domain the
        # agent may replay its un-domained parts to — auth headers and DOM storage carry no
        # domain of their own, so without an anchor a crawl that follows an off-site link
        # would hand another host the operator's bearer token. Cookies are matched by their
        # own domain on top of this.
        "auth_domain": _registrable(_host(crawl.seed_url)) if crawl.persona_id else None,
    }
    # Anti-affinity: if any URL in this batch was requeued after a host BLOCKED a
    # specific agent, steer this shard away from that agent so the retry lands on a
    # different agent/IP (see the queue processor's candidate ordering). Union
    # across the batch.
    _avoid_agents = sorted({
        str(b["avoid_agent"]) for b in batch
        if isinstance(b, dict) and b.get("avoid_agent")
    })
    task = AutomationTask(
        target_id=None,
        workflow_id=crawl.workflow_id,
        trigger_type="crawl",
        trigger_context={
            CTX_CRAWL_ID: crawl.id,
            CTX_CRAWL_SHARD: batch,
            CTX_CRAWL_EXTRACT: extract,
            **({"_avoid_agents": _avoid_agents} if _avoid_agents else {}),
        },
        status="queued",
        queue_priority=100,  # low priority within the scheduled class
        queue_expires_at=datetime.utcnow() + timedelta(hours=2),
        queue_traffic_type="scheduled",
        max_attempts=1,
    )
    db.add(task)
    await db.flush()
    return task


async def _pump(crawl_id: int) -> None:
    """Cut and dispatch as many shards as the concurrency cap + budget allow.
    Finalizes the crawl when the frontier is drained and nothing is in flight."""
    r = get_redis()
    async with AsyncSessionLocal() as db:
        crawl = await db.get(CrawlJob, crawl_id)
        if not crawl or crawl.is_terminal:
            return
        if crawl.status == "stopping":
            # Draining — cut nothing more; finalize once in-flight hits zero.
            inflight = int(await r.get(_k_inflight(crawl_id)) or 0)
            if inflight <= 0:
                await _finalize(db, crawl, "cancelled")
            return

        inflight = int(await r.get(_k_inflight(crawl_id)) or 0)
        slots = max(0, (crawl.max_concurrent_shards or 6) - inflight)

        # CROSS-CRAWL PER-HOST COOLDOWN. A healthy host is NOT throttled — the normal
        # dispatch/load-balancing already spreads work correctly. This only bites
        # when a host is actively REFUSING us: any crawl that gets an agent blocked
        # sets a shared cooldown, and while it lives EVERY crawl of that host pauses
        # (dispatch nothing this cycle; sweep_crawls re-pumps once it lapses) so we
        # don't collectively keep hammering a site that just told us to back off.
        _reg = _registrable(_host(crawl.seed_url))
        if _reg:
            try:
                if await r.get(_k_host_cooldown(_reg)):
                    slots = 0
            except Exception as e:  # noqa: BLE001 — throttle must never wedge the loop
                logger.warning(f"[{DRAGNET_NAME}] host cooldown check skipped for crawl {crawl_id}: {e}")

        shard_size = max(1, crawl.shard_size or 25)
        dispatched = 0
        _depth_cut = 0

        while slots > 0:
            # Budget gate: stop cutting once we've discovered the page ceiling.
            if crawl.pages_discovered >= (crawl.page_budget or 1000) and \
               await r.zcard(_k_frontier(crawl_id)) == 0:
                break
            batch: list = []
            # ZPOPMAX pulls the highest-scoring (most relevant) URLs first, so the
            # page_budget is spent on what the operator asked for. Returns up to
            # shard_size (member, score) pairs, fewer when the frontier is short.
            popped = await r.zpopmax(_k_frontier(crawl_id), shard_size)
            for member, _score in popped:
                try:
                    batch.append(json.loads(member))
                except Exception:
                    continue
            if not batch:
                break
            await _mint_shard_task(db, crawl, batch)
            await r.incr(_k_inflight(crawl_id))
            await r.expire(_k_inflight(crawl_id), _KEY_TTL)
            _depth_cut = max(_depth_cut, max(b.get("depth", 0) for b in batch))
            slots -= 1
            dispatched += 1

        if dispatched:
            # ATOMIC increments — pumps run concurrently (every shard completion
            # re-pumps), so an ORM read-modify-write here loses updates and
            # shards_dispatched drifts below the real task count. Same principle as
            # on_shard_complete.
            await db.execute(
                update(CrawlJob)
                .where(CrawlJob.id == crawl.id)
                .values(
                    shards_dispatched=CrawlJob.shards_dispatched + dispatched,
                    current_depth=func.max(CrawlJob.current_depth, _depth_cut),
                )
            )

        # Convergence: nothing queued, nothing running → done.
        inflight = int(await r.get(_k_inflight(crawl_id)) or 0)
        frontier_len = await r.zcard(_k_frontier(crawl_id))
        if dispatched == 0 and inflight <= 0 and frontier_len == 0:
            await _finalize(db, crawl, "completed")
        else:
            await db.commit()
        if dispatched:
            logger.info(f"[{DRAGNET_NAME}] crawl {crawl_id} cut {dispatched} shard(s); "
                        f"frontier={frontier_len} inflight={inflight}")


# --------------------------------------------------------------------------- #
# End-of-crawl reconciliation                                                  #
# --------------------------------------------------------------------------- #
_RECON_ROW_CAP = 500_000  # bound the reconcile working set (huge-crawl safety)


def _record_dedup_key(row: dict) -> str:
    """Stable identity for a single extracted record, so two shards that scraped the
    same page collapse to ONE reconciled record. Volatile fields (fetched_at) are
    excluded. A markdown page = one row keyed by its URL+content; a schema record
    keeps its own identity (many per page), so distinct records are NOT merged."""
    if not isinstance(row, dict):
        return f"raw:{hash(str(row))}"
    url = row.get("url") or row.get("_source_url") or ""
    try:
        url = normalize_url(url) if url else ""
    except Exception:
        pass
    stable = {k: v for k, v in row.items() if k not in ("fetched_at",)}
    try:
        body = json.dumps(stable, sort_keys=True, default=str)
    except Exception:
        body = str(stable)
    # Content-addressing only — this is a dedup key for records already fetched,
    # never a signature or an integrity check, so collision resistance is not a
    # security property here. usedforsecurity=False says so explicitly, which is
    # also what keeps the build green on FIPS-mode interpreters (where the flag
    # is what allows SHA-1 at all) and stops SAST reporting it every run.
    return f"{url}|{hashlib.sha1(body.encode('utf-8', 'ignore'), usedforsecurity=False).hexdigest()}"


async def _reconcile(db, crawl: CrawlJob) -> None:
    """End-of-crawl reconciliation: every shard owns a SLICE of the dataset, so at
    finalize we collapse them into ONE canonical count — dedup each shard's records
    by identity (URL + content) across the WHOLE crawl. The deduped rows stay
    readable via the Workflow Data API lineage engine; here we record the
    authoritative totals the UI/API report. Best-effort — never blocks finalize."""
    if not crawl.workflow_id:
        return
    seen: set = set()
    total_rows = 0
    capped = False
    last_id = 0
    try:
        while True:
            batch = (await db.execute(
                select(AutomationTask.id, AutomationTask.result_data)
                .where(AutomationTask.workflow_id == crawl.workflow_id)
                .where(AutomationTask.id > last_id)
                .order_by(AutomationTask.id.asc())
                .limit(500)
            )).all()
            if not batch:
                break
            for tid, rd in batch:
                last_id = tid
                rows = (rd or {}).get("extracted_data") if isinstance(rd, dict) else None
                if not isinstance(rows, list):
                    continue
                for row in rows:
                    total_rows += 1
                    if len(seen) < _RECON_ROW_CAP:
                        seen.add(_record_dedup_key(row))
                    else:
                        capped = True
        unique = len(seen)
        crawl.records_total = unique
        crawl.duplicates_removed = max(0, total_rows - unique) if not capped else None
        crawl.reconciled_at = datetime.now(timezone.utc)
        logger.info(f"[{DRAGNET_NAME}] crawl {crawl.id} reconciled: "
                    f"{unique} unique / {total_rows} rows"
                    + (" (capped)" if capped else ""))
    except Exception as e:  # noqa: BLE001 — reconciliation must never block finalize
        logger.warning(f"[{DRAGNET_NAME}] reconcile failed for crawl {crawl.id}: {e}")


async def _emit_crawl_event(crawl_id: int, event_type: str) -> None:
    """Fire a crawl lifecycle event into the automation trigger system on its OWN
    session, fire-and-forget. This is what lets automations REACT to a crawl —
    'crawl finished -> notify / run a workflow over the collected pages / chain'.
    Never raises into the crawl coordinator."""
    try:
        from services.unified_trigger_service import get_unified_trigger_service
        async with AsyncSessionLocal() as db:
            crawl = await db.get(CrawlJob, crawl_id)
            if not crawl:
                return
            svc = get_unified_trigger_service(db)
            await svc.process_crawl_event(event_type, crawl)
    except Exception as e:
        logger.warning(f"[{DRAGNET_NAME}] emit {event_type} for crawl {crawl_id} failed: {e}")


async def _finalize(db, crawl: CrawlJob, status: str) -> None:
    crawl.status = status
    crawl.completed_at = datetime.now(timezone.utc)
    # Reconcile the fragmented per-shard datasets into one canonical count before
    # we mark the crawl done (so the terminal row already carries records_total).
    await _reconcile(db, crawl)
    await db.commit()
    # Drop the crawl out of the Live-activity feed the instant it converges.
    _emit_crawl_run_event(crawl, "ended")
    r = get_redis()
    for k in (_k_frontier(crawl.id), _k_visited(crawl.id), _k_inflight(crawl.id),
              _k_shard_progress(crawl.id)):
        try:
            await r.delete(k)
        except Exception:
            pass
    # Drop this crawl's in-process shard-phase lock; late waiters keep their own
    # reference to the same Lock object, so popping is safe.
    _SHARD_PHASE_LOCKS.pop(int(crawl.id), None)
    logger.info(f"[{DRAGNET_NAME}] crawl {crawl.id} → {status} "
                f"(pages_done={crawl.pages_done}, failed={crawl.pages_failed}, "
                f"records={crawl.records_total})")
    # Fire the lifecycle event so downstream automations can react. An operator
    # cancel is deliberate and fires nothing; convergence fires completed, a dead
    # crawl fires failed. Detached task → never blocks/raises into finalize.
    _evt = {"completed": "crawl_completed", "failed": "crawl_failed"}.get(status)
    if _evt:
        asyncio.create_task(_emit_crawl_event(crawl.id, _evt))


# --------------------------------------------------------------------------- #
# Completion hook (crawl-native THIN funnel + the shared post-shard advance)    #
# --------------------------------------------------------------------------- #
def attach_page_thumbnails(crawl_id, result_data: Optional[dict]) -> None:
    """Move a shard's page thumbnails from inline base64 (wire transport) into
    storage, rewriting each page/row to a served proxy PATH.

    The fleet agent ships a light JPEG thumbnail as ``screenshot_b64`` on each page
    meta + extracted-data row (browser-rendered pages only). We store it under a
    crawl-namespaced, unguessable key and replace it with ``/crawl/{id}/screenshot/
    {token}`` — an authenticated same-origin path the results UI loads via
    <AuthImage>. The base64 is DROPPED from the row so it never lands in the crawl's
    persisted result_data (kept lean, à la visual snapshots).

    Mutates ``result_data`` in place (the same object assigned to
    ``task.result_data``), so the caller's commit persists the rewrite. Blocking
    (sync storage writes) — call via ``asyncio.to_thread`` from the async path.
    Identical thumbnails (page meta + its rows share one) are stored once.
    Best-effort: a page whose thumbnail can't be stored simply loses it (the
    favicon still shows)."""
    if not isinstance(result_data, dict) or not crawl_id:
        return
    minted: dict = {}  # screenshot_b64 -> served path (or None when storage failed)

    def _rewrite(obj) -> None:
        if not isinstance(obj, dict):
            return
        b64 = obj.pop("screenshot_b64", None)
        if not b64:
            return
        if b64 not in minted:
            token = secrets.token_urlsafe(16)
            ok = visual_storage.store_crawl_thumbnail_b64(b64, crawl_id, token)
            minted[b64] = f"/crawl/{crawl_id}/screenshot/{token}" if ok else None
        path = minted[b64]
        if path:
            obj["screenshot"] = path

    for p in (result_data.get("pages") or []):
        _rewrite(p)
    for row in (result_data.get("extracted_data") or []):
        _rewrite(row)


async def complete_shard_task(
    task_id: int,
    crawl_id: int,
    *,
    success: bool,
    result_data: Optional[dict] = None,
    error: Optional[str] = None,
    reporter_agent: Optional[str] = None,
) -> bool:
    """Crawl-NATIVE shard completion — the THIN path.

    A shard completion is a high-frequency fan-in event (a whole wave of them
    lands within the same second), not a workflow run. Routing it through the
    generic workflow-completion path drags each one through the heavyweight
    ceremony (attestation, trigger dispatch, shared workflow-row counters) in one
    LONG transaction per shard — a converging wave holds that many pooled
    connections at once, starves the pool, and the starved completions are LOST
    (shards stuck in 'running' until the reaper). Crawl lifecycle events fire at
    CRAWL level, so none of that ceremony applies.

    The terminal stamp here is ONE atomic claim UPDATE:
      - idempotent:  WHERE status is non-terminal → a stream redelivery or a
        late duplicate frame is a no-op (returns False, nothing double-counted)
      - authorized:  WHERE executor IS NULL (adopt the authenticated reporter —
        covers the write-after-send race where the frame beats the dispatch's
        assignment commit) OR executor = reporter (anti-forgery)
    No read-modify-write, no row-lock waits, milliseconds of connection hold.
    Then the crawl advances through the normal on_shard_complete hook.
    Returns True iff THIS call claimed the completion.
    """
    # Offload inline page thumbnails to storage BEFORE persisting, so the base64
    # never lands in the task row (blocking writes → thread). Mutates result_data
    # in place; best-effort.
    if result_data:
        try:
            await asyncio.to_thread(attach_page_thumbnails, crawl_id, result_data)
        except Exception as th_e:  # noqa: BLE001
            logger.warning(f"[{DRAGNET_NAME}] shard {task_id}: thumbnail offload failed: {th_e}")

    from sqlalchemy import or_
    values: dict = {
        "status": "success" if success else "failed",
        "success": bool(success),
        "completed_at": datetime.now(timezone.utc),
        "error_message": (str(error)[:2000] if error else None),
    }
    if result_data is not None:
        values["result_data"] = result_data
    stmt = (
        update(AutomationTask)
        .where(AutomationTask.id == int(task_id))
        .where(AutomationTask.status.notin_(("success", "failed", "timeout", "cancelled")))
        .values(**values)
        .returning(AutomationTask.trigger_context)
    )
    if reporter_agent:
        stmt = stmt.where(or_(AutomationTask.executor_agent_id.is_(None),
                              AutomationTask.executor_agent_id == str(reporter_agent)))
        stmt = stmt.values(executor_agent_id=func.coalesce(
            AutomationTask.executor_agent_id, str(reporter_agent)))
    async with AsyncSessionLocal() as db:
        claimed = (await db.execute(stmt)).first()
        await db.commit()

    if claimed is None:
        logger.info(f"[{DRAGNET_NAME}] shard task {task_id}: completion not claimed "
                    f"(already terminal, or reporter {reporter_agent!r} is not the "
                    f"recorded executor) — no-op")
        return False

    (tc,) = claimed

    # FREE THE CAPACITY SLOT. Dispatch reserves one against the executing agent
    # (reserve_agent_slot, from the direct-run pick or the queue drainer), and the
    # ONLY release lives in _process_task_completion — which this THIN path
    # deliberately bypasses. So every shard that completed here leaked its slot:
    # the agent's coordinator-side `active_sessions` only ever climbed, it was
    # reported busy forever, and once the count reached its max NOTHING could be
    # dispatched to it again — crawls, workflows and monitor checks alike.
    # Idempotent and keyed by task id, so it is safe here even though the reaper
    # below may also release the same id.
    try:
        from routers.user_recorder_ws import release_agent_slot
        release_agent_slot(task_id)
    except Exception:  # noqa: BLE001 — never let capacity bookkeeping break completion
        pass

    # Realtime: clear this shard from the runs feed instantly. The crawl-level
    # live card updates flow from on_shard_complete's _emit_crawl_run_event.
    try:
        await emit_run_event(run_type="workflow", row_id=int(task_id),
                             status="success" if success else "failed", event="ended")
    except Exception:
        pass

    # on_shard_complete only reads .id and .trigger_context off the task — hand it
    # a lightweight shim instead of re-SELECTing the (large result_data) row.
    # Carry the reporting agent so the block-requeue can steer blocked URLs AWAY
    # from the agent the host just refused.
    from types import SimpleNamespace
    shim = SimpleNamespace(id=int(task_id), trigger_context=tc or {},
                           executor_agent_id=reporter_agent)
    await on_shard_complete(shim, result_data)
    return True


def _parse_credited(raw) -> tuple[int, int]:
    """`"<done>:<failed>"` from the shard-progress hash → `(done, failed)`.

    Anything unreadable counts as nothing credited yet: under-counting here just
    re-credits pages the completion path is about to count anyway, while a wrong
    guess the other way would silently swallow them.
    """
    if not raw:
        return 0, 0
    try:
        text = raw.decode() if isinstance(raw, (bytes, bytearray)) else str(raw)
        done, _, failed = text.partition(":")
        return max(0, int(done or 0)), max(0, int(failed or 0))
    except Exception:
        return 0, 0


async def on_shard_progress(task: AutomationTask, done: int, failed: int) -> None:
    """A crawl shard reported its RUNNING page tally. Advance the crawl's counters
    by whatever that adds over what this task was already credited.

    Only the delta is applied, and the completion path subtracts the same total, so
    the shard's pages are counted exactly once however many frames arrive (or don't
    — an agent that never reports still gets its full credit at completion, which is
    what keeps this independently deployable from the agent).

    Best-effort; never raises into the WS handler."""
    ctx = task.trigger_context or {}
    crawl_id = ctx.get(CTX_CRAWL_ID)
    if not crawl_id:
        return
    done = max(0, int(done or 0))
    failed = max(0, int(failed or 0))
    try:
        r = get_redis()
        key = _k_shard_progress(int(crawl_id))
        field = str(task.id)
        # Same lock the completion path holds: a progress frame and the shard's own
        # result must not interleave between reading the credited total and writing
        # the new one, or the overlap is credited twice.
        async with _shard_phase_lock(int(crawl_id)), AsyncSessionLocal() as db:
            crawl = await db.get(CrawlJob, crawl_id)
            # A cancelled/finished crawl must not be resurrected by a late frame from
            # a shard still running on its agent.
            if not crawl or crawl.is_terminal:
                return
            prev_done, prev_failed = _parse_credited(await r.hget(key, field))
            d_done = max(0, done - prev_done)
            d_failed = max(0, failed - prev_failed)
            if not (d_done or d_failed):
                return
            await r.hset(key, field, f"{done}:{failed}")
            await r.expire(key, _KEY_TTL)
            fresh = (await db.execute(
                update(CrawlJob)
                .where(CrawlJob.id == crawl.id)
                .values(
                    pages_done=CrawlJob.pages_done + d_done,
                    pages_failed=CrawlJob.pages_failed + d_failed,
                )
                .returning(CrawlJob.pages_done, CrawlJob.pages_failed)
            )).first()
            await db.commit()
            if fresh:
                crawl.pages_done, crawl.pages_failed = fresh
            # Push the fresh counters at any live subscriber — this is the whole
            # point: the detail card animates mid-shard instead of after it.
            _emit_crawl_run_event(crawl, "updated")
    except Exception as e:
        logger.debug("crawl shard progress ignored for task %s: %s", task.id, e)


async def on_shard_complete(task: AutomationTask, result_data: Optional[dict]) -> None:
    """A crawl-shard task finished. Store its pages' effect on counters, admit
    the URLs it discovered, and pump fresh shards. Uses its OWN db session so it
    never entangles the task-completion transaction it is called from.
    Best-effort; never raises into the completion path."""
    ctx = task.trigger_context or {}
    crawl_id = ctx.get(CTX_CRAWL_ID)
    if not crawl_id:
        return
    try:
        r = get_redis()
        # Decrement in-flight FIRST so _pump sees the freed slot / convergence.
        try:
            await r.decr(_k_inflight(crawl_id))
        except Exception:
            pass

        # Serialize IN-PROCESS (free wait — see _SHARD_PHASE_LOCKS) so concurrent
        # shard hooks don't each hold a pooled connection. The counters themselves
        # are written as ATOMIC in-place increments at the end (never ORM
        # read-modify-write), so they are correct under any concurrency without
        # row locks.
        async with _shard_phase_lock(int(crawl_id)), AsyncSessionLocal() as db:
            crawl = await db.get(CrawlJob, crawl_id)
            if not crawl or crawl.is_terminal:
                return

            shard = ctx.get(CTX_CRAWL_SHARD) or []
            shard_depth = max((b.get("depth", 0) for b in shard), default=0)
            seed_host = _host(crawl.seed_url)
            seed_reg = _registrable(seed_host)

            rd = result_data or {}
            pages = rd.get("pages") or []
            failed = rd.get("failed") or []
            # The wire contract carries anchor text for relevance ranking
            # (discovered_links); fall back to the plain URL list from older agents
            # (anchor "" → path-only scoring), so agent↔coordinator stay
            # independently deployable.
            links = rd.get("discovered_links")
            if links is None:
                links = [{"url": u, "text": ""} for u in (rd.get("discovered_urls") or [])]

            ok_pages = [p for p in pages if (p or {}).get("status") in (None, "ok", 200, "200")]
            # Deltas only — the shared-row counters are applied as ONE atomic
            # UPDATE below, after the admit loop knows `newly`.
            _d_done = len(ok_pages)
            _d_failed = len(failed) + (len(pages) - len(ok_pages))
            # Subtract what this shard's live `task_progress` frames already credited.
            # The result stays AUTHORITATIVE (it is the full batch); progress only ever
            # advanced the counters toward it, so the remainder is what is still owed.
            # Clamped at zero: an agent that over-reported must not walk the counter
            # backwards. The field is dropped either way — the shard is over.
            try:
                _credited_done, _credited_failed = _parse_credited(
                    await r.hget(_k_shard_progress(int(crawl_id)), str(task.id))
                )
                await r.hdel(_k_shard_progress(int(crawl_id)), str(task.id))
            except Exception:
                _credited_done, _credited_failed = 0, 0
            _d_done = max(0, _d_done - _credited_done)
            _d_failed = max(0, _d_failed - _credited_failed)

            # Admit newly-discovered in-scope URLs one level deeper, ranked by
            # relevance to the crawl's intent (anchor text + path). Most are
            # rejected by the visited set — that is the point.
            newly = 0
            if crawl.pages_discovered < (crawl.page_budget or 1000):
                targeting = crawl_targeting.build_targeting(crawl)
                for item in links[:5000]:
                    if crawl.pages_discovered + newly >= (crawl.page_budget or 1000):
                        break
                    u = item.get("url") if isinstance(item, dict) else item
                    anchor = item.get("text", "") if isinstance(item, dict) else ""
                    if not u:
                        continue
                    if await _admit(crawl, u, shard_depth + 1, seed_host, seed_reg,
                                    anchor_text=anchor, targeting=targeting):
                        newly += 1
            # ATOMIC in-place increments — the ONLY way the shared crawl-row
            # counters are ever advanced. Two shards finishing together can't
            # lose an update (no read-modify-write) and can't deadlock (single
            # short UPDATE).
            fresh = (await db.execute(
                update(CrawlJob)
                .where(CrawlJob.id == crawl.id)
                .values(
                    pages_done=CrawlJob.pages_done + _d_done,
                    pages_failed=CrawlJob.pages_failed + _d_failed,
                    shards_done=CrawlJob.shards_done + 1,
                    pages_discovered=CrawlJob.pages_discovered + newly,
                )
                .returning(CrawlJob.pages_done, CrawlJob.pages_failed,
                           CrawlJob.shards_done, CrawlJob.pages_discovered)
            )).first()
            await db.commit()
            if fresh:
                # Refresh the in-memory row with the authoritative post-increment
                # values purely for the event emit below (session closes after).
                (crawl.pages_done, crawl.pages_failed,
                 crawl.shards_done, crawl.pages_discovered) = fresh

            # Stream this shard's fresh counters to any live subscriber (the crawl
            # detail card animates straight from the push).
            _emit_crawl_run_event(crawl, "updated")

            # BLOCK / RATE-LIMIT RECOVERY. URLs the host REFUSED (429/403/captcha)
            # are not dead links — requeue them (bypassing the visited dedup, like
            # the stale-shard reaper) so a DIFFERENT agent/IP retries them, carrying
            # `avoid_agent` (the agent just refused) + a per-URL attempt count so a
            # persistently-blocked URL is finally dropped instead of looping.
            blocked = rd.get("blocked") or []
            if blocked and crawl.status not in ("stopping", "cancelled") and not crawl.is_terminal:
                _blocked_agent = getattr(task, "executor_agent_id", None)
                # The retry budget is the COORDINATOR's bookkeeping — it lives on the
                # shard entry we minted, NOT on the agent's `blocked` report, which
                # does not echo it back. Reading it off the agent pinned `attempts` at
                # 0 forever, so the give-up test below never fired: a host that 403s
                # every URL had the SAME urls requeued by every shard, the frontier
                # never drained, convergence (frontier_len == 0) never ran, and the
                # crawl sat in "crawling" for good while the host-cooldown held
                # dispatch at zero. An untrusted agent must never be the source of
                # truth for a retry counter — omitting it loops us indefinitely.
                _attempts_by_url = {
                    b["url"]: int(b.get("block_attempts", 0) or 0)
                    for b in shard if isinstance(b, dict) and b.get("url")
                }
                _depth_by_url = {
                    b["url"]: int(b.get("depth", 0) or 0)
                    for b in shard if isinstance(b, dict) and b.get("url")
                }
                requeued = 0
                exhausted = 0
                for b in blocked:
                    bu = (b.get("url") if isinstance(b, dict) else b)
                    if not bu:
                        continue
                    # Trust whichever count is HIGHER so a stale/echoing agent can
                    # only ever shorten the retry budget, never extend it.
                    attempts = max(
                        _attempts_by_url.get(bu, 0),
                        int((b.get("block_attempts", 0) if isinstance(b, dict) else 0) or 0),
                    )
                    if attempts >= _MAX_BLOCK_RETRIES:
                        exhausted += 1
                        continue  # give up — the site keeps refusing this URL
                    entry = {"url": bu,
                             "depth": int((b.get("depth") if isinstance(b, dict) else None)
                                          or _depth_by_url.get(bu, 0) or 0),
                             "block_attempts": attempts + 1}
                    if _blocked_agent:
                        entry["avoid_agent"] = str(_blocked_agent)
                    try:
                        await r.zadd(_k_frontier(crawl_id), {json.dumps(entry): 0.0})
                        requeued += 1
                    except Exception:
                        pass
                if requeued:
                    await r.expire(_k_frontier(crawl_id), _KEY_TTL)
                if requeued or exhausted:
                    logger.info(
                        f"[{DRAGNET_NAME}] crawl {crawl_id}: {requeued} blocked URL(s) "
                        f"requeued for redispatch, {exhausted} gave up after "
                        f"{_MAX_BLOCK_RETRIES} attempts (avoid={_blocked_agent})")

                # ADAPTIVE BACK-OFF: the host is refusing us. Ease off atomically —
                # longer per-page politeness delay + fewer parallel shards — so the
                # crawl slows instead of hammering the wall. Clamped so it can't seize
                # up. (delay_ms * 3 / 2 = +50%.) SQLite's `/` is float division, so the
                # result is CAST back to INTEGER (truncating, like the cloud's integer
                # arithmetic) — without it the column would silently hold a float.
                # func.min/func.max here are SQLite's SCALAR min()/max(), not the
                # aggregates: two arguments = clamp, which is exactly the intent.
                if rd.get("agent_blocked") or len(blocked) * 2 >= max(2, len(shard)):
                    await db.execute(
                        update(CrawlJob).where(CrawlJob.id == crawl.id).values(
                            delay_ms=func.min(
                                cast(CrawlJob.delay_ms * 3 / 2 + 250, Integer),
                                _MAX_BACKOFF_DELAY_MS),
                            max_concurrent_shards=func.max(
                                1, CrawlJob.max_concurrent_shards - 1),
                        )
                    )
                    await db.commit()
                    # CROSS-CRAWL cooldown: park EVERY crawl of this host (not just
                    # this one) for a spell so they don't collectively keep hammering
                    # a site that's actively refusing us. Honors Retry-After, clamped.
                    if seed_reg:
                        _cd = min(max(int(rd.get("retry_after") or 0), _HOST_COOLDOWN_S),
                                  _HOST_COOLDOWN_MAX_S)
                        try:
                            await r.set(_k_host_cooldown(seed_reg), "1", ex=_cd)
                        except Exception:
                            pass
                    logger.info(
                        f"[{DRAGNET_NAME}] crawl {crawl_id}: host blocking — backed off "
                        f"(agent={_blocked_agent}, host={seed_reg} "
                        f"cooldown={_HOST_COOLDOWN_S}s+)")
    except Exception as e:
        logger.warning(f"[{DRAGNET_NAME}] on_shard_complete failed for task {task.id}: {e}")
    # Pump outside the try so a fresh session drives the next wave / finalize.
    # Same in-process lock: N shards finishing together must not each open a
    # pump session concurrently — one pump sees the freed slots of all of them.
    try:
        async with _shard_phase_lock(int(crawl_id)):
            await _pump(crawl_id)
    except Exception as e:
        logger.warning(f"[{DRAGNET_NAME}] pump after shard {task.id} failed: {e}")


# --------------------------------------------------------------------------- #
# Public: start / cancel                                                       #
# --------------------------------------------------------------------------- #
async def _mint_crawl_workflow(db, crawl: CrawlJob) -> AutomationWorkflow:
    """Mint the synthetic per-crawl workflow shard results aggregate under.
    A single `crawl_batch` step is the marker the agent special-cases; the actual
    URL batch + extraction spec ride in each shard's trigger_context."""
    wf = AutomationWorkflow(
        name=crawl.name,
        workflow_type=CRAWL_WORKFLOW_TYPE,
        steps=[{
            "id": "1",
            "type": CRAWL_STEP_TYPE,
            "config": {
                "extract_mode": crawl.extract_mode,
                "delay_ms": crawl.delay_ms,
            },
        }],
        form_data={},
        credentials_encrypted=None,
        default_persona_id=crawl.persona_id,
    )
    db.add(wf)
    await db.flush()
    return wf


async def start_crawl(
    db,
    *,
    seed_url: str,
    name: Optional[str] = None,
    extract_mode: str = "markdown",
    extract_schema: Optional[dict] = None,
    content_spec: Optional[dict] = None,
    render_mode: str = "auto",
    ocr_mode: str = "auto",
    persona_id: Optional[int] = None,
    intent: Optional[str] = None,
    seed_urls: Optional[list] = None,
    relevance_threshold: float = 0.0,
    include_paths: Optional[list] = None,
    exclude_paths: Optional[list] = None,
    max_depth: Optional[int] = None,
    page_budget: int = 1000,
    max_concurrent_shards: Optional[int] = None,
    shard_size: int = 25,
    delay_ms: int = 250,
    respect_robots: bool = True,
    same_domain: bool = True,
    allow_subdomains: bool = True,
    ai_session_id: Optional[int] = None,
) -> CrawlJob:
    """Validate the seed, mint the synthetic workflow + CrawlJob, and kick off
    seeding in the background so the caller returns immediately."""
    from services import domain_guard
    from fastapi import HTTPException

    seed_url = (seed_url or "").strip()
    if not seed_url.startswith(("http://", "https://")):
        seed_url = "https://" + seed_url

    await domain_guard.ensure_loaded(db)
    # DNS-resolving check here (creation path, once per crawl — not a hot path) so a
    # public seed hostname that only resolves to a private/internal IP is refused up
    # front, not just literal-IP seeds.
    verdict = await url_policy.check_url(seed_url)
    if not verdict.allowed:
        raise HTTPException(400, verdict.message or "Seed URL is not allowed.")

    # PERSONA OWNERSHIP + USABILITY, validated at CREATION. Without this a bad
    # persona id was accepted, queued, and only then died in the seeder with
    # "missing or inactive". Refusing here turns that into an immediate, actionable
    # error, and gives the scope derivation below a session to fetch with.
    persona_session = None
    if persona_id:
        from services.persona_service import PersonaService
        _persona = await PersonaService.get_owned(db, int(persona_id))
        if not _persona:
            raise HTTPException(404, "persona_id does not reference one of your personas")
        if not getattr(_persona, "is_active", True):
            raise HTTPException(
                409,
                "That persona is inactive. Re-activate it (or pick another) to crawl "
                "pages behind its login.",
            )
        persona_session = PersonaService.load_session(_persona)
        if not _session_is_usable(persona_session):
            raise HTTPException(
                422,
                "That persona has no live login session. Sign in once with the persona "
                "(this captures the session the crawl replays), then start the crawl.",
            )

    # Concurrency default. There is no plan ladder self-hosted, so the operator's
    # OWN fleet size is the real ceiling — and the WorkflowQueue processor already
    # gates dispatch on live agent capacity. Keep a conservative per-crawl default
    # and let the caller raise it.
    if max_concurrent_shards is None:
        max_concurrent_shards = 6

    # Targeting: when the operator gave a plain-English GOAL and supplied NO explicit
    # scope, translate the goal into include/exclude paths + depth off a sample of
    # the site's real URLs. We only derive when nothing was pinned by hand — so the
    # wizard's "Preview scope" (which fills the paths) isn't wastefully re-derived
    # here, and any explicit value is honored as-is (merge_scope). Derivation never
    # raises and degrades to whole-site defaults with no AI configured; the goal
    # still RANKS the frontier either way.
    intent = (intent or "").strip() or None
    derived: Optional[dict] = None
    if intent and include_paths is None and exclude_paths is None and max_depth is None:
        # Sampled with the persona's session when there is one: deriving scope from
        # the logged-OUT view of a login-gated site yields paths for the login flow.
        sample = await _sample_site_urls(seed_url, auth=persona_session)
        derived = await crawl_targeting.derive_scope(intent, sample, seed_url)
    eff = crawl_targeting.merge_scope(
        derived or {},
        include_paths=include_paths,
        exclude_paths=exclude_paths,
        max_depth=max_depth,
    )
    seed_urls = [u for u in (seed_urls or []) if isinstance(u, str) and u.strip()] or None

    host = _host(seed_url)
    crawl = CrawlJob(
        name=name or f"{DRAGNET_NAME}: {host}",
        seed_url=seed_url,
        intent=intent,
        seed_urls=seed_urls,
        relevance_threshold=float(relevance_threshold or 0.0),
        derived_scope=derived,
        include_paths=eff["include_paths"],
        exclude_paths=eff["exclude_paths"],
        max_depth=int(eff["max_depth"]),
        same_domain=bool(same_domain),
        allow_subdomains=bool(allow_subdomains),
        extract_mode=extract_mode if extract_mode in ("markdown", "schema") else "markdown",
        extract_schema=extract_schema,
        content_spec=(content_spec if isinstance(content_spec, dict) else None),
        render_mode=render_mode if render_mode in ("auto", "http", "browser") else "auto",
        ocr_mode=ocr_mode if ocr_mode in ("auto", "off", "force") else "auto",
        persona_id=persona_id,
        respect_robots=bool(respect_robots),
        delay_ms=int(delay_ms),
        max_concurrent_shards=int(max_concurrent_shards),
        shard_size=int(shard_size),
        page_budget=int(page_budget),
        ai_session_id=ai_session_id,
        status="queued",
    )
    db.add(crawl)
    await db.flush()

    wf = await _mint_crawl_workflow(db, crawl)
    crawl.workflow_id = wf.id
    await db.commit()

    # Surface the queued crawl in Live activity immediately (before the seeder
    # flips it to mapping), so a just-launched crawl shows without poll latency.
    _emit_crawl_run_event(crawl, "started")

    # Seed + pump off the request path.
    asyncio.create_task(_seed(crawl.id))
    return crawl


async def cancel_crawl(db, crawl: CrawlJob) -> None:
    """Cancel NOW: stop cutting shards, cancel everything still queued, and mark the
    crawl terminal immediately — the operator sees an instant stop, not a lingering
    'stopping' that waits for in-flight shards to drain.

    A shard already running on an agent can't be killed mid-flight (there's no
    abort-signal channel to the fleet), but it doesn't need to be: it orphan-drains
    and its completion no-ops against the now-terminal crawl (on_shard_complete bails
    on is_terminal). Pages a running shard had already fetched are dropped — that's
    the deliberate trade-off for an instant stop."""
    if crawl.is_terminal:
        return
    r = get_redis()
    for k in (_k_frontier(crawl.id), _k_shard_progress(crawl.id)):
        try:
            await r.delete(k)
        except Exception:
            pass
    # Cancel still-queued shard tasks (never dispatched → cancelling frees a real
    # fleet slot). Running/assigned shards are left to finish on their agent; their
    # completion is discarded once the crawl is terminal.
    rows = (await db.execute(
        select(AutomationTask)
        .where(AutomationTask.workflow_id == crawl.workflow_id)
        .where(AutomationTask.status == "queued")
    )).scalars().all()
    for t in rows:
        if (t.trigger_context or {}).get(CTX_CRAWL_ID) == crawl.id:
            t.status = "cancelled"
            t.error_message = "Crawl cancelled"
            t.completed_at = datetime.utcnow()
    # Finalize immediately instead of draining. _finalize reconciles whatever pages
    # completed shards already contributed, flips the crawl to terminal, drops it
    # from the Live feed, and clears the inflight/frontier redis keys.
    await _finalize(db, crawl, "cancelled")


# --------------------------------------------------------------------------- #
# Crash-safety sweep (called from the scheduler)                              #
# --------------------------------------------------------------------------- #
async def sweep_crawls() -> None:
    """Reconcile non-terminal crawls against real task state. Fixes stalls where
    a shard task died/expired without routing through on_shard_complete: recompute
    the true in-flight count and re-pump.

    Also rescues crawls stranded in "queued": start_crawl fires the seeder as a
    detached task, so a worker restart (or a dropped task) between start_crawl's
    commit and _seed's first status flip leaves the crawl queued forever — a
    "running" crawl that never actually ran. Re-kick the seeder once the crawl has
    been queued longer than a launch is plausibly still in flight."""
    r = get_redis()
    now = datetime.now(timezone.utc)

    # Reap stale shards FIRST: a shard whose agent dropped mid-run (or whose
    # completion frame never arrived) sticks in "running"/"assigned" forever,
    # pinning the crawl's inflight counter so it can neither dispatch more nor
    # finalize. REQUEUE each stale shard's URLs to the frontier (so no pages are
    # lost) THEN fail the task; the per-crawl recount below frees the slot and
    # _pump re-dispatches the requeued URLs on a healthy agent.
    try:
        async with AsyncSessionLocal() as db:
            stale = (await db.execute(
                select(AutomationTask.id, AutomationTask.trigger_context)
                .where(AutomationTask.trigger_type == "crawl")
                .where(AutomationTask.status.in_(("assigned", "running")))
                .where(AutomationTask.created_at
                       < now - timedelta(seconds=_SHARD_STALE_AFTER_S))
                .limit(1000)
            )).all()
            if stale:
                requeued = 0
                for _tid, ctx in stale:
                    ctx = ctx or {}
                    cid = ctx.get(CTX_CRAWL_ID)
                    shard = ctx.get(CTX_CRAWL_SHARD) or []
                    # Direct zadd (bypasses the visited-set dedup in _admit — these
                    # URLs were already admitted once). Score 0 = crawl next.
                    mapping = {json.dumps({"url": b["url"], "depth": b.get("depth", 0)}): 0.0
                               for b in shard if isinstance(b, dict) and b.get("url")}
                    if cid and mapping:
                        try:
                            await r.zadd(_k_frontier(cid), mapping)
                            await r.expire(_k_frontier(cid), _KEY_TTL)
                            requeued += len(mapping)
                        except Exception:
                            pass
                await db.execute(
                    update(AutomationTask)
                    .where(AutomationTask.id.in_([tid for tid, _ in stale]))
                    .values(status="failed",
                            error_message="shard reaped by sweep; urls requeued"))
                await db.commit()
                # Same leak as the thin completion path: this stamps tasks terminal
                # with a direct UPDATE, so nothing releases the capacity slot the
                # dispatcher reserved. A reaped shard is precisely the case where the
                # agent is already wedged, so leaving its slot held is how a stuck
                # crawl permanently shrinks the fleet. Idempotent per task id.
                try:
                    from routers.user_recorder_ws import release_agent_slot
                    for _tid, _ in stale:
                        release_agent_slot(_tid)
                except Exception:  # noqa: BLE001 — bookkeeping must not break the sweep
                    pass
                logger.warning(
                    f"[{DRAGNET_NAME}] reaped {len(stale)} stale crawl shard(s) "
                    f"(> {_SHARD_STALE_AFTER_S}s), requeued {requeued} url(s)")
    except Exception as e:
        logger.warning(f"[{DRAGNET_NAME}] stale-shard reap failed: {e}")

    async with AsyncSessionLocal() as db:
        crawls = (await db.execute(
            select(CrawlJob).where(CrawlJob.status.in_(("queued", "mapping", "crawling", "stopping")))
        )).scalars().all()
    for crawl in crawls:
        try:
            if crawl.status == "queued":
                created = crawl.created_at
                if created is not None and created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                # Don't race a just-launched crawl whose seeder is still spinning up.
                if created is None or (now - created) >= timedelta(seconds=_SEED_RESCUE_AFTER_S):
                    logger.warning(f"[{DRAGNET_NAME}] re-seeding stranded queued crawl {crawl.id}")
                    asyncio.create_task(_seed(crawl.id))
                continue
            # True in-flight = shard tasks not yet terminal.
            async with AsyncSessionLocal() as db:
                live = await db.scalar(
                    select(func.count(AutomationTask.id))
                    .where(AutomationTask.workflow_id == crawl.workflow_id)
                    .where(AutomationTask.status.in_(("queued", "pending", "assigned", "running")))
                )
            try:
                await r.set(_k_inflight(crawl.id), int(live or 0), ex=_KEY_TTL)
            except Exception:
                pass
            await _pump(crawl.id)
        except Exception as e:
            logger.warning(f"[{DRAGNET_NAME}] sweep failed for crawl {crawl.id}: {e}")
