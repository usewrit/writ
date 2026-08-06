"""A crawl is not a workflow: the two must never read as each other.

A crawl borrows the workflow tables as storage — a synthetic AutomationWorkflow
(``workflow_type='crawl'``) with one AutomationTask (``trigger_type='crawl'``) per
shard. Nothing else about it is a workflow: it has no recorded recipe, its "runs"
are pages, and its result payload is a LIST of page records rather than a single
extracted result.

Everything here pins one shipped bug where that boundary leaked:

* the crawl's dataset row was listed in the workflow library, and opening it
  crashed the whole workflows page — ``WorkflowResponse.last_run_extracted_data``
  was typed ``dict``, so a shard's list of pages failed validation with
  "Input should be a valid dictionary" (which also took out any ordinary workflow
  that extracts a list of rows, e.g. a scraped listing page);
* ``automation_workflows.id`` is a bare SQLite rowid alias — no AUTOINCREMENT — so
  the id freed by a removed crawl is handed to the NEXT workflow created. Any shard
  row that outlived its crawl re-attached to that workflow and showed up as its
  extracted data, and as its last run.
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

_TMP_DB = tempfile.NamedTemporaryFile(prefix="writ-crawl-iso-test-", suffix=".db", delete=False)
_TMP_DB.close()
os.environ["WRIT_DB_PATH"] = _TMP_DB.name
os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("ALLOW_INSECURE_DEV", "true")

pytest.importorskip("fakeredis")


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


async def _seed_crawl(db, name, url, *, pages=1, with_job=True):
    """A crawl exactly as crawl_orchestrator lays it out: synthetic workflow,
    CrawlJob, and one shard task per batch carrying a LIST of page records."""
    from models.automation_task import AutomationTask
    from models.automation_workflow import AutomationWorkflow
    from models.crawl_job import CrawlJob
    from services.brand import CRAWL_STEP_TYPE, CRAWL_TRIGGER_TYPE, CRAWL_WORKFLOW_TYPE

    wf = AutomationWorkflow(
        name=name, workflow_type=CRAWL_WORKFLOW_TYPE,
        steps=[{"id": "1", "type": CRAWL_STEP_TYPE, "config": {}}], form_data={},
    )
    db.add(wf)
    await db.flush()
    if with_job:
        db.add(CrawlJob(seed_url=url, name=name, workflow_id=wf.id, status="completed"))
    for i in range(pages):
        db.add(AutomationTask(
            workflow_id=wf.id, trigger_type=CRAWL_TRIGGER_TYPE, status="success", success=True,
            result_data={"extracted_data": [{"url": f"{url}/p{i}", "content_kind": "html"}]},
        ))
    await db.commit()
    return wf.id


async def _seed_workflow(db, name, records):
    """A recorded workflow whose run extracted a LIST of records."""
    from models.automation_task import AutomationTask
    from models.automation_workflow import AutomationWorkflow

    wf = AutomationWorkflow(
        name=name, workflow_type="recorded",
        steps=[{"id": "1", "type": "extract", "config": {}}], form_data={},
    )
    db.add(wf)
    await db.flush()
    db.add(AutomationTask(
        workflow_id=wf.id, trigger_type="manual", status="success", success=True,
        result_data={"extracted_data": records},
    ))
    await db.commit()
    return wf.id


def test_list_shaped_extracted_data_serializes(loop):
    """The reported 500. A run that extracted a list of rows must serialize."""
    async def _run():
        await _fresh_schema()
        from database import AsyncSessionLocal
        from models.automation_task import AutomationTask
        from models.automation_workflow import AutomationWorkflow
        from routers import automation as A
        from sqlalchemy import select

        async with AsyncSessionLocal() as db:
            wf_id = await _seed_workflow(db, "listing scrape", [
                {"title": "A Light in the Attic", "price": "51.77"},
                {"title": "Tipping the Velvet", "price": "53.74"},
            ])
            wf = await db.get(AutomationWorkflow, wf_id)
            task = (await db.execute(
                select(AutomationTask).where(AutomationTask.workflow_id == wf_id)
            )).scalar_one()

            resp = A.workflow_to_response(wf, last_task=task)
            assert isinstance(resp.last_run_extracted_data, list)
            assert len(resp.last_run_extracted_data) == 2
            assert resp.last_run_has_extracted_data is True

    loop.run_until_complete(_run())


def test_crawl_dataset_is_not_a_workflow(loop):
    """It is absent from the library query and 404s on the recipe surfaces."""
    async def _run():
        await _fresh_schema()
        from database import AsyncSessionLocal
        from fastapi import HTTPException
        from models.automation_workflow import AutomationWorkflow
        from routers import automation as A
        from services.brand import CRAWL_WORKFLOW_TYPE
        from sqlalchemy import select

        async with AsyncSessionLocal() as db:
            crawl_wf_id = await _seed_crawl(db, "books.toscrape.com", "https://books.toscrape.com")
            wf_id = await _seed_workflow(db, "my recorded extractor", [{"title": "x"}])

            listed = (await db.execute(
                select(AutomationWorkflow.id)
                .where(AutomationWorkflow.workflow_type != CRAWL_WORKFLOW_TYPE)
            )).scalars().all()
            assert wf_id in listed
            assert crawl_wf_id not in listed, "a crawl must not appear in the workflow library"

            crawl_wf = await db.get(AutomationWorkflow, crawl_wf_id)
            with pytest.raises(HTTPException) as exc:
                A._reject_crawl_dataset(crawl_wf, crawl_wf_id)
            assert exc.value.status_code == 404

            # ...and a real workflow sails through the same guard.
            A._reject_crawl_dataset(await db.get(AutomationWorkflow, wf_id), wf_id)

    loop.run_until_complete(_run())


def test_recycled_workflow_id_never_inherits_crawl_pages(loop):
    """SQLite hands a removed crawl's id to the next workflow. Even with stray
    shard rows still pointing at that id, the workflow's dataset must show only
    its own records — matching on workflow_id alone is not enough."""
    async def _run():
        await _fresh_schema()
        from database import AsyncSessionLocal
        from models.automation_workflow import AutomationWorkflow
        from routers import automation as A
        from sqlalchemy import text

        async with AsyncSessionLocal() as db:
            crawl_wf_id = await _seed_crawl(
                db, "books.toscrape.com", "https://books.toscrape.com", pages=3)

        # Strand the pages: drop the crawl with FK enforcement off, which is the
        # only way the workflow row can go while its tasks stay (a pre-fix build,
        # or any connection that skipped PRAGMA foreign_keys=ON).
        async with AsyncSessionLocal() as db:
            await db.execute(text("PRAGMA foreign_keys=OFF"))
            await db.execute(text("DELETE FROM crawl_jobs"))
            await db.execute(text(f"DELETE FROM automation_workflows WHERE id = {crawl_wf_id}"))
            await db.commit()
            await db.execute(text("PRAGMA foreign_keys=ON"))

        async with AsyncSessionLocal() as db:
            wf_id = await _seed_workflow(db, "my recorded extractor", [
                {"title": "A Light in the Attic", "price": "51.77"},
                {"title": "Tipping the Velvet", "price": "53.74"},
            ])
            assert wf_id == crawl_wf_id, (
                "expected SQLite to recycle the freed rowid — this test is only "
                "meaningful when the new workflow lands on the crawl's old id"
            )

            wf = await db.get(AutomationWorkflow, wf_id)
            tasks, _ = await A._scan_workflow_data_tasks(db, wf_id, workflow=wf)
            rows = [r for t in tasks for r in (t.result_data or {}).get("extracted_data") or []]
            assert len(tasks) == 1, "the workflow's dataset picked up crawl shard runs"
            assert not any("content_kind" in r for r in rows), f"crawl pages leaked: {rows}"
            assert {r["title"] for r in rows} == {"A Light in the Attic", "Tipping the Velvet"}

    loop.run_until_complete(_run())


def test_purge_clears_orphans_and_spares_live_crawls(loop):
    """Both orphan shapes go; a crawl that still exists is untouched."""
    async def _run():
        await _fresh_schema()
        from database import AsyncSessionLocal
        from models.automation_task import AutomationTask
        from models.automation_workflow import AutomationWorkflow
        from services.crawl_orchestrator import purge_orphaned_crawl_datasets
        from sqlalchemy import select, text

        async with AsyncSessionLocal() as db:
            live_id = await _seed_crawl(db, "live crawl", "https://example.com", pages=2)
            # Shape B: dataset workflow survives, its CrawlJob is gone.
            dead_id = await _seed_crawl(db, "dead crawl", "https://gone.example")
            await db.execute(text("DELETE FROM crawl_jobs WHERE name = 'dead crawl'"))
            await db.commit()
        # Shape A: shard rows with no workflow at all.
        async with AsyncSessionLocal() as db:
            stray_id = await _seed_crawl(db, "stray", "https://stray.example", pages=2)
            await db.execute(text("PRAGMA foreign_keys=OFF"))
            await db.execute(text("DELETE FROM crawl_jobs WHERE name = 'stray'"))
            await db.execute(text(f"DELETE FROM automation_workflows WHERE id = {stray_id}"))
            await db.commit()
            await db.execute(text("PRAGMA foreign_keys=ON"))

        async with AsyncSessionLocal() as db:
            stats = await purge_orphaned_crawl_datasets(db)
        assert stats == {"workflows": 1, "tasks": 3}, stats

        async with AsyncSessionLocal() as db:
            assert await db.get(AutomationWorkflow, dead_id) is None
            assert await db.get(AutomationWorkflow, live_id) is not None
            survivors = (await db.execute(
                select(AutomationTask.workflow_id))).scalars().all()
            assert survivors == [live_id, live_id], survivors

            # Idempotent: a healthy database purges nothing.
            assert await purge_orphaned_crawl_datasets(db) == {"workflows": 0, "tasks": 0}

    loop.run_until_complete(_run())


def test_crawl_shard_does_not_count_as_a_workflow_run(loop):
    """A 30-shard crawl is one crawl, not 30 workflow runs."""
    async def _run():
        await _fresh_schema()
        from database import AsyncSessionLocal
        from models.automation_task import AutomationTask
        from models.automation_workflow import AutomationWorkflow
        from routers.automation import _process_task_completion
        from sqlalchemy import select

        async with AsyncSessionLocal() as db:
            crawl_wf_id = await _seed_crawl(
                db, "books.toscrape.com", "https://books.toscrape.com", pages=0)
            wf = await db.get(AutomationWorkflow, crawl_wf_id)
            before = (wf.total_run_count or 0, wf.usage_count or 0, wf.last_run_at)

            shard = AutomationTask(
                workflow_id=crawl_wf_id, trigger_type="crawl", status="running",
                trigger_context={"_crawl_id": 1, "_crawl_shard": []},
            )
            db.add(shard)
            await db.commit()

            await _process_task_completion(
                db, shard, True,
                result_data={"extracted_data": [
                    {"url": "https://books.toscrape.com/p0", "content_kind": "html"}]},
            )

            wf = await db.get(AutomationWorkflow, crawl_wf_id)
            await db.refresh(wf)
            assert (wf.total_run_count or 0, wf.usage_count or 0, wf.last_run_at) == before, (
                "a crawl shard wrote run counters onto the dataset row"
            )
            done = (await db.execute(
                select(AutomationTask).where(AutomationTask.id == shard.id))).scalar_one()
            assert done.status == "success", "the shard itself must still complete normally"

    loop.run_until_complete(_run())
