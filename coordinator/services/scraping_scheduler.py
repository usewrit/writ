"""
Scraping Scheduler - Background task for scheduled scrape job dispatch.

Runs every 30 seconds, checks for due ScrapeJobs, and creates
AutomationTasks for agents to execute. Respects rate limit groups
and implements tier promotion on repeated failures.
"""
import asyncio
import logging
from datetime import datetime, timezone, timedelta

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from database import AsyncSessionLocal
from models.scrape_job import ScrapeJob
from models.scrape_result import ScrapeResult
from models.automation_task import AutomationTask

logger = logging.getLogger(__name__)

SCHEDULER_INTERVAL_S = 30  # Check for due jobs every 30 seconds


async def scraping_scheduler():
    """
    Background task that creates AutomationTasks for due scrape jobs.

    Checks every 30 seconds for ScrapeJobs whose next_scheduled_at has passed.
    Creates one AutomationTask per due job, respecting rate limit group concurrency.
    """
    await asyncio.sleep(15)  # Stagger startup to avoid thundering herd
    logger.info("Scraping scheduler started")

    # Reclaim abandoned MCP browser sessions. A session the connected client walked
    # away from holds a real browser open, and only an explicit close frees it —
    # see services.mcp_record.reap_stale.
    #
    # THE SWEEP INTERVAL ADDS DIRECTLY TO THE LEAK: a session goes stale at
    # _IDLE_TTL_S but is only closed on the next sweep, so the real window is
    # TTL + interval (30 + 5 = 35 minutes at the original settings). Derived from
    # the TTL so shortening one can never silently leave the other coarse.
    from services import mcp_record as _mcp_record

    _BROWSER_REAP_SWEEPS_PER_TTL = 4
    browser_reap_every = max(1, int(
        _mcp_record._IDLE_TTL_S / (_BROWSER_REAP_SWEEPS_PER_TTL * SCHEDULER_INTERVAL_S)
    ))
    browser_reap_ticks = 0

    while True:
        try:
            await _process_due_jobs()
        except asyncio.CancelledError:
            logger.info("Scraping scheduler cancelled")
            raise
        except Exception as e:
            logger.error(f"Scraping scheduler error: {e}", exc_info=True)

        browser_reap_ticks += 1
        if browser_reap_ticks % browser_reap_every == 0:
            try:
                await _mcp_record.reap_stale()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — housekeeping never stops the loop
                logger.warning(f"Browser-session reap failed: {e}")

        await asyncio.sleep(SCHEDULER_INTERVAL_S)


async def _process_due_jobs():
    """Find and dispatch all due scrape jobs."""
    async with AsyncSessionLocal() as db:
        now = datetime.now(timezone.utc)

        # Find active, scheduled jobs that are due
        result = await db.execute(
            select(ScrapeJob)
            .where(
                ScrapeJob.is_active == True,
                ScrapeJob.schedule_enabled == True,
                ScrapeJob.next_scheduled_at <= now,
            )
            .order_by(ScrapeJob.next_scheduled_at)
            .limit(20)  # Process at most 20 per cycle to avoid long transactions
        )
        due_jobs = result.scalars().all()

        if not due_jobs:
            return

        logger.info(f"Scraping scheduler: {len(due_jobs)} job(s) due")

        # Group by rate_limit_group to enforce concurrency
        group_concurrency: dict[str, int] = {}

        for job in due_jobs:
            group = job.rate_limit_group

            # Lazy-load group concurrency
            if group not in group_concurrency:
                group_concurrency[group] = await _get_group_concurrency(group, db)

            # Check concurrency limit
            if group_concurrency[group] >= job.max_concurrent_per_group:
                logger.debug(
                    f"Skipping job {job.id} ({job.name}): rate limit group "
                    f"'{group}' at capacity ({group_concurrency[group]}/{job.max_concurrent_per_group})"
                )
                # Still advance next_scheduled_at to avoid stacking up
                job.next_scheduled_at = now + timedelta(milliseconds=job.schedule_interval_ms)
                continue

            # Single-user coordinator: no billing/execution-limit gate.

            # Determine execution tier
            tier = job.effective_tier

            # Create AutomationTask
            task = AutomationTask(
                target_id=None,
                workflow_id=None,
                scrape_job_id=job.id,
                trigger_type="scraping",
                status="pending",
                max_attempts=3,
                trigger_context={
                    "scrape_type": job.scrape_type,
                    "target_config": job.target_config,
                    "tier": tier,
                    "anti_detection_config": job.anti_detection_config or {},
                },
            )
            db.add(task)

            # Advance schedule
            job.last_scheduled_at = now
            job.next_scheduled_at = now + timedelta(milliseconds=job.schedule_interval_ms)

            # Track concurrency
            group_concurrency[group] += 1

            logger.info(
                f"Scheduled scraping task for job {job.id} ({job.name}) "
                f"tier={tier}, next_run={job.next_scheduled_at.isoformat()}"
            )

        await db.commit()


async def _get_group_concurrency(rate_limit_group: str, db: AsyncSession) -> int:
    """Count active scraping tasks in a rate limit group."""
    result = await db.execute(
        select(func.count(AutomationTask.id))
        .join(ScrapeJob, ScrapeJob.id == AutomationTask.scrape_job_id)
        .where(
            AutomationTask.trigger_type == "scraping",
            AutomationTask.status.in_(["pending", "assigned", "running"]),
            ScrapeJob.rate_limit_group == rate_limit_group,
        )
    )
    return result.scalar() or 0
