"""A crawl shard's page thumbnails must be offloaded on BOTH completion funnels.

A shard can land two ways on this coordinator:

* the crawl-native THIN funnel — ``crawl_orchestrator.complete_shard_task``, one
  atomic claim UPDATE, used by the high-frequency fan-in path;
* the generic FULL funnel — ``routers.automation._process_task_completion``,
  shared with ordinary workflow runs (HTTP ``/tasks/{id}/complete`` and the WS
  handler both land here).

The agent ships each browser-rendered page's thumbnail as inline base64
(``screenshot_b64``) on the page meta AND on its extracted-data row. That base64
must never reach the persisted ``result_data``: it is stored out-of-row and the
row is rewritten to a served ``/crawl/{id}/screenshot/{token}`` path the results
grid loads via ``<AuthImage>``.

The full funnel used to skip that step, so an otherwise identical shard produced a
DIFFERENT row shape depending on which funnel claimed it — bloated rows carrying
raw base64, and a thumbnail the grid could never resolve. This pins both funnels
to the same shape.
"""
import asyncio
import base64
import os
import re
import sys
import tempfile
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# Point the app at a throwaway SQLite file BEFORE `database` is imported (the
# engine is built at import time from settings.database_url).
_TMP_DB = tempfile.NamedTemporaryFile(prefix="writ-thumb-funnel-test-", suffix=".db", delete=False)
_TMP_DB.close()
os.environ["WRIT_DB_PATH"] = _TMP_DB.name
os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("ALLOW_INSECURE_DEV", "true")

pytest.importorskip("fakeredis")

_THUMB_B64 = base64.b64encode(b"\xff\xd8\xff-not-a-real-jpeg").decode()
_SERVED_PATH = re.compile(r"^/crawl/\d+/screenshot/[A-Za-z0-9_-]+$")


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


def _stub_network(monkeypatch):
    """Neutralise every egress seam start_crawl touches, plus the detached seeder
    and the pump (this test owns the timing and never dispatches a shard). Each
    stub absorbs any argument, so a signature change upstream can't turn into a
    TypeError thrown from inside the code under test."""
    from services import crawl_orchestrator as co
    from services import domain_guard, robots_guard, url_policy

    class _Ok:
        allowed = True
        message = None

    async def _check_url(*a, **kw):
        return _Ok()

    async def _robots_ok(*a, **kw):
        return True

    async def _empty(*a, **kw):
        return []

    async def _none(*a, **kw):
        return None

    monkeypatch.setattr(url_policy, "check_url", _check_url)
    monkeypatch.setattr(co.url_policy, "check_url", _check_url)
    monkeypatch.setattr(robots_guard, "is_allowed", _robots_ok)
    monkeypatch.setattr(co.robots_guard, "is_allowed", _robots_ok)
    monkeypatch.setattr(co, "_discover_sitemap_urls", _empty)
    monkeypatch.setattr(co, "_harvest_seed_links", _empty)
    monkeypatch.setattr(domain_guard, "ensure_loaded", _none)
    monkeypatch.setattr(co, "_seed", _none)
    monkeypatch.setattr(co, "_pump", _none)


def _stub_thumbnail_storage(monkeypatch):
    """Accept every thumbnail write and record (crawl_id, token).

    The shipped storage backend declines thumbnails outright when neither MinIO
    nor WRIT_FILES_DIR is configured (as in a bare test run), which would hide the
    difference between the two funnels — both rows would simply lose the key. A
    stub that SUCCEEDS makes the served path observable, and the recorded calls
    prove the page meta and its row share one stored object."""
    from services import visual_storage

    stored: list = []

    def _store(b64, crawl_id, token):
        stored.append((int(crawl_id), token, b64))
        return True

    monkeypatch.setattr(visual_storage, "store_crawl_thumbnail_b64", _store)
    return stored


def _payload(url: str) -> dict:
    """A browser-lane shard result as the agent ships it: the SAME inline
    thumbnail on the page meta and on the extracted row."""
    return {
        "engine": "browser",
        "pages": [{"url": url, "status": "ok", "title": "Home",
                   "screenshot_b64": _THUMB_B64}],
        "failed": [],
        "discovered_links": [],
        "extracted_data": [{"url": url, "markdown": "# Home",
                            "screenshot_b64": _THUMB_B64}],
    }


def _rows(result_data: dict) -> list:
    return list(result_data.get("pages") or []) + list(result_data.get("extracted_data") or [])


def test_both_completion_funnels_offload_page_thumbnails(loop, monkeypatch):
    """Two shards of one crawl, byte-identical payloads, one through each funnel:
    neither persists base64, both carry a served path, and the row shapes match."""
    from database import AsyncSessionLocal
    from models.automation_task import AutomationTask
    from services import crawl_orchestrator as co
    import routers.automation as automation_router

    _stub_network(monkeypatch)
    stored = _stub_thumbnail_storage(monkeypatch)

    async def main():
        await _fresh_schema()
        async with AsyncSessionLocal() as db:
            crawl = await co.start_crawl(
                db, seed_url="https://example.com", page_budget=100, max_depth=0,
            )
            cid = crawl.id
            thin = await co._mint_shard_task(
                db, crawl, [{"url": "https://example.com/thin", "depth": 0}])
            full = await co._mint_shard_task(
                db, crawl, [{"url": "https://example.com/full", "depth": 0}])
            thin.status = "running"
            full.status = "running"
            thin_id, full_id = thin.id, full.id
            await db.commit()

        # THIN funnel — the crawl-native claim.
        await co.complete_shard_task(
            thin_id, cid, success=True,
            result_data=_payload("https://example.com/thin"), reporter_agent="agent-1")

        # FULL funnel — the shared workflow-completion path.
        async with AsyncSessionLocal() as db:
            task = await db.get(AutomationTask, full_id)
            await automation_router._process_task_completion(
                db, task, True, result_data=_payload("https://example.com/full"))
            await db.commit()

        async with AsyncSessionLocal() as db:
            thin_row = await db.get(AutomationTask, thin_id)
            full_row = await db.get(AutomationTask, full_id)
            return cid, thin_row.result_data, full_row.result_data

    cid, thin_data, full_data = loop.run_until_complete(main())

    for funnel, data in (("thin", thin_data), ("full", full_data)):
        rows = _rows(data)
        assert len(rows) == 2, f"{funnel}: payload lost a page/row"
        for row in rows:
            assert "screenshot_b64" not in row, (
                f"{funnel} funnel persisted raw base64 into result_data")
            assert _SERVED_PATH.match(row.get("screenshot") or ""), (
                f"{funnel} funnel did not rewrite the thumbnail to a served path: "
                f"{row.get('screenshot')!r}")

    # Same shape from either funnel — that is the whole point.
    assert [sorted(r) for r in _rows(thin_data)] == [sorted(r) for r in _rows(full_data)]

    # One stored object per shard: the page meta and its row share a thumbnail and
    # must not be stored (or tokenised) twice.
    assert len(stored) == 2, f"expected one stored thumbnail per shard, got {stored}"
    assert {c for c, _, _ in stored} == {cid}
    assert {b for _, _, b in stored} == {_THUMB_B64}
    assert len({t for _, t, _ in stored}) == 2, "each shard mints its own token"
