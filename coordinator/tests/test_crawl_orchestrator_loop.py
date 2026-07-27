"""
End-to-end exercise of the Dragnet crawl coordinator loop on the self-host stack.

Drives the real orchestrator against a throwaway SQLite file + the in-process
fakeredis keyspace, so every ported behaviour is checked against actual code
rather than a mock: the RANKED frontier, the shard cut, the THIN completion
claim, the ATOMIC counter advance, block requeue + adaptive back-off + the
cross-crawl host cooldown, and the end-of-crawl reconcile at finalize.

No network: robots/SSRF/sitemap discovery are stubbed at their module seams, so
the test is hermetic and fast.
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# Point the app at a throwaway SQLite file BEFORE `database` is imported (the
# engine is built at import time from settings.database_url).
_TMP_DB = tempfile.NamedTemporaryFile(prefix="writ-crawl-test-", suffix=".db", delete=False)
_TMP_DB.close()
os.environ["WRIT_DB_PATH"] = _TMP_DB.name
# Settings refuses to build with the shipped default signing secret; this is a
# throwaway in-process instance, so take the documented local-trial escape hatch.
os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("ALLOW_INSECURE_DEV", "true")


@pytest.fixture(scope="module")
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="module")
def loop():
    lp = asyncio.new_event_loop()
    yield lp
    lp.close()


async def _fresh_schema():
    from database import Base, engine
    import models  # noqa: F401 — registers every table on Base.metadata

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)


def _stub_network(monkeypatch, sitemap_urls):
    """Neutralise every egress seam the crawl loop touches."""
    from services import crawl_orchestrator as co
    from services import robots_guard, url_policy

    class _Ok:
        allowed = True
        message = None

    async def _check_url(url, **kw):
        return _Ok()

    async def _robots_ok(url, respect_robots=True):
        return True

    async def _sitemap(seed_url):
        return list(sitemap_urls)

    async def _harvest(seed_url, cap=200):
        return []

    async def _ensure_loaded(db):
        return None

    monkeypatch.setattr(url_policy, "check_url", _check_url)
    monkeypatch.setattr(co.url_policy, "check_url", _check_url)
    monkeypatch.setattr(robots_guard, "is_allowed", _robots_ok)
    monkeypatch.setattr(co.robots_guard, "is_allowed", _robots_ok)
    monkeypatch.setattr(co, "_discover_sitemap_urls", _sitemap)
    monkeypatch.setattr(co, "_harvest_seed_links", _harvest)
    from services import domain_guard
    monkeypatch.setattr(domain_guard, "ensure_loaded", _ensure_loaded)

    # start_crawl fires the seeder as a DETACHED task. Neuter it so each test
    # drives `_seed_inner` itself and owns the timing (a second seed pass would
    # otherwise find every URL already in the visited set and admit nothing).
    async def _no_seed(crawl_id):
        return None
    monkeypatch.setattr(co, "_seed", _no_seed)


def _freeze_pump(monkeypatch):
    """Stop the pump from draining the frontier, so a test can inspect it.
    (`on_shard_complete` and `sweep_crawls` both re-pump on the way out, which
    would otherwise pop the very URLs the test wants to see.)"""
    from services import crawl_orchestrator as co

    async def _no_pump(crawl_id):
        return None
    monkeypatch.setattr(co, "_pump", _no_pump)


def test_frontier_is_relevance_ranked(loop, monkeypatch):
    """The frontier is a ZSET scored by relevance to the intent, and _pump pops the
    most-relevant URLs FIRST — so the page_budget goes to what was asked for."""
    from services import crawl_orchestrator as co
    from database import AsyncSessionLocal
    from models.crawl_job import CrawlJob
    from utils.redis_client import get_redis

    _stub_network(monkeypatch, [
        "https://example.com/pricing/plans",
        "https://example.com/legal/terms",
        "https://example.com/docs/pricing-api",
    ])
    _freeze_pump(monkeypatch)  # we want the seeded frontier, not a dispatch

    async def main():
        await _fresh_schema()
        async with AsyncSessionLocal() as db:
            crawl = await co.start_crawl(
                db, seed_url="https://example.com", intent="pricing plans",
                page_budget=50,
            )
            cid = crawl.id
        await co._seed_inner(cid)

        r = get_redis()
        popped = await r.zpopmax(co._k_frontier(cid), 10)
        urls = [json.loads(m)["url"] for m, _s in popped]
        scores = [s for _m, s in popped]
        async with AsyncSessionLocal() as db:
            row = await db.get(CrawlJob, cid)
            return urls, scores, row.pages_discovered

    urls, scores, discovered = loop.run_until_complete(main())

    assert discovered == 4, f"seed + 3 sitemap URLs admitted, got {discovered}"
    assert scores == sorted(scores, reverse=True), "ZPOPMAX must return best-first"
    # Both pricing pages outrank the unrelated legal page.
    assert urls.index("https://example.com/pricing/plans") < urls.index("https://example.com/legal/terms")
    assert urls.index("https://example.com/docs/pricing-api") < urls.index("https://example.com/legal/terms")


def test_relevance_threshold_drops_offtopic(loop, monkeypatch):
    """A below-threshold discovered URL is dropped at admission (and stays in the
    visited set, so it is never reconsidered). Depth-0 seeds are never dropped."""
    from services import crawl_orchestrator as co

    _stub_network(monkeypatch, [])

    async def main():
        await _fresh_schema()
        from models.crawl_job import CrawlJob
        crawl = CrawlJob(
            id=9001, name="t", seed_url="https://example.com",
            include_paths=[], exclude_paths=[], max_depth=3,
            same_domain=True, allow_subdomains=True,
            # A full 2/2 token match at depth 1 with max_depth 3 scores
            # 0.55*1.0 + 0.45*0 - 0.25*(1/4) = 0.4875; an unrelated path scores
            # -0.0625. 0.3 sits cleanly between them.
            intent="pricing plans", relevance_threshold=0.3,
            page_budget=100, respect_robots=False,
        )
        on_topic = await co._admit(crawl, "https://example.com/pricing/plans", 1,
                                   "example.com", "example.com")
        off_topic = await co._admit(crawl, "https://example.com/careers/jobs", 1,
                                    "example.com", "example.com")
        # Depth 0 is the explicit entry set — exempt from the threshold.
        seed_like = await co._admit(crawl, "https://example.com/careers/other", 0,
                                    "example.com", "example.com")
        return on_topic, off_topic, seed_like

    on_topic, off_topic, seed_like = loop.run_until_complete(main())
    assert on_topic is True
    assert off_topic is False, "an off-topic URL below the threshold must be dropped"
    assert seed_like is True, "depth-0 URLs bypass the relevance threshold"


def test_shard_completion_counters_and_reconcile(loop, monkeypatch):
    """A shard completing through the THIN funnel: atomic counter advance, anchor-
    text discovery, convergence → finalize with reconciled record totals."""
    from services import crawl_orchestrator as co
    from database import AsyncSessionLocal
    from models.automation_task import AutomationTask
    from models.crawl_job import CrawlJob

    _stub_network(monkeypatch, [])

    async def main():
        await _fresh_schema()
        async with AsyncSessionLocal() as db:
            crawl = await co.start_crawl(
                db, seed_url="https://example.com", page_budget=100, max_depth=0,
            )
            cid, wf_id = crawl.id, crawl.workflow_id
            # Mint a shard task by hand so the test owns the timing.
            task = await co._mint_shard_task(
                db, crawl, [{"url": "https://example.com", "depth": 0}])
            tid = task.id
            task.status = "running"
            await db.commit()

        result_data = {
            "engine": "http",
            "pages": [{"url": "https://example.com", "status": "ok", "title": "Home"}],
            "failed": [],
            # Anchor text rides the new contract; max_depth=0 means nothing is
            # admitted from it, which is the point — counters must still be right.
            "discovered_links": [{"url": "https://example.com/a", "text": "About us"}],
            "extracted_data": [
                {"url": "https://example.com", "markdown": "# Home", "fetched_at": "t1"},
                # Same page + same content, different volatile field → ONE record.
                {"url": "https://example.com", "markdown": "# Home", "fetched_at": "t2"},
            ],
        }
        claimed = await co.complete_shard_task(
            tid, cid, success=True, result_data=result_data, reporter_agent="agent-1")
        # A duplicate/redelivered frame must be a no-op.
        again = await co.complete_shard_task(
            tid, cid, success=True, result_data=result_data, reporter_agent="agent-1")

        async with AsyncSessionLocal() as db:
            row = await db.get(CrawlJob, cid)
            task = await db.get(AutomationTask, tid)
            return claimed, again, row, task

    claimed, again, row, task = loop.run_until_complete(main())

    assert claimed is True and again is False, "the claim UPDATE must be idempotent"
    assert task.status == "success"
    assert task.executor_agent_id == "agent-1"
    assert row.pages_done == 1 and row.pages_failed == 0 and row.shards_done == 1
    # Frontier drained + nothing in flight → converged.
    assert row.status == "completed"
    # Reconcile deduped the two identical records (fetched_at is excluded).
    assert row.records_total == 1, f"expected 1 unique record, got {row.records_total}"
    assert row.duplicates_removed == 1
    assert row.reconciled_at is not None


def test_blocked_urls_requeue_backoff_and_host_cooldown(loop, monkeypatch):
    """A host refusing the agent: blocked URLs are requeued away from that agent,
    the crawl backs off (delay up, concurrency down), and every crawl of the host
    is parked by the shared cooldown."""
    from services import crawl_orchestrator as co
    from database import AsyncSessionLocal
    from models.crawl_job import CrawlJob
    from utils.redis_client import get_redis

    _stub_network(monkeypatch, [])
    _freeze_pump(monkeypatch)  # keep the requeued URLs in the frontier to assert on

    async def main():
        await _fresh_schema()
        async with AsyncSessionLocal() as db:
            crawl = await co.start_crawl(
                db, seed_url="https://blocked.example", page_budget=100,
                delay_ms=250, max_concurrent_shards=6,
            )
            cid = crawl.id
            task = await co._mint_shard_task(db, crawl, [
                {"url": "https://blocked.example/a", "depth": 1},
                {"url": "https://blocked.example/b", "depth": 1},
            ])
            tid = task.id
            task.status = "running"
            await db.commit()

        result_data = {
            "engine": "http",
            "pages": [],
            "failed": [{"url": "https://blocked.example/a", "reason": "429"}],
            "blocked": [
                {"url": "https://blocked.example/a", "depth": 1, "block_attempts": 0},
                {"url": "https://blocked.example/b", "depth": 1, "block_attempts": 3},
            ],
            "agent_blocked": True,
            "retry_after": 90,
            "discovered_links": [],
            "extracted_data": [],
        }
        await co.complete_shard_task(tid, cid, success=True, result_data=result_data,
                                     reporter_agent="agent-7")

        r = get_redis()
        members = await r.zrange(co._k_frontier(cid), 0, -1)
        cooldown = await r.get(co._k_host_cooldown("blocked.example"))
        async with AsyncSessionLocal() as db:
            row = await db.get(CrawlJob, cid)
            return [json.loads(m) for m in members], cooldown, row

    requeued, cooldown, row = loop.run_until_complete(main())

    urls = {e["url"] for e in requeued}
    assert "https://blocked.example/a" in urls, "a blocked URL must be retried"
    assert "https://blocked.example/b" not in urls, \
        "a URL already at _MAX_BLOCK_RETRIES must be given up on, not looped"
    entry = next(e for e in requeued if e["url"].endswith("/a"))
    assert entry["block_attempts"] == 1
    assert entry["avoid_agent"] == "agent-7", "the retry must steer away from the blocked agent"

    assert cooldown, "the refusing host must earn a cross-crawl cooldown"
    # 250 * 1.5 + 250 = 625, clamped under _MAX_BACKOFF_DELAY_MS, stored as an int.
    assert row.delay_ms == 625 and isinstance(row.delay_ms, int)
    assert row.max_concurrent_shards == 5


def test_pump_respects_host_cooldown(loop, monkeypatch):
    """While a host is cooling down, NO crawl of that host cuts a shard."""
    from services import crawl_orchestrator as co
    from database import AsyncSessionLocal
    from models.automation_task import AutomationTask
    from models.crawl_job import CrawlJob
    from sqlalchemy import func, select
    from utils.redis_client import get_redis

    _stub_network(monkeypatch, ["https://cool.example/one", "https://cool.example/two"])

    async def main():
        await _fresh_schema()
        async with AsyncSessionLocal() as db:
            crawl = await co.start_crawl(db, seed_url="https://cool.example", page_budget=50)
            cid = crawl.id
        r = get_redis()
        await r.set(co._k_host_cooldown("cool.example"), "1", ex=60)
        await co._seed_inner(cid)  # seeds, then pumps

        async with AsyncSessionLocal() as db:
            shards = await db.scalar(
                select(func.count(AutomationTask.id))
                .where(AutomationTask.trigger_type == "crawl"))
            row = await db.get(CrawlJob, cid)
        frontier = await r.zcard(co._k_frontier(cid))
        return shards, frontier, row.status

    shards, frontier, status = loop.run_until_complete(main())
    assert shards == 0, "a cooling-down host must not be dispatched to"
    assert frontier > 0, "the URLs stay queued for when the cooldown lapses"
    assert status == "crawling", "a parked crawl must not be finalized as complete"


def test_sweep_reaps_stale_shard_and_requeues(loop, monkeypatch):
    """A shard whose agent vanished is reaped and its URLs returned to the frontier,
    so no pages are lost and the crawl can converge."""
    from datetime import datetime, timedelta

    from services import crawl_orchestrator as co
    from database import AsyncSessionLocal
    from models.automation_task import AutomationTask
    from utils.redis_client import get_redis

    _stub_network(monkeypatch, [])
    _freeze_pump(monkeypatch)  # the sweep re-pumps on the way out; hold the frontier

    async def main():
        await _fresh_schema()
        async with AsyncSessionLocal() as db:
            crawl = await co.start_crawl(db, seed_url="https://stale.example", page_budget=50)
            cid = crawl.id
            task = await co._mint_shard_task(db, crawl, [
                {"url": "https://stale.example/x", "depth": 1}])
            task.status = "running"
            task.created_at = (datetime.utcnow()
                               - timedelta(seconds=co._SHARD_STALE_AFTER_S + 60))
            tid = task.id
            await db.commit()

        await co.sweep_crawls()

        r = get_redis()
        members = await r.zrange(co._k_frontier(cid), 0, -1)
        async with AsyncSessionLocal() as db:
            task = await db.get(AutomationTask, tid)
        return [json.loads(m)["url"] for m in members], task.status

    urls, task_status = loop.run_until_complete(main())
    assert "https://stale.example/x" in urls, "a reaped shard's URLs must be requeued"
    assert task_status == "failed"
