"""
Saved crawls: slug minting, config validation, and the FRESHNESS contract.

The single definition of what ``max_age`` means for a crawl on this coordinator,
so the HTTP route and the MCP tool cannot drift into two different answers. It
deliberately reuses the vocabulary already shipped for workflow runs in
``routers/mcp_server`` (``FRESHNESS_ARG``, the ``_cache`` stamp) rather than
inventing a second dialect.

The contract: ``max_age=N`` means "data already collected within the last N
seconds is acceptable; otherwise go get it again". A hit returns the previous
run's data with a ``_cache`` stamp; a miss re-crawls with the saved settings.
``max_age=0`` (or ``Cache-Control: no-cache``) always re-crawls.

Single-owner coordinator: no tenant scoping — every saved crawl belongs to the
one operator, exactly like every CrawlJob does.
"""
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.crawl_definition import CrawlDefinition
from models.crawl_job import CrawlJob

logger = logging.getLogger(__name__)

# Cap an echoed freshness window at 30 days: beyond that "reuse" stops meaning
# "recent" and starts meaning "never crawl again".
MAX_FRESHNESS_SECONDS = 30 * 24 * 3600

_SLUG_STRIP_RE = re.compile(r"[^a-z0-9]+")
_MAX_AGE_RE = re.compile(r"max-age\s*=\s*(\d+)")


def slugify(value: str) -> str:
    base = _SLUG_STRIP_RE.sub("-", (value or "").strip().lower()).strip("-")
    return base[:100]


async def mint_slug(db: AsyncSession, desired: str) -> str:
    """A unique slug. Collisions get a numeric suffix rather than an error —
    two saved crawls of the same site is a reasonable thing to want."""
    base = slugify(desired) or "crawl"
    candidate = base
    for attempt in range(2, 200):
        exists = await db.scalar(
            select(CrawlDefinition.id).where(CrawlDefinition.slug == candidate)
        )
        if not exists:
            return candidate
        candidate = f"{base}-{attempt}"
    return f"{base}-{int(datetime.now(timezone.utc).timestamp())}"


def validate_config(payload: dict) -> dict:
    """Round-trip a saved config through the live StartCrawlRequest model.

    Imported lazily: routers/crawl imports this service, so a module-level
    import of the router would close a cycle.

    Validating on WRITE and on READ is intentional — a blob written by an older
    build must not dispatch a crawl the live endpoint would have rejected.
    """
    from routers.crawl import StartCrawlRequest

    model = StartCrawlRequest(**(payload or {}))
    return model.model_dump(exclude_none=True)


def config_from_crawl(crawl: CrawlJob) -> dict:
    """The settings a crawl actually ran with, as a StartCrawlRequest payload.

    Read from the ROW, not from ``summary()``: the status view deliberately omits
    the politeness/shard/path-filter knobs, so anything reconstructed from it
    would quietly fall back to defaults and produce a saved crawl that behaves
    differently from the one the user pointed at.

    ``derived_scope`` is intentionally NOT copied. It is the audit of what was
    derived from ``intent`` on that run; carrying it forward would freeze one
    run's interpretation into every future run, when re-deriving from the same
    intent is what a user re-running a crawl expects.
    """
    return {
        "url": crawl.seed_url,
        "name": crawl.name,
        "executor": crawl.executor,
        "extract_mode": crawl.extract_mode,
        "extract_schema": crawl.extract_schema,
        "extract_prompt": crawl.extract_prompt,
        "content_spec": crawl.content_spec,
        "render_mode": crawl.render_mode,
        "ocr_mode": crawl.ocr_mode,
        "persona_id": crawl.persona_id,
        "intent": crawl.intent,
        "seed_urls": crawl.seed_urls,
        "relevance_threshold": crawl.relevance_threshold or 0.0,
        "include_paths": crawl.include_paths,
        "exclude_paths": crawl.exclude_paths,
        "max_depth": crawl.max_depth,
        "page_budget": crawl.page_budget,
        "max_concurrent_shards": crawl.max_concurrent_shards,
        "shard_size": crawl.shard_size,
        "delay_ms": crawl.delay_ms,
        "respect_robots": bool(crawl.respect_robots),
        "same_domain": bool(crawl.same_domain),
        "allow_subdomains": bool(crawl.allow_subdomains),
    }


def clamp_freshness(value: Optional[int]) -> Optional[int]:
    if value is None:
        return None
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return None
    if seconds < 0:
        return None
    return min(seconds, MAX_FRESHNESS_SECONDS)


def requested_max_age(
    header_value: Optional[str],
    query_value: Optional[str],
    body_value: Optional[int] = None,
) -> Optional[int]:
    """Caller's freshness ceiling, or None if they did not ask for one.

    Three accepted spellings, in precedence order — the same ones the workflow
    surfaces already honor, so a caller learns ``max_age`` once:

      1. ``Cache-Control: max-age=N`` / ``no-cache`` / ``no-store``
      2. ``?max_age=N``
      3. a ``max_age`` field in the JSON body

    A malformed value is ignored rather than rejected: failing an expensive
    crawl over an unparseable cache header would be the worse outcome.
    """
    cc = (header_value or "").lower()
    if "no-cache" in cc or "no-store" in cc:
        return 0
    match = _MAX_AGE_RE.search(cc)
    if match:
        return clamp_freshness(int(match.group(1)))
    raw = (query_value or "").strip()
    if raw.isdigit():
        return clamp_freshness(int(raw))
    return clamp_freshness(body_value)


def cache_stamp(hit: bool, age_seconds: Optional[float] = None,
                source_crawl_id: Optional[int] = None) -> dict:
    """The canonical ``_cache`` envelope, stamped into the response BODY.

    Not headers-only: MCP tools and SDK clients return a payload, not an HTTP
    response object, so a header-only freshness signal is invisible on exactly
    the surfaces that most need to know whether they just paid for a crawl.
    """
    stamp: dict = {"hit": bool(hit)}
    if age_seconds is not None:
        stamp["age_seconds"] = int(age_seconds)
    if source_crawl_id is not None:
        stamp["source_crawl_id"] = source_crawl_id
    return stamp


async def find_fresh_run(
    db: AsyncSession, *, definition_id: int, max_age_seconds: int
) -> Optional[CrawlJob]:
    """The newest reusable run for a definition, or None.

    Reusable means all of:

      * ``status == "completed"`` — a failed or cancelled crawl is not data you
        already have (mirrors the success-only rule in the workflow run cache).
      * finished within ``max_age_seconds``.
      * ``pages_done > 0`` — a crawl that converged having fetched nothing is
        not a result worth serving for the next N hours. That is exactly the
        shape a fully-blocked host produces (every page 403s, the crawl still
        completes), and pinning that empty answer behind a long max_age would
        turn one bad crawl into a day of silently empty responses.
    """
    if max_age_seconds <= 0:
        return None
    cutoff = datetime.utcnow() - timedelta(seconds=max_age_seconds)
    return await db.scalar(
        select(CrawlJob)
        .where(
            CrawlJob.definition_id == definition_id,
            CrawlJob.status == "completed",
            CrawlJob.completed_at.isnot(None),
            CrawlJob.completed_at >= cutoff,
            CrawlJob.pages_done > 0,
        )
        .order_by(CrawlJob.completed_at.desc())
        .limit(1)
    )


# The keyword surface of crawl_orchestrator.start_crawl a SAVED config may drive.
# `url` is excluded because it is renamed to `seed_url`; `ai_session_id` is
# caller-supplied identity a stored blob must never be able to set.
_START_CRAWL_KEYS = frozenset({
    "name", "executor", "extract_mode", "extract_schema", "extract_prompt",
    "content_spec", "render_mode",
    "ocr_mode", "persona_id", "intent", "seed_urls", "relevance_threshold",
    "include_paths", "exclude_paths", "max_depth", "page_budget",
    "max_concurrent_shards", "shard_size", "delay_ms", "respect_robots",
    "same_domain", "allow_subdomains",
})


def to_start_kwargs(config: dict) -> dict:
    """Map a saved config onto ``crawl_orchestrator.start_crawl``'s keywords.

    Revalidates first, so a stored blob can never widen what the orchestrator is
    asked to do, and drops unknown keys rather than splatting into a TypeError.
    """
    clean = validate_config(config)
    kwargs = {k: v for k, v in clean.items() if k in _START_CRAWL_KEYS}
    kwargs["seed_url"] = clean.get("url")
    return kwargs


async def wait_for_crawl(db: AsyncSession, crawl_id: int, *, timeout: int,
                         poll_seconds: float = 2.0) -> Optional[CrawlJob]:
    """Poll a crawl to terminal state, or return it still-running at timeout.

    Returns the row either way (None only if it vanished mid-wait) — the caller
    decides whether non-terminal is a 504 or a status report. Each poll
    re-SELECTs the row with ``populate_existing`` because the crawl converges in
    a DIFFERENT session (the orchestrator's background task); re-reading the
    identity map would report the crawl running forever.

    Deliberately NOT ``db.expire_all()``: that also expires every OTHER object
    the caller still holds in this session — the run route serializes its
    CrawlDefinition right after this returns, and sync attribute access on an
    expired instance of an AsyncSession raises MissingGreenlet. That 500'd
    every wait=true run whose crawl outlived a single poll interval.
    """
    import asyncio

    deadline = datetime.utcnow() + timedelta(seconds=timeout)
    crawl = await db.get(CrawlJob, crawl_id)
    while crawl is not None and not crawl.is_terminal and datetime.utcnow() < deadline:
        await asyncio.sleep(poll_seconds)
        crawl = (await db.execute(
            select(CrawlJob)
            .where(CrawlJob.id == crawl_id)
            .execution_options(populate_existing=True)
        )).scalar_one_or_none()
    return crawl


def run_age_seconds(crawl: CrawlJob) -> Optional[float]:
    """How long ago a crawl finished, in seconds."""
    if not crawl.completed_at:
        return None
    completed = crawl.completed_at
    if completed.tzinfo is not None:
        completed = completed.astimezone(timezone.utc).replace(tzinfo=None)
    return max(0.0, (datetime.utcnow() - completed).total_seconds())
