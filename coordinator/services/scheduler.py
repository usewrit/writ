"""In-process APScheduler jobs for the self-hosted coordinator.

The coordinator is a single-process, single-user fleet dispatcher: it hands
targets/workflows to a fleet of directly-connected ``writ-agent``s over the WS
(``user_recorder_ws._connections``) and INGESTS their results. It NEVER fetches a
target URL itself. These jobs replace the cloud's Redis-backed distributed
scheduler with a plain ``AsyncIOScheduler`` (no distributed lock is needed —
one process). Every job runs with ``coalesce=True`` and ``max_instances=1`` so a
slow tick can never pile up overlapping runs.

Jobs
----
1. monitor_dispatch (~20s)  — reconcile enabled monitored targets onto connected
   fleet agents (capacity-aware, sticky per target) and push ``assign_targets``.
   Re-distributes/re-pushes ONLY when the assignment inputs changed (enabled
   target set / selector baselines) or the connected-agent set changed
   (agent (re)connect / disconnect). No in-process fetch.
2. monitor_staleness_sweep (~60s) — fire monitor_stale/recovered off missing
   DB reports (services.monitor_health_sweep._run_once).
3. scheduled_workflows (~15s) — dispatch due schedule_enabled workflows to the
   fleet, gated by asyncio.Semaphore(max_concurrent_runs).
4. housekeeping (daily) — retention purge + stored-file cleanup + notification
   log rotation.
5. crawl_sweep (~30s) — Dragnet distributed-crawl crash-safety: reconcile
   in-flight shard counts from real task state and re-pump / finalize stalled
   crawls (services.crawl_orchestrator.sweep_crawls).
6. capacity_reconcile (~60s) — free agent capacity slots whose task already
   reached a terminal state. `active_sessions` is coordinator-owned and moves only
   on reserve/release, so a completion path that skips the release leaks that slot
   permanently and the agent is reported busy forever
   (routers.user_recorder_ws.reconcile_agent_slots).

Call ``build_scheduler()`` to construct the (not-yet-started) scheduler with all
jobs registered; ``main.lifespan`` starts/stops it.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional, Set, Tuple

from sqlalchemy import select, delete, func

logger = logging.getLogger(__name__)

# Job ids (stable — introspected by the runtime test + used for coalescing).
JOB_MONITOR_DISPATCH = "monitor_dispatch"
JOB_STALENESS_SWEEP = "monitor_staleness_sweep"
JOB_SCHEDULED_WORKFLOWS = "scheduled_workflows"
JOB_HOUSEKEEPING = "housekeeping"
JOB_CRAWL_SWEEP = "crawl_sweep"
JOB_CAPACITY_RECONCILE = "capacity_reconcile"

# Intervals (seconds).
MONITOR_DISPATCH_INTERVAL_S = 20
STALENESS_SWEEP_INTERVAL_S = 60
SCHEDULED_WORKFLOWS_INTERVAL_S = 15
HOUSEKEEPING_INTERVAL_S = 24 * 3600
# Dragnet crawl crash-safety sweep — reconcile in-flight shard counts from real
# task state and re-pump / finalize stalled crawls (services.crawl_orchestrator).
CRAWL_SWEEP_INTERVAL_S = 30
# Slot reconciliation is a SAFETY NET, not the release path — releases happen
# inline on completion. 60s is frequent enough that a leaked slot cannot wedge a
# fleet for long, and rare enough to stay off the hot path.
CAPACITY_RECONCILE_INTERVAL_S = 60

# Module-level reconcile fingerprint: the last (assignment-inputs, connected-agent
# set) signature the monitor-dispatch job acted on. A tick that sees the same
# signature skips the whole-fleet redistribute + re-push (deterministic recompute
# → same assignments), so we honour "re-push only on change / (re)connect".
_last_reconcile_sig: Optional[Tuple[str, frozenset]] = None
# Last capacity/distribution stats from the monitor-dispatch reconcile, cached so
# the fleet-capacity advisory endpoint can report authoritative slot headroom
# without forcing its own redistribution. None until the first reconcile runs.
_last_reconcile_stats: Optional[dict] = None


def get_last_reconcile_stats() -> Optional[dict]:
    """The most recent monitor-dispatch distribution stats (or None)."""
    return _last_reconcile_stats


# ---------------------------------------------------------------------------
# Job 1 — monitor dispatch / reconcile
# ---------------------------------------------------------------------------
async def _connected_agent_ids() -> Set[str]:
    """agent_ids holding a live WS on THIS process (no external ws-gateway)."""
    try:
        from routers.user_recorder_ws import _connections
        return set(_connections.keys())
    except Exception:  # pragma: no cover - defensive
        return set()


async def _assignment_input_signature(db) -> str:
    """A cheap fingerprint of everything the distribution reads: the enabled-target
    set + each selector's baseline hash. If this is unchanged AND the connected
    agent set is unchanged, a redistribution would deterministically reproduce the
    current assignments, so we skip it (sticky per target)."""
    from models.target import Target
    from models.target_selector import TargetSelector

    target_rows = (await db.execute(
        select(Target.id, Target.url, Target.check_period_ms)
        .where(Target.enabled == True)  # noqa: E712
        .order_by(Target.id)
    )).all()
    sel_rows = (await db.execute(
        select(TargetSelector.id, TargetSelector.target_id, TargetSelector.baseline_hash)
        .order_by(TargetSelector.id)
    )).all()
    import hashlib
    # A change-detection fingerprint over the current target/selector set, used to
    # decide whether assignments need recomputing. Not a signature — nothing trusts
    # it across a boundary — so SHA-1 is fine and the flag records that intent.
    h = hashlib.sha1(usedforsecurity=False)
    for tid, url, period in target_rows:
        h.update(f"{tid}|{url}|{period}\n".encode("utf-8", "replace"))
    h.update(b"--selectors--\n")
    for sid, tid, bh in sel_rows:
        h.update(f"{sid}|{tid}|{bh}\n".encode("utf-8", "replace"))
    return h.hexdigest()


async def monitor_dispatch_tick() -> None:
    """Reconcile monitored targets onto connected fleet agents and push assignments.

    Distributes STICKY per target via the capacity-aware distributor (which also
    pushes ``assign_targets`` to every directly-connected recorder). Only runs the
    whole-fleet redistribution when the assignment inputs or the connected-agent
    set changed since the last tick. NEVER fetches a target itself.
    """
    global _last_reconcile_sig

    from database import AsyncSessionLocal
    from models.agent import Agent, AgentStatus
    from services.capacity_aware_distributor import CapacityAwareDistributor
    from services.monitoring_dispatch import push_to_agent_id, _config_int

    connected = await _connected_agent_ids()

    async with AsyncSessionLocal() as db:
        # Which ACTIVE agents are also live on the WS right now — the fleet we can
        # actually dispatch to. (Poll agents have no live socket; skip them here.)
        active_ids = set((await db.execute(
            select(Agent.agent_id).where(Agent.status == AgentStatus.ACTIVE)
        )).scalars().all())
        live_fleet = frozenset(connected & active_ids)

        input_sig = await _assignment_input_signature(db)
        sig = (input_sig, live_fleet)

        if sig == _last_reconcile_sig:
            # Nothing that affects assignments changed and no agent (re)connected /
            # disconnected → the current assignments already hold. Skip the
            # redistribute + re-push (idempotent, deterministic).
            return

        prev = _last_reconcile_sig
        _last_reconcile_sig = sig

        if not live_fleet:
            logger.debug("monitor_dispatch: no connected fleet agents; nothing to dispatch")
            return

        global_period_ms = await _config_int(db, "global_period_ms", 60000)

        # The signature changed, so at least one of: assignment inputs changed, or
        # the connected-fleet set changed (an agent (re)connected or disconnected).
        # In every such case we redistribute STICKY per target — the capacity-aware
        # distributor keeps stable targets on their current agent and only reassigns
        # a disconnected agent's targets to survivors — and it pushes assign_targets
        # to every directly-connected recorder, so a (re)connected agent gets its
        # frame here. Unchanged ticks were already short-circuited above, honouring
        # "re-push only on assignment/baseline change or agent (re)connect".
        prev_fleet = prev[1] if prev else frozenset()
        newly_connected = live_fleet - prev_fleet

        try:
            stats = await CapacityAwareDistributor(db).distribute_timeslots_and_targets(
                global_period_ms
            )
            # Cache for the fleet-capacity advisory endpoint (authoritative headroom).
            global _last_reconcile_stats
            _last_reconcile_stats = stats
            logger.info(
                "monitor_dispatch: reconciled to %d fleet agent(s): %s",
                len(live_fleet), stats,
            )
        except Exception as e:  # noqa: BLE001
            logger.error("monitor_dispatch: distribution failed: %s", e, exc_info=True)
            # Force a retry next tick.
            _last_reconcile_sig = prev
            return

        # Belt-and-braces: make sure any agent that JUST (re)connected also has its
        # current assignment pushed even if the distributor's own connected-recorder
        # push skipped it (e.g. registry race on a fresh connect).
        if newly_connected:
            for aid in newly_connected:
                try:
                    await push_to_agent_id(aid)
                except Exception as e:  # noqa: BLE001
                    logger.warning("monitor_dispatch: push to %s failed: %s", aid, e)


# ---------------------------------------------------------------------------
# Job 2 — staleness sweep
# ---------------------------------------------------------------------------
async def staleness_sweep_tick() -> None:
    """Fire monitor_stale/recovered off missing DB reports (was previously
    unwired). Delegates to the existing single-shot sweep."""
    from services.monitor_health_sweep import _run_once
    await _run_once()


# ---------------------------------------------------------------------------
# Job 3 — scheduled workflows
# ---------------------------------------------------------------------------
async def _max_concurrent_runs(db) -> int:
    """Governor ceiling for scheduled dispatch.

    Reads, in order: the operator's **Settings → Runtime** value, a legacy
    standalone `max_concurrent_runs` Config row, then the env default.

    The first source is the fix for a dead control: Settings → Runtime writes the
    whole section as one JSON row under `coordinator_runtime`
    (`services.coordinator_settings.KEY_RUNTIME`), while this function only ever
    looked for a *top-level* Config row literally keyed `max_concurrent_runs`.
    Nothing has ever written that key from the UI, so the number the operator set
    was silently ignored and the scheduler always used the env default. The legacy
    key is still honoured second, since an older build or a manual insert may have
    populated it.
    """
    from config import settings
    from models.config import Config
    from services.coordinator_settings import KEY_RUNTIME

    def _as_int(raw) -> int | None:
        if isinstance(raw, dict):
            raw = raw.get("value")
        try:
            n = int(raw)
        except (TypeError, ValueError):
            return None
        return n if n >= 1 else None

    rows = (await db.execute(
        select(Config).where(Config.key.in_([KEY_RUNTIME, "max_concurrent_runs"]))
    )).scalars().all()
    by_key = {r.key: r.value for r in rows}

    val = None
    section = by_key.get(KEY_RUNTIME)
    if isinstance(section, dict):
        val = _as_int(section.get("max_concurrent_runs"))
    if val is None and "max_concurrent_runs" in by_key:
        val = _as_int(by_key["max_concurrent_runs"])
    if val is None:
        val = int(getattr(settings, "max_concurrent_runs", 5) or 5)
    return max(1, val)


def scheduled_recurrence_from_blocks(blocks) -> Optional[dict]:
    """Extract the recurrence of a SCHEDULED-root automation from its block tree.

    Returns ``{kind, interval_ms, time, days, tz}`` when `blocks` is a block tree
    whose ROOT event block (``type == "event"`` with no ``parentId``) has
    ``blockType == "scheduled"``, reading the recurrence from that block's
    ``config`` (``mode`` -> kind; ``interval_ms`` or ``interval_minutes * 60000``;
    ``time``; ``days``; ``tz``). Returns None for any non-scheduled-root automation.

    Mirrors the cloud's `central_scheduler.scheduled_recurrence_from_blocks` and
    the desktop daemon's `scheduled_recurrence` (scheduled_automations.rs) — one
    block shape, three runtimes. The values feed
    `schedule_recurrence.compute_next_run(kind, now, interval_ms, time, days, tz)`
    directly.
    """
    if not isinstance(blocks, list):
        return None
    root = None
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "event":
            continue
        if block.get("parentId"):
            continue
        root = block
        break
    if root is None:
        return None
    if (root.get("blockType") or root.get("block_type")) != "scheduled":
        return None

    cfg = root.get("config") or {}
    if not isinstance(cfg, dict):
        cfg = {}

    # `mode` (block naming) maps to schedule_recurrence's `kind`. Absent ⇒ interval.
    kind = (cfg.get("mode") or "interval")
    if isinstance(kind, str):
        kind = kind.lower()
    else:
        kind = "interval"

    # interval_ms wins; interval_minutes is a back-compat shim (×60000).
    interval_ms = cfg.get("interval_ms")
    if interval_ms is None and cfg.get("interval_minutes") is not None:
        try:
            interval_ms = int(cfg["interval_minutes"]) * 60000
        except (TypeError, ValueError):
            interval_ms = None

    days = cfg.get("days")
    if not isinstance(days, list):
        days = None

    return {
        "kind": kind,
        "interval_ms": interval_ms,
        "time": cfg.get("time"),
        "days": days,
        "tz": cfg.get("tz"),
    }


async def _dispatch_due_scheduled_automations(db, now: datetime) -> int:
    """Fire all DUE standalone SCHEDULED automations (trigger rules whose ROOT
    event block is blockType == "scheduled") as of `now`. Returns the count fired.

    Mirrors the workflow due-scan lane above and the cloud's identical lane: for
    each due rule ADVANCE FIRST (stamp last_triggered_at = now, recompute
    next_scheduled_at from the block's recurrence config) and commit, so a slow or
    failing fire can't leave the rule perpetually due, THEN fire via
    UnifiedTriggerService.process_scheduled_automation — the same firing path the
    event-driven automations use, so guardrails (cooldown / max_fires) and block
    processing apply unchanged. Per-rule failures are isolated (logged + continue)
    so one bad automation can't wedge the tick.
    """
    from models.trigger_rule import TriggerRule
    from services.schedule_recurrence import compute_next_run
    from services.unified_trigger_service import UnifiedTriggerService

    result = await db.execute(
        select(TriggerRule).where(
            TriggerRule.enabled == True,  # noqa: E712
            TriggerRule.next_scheduled_at.isnot(None),
            TriggerRule.next_scheduled_at <= now,
        )
    )
    due_rules = result.scalars().all()
    if not due_rules:
        return 0

    fired = 0
    for rule in due_rules:
        # Re-confirm this is genuinely a scheduled-root automation and read its
        # recurrence from the block tree. If it isn't (e.g. blocks were rewritten
        # to a non-scheduled root while a stale next_scheduled_at lingered), clear
        # the cadence so it drops out of the due index instead of firing every tick.
        recurrence = scheduled_recurrence_from_blocks(rule.blocks)
        if recurrence is None:
            rule.next_scheduled_at = None
            await db.commit()
            continue

        # ADVANCE FIRST: recompute the next occurrence from the block config, then
        # COMMIT — so even if the fire below is slow or raises, the rule is no
        # longer due (it retries next occurrence). Deliberately does NOT stamp
        # last_triggered_at: due-ness is purely next_scheduled_at, and the cooldown
        # guardrail measures from last_triggered_at — stamping it here would make
        # any scheduled automation with a cooldown_minutes suppress ITSELF on every
        # tick. The service stamps it on an actual successful fire.
        rule.next_scheduled_at = compute_next_run(
            recurrence["kind"],
            now,
            recurrence["interval_ms"],
            recurrence["time"],
            recurrence["days"],
            recurrence["tz"],
        )
        await db.commit()

        # FIRE via the shared firing path. process_scheduled_automation re-stamps
        # last_triggered_at/trigger_count + commits its own execution record; the
        # cadence advance above is what guards against double-fire.
        try:
            await UnifiedTriggerService(db).process_scheduled_automation(rule)
            fired += 1
        except Exception as e:
            logger.error(
                "scheduled_automations: fire failed for rule %s: %s", rule.id, e,
            )
            # Roll back any partial fire state so the session stays clean for the
            # next due rule; the cadence was already advanced + committed above.
            try:
                await db.rollback()
            except Exception:
                pass

    return fired


async def scheduled_workflows_tick() -> None:
    """Dispatch due schedule_enabled workflows to the fleet, gated by a semaphore
    sized to max_concurrent_runs. Advances next_scheduled_at so a slow/queued run
    can't stack up.

    Also fires due SCHEDULED AUTOMATIONS (time-rooted trigger rules) — same tick,
    same cadence: both are "scan a next_scheduled_at index, advance, dispatch",
    and a separate job id would buy nothing but registration ceremony."""
    from database import AsyncSessionLocal
    from models.automation_workflow import AutomationWorkflow

    async with AsyncSessionLocal() as db:
        now = datetime.now(timezone.utc)
        limit = await _max_concurrent_runs(db)

        # Time-rooted AUTOMATIONS first (cheap: the due index is NULL for every
        # event-driven rule). Isolated so a bad rule can't cost the workflow lane.
        try:
            fired = await _dispatch_due_scheduled_automations(db, now)
            if fired:
                logger.info("scheduled_automations: %d automation(s) fired", fired)
        except Exception as e:
            logger.error("scheduled_automations: lane failed: %s", e)
            try:
                await db.rollback()
            except Exception:
                pass

        # Due-scan: interval workflows need a schedule_interval_ms; daily/weekly
        # workflows have a NULL interval but a computed next_scheduled_at, so gate
        # on next_scheduled_at being set rather than on the interval column. The
        # next-run computation (router + advance below) never sets
        # next_scheduled_at unless the schedule is actually runnable.
        due_ids = (await db.execute(
            select(AutomationWorkflow.id)
            .where(
                AutomationWorkflow.schedule_enabled == True,  # noqa: E712
                AutomationWorkflow.is_active == True,  # noqa: E712
                AutomationWorkflow.next_scheduled_at.isnot(None),
                AutomationWorkflow.next_scheduled_at <= now,
            )
            .order_by(AutomationWorkflow.next_scheduled_at)
            .limit(limit)
        )).scalars().all()

    if not due_ids:
        return

    # Gate true concurrency at max_concurrent_runs. Each workflow dispatches in its
    # OWN session so a failed dispatch (rolled-back session) can't poison the others
    # or the schedule-advance write.
    sem = asyncio.Semaphore(limit)
    results = await asyncio.gather(
        *(_dispatch_one_scheduled(wf_id, now, sem) for wf_id in due_ids)
    )
    dispatched = sum(1 for ok in results if ok)
    logger.info(
        "scheduled_workflows: %d/%d due workflow(s) dispatched", dispatched, len(due_ids)
    )


async def _dispatch_one_scheduled(workflow_id: int, now: datetime, sem: asyncio.Semaphore) -> bool:
    """Dispatch a single due workflow in an isolated session; always advance its
    schedule so it can't stack up. Returns True if the dispatch itself succeeded."""
    from database import AsyncSessionLocal
    from models.automation_workflow import AutomationWorkflow
    from routers.automation import _dispatch_to_recorder_or_queue

    async with sem:
        ok = False
        async with AsyncSessionLocal() as db:
            workflow = await db.get(AutomationWorkflow, workflow_id)
            if workflow is None:
                return False
            try:
                # target_id=None: a standalone scheduled workflow is not tied to a
                # monitored target. AutomationTask.target_id is nullable for exactly
                # this case; the dispatch default of 0 would violate the targets.id
                # foreign key (no target id 0).
                await _dispatch_to_recorder_or_queue(
                    db=db,
                    workflow=workflow,
                    target_id=None,
                    trigger_type="scheduled",
                )
                # The dispatch helper stages the AutomationTask but leaves the commit
                # to its caller (the route handler normally commits).
                await db.commit()
                ok = True
            except Exception as e:  # noqa: BLE001 — one bad workflow can't kill the batch
                logger.warning(
                    "scheduled_workflows: dispatch failed for workflow %s: %s",
                    workflow_id, e,
                )
                # The dispatch may have poisoned this session mid-flush; roll back so
                # the schedule-advance below runs on a clean transaction.
                await db.rollback()

        # Advance the schedule in a fresh session regardless (mirrors the scraping
        # scheduler's advance-on-dispatch behaviour), so a failing workflow doesn't
        # re-fire every tick.
        async with AsyncSessionLocal() as db:
            workflow = await db.get(AutomationWorkflow, workflow_id)
            if workflow is not None:
                from services.schedule_recurrence import compute_next_run
                workflow.last_scheduled_at = now
                # Structured recurrence: interval kind is byte-identical to the old
                # `now + interval` (default 60s floor); daily/weekly compute the next
                # local wall-clock occurrence.
                workflow.next_scheduled_at = compute_next_run(
                    workflow.schedule_kind,
                    now,
                    workflow.schedule_interval_ms or 60000,
                    workflow.schedule_time,
                    workflow.schedule_days,
                    workflow.schedule_tz,
                )
                await db.commit()
        return ok


# ---------------------------------------------------------------------------
# Job 4 — housekeeping
# ---------------------------------------------------------------------------
async def housekeeping_tick() -> None:
    """Daily retention purge + stored-file cleanup + notification-log rotation."""
    from config import settings
    from database import AsyncSessionLocal
    from models.automation_task import AutomationTask
    from models.detected_change import DetectedChange
    from models.uptime_check import UptimeCheck
    from models.notification_log import NotificationLog

    now = datetime.now(timezone.utc)
    run_days = int(getattr(settings, "run_retention_days", 90) or 90)
    change_days = int(getattr(settings, "detected_change_retention_days", 90) or 90)
    log_days = int(getattr(settings, "log_retention_days", 90) or 90)

    run_cutoff = now - timedelta(days=run_days)
    change_cutoff = now - timedelta(days=change_days)
    log_cutoff = now - timedelta(days=log_days)

    async with AsyncSessionLocal() as db:
        # Runs: terminal AutomationTasks older than the run-retention window.
        res = await db.execute(
            delete(AutomationTask).where(
                AutomationTask.created_at < run_cutoff,
                AutomationTask.status.in_(["success", "failed", "timeout", "cancelled"]),
            )
        )
        runs_purged = res.rowcount or 0

        # Uptime checks (per-check history) older than the run-retention window.
        res = await db.execute(
            delete(UptimeCheck).where(UptimeCheck.checked_at < run_cutoff)
        )
        uptime_purged = res.rowcount or 0

        # Detected changes older than the change-retention window.
        res = await db.execute(
            delete(DetectedChange).where(DetectedChange.last_detected_at < change_cutoff)
        )
        changes_purged = res.rowcount or 0

        # Notification-log rotation: drop rows older than the log-retention window.
        res = await db.execute(
            delete(NotificationLog).where(NotificationLog.sent_at < log_cutoff)
        )
        notif_purged = res.rowcount or 0

        await db.commit()

        # Stored-file cleanup: expired ephemerals + already-soft-deleted rows whose
        # storage bytes may linger. delete_file is idempotent + best-effort at the
        # storage layer. Runs in its own session so a storage hiccup can't roll back
        # the purge above.
        files_swept = await _sweep_stored_files(now)

    # Staged transfer packages hold DECRYPTED work (spec §10), so they are swept on
    # their own short TTL rather than left until someone finishes an abandoned import
    # wizard. Scrubs the row's payload, deletes the staged file, and reclaims files
    # whose row is already gone (the crash-between-write-and-commit case). Own
    # session; never fatal to the rest of housekeeping.
    transfers_swept = 0
    try:
        from services import transfer_import_service as _transfer

        async with AsyncSessionLocal() as t_db:
            transfers_swept = await _transfer.sweep_expired(t_db)
    except Exception as exc:  # noqa: BLE001
        logger.warning("housekeeping: staged transfer sweep failed: %s", exc)

    logger.info(
        "housekeeping: purged runs=%d uptime=%d changes=%d notif_logs=%d files=%d transfers=%d",
        runs_purged, uptime_purged, changes_purged, notif_purged, files_swept, transfers_swept,
    )


async def _sweep_stored_files(now: datetime) -> int:
    """Hard-delete stored files whose ephemeral TTL has lapsed (soft-delete + drop
    the storage object). Best-effort per file so one failure can't abort the sweep."""
    from database import AsyncSessionLocal
    from models.stored_file import StoredFile
    from services import file_service

    swept = 0
    async with AsyncSessionLocal() as db:
        expired = (await db.execute(
            select(StoredFile.id).where(
                StoredFile.deleted_at.is_(None),
                StoredFile.expires_at.isnot(None),
                StoredFile.expires_at < now,
            )
        )).scalars().all()
        for fid in expired:
            try:
                await file_service.delete_file(db, str(fid))
                swept += 1
            except Exception as e:  # noqa: BLE001
                logger.warning("housekeeping: stored-file sweep failed for %s: %s", fid, e)
        if swept:
            await db.commit()
    return swept


# ---------------------------------------------------------------------------
# Job 5 — Dragnet crawl crash-safety sweep
# ---------------------------------------------------------------------------
async def capacity_reconcile_tick() -> None:
    """Free agent capacity slots whose task is no longer running.

    `active_sessions` is coordinator-owned in-memory state adjusted only by
    reserve/release. A completion path that stamps a task terminal without
    releasing leaks that slot FOREVER — the agent is reported busier than it is and,
    at max_sessions, silently stops receiving work. This derives the truth from the
    task table instead of trusting the counter, so the fleet self-heals.
    """
    from database import AsyncSessionLocal
    from routers.user_recorder_ws import reconcile_agent_slots

    async with AsyncSessionLocal() as db:
        await reconcile_agent_slots(db)


async def crawl_sweep_tick() -> None:
    """Reconcile non-terminal Dragnet crawls against real shard-task state and
    re-pump / finalize any that stalled (a shard died/expired without routing
    through on_shard_complete). Delegates to the orchestrator's sweep."""
    from services.crawl_orchestrator import sweep_crawls
    await sweep_crawls()


# ---------------------------------------------------------------------------
# Scheduler assembly
# ---------------------------------------------------------------------------
def build_scheduler():
    """Construct (but do not start) the AsyncIOScheduler with all jobs.

    Single-process coordinator → no distributed lock. Every job is coalesced with
    max_instances=1 so a slow tick can never overlap itself.
    """
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    scheduler = AsyncIOScheduler(
        job_defaults={
            "coalesce": True,
            "max_instances": 1,
            "misfire_grace_time": 30,
        }
    )

    scheduler.add_job(
        monitor_dispatch_tick,
        trigger="interval",
        seconds=MONITOR_DISPATCH_INTERVAL_S,
        id=JOB_MONITOR_DISPATCH,
        name="Monitor dispatch / reconcile",
        next_run_time=_soon(5),
        replace_existing=True,
    )
    scheduler.add_job(
        staleness_sweep_tick,
        trigger="interval",
        seconds=STALENESS_SWEEP_INTERVAL_S,
        id=JOB_STALENESS_SWEEP,
        name="Monitor staleness sweep",
        next_run_time=_soon(STALENESS_SWEEP_INTERVAL_S),
        replace_existing=True,
    )
    scheduler.add_job(
        scheduled_workflows_tick,
        trigger="interval",
        seconds=SCHEDULED_WORKFLOWS_INTERVAL_S,
        id=JOB_SCHEDULED_WORKFLOWS,
        name="Scheduled workflow dispatch",
        next_run_time=_soon(10),
        replace_existing=True,
    )
    scheduler.add_job(
        housekeeping_tick,
        trigger="interval",
        seconds=HOUSEKEEPING_INTERVAL_S,
        id=JOB_HOUSEKEEPING,
        name="Retention housekeeping",
        next_run_time=_soon(HOUSEKEEPING_INTERVAL_S),
        replace_existing=True,
    )
    scheduler.add_job(
        capacity_reconcile_tick,
        trigger="interval",
        seconds=CAPACITY_RECONCILE_INTERVAL_S,
        id=JOB_CAPACITY_RECONCILE,
        name="Agent capacity reconcile",
        next_run_time=_soon(CAPACITY_RECONCILE_INTERVAL_S),
        replace_existing=True,
    )
    scheduler.add_job(
        crawl_sweep_tick,
        trigger="interval",
        seconds=CRAWL_SWEEP_INTERVAL_S,
        id=JOB_CRAWL_SWEEP,
        name="Dragnet crawl sweep",
        next_run_time=_soon(CRAWL_SWEEP_INTERVAL_S),
        replace_existing=True,
    )

    return scheduler


def _soon(delay_s: float) -> datetime:
    """First-run time `delay_s` seconds from now (staggers job startup)."""
    return datetime.now(timezone.utc) + timedelta(seconds=delay_s)
