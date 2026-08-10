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

    # The in-process Redis is a MODULE-GLOBAL that outlives each test, while
    # dropping the schema restarts crawl ids at 1. Without this flush a later
    # crawl inherits the previous test's frontier / visited set / in-flight count
    # / host cooldown under the very same keys — so assertions read another
    # test's leftovers and pass or fail depending on file order. Reset both
    # halves of the world together, not just the SQL half.
    from utils.redis_client import get_redis
    await get_redis().flushall()


def _setattr_compatible(monkeypatch, target, name, replacement):
    """`monkeypatch.setattr`, but first prove the stub can absorb every argument
    the REAL function accepts.

    A stub silently rots when the function it stands in for gains a parameter: the
    patch still applies, and the mismatch only surfaces as a ``TypeError`` thrown
    from deep inside the code under test — which reads like a product bug, not a
    stale double. (That is exactly how `auth=` slipped past when login-before-crawl
    was added.) Checking here fails at the patch site with a message naming the
    parameter that drifted.
    """
    import inspect

    real = getattr(target, name)
    real_params = inspect.signature(real).parameters
    stub_params = inspect.signature(replacement).parameters
    absorbs_any = any(
        p.kind in (inspect.Parameter.VAR_KEYWORD, inspect.Parameter.VAR_POSITIONAL)
        for p in stub_params.values()
    )
    if not absorbs_any:
        missing = [
            pname for pname, p in real_params.items()
            if pname not in stub_params
            and p.kind not in (inspect.Parameter.VAR_KEYWORD, inspect.Parameter.VAR_POSITIONAL)
        ]
        assert not missing, (
            f"stub for {getattr(target, '__name__', target)}.{name} is stale: the real "
            f"function now takes {missing} — add {'it' if len(missing) == 1 else 'them'} "
            f"to the stub so this test keeps exercising the real call."
        )
    monkeypatch.setattr(target, name, replacement)


def _stub_network(monkeypatch, sitemap_urls):
    """Neutralise every egress seam the crawl loop touches.

    Each stub mirrors the real signature (including keyword-only params like the
    `auth` session threaded through by login-before-crawl) and is installed via
    `_setattr_compatible`, so a future signature change fails here rather than
    somewhere inside the orchestrator.
    """
    from services import crawl_orchestrator as co
    from services import robots_guard, url_policy

    class _Ok:
        allowed = True
        message = None

    async def _check_url(url, **kw):
        return _Ok()

    async def _robots_ok(url, *, respect_robots=True):
        return True

    async def _sitemap(seed_url, *, auth=None):
        return list(sitemap_urls)

    async def _harvest(seed_url, *, cap=200, auth=None):
        return []

    async def _ensure_loaded(db):
        return None

    _setattr_compatible(monkeypatch, url_policy, "check_url", _check_url)
    _setattr_compatible(monkeypatch, co.url_policy, "check_url", _check_url)
    _setattr_compatible(monkeypatch, robots_guard, "is_allowed", _robots_ok)
    _setattr_compatible(monkeypatch, co.robots_guard, "is_allowed", _robots_ok)
    _setattr_compatible(monkeypatch, co, "_discover_sitemap_urls", _sitemap)
    _setattr_compatible(monkeypatch, co, "_harvest_seed_links", _harvest)
    from services import domain_guard
    _setattr_compatible(monkeypatch, domain_guard, "ensure_loaded", _ensure_loaded)

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


def test_goal_directed_seeding_reserves_budget_for_discovery(loop, monkeypatch):
    """An intent whose words appear NOWHERE in a big opaque sitemap must not let
    that sitemap fill the page budget. Admission slots are the budget currency
    (`pages_discovered >= page_budget` shuts admission off for the rest of the
    crawl), so before this rule an intent like "windows xp" seeded 50 junk rows,
    left zero slots for the on-topic links discovered while crawling, and the
    run "crawled home pages". Junk now gets a small hub-first exploration
    allowance; every goal-matching sitemap row is still seeded, ranked on top."""
    from services import crawl_orchestrator as co
    from database import AsyncSessionLocal
    from models.crawl_job import CrawlJob
    from utils.redis_client import get_redis

    opaque = [f"https://example.com/p/{i}" for i in range(60)]
    matched = ["https://example.com/kb/windows-xp-install",
               "https://example.com/kb/windows-xp-networking"]
    _stub_network(monkeypatch, opaque + matched)  # matched buried at the END
    _freeze_pump(monkeypatch)

    async def main():
        await _fresh_schema()
        async with AsyncSessionLocal() as db:
            crawl = await co.start_crawl(
                db, seed_url="https://example.com", intent="windows xp",
                page_budget=50,
            )
            cid = crawl.id
        await co._seed_inner(cid)

        r = get_redis()
        popped = await r.zpopmax(co._k_frontier(cid), 100)
        urls = [json.loads(m)["url"] for m, _s in popped]
        async with AsyncSessionLocal() as db:
            row = await db.get(CrawlJob, cid)
            return urls, row.pages_discovered

    urls, discovered = loop.run_until_complete(main())

    quota = max(co._EXPLORE_SEED_FLOOR, 50 // 10)
    assert discovered <= 1 + len(matched) + quota, (
        f"opaque sitemap rows filled the budget again: {discovered} admitted")
    for u in matched:
        assert u in urls, "every goal-matching sitemap URL must be seeded"
    # ZPOPMAX order: the matching pages outrank every exploration seed.
    assert set(urls[:2]) == set(matched)


def test_goal_directed_shard_links_bound_exploration(loop, monkeypatch):
    """One link-heavy junk page must not consume the remaining budget slots:
    with an intent, a completed shard admits every goal-matching link but only
    _EXPLORE_SHARD_CAP zero-relevance ones."""
    from services import crawl_orchestrator as co
    from database import AsyncSessionLocal
    from models.crawl_job import CrawlJob
    from utils.redis_client import get_redis

    _stub_network(monkeypatch, [])
    _freeze_pump(monkeypatch)

    async def main():
        await _fresh_schema()
        async with AsyncSessionLocal() as db:
            crawl = await co.start_crawl(
                db, seed_url="https://example.com", intent="windows xp",
                page_budget=100, max_depth=2,
            )
            cid = crawl.id
            task = await co._mint_shard_task(
                db, crawl, [{"url": "https://example.com", "depth": 0}])
            tid = task.id
            task.status = "running"
            await db.commit()

        links = [{"url": f"https://example.com/p/{i}", "text": f"page {i}"}
                 for i in range(60)]
        links.insert(30, {"url": "https://example.com/kb/1",
                          "text": "Windows XP setup guide"})
        links.append({"url": "https://example.com/kb/2",
                      "text": "windows-xp driver archive"})
        result_data = {
            "engine": "http",
            "pages": [{"url": "https://example.com", "status": "ok"}],
            "failed": [],
            "discovered_links": links,
            "extracted_data": [],
        }
        await co.complete_shard_task(
            tid, cid, success=True, result_data=result_data, reporter_agent="agent-1")

        r = get_redis()
        members = await r.zrange(co._k_frontier(cid), 0, -1)
        urls = [json.loads(m)["url"] for m in members]
        async with AsyncSessionLocal() as db:
            row = await db.get(CrawlJob, cid)
            return urls, row.pages_discovered

    urls, discovered = loop.run_until_complete(main())

    assert "https://example.com/kb/1" in urls and "https://example.com/kb/2" in urls, (
        "goal-matching links must always be admitted")
    junk = [u for u in urls if "/p/" in u]
    assert len(junk) <= co._EXPLORE_SHARD_CAP, (
        f"a junk page consumed {len(junk)} budget slots; cap is {co._EXPLORE_SHARD_CAP}")
    assert discovered == len(urls)


def test_map_scent_expansion_reaches_anchored_links(loop, monkeypatch):
    """A /map search that matches nothing lexically must follow the scent: fetch
    the most promising (hub-first) candidates and pick up THEIR link anchors, so
    "windows xp" surfaces the XP thread even when every URL is opaque. And a
    search that already matches plenty must not fetch anything at all."""
    from services import crawl_orchestrator as co
    from services import crawl_targeting

    calls = []
    harvests = {
        "https://example.com/forums": [
            {"url": "https://example.com/t/9911",
             "text": "Windows XP won't boot after update"},
            {"url": "https://example.com/t/9912", "text": "Photography corner"},
        ],
    }

    async def _harvest(seed_url, *, cap=200, auth=None):
        calls.append(seed_url)
        return harvests.get(seed_url, [])

    _setattr_compatible(monkeypatch, co, "_harvest_seed_links", _harvest)

    t = crawl_targeting.make_targeting(intent="windows xp")
    candidates = ["https://example.com/a/b/deep1",
                  "https://example.com/forums",
                  "https://example.com/a/b/deep2"]
    text_by_url = {}

    async def main():
        out = await co.expand_search_candidates(
            "https://example.com", list(candidates), text_by_url, t)
        ranked = await crawl_targeting.rank_urls(
            t, ((u, text_by_url.get(u, "")) for u in out), depth=0)
        return out, ranked

    out, ranked = loop.run_until_complete(main())

    assert "https://example.com/t/9911" in out, "expansion must surface anchored links"
    assert ranked[0][0] == "https://example.com/t/9911", (
        "the XP-anchored link must rank first after expansion")
    assert calls[0] == "https://example.com/forums", (
        "expansion must open hub-like (shallow) candidates first")
    assert len(calls) <= co.MAP_EXPAND_PAGES

    # Plenty of matches ⇒ zero extra fetches.
    calls.clear()
    many = [f"https://example.com/windows-xp/{i}" for i in range(12)]

    async def main2():
        return await co.expand_search_candidates(
            "https://example.com", list(many), {}, t)

    out2 = loop.run_until_complete(main2())
    assert out2 == many and calls == []


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


def test_block_retry_budget_survives_agent_not_echoing_attempts(loop, monkeypatch):
    """The retry budget must come from the SHARD we minted, not the agent's report.

    A REAL agent's `blocked[]` entry is `{url, depth, status, block_kind,
    retry_after}` — it does NOT echo `block_attempts`. Reading the count off that
    payload pinned `attempts` at 0 on every pass, so a host that 403s everything
    had the same URLs requeued forever: the frontier never drained, the
    `frontier_len == 0` convergence never fired, and the crawl sat in "crawling"
    while the host-cooldown held dispatch at 0 — a permanently frozen crawl.

    (The sibling test above passes `block_attempts` INSIDE the agent report, which
    a real agent never does — that is what made this bug look covered.)
    """
    from services import crawl_orchestrator as co
    from database import AsyncSessionLocal
    from utils.redis_client import get_redis

    _stub_network(monkeypatch, [])
    _freeze_pump(monkeypatch)

    async def main():
        await _fresh_schema()
        async with AsyncSessionLocal() as db:
            crawl = await co.start_crawl(
                db, seed_url="https://forbidden.example", page_budget=100,
                delay_ms=250, max_concurrent_shards=6,
            )
            cid = crawl.id
            # The coordinator's own bookkeeping rides on the shard batch.
            task = await co._mint_shard_task(db, crawl, [
                {"url": "https://forbidden.example/a", "depth": 1, "block_attempts": 0},
                {"url": "https://forbidden.example/b", "depth": 1,
                 "block_attempts": co._MAX_BLOCK_RETRIES},
            ])
            tid = task.id
            task.status = "running"
            await db.commit()

        # Exactly what the fleet agent really sends for a 403 wall — no attempts field.
        result_data = {
            "engine": "http",
            "pages": [],
            "failed": [
                {"url": "https://forbidden.example/a", "reason": "blocked (forbidden)",
                 "blocked": True, "block_kind": "forbidden"},
                {"url": "https://forbidden.example/b", "reason": "blocked (forbidden)",
                 "blocked": True, "block_kind": "forbidden"},
            ],
            "blocked": [
                {"url": "https://forbidden.example/a", "depth": 1, "status": 403,
                 "block_kind": "forbidden", "retry_after": None},
                {"url": "https://forbidden.example/b", "depth": 1, "status": 403,
                 "block_kind": "forbidden", "retry_after": None},
            ],
            "agent_blocked": True,
            "retry_after": 0,
            "discovered_links": [],
            "extracted_data": [],
        }
        await co.complete_shard_task(tid, cid, success=True, result_data=result_data,
                                     reporter_agent="agent-9")

        r = get_redis()
        members = await r.zrange(co._k_frontier(cid), 0, -1)
        return [json.loads(m) for m in members]

    requeued = loop.run_until_complete(main())
    urls = {e["url"] for e in requeued}

    assert "https://forbidden.example/b" not in urls, (
        "a URL already at _MAX_BLOCK_RETRIES must be dropped even though the agent "
        "did not echo block_attempts — otherwise the frontier never drains and the "
        "crawl can never converge"
    )
    entry = next(e for e in requeued if e["url"].endswith("/a"))
    assert entry["block_attempts"] == 1, "the count must advance, not reset to 1 forever"
    assert entry["depth"] == 1, "depth must survive the requeue"
    assert entry["avoid_agent"] == "agent-9"


def test_block_retry_budget_terminates_after_max_attempts(loop, monkeypatch):
    """End-to-end: a host that refuses EVERYTHING drains the frontier instead of
    looping. Re-dispatching the same URL with a non-echoing agent must exhaust the
    budget in _MAX_BLOCK_RETRIES rounds and leave the frontier empty."""
    from services import crawl_orchestrator as co
    from database import AsyncSessionLocal
    from utils.redis_client import get_redis

    _stub_network(monkeypatch, [])
    _freeze_pump(monkeypatch)

    def _agent_report(batch):
        return {
            "engine": "http", "pages": [], "failed": [],
            "blocked": [{"url": b["url"], "depth": b.get("depth", 0), "status": 403,
                         "block_kind": "forbidden", "retry_after": None} for b in batch],
            "agent_blocked": True, "retry_after": 0,
            "discovered_links": [], "extracted_data": [],
        }

    async def main():
        await _fresh_schema()
        async with AsyncSessionLocal() as db:
            crawl = await co.start_crawl(
                db, seed_url="https://wall.example", page_budget=100,
                delay_ms=250, max_concurrent_shards=6,
            )
            cid = crawl.id

        batch = [{"url": "https://wall.example/a", "depth": 1}]
        rounds = 0
        r = get_redis()
        # Bound the loop well above the budget so a regression fails fast instead
        # of hanging the suite.
        while batch and rounds < co._MAX_BLOCK_RETRIES + 5:
            rounds += 1
            async with AsyncSessionLocal() as db:
                from models.crawl_job import CrawlJob
                cj = await db.get(CrawlJob, cid)
                task = await co._mint_shard_task(db, cj, batch)
                tid = task.id
                task.status = "running"
                await db.commit()
            await co.complete_shard_task(tid, cid, success=True,
                                         result_data=_agent_report(batch),
                                         reporter_agent="agent-9")
            members = await r.zrange(co._k_frontier(cid), 0, -1)
            # Drain what the requeue put back; that is the next round's shard.
            batch = [json.loads(m) for m in members]
            if batch:
                await r.zremrangebyrank(co._k_frontier(cid), 0, -1)
        return rounds, batch

    rounds, leftover = loop.run_until_complete(main())
    assert not leftover, "the frontier must drain — a refused URL cannot requeue forever"
    # The first dispatch is the original attempt; _MAX_BLOCK_RETRIES retries follow,
    # and the round after the last one drops the URL.
    assert rounds == co._MAX_BLOCK_RETRIES + 1, (
        f"expected the budget to be spent in {co._MAX_BLOCK_RETRIES + 1} rounds, took {rounds}"
    )


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


def test_live_shard_progress_advances_counters_without_double_counting(loop, monkeypatch):
    """A running shard reports its page tally; the crawl's counters move BEFORE the
    batch returns, and the completion path credits only the remainder.

    Why this matters: a shard is the accounting unit, so `pages_done` could not move
    for the whole life of a shard. On the browser lane that is 25 URLs at seconds
    each — over a minute of a run reading as frozen on page 1, which is what
    operators cancelled (and the shard's results were then discarded as late).

    The invariant under test is that live crediting is EXACTLY additive: whatever
    sequence of progress frames arrives, the totals after completion are the same as
    if none had.
    """
    from services import crawl_orchestrator as co
    from database import AsyncSessionLocal
    from models.crawl_job import CrawlJob
    from types import SimpleNamespace

    _stub_network(monkeypatch, [])

    async def main():
        await _fresh_schema()
        async with AsyncSessionLocal() as db:
            crawl = await co.start_crawl(
                db, seed_url="https://example.com", page_budget=100, max_depth=0)
            cid = crawl.id
            batch = [{"url": f"https://example.com/{i}", "depth": 0} for i in range(4)]
            task = await co._mint_shard_task(db, crawl, batch)
            tid = task.id
            task.status = "running"
            await db.commit()

        shim = SimpleNamespace(
            id=tid, trigger_context={co.CTX_CRAWL_ID: cid, co.CTX_CRAWL_SHARD: batch})

        # Two pages in, mid-shard.
        await co.on_shard_progress(shim, done=2, failed=0)
        async with AsyncSessionLocal() as db:
            mid = await db.get(CrawlJob, cid)
            mid_done, mid_failed = mid.pages_done, mid.pages_failed

        # A redelivered frame with the SAME tally must add nothing.
        await co.on_shard_progress(shim, done=2, failed=0)
        # …and one more page, plus a failure.
        await co.on_shard_progress(shim, done=3, failed=1)
        async with AsyncSessionLocal() as db:
            late = await db.get(CrawlJob, cid)
            late_done, late_failed = late.pages_done, late.pages_failed

        # The shard finishes: 3 ok + 1 failed — exactly what progress last reported.
        result_data = {
            "engine": "browser",
            "pages": [{"url": f"https://example.com/{i}", "status": "ok"} for i in range(3)],
            "failed": [{"url": "https://example.com/3", "reason": "timeout"}],
            "discovered_links": [],
            "extracted_data": [],
        }
        await co.complete_shard_task(
            tid, cid, success=True, result_data=result_data, reporter_agent="agent-1")

        async with AsyncSessionLocal() as db:
            row = await db.get(CrawlJob, cid)
            return (mid_done, mid_failed, late_done, late_failed,
                    row.pages_done, row.pages_failed, row.shards_done)

    (mid_done, mid_failed, late_done, late_failed,
     final_done, final_failed, shards_done) = loop.run_until_complete(main())

    assert (mid_done, mid_failed) == (2, 0), "counters must move mid-shard"
    assert (late_done, late_failed) == (3, 1), "a repeated tally adds nothing; a higher one adds the delta"
    # The completion path already saw all 4 credited, so it adds nothing further.
    assert (final_done, final_failed) == (3, 1), (
        f"pages must be counted exactly once, got {final_done} done / {final_failed} failed")
    assert shards_done == 1


def test_shard_progress_is_ignored_after_the_crawl_is_terminal(loop, monkeypatch):
    """A cancelled crawl must not be resurrected by a frame from a shard still
    running on its agent — cancel is an INSTANT stop, and its in-flight shards
    orphan-drain (the same reason on_shard_complete bails on is_terminal)."""
    from services import crawl_orchestrator as co
    from database import AsyncSessionLocal
    from models.crawl_job import CrawlJob
    from types import SimpleNamespace

    _stub_network(monkeypatch, [])

    async def main():
        await _fresh_schema()
        async with AsyncSessionLocal() as db:
            crawl = await co.start_crawl(
                db, seed_url="https://example.com", page_budget=100, max_depth=0)
            cid = crawl.id
            batch = [{"url": "https://example.com/a", "depth": 0}]
            task = await co._mint_shard_task(db, crawl, batch)
            tid = task.id
            task.status = "running"
            await db.commit()
            await co.cancel_crawl(db, crawl)
            await db.commit()

        shim = SimpleNamespace(
            id=tid, trigger_context={co.CTX_CRAWL_ID: cid, co.CTX_CRAWL_SHARD: batch})
        await co.on_shard_progress(shim, done=5, failed=0)

        async with AsyncSessionLocal() as db:
            row = await db.get(CrawlJob, cid)
            return row.status, row.pages_done

    status, pages_done = loop.run_until_complete(main())
    assert status == "cancelled"
    assert pages_done == 0, "a late frame must not credit pages to a terminal crawl"


def test_progress_frame_from_the_wire_moves_the_crawl_counter(loop, monkeypatch):
    """The whole path, as an agent drives it: a `task_progress` frame arrives on the
    recorder socket and the crawl's page counter moves — no `task_result` involved.

    This is the seam the fix lives on. `_handle_task_progress` used to do nothing but
    stamp a status string, so a crawl's counters could not move until a whole 25-URL
    shard came back; on the browser lane that is a minute-plus of a run that looks
    stuck on page 1.
    """
    from services import crawl_orchestrator as co
    from routers import user_recorder_ws as ws
    from database import AsyncSessionLocal
    from models.crawl_job import CrawlJob

    _stub_network(monkeypatch, [])

    async def main():
        await _fresh_schema()
        async with AsyncSessionLocal() as db:
            crawl = await co.start_crawl(
                db, seed_url="https://example.com", page_budget=100, max_depth=0)
            cid = crawl.id
            batch = [{"url": f"https://example.com/{i}", "depth": 0} for i in range(6)]
            task = await co._mint_shard_task(db, crawl, batch)
            tid = task.id
            task.status = "running"
            # The frame is only trusted from the agent the shard was dispatched to.
            task.executor_agent_id = "agent-7"
            await db.commit()

        ws._dispatched_tasks["agent-7"] = {str(tid)}
        try:
            await ws._handle_task_progress("agent-7", {
                "type": "task_progress",
                "task_id": str(tid),
                "crawl_pages_done": 4,
                "crawl_pages_failed": 1,
                "crawl_pages_total": 6,
                "message": "5/6 pages",
            })
            async with AsyncSessionLocal() as db:
                credited = await db.get(CrawlJob, cid)
                credited_done, credited_failed = credited.pages_done, credited.pages_failed

            # An agent the shard was NOT dispatched to must not move anything.
            ws._dispatched_tasks["agent-9"] = {str(tid)}
            await ws._handle_task_progress("agent-9", {
                "type": "task_progress",
                "task_id": str(tid),
                "crawl_pages_done": 99,
                "crawl_pages_failed": 0,
            })
        finally:
            ws._dispatched_tasks.pop("agent-7", None)
            ws._dispatched_tasks.pop("agent-9", None)

        async with AsyncSessionLocal() as db:
            row = await db.get(CrawlJob, cid)
            return credited_done, credited_failed, row.pages_done, row.pages_failed

    done, failed, after_stranger, after_stranger_failed = loop.run_until_complete(main())

    assert (done, failed) == (4, 1), "the frame's tally must land on the crawl row"
    assert (after_stranger, after_stranger_failed) == (4, 1), (
        "only the assigned executor may report this shard's progress")
