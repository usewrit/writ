"""The `ai` crawl executor on the self-hosted coordinator.

`executor` (WHO reads each page) and `render_mode` (HOW each page is fetched) are
orthogonal, and they meet in one place: the AI pass consumes the markdown rows the
fetch lane produced. A regression in either shows up as "the AI reader returned
nothing" rather than as a fetch error, so these pin both halves:

  * the shard payload keeps the operator's render lane AND forces the agent's own
    output to markdown, whatever output shape was asked for (the AI pass is what
    turns it into records);
  * an AI crawl with nothing to run on is refused at CREATION rather than fetching
    a whole site and silently keeping the markdown;
  * the per-shard pass replaces rows with records, keeps the markdown for any page
    the provider could not read, and carries the row's lane markers (including a
    browser-rendered page's thumbnail) onto the records;
  * finalize WAITS for that pass — reconciling mid-flight would bank a
    records_total that misses whatever landed after.

Same hermetic harness as test_crawl_orchestrator_loop: throwaway SQLite +
in-process fakeredis, every egress seam stubbed.
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

_TMP_DB = tempfile.NamedTemporaryFile(prefix="writ-crawl-ai-test-", suffix=".db", delete=False)
_TMP_DB.close()
os.environ["WRIT_DB_PATH"] = _TMP_DB.name
os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("ALLOW_INSECURE_DEV", "true")


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
    from utils.redis_client import get_redis
    await get_redis().flushall()


def _stub_network(monkeypatch):
    """Neutralise every egress seam start_crawl touches, and the detached seeder."""
    from services import crawl_orchestrator as co
    from services import domain_guard, url_policy

    class _Ok:
        allowed = True
        message = None

    async def _check_url(url, **kw):
        return _Ok()

    async def _ensure_loaded(db):
        return None

    async def _no_seed(crawl_id):
        return None

    monkeypatch.setattr(url_policy, "check_url", _check_url)
    monkeypatch.setattr(co.url_policy, "check_url", _check_url)
    monkeypatch.setattr(domain_guard, "ensure_loaded", _ensure_loaded)
    monkeypatch.setattr(co, "_seed", _no_seed)


def _with_provider(monkeypatch, available: bool = True):
    """Stand in for "the operator has an AI provider configured"."""
    from services import crawl_orchestrator as co

    async def _available():
        return available

    monkeypatch.setattr(co, "ai_extraction_available", _available)


def _freeze_pump(monkeypatch):
    from services import crawl_orchestrator as co

    async def _no_pump(crawl_id):
        return None
    monkeypatch.setattr(co, "_pump", _no_pump)


# --- creation-time preconditions ---------------------------------------------

def test_ai_executor_requires_an_extraction_instruction(loop, monkeypatch):
    from fastapi import HTTPException
    from services import crawl_orchestrator as co
    from database import AsyncSessionLocal

    _stub_network(monkeypatch)
    _with_provider(monkeypatch)

    async def main():
        await _fresh_schema()
        async with AsyncSessionLocal() as db:
            with pytest.raises(HTTPException) as ei:
                await co.start_crawl(db, seed_url="https://example.com", executor="ai")
            return ei.value

    exc = loop.run_until_complete(main())
    assert exc.status_code == 400
    assert "instruction" in str(exc.detail).lower()


def test_ai_executor_refused_when_no_provider_is_configured(loop, monkeypatch):
    """Without a provider every page's read fails and keeps its markdown — a whole
    site fetched to produce exactly what a regular crawl would have, with no error
    anywhere. Refusing up front is the only honest outcome."""
    from fastapi import HTTPException
    from services import crawl_orchestrator as co
    from database import AsyncSessionLocal

    _stub_network(monkeypatch)
    _with_provider(monkeypatch, available=False)

    async def main():
        await _fresh_schema()
        async with AsyncSessionLocal() as db:
            with pytest.raises(HTTPException) as ei:
                await co.start_crawl(
                    db, seed_url="https://example.com", executor="ai",
                    extract_prompt="the price",
                )
            return ei.value

    exc = loop.run_until_complete(main())
    assert exc.status_code == 409
    assert "Settings" in str(exc.detail)


# --- the shard payload: executor and lane are independent ---------------------

@pytest.mark.parametrize("render_mode", ["http", "browser", "auto"])
def test_ai_shard_forces_markdown_and_keeps_the_render_lane(loop, monkeypatch, render_mode):
    from services import crawl_orchestrator as co
    from services.brand import CTX_CRAWL_EXTRACT
    from database import AsyncSessionLocal

    _stub_network(monkeypatch)
    _with_provider(monkeypatch)
    _freeze_pump(monkeypatch)

    async def main():
        await _fresh_schema()
        async with AsyncSessionLocal() as db:
            crawl = await co.start_crawl(
                db, seed_url="https://example.com", executor="ai",
                # schema OUTPUT with the ai executor: the agent must still be told
                # markdown — the coordinator's AI pass is what produces records.
                extract_mode="schema", extract_prompt="the product name and price",
                render_mode=render_mode, page_budget=10,
            )
            task = await co._mint_shard_task(
                db, crawl, [{"url": "https://example.com", "depth": 0}])
            await db.commit()
            return (task.trigger_context or {}).get(CTX_CRAWL_EXTRACT) or {}

    extract = loop.run_until_complete(main())
    assert extract["executor"] == "ai"
    assert extract["mode"] == "markdown", "the agent fetches content; the AI pass shapes it"
    assert extract["prompt"] == "the product name and price"
    assert extract["render_mode"] == render_mode, "the AI executor must not override the lane"


def test_regular_executor_leaves_the_agent_output_untouched(loop, monkeypatch):
    from services import crawl_orchestrator as co
    from services.brand import CTX_CRAWL_EXTRACT
    from database import AsyncSessionLocal

    _stub_network(monkeypatch)
    _freeze_pump(monkeypatch)

    async def main():
        await _fresh_schema()
        async with AsyncSessionLocal() as db:
            crawl = await co.start_crawl(
                db, seed_url="https://example.com", extract_mode="schema",
                extract_schema={"row_selector": ".row", "fields": {"t": "h2"}},
                page_budget=10,
            )
            task = await co._mint_shard_task(
                db, crawl, [{"url": "https://example.com", "depth": 0}])
            await db.commit()
            return (task.trigger_context or {}).get(CTX_CRAWL_EXTRACT) or {}

    extract = loop.run_until_complete(main())
    assert extract["executor"] == "regular"
    assert extract["mode"] == "schema"
    assert extract["prompt"] is None


# --- the per-shard AI pass ----------------------------------------------------

def _shard_with_rows(rows):
    return {
        "engine": "http",
        "pages": [{"url": r["url"], "status": "ok"} for r in rows],
        "failed": [],
        "discovered_links": [],
        "extracted_data": rows,
    }


def _run_ai_pass(loop, monkeypatch, rows, reply, *, raises=False):
    """Drive one shard through the coordinator's AI pass and hand back its rows."""
    from services import crawl_orchestrator as co
    from services import agent_brain
    from database import AsyncSessionLocal
    from models.automation_task import AutomationTask
    from utils.redis_client import get_redis

    _stub_network(monkeypatch)
    _with_provider(monkeypatch)
    _freeze_pump(monkeypatch)

    seen = []

    async def _call_ai(messages, system_prompt="", max_tokens=1500, purpose="assist", **kw):
        seen.append((messages, purpose))
        if raises:
            raise RuntimeError("provider down")
        return reply, 10, 10, "test-model"

    monkeypatch.setattr(agent_brain, "call_ai", _call_ai)

    async def main():
        await _fresh_schema()
        async with AsyncSessionLocal() as db:
            crawl = await co.start_crawl(
                db, seed_url="https://example.com", executor="ai",
                extract_prompt="the headline", page_budget=10,
            )
            cid = crawl.id
            task = await co._mint_shard_task(
                db, crawl, [{"url": r["url"], "depth": 0} for r in rows])
            tid = task.id
            task.status = "running"
            task.result_data = _shard_with_rows(rows)
            await db.commit()

        r = get_redis()
        await r.incr(co._k_ai_inflight(cid))
        await co._ai_extract_shard(cid, tid, "the headline")

        async with AsyncSessionLocal() as db:
            done = await db.get(AutomationTask, tid)
            out = dict(done.result_data or {})
        return out, int(await r.get(co._k_ai_inflight(cid)) or 0), seen

    return loop.run_until_complete(main())


def test_ai_pass_replaces_markdown_rows_with_records(loop, monkeypatch):
    rows = [{"url": "https://example.com/a", "markdown": "# A", "depth": 1,
             "content_kind": "html"}]
    out, inflight, seen = _run_ai_pass(
        loop, monkeypatch, rows, '[{"headline": "Hello"}]')

    assert out["ai_extracted"] is True
    assert len(out["extracted_data"]) == 1
    rec = out["extracted_data"][0]
    assert rec["headline"] == "Hello"
    assert "markdown" not in rec
    assert rec["_source_url"] == "https://example.com/a"
    # Lane markers survive the replacement.
    assert rec["content_kind"] == "html" and rec["depth"] == 1
    assert inflight == 0, "the pass must release the convergence gate"
    assert seen and seen[0][1] == "crawl_extract"


def test_ai_pass_carries_a_browser_lane_thumbnail_onto_the_records(loop, monkeypatch):
    """A warm-rendered page stamps its thumbnail on every row it produced. The
    records REPLACE those rows, so without carrying it forward turning the AI
    reader on silently strips the results table's images."""
    rows = [{"url": "https://example.com/a", "markdown": "# A", "depth": 1,
             "content_kind": "html", "screenshot": "/crawl/1/screenshot/tok"}]
    out, _inflight, _seen = _run_ai_pass(
        loop, monkeypatch, rows, '[{"headline": "Hello"}]')

    assert out["extracted_data"][0]["screenshot"] == "/crawl/1/screenshot/tok"


def test_ai_pass_keeps_the_markdown_when_the_provider_fails(loop, monkeypatch):
    rows = [{"url": "https://example.com/a", "markdown": "# A", "depth": 1}]
    out, inflight, _seen = _run_ai_pass(
        loop, monkeypatch, rows, "", raises=True)

    assert out["extracted_data"] == rows, "a provider hiccup must never lose the page"
    assert inflight == 0


def test_ai_pass_keeps_the_markdown_when_the_reply_is_unusable(loop, monkeypatch):
    rows = [{"url": "https://example.com/a", "markdown": "# A", "depth": 1}]
    out, _inflight, _seen = _run_ai_pass(
        loop, monkeypatch, rows, "I'm afraid I can't do that.")

    assert out["extracted_data"] == rows


# --- convergence --------------------------------------------------------------

def test_finalize_waits_for_the_ai_pass(loop, monkeypatch):
    """Nothing queued and nothing fetching is NOT convergence while the AI pass is
    still writing records — reconciling there banks a short records_total."""
    from services import crawl_orchestrator as co
    from database import AsyncSessionLocal
    from models.crawl_job import CrawlJob
    from utils.redis_client import get_redis

    _stub_network(monkeypatch)
    _with_provider(monkeypatch)

    async def main():
        await _fresh_schema()
        async with AsyncSessionLocal() as db:
            crawl = await co.start_crawl(
                db, seed_url="https://example.com", executor="ai",
                extract_prompt="the headline", page_budget=10,
            )
            cid = crawl.id
            crawl.status = "crawling"
            await db.commit()

        r = get_redis()
        await r.incr(co._k_ai_inflight(cid))       # one shard's AI pass in flight
        await co._pump(cid)                        # frontier empty, nothing fetching
        async with AsyncSessionLocal() as db:
            held = (await db.get(CrawlJob, cid)).status

        await r.decr(co._k_ai_inflight(cid))       # the pass drains
        await co._pump(cid)
        async with AsyncSessionLocal() as db:
            settled = (await db.get(CrawlJob, cid)).status
        return held, settled

    held, settled = loop.run_until_complete(main())
    assert held == "crawling", "an in-flight AI pass must hold finalize"
    assert settled == "completed"
