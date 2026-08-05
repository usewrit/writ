"""
WorkflowQueue — persistent workflow execution queue backed by AutomationTask.

Tasks with status="queued" wait for recorder capacity. A background processor
dequeues them by priority (direct > scheduled) as slots become available.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import List

from sqlalchemy import select, func, update
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from models.automation_task import AutomationTask
from models.automation_workflow import AutomationWorkflow
from services.recorder_capacity_manager import RecorderCapacityManager
from services.workflow_router import TrafficType

logger = logging.getLogger(__name__)

MAX_QUEUE = 50
# A user-initiated (direct) run waits for a free agent rather than failing on a
# short timer — clicking "run" means "run it when an agent is available". Kept
# finite only as a safety net against zombie rows; the queue depth cap is the
# real backpressure.
QUEUE_EXPIRY_DIRECT = 7 * 24 * 3600   # 7 days (effectively "wait for an agent")
QUEUE_EXPIRY_SCHEDULED = 1800         # 30 min for scheduled workflows (next tick supersedes)


async def _release_queue_usage(db: AsyncSession, task) -> None:
    """No-op on the single-user coordinator.

    In the cloud this released a queued MARKETPLACE task's creator usage-limit
    reservations (per-buyer concurrency slot + window reservation) on a non-success
    terminal reached off the _process_task_completion path. There is no marketplace
    metering here, so there is nothing to release."""
    return


class WorkflowQueue:

    @staticmethod
    async def enqueue(
        db: AsyncSession,
        workflow: AutomationWorkflow,
        target_id: int,
        trigger_type: str,
        trigger_rule_id: int = None,
        trigger_context: dict = None,
        traffic_type: str = "direct",
        form_data: dict = None,
        task_tenant_id=None,
    ) -> AutomationTask:
        """Create a queued task.

        ``task_tenant_id`` is accepted for call-site compatibility and ignored —
        the coordinator has no per-owner task scoping, queue-depth partitioning,
        or plan-tier priority.
        """
        if traffic_type == "scheduled":
            interval_s = (workflow.schedule_interval_ms or 60000) / 1000
            expiry_seconds = min(QUEUE_EXPIRY_SCHEDULED, int(interval_s * 0.8))
        else:
            # direct + called both "wait for an agent" (long expiry).
            expiry_seconds = QUEUE_EXPIRY_DIRECT

        # Single owner: no plan tiers, so pure FIFO within a traffic class.
        priority = 0

        ctx = dict(trigger_context or {})
        if form_data:
            ctx['_queued_form_data'] = form_data

        task = AutomationTask(
            target_id=target_id,
            workflow_id=workflow.id,
            trigger_type=trigger_type,
            trigger_rule_id=trigger_rule_id,
            trigger_context=ctx,
            status="queued",
            queue_priority=priority,
            queue_expires_at=datetime.utcnow() + timedelta(seconds=expiry_seconds),
            queue_traffic_type=traffic_type,
            max_attempts=(workflow.retry_count or 0) + 1,
        )
        db.add(task)
        await db.flush()
        logger.info(
            f"Queued workflow {workflow.id} (traffic={traffic_type}, "
            f"priority={task.queue_priority}, expires={task.queue_expires_at})"
        )
        return task

    @staticmethod
    async def dequeue_batch(db: AsyncSession, traffic_type: str, limit: int = 5) -> List[AutomationTask]:
        """Next queued tasks for ONE traffic class, best first: plan priority ASC
        then FIFO. Legacy/NULL queue_traffic_type rows are treated as 'direct'."""
        from sqlalchemy import or_
        q = (
            select(AutomationTask)
            .where(AutomationTask.status == "queued")
            .where(AutomationTask.queue_expires_at > datetime.utcnow())
        )
        if traffic_type == "direct":
            q = q.where(or_(
                AutomationTask.queue_traffic_type == "direct",
                AutomationTask.queue_traffic_type.is_(None),
            ))
        else:
            q = q.where(AutomationTask.queue_traffic_type == traffic_type)
        q = q.order_by(AutomationTask.queue_priority, AutomationTask.created_at).limit(limit)
        result = await db.execute(q)
        return list(result.scalars().all())

    @staticmethod
    async def expire_stale(db: AsyncSession) -> int:
        """Cancel expired queued tasks. Returns count expired.

        Fetch-then-mark (not a bulk UPDATE) so each cancelled marketplace run can
        release its usage-limit reservations — the no-TTL concurrency counter
        would otherwise leak permanently on every queue-expired run (the common
        capacity-starvation case). The caller commits the status changes; the
        Redis releases are independent of that commit."""
        rows = (await db.execute(
            select(AutomationTask)
            .where(AutomationTask.status == "queued")
            .where(AutomationTask.queue_expires_at <= datetime.utcnow())
        )).scalars().all()
        if not rows:
            return 0
        now = datetime.utcnow()
        for t in rows:
            t.status = "cancelled"
            t.error_message = "Queue expiry: no recorder capacity within timeout"
            t.completed_at = now
            await _release_queue_usage(db, t)
        logger.info(f"Expired {len(rows)} queued tasks: {[t.id for t in rows]}")
        return len(rows)

    @staticmethod
    async def _depth_stats(db: AsyncSession, statuses) -> dict:
        """Traffic-bucketed depth + oldest-age over the tasks in ``statuses``.

        ``*_total`` is counted from the status predicate directly, NOT by summing
        the traffic buckets — otherwise a task with a NULL/legacy
        ``queue_traffic_type`` (which ``dequeue_batch`` still picks up as
        'direct') would be invisible and the count would under-report. NULL is
        folded into the direct bucket to match ``dequeue_batch`` so the buckets
        still sum to the total.
        """
        from sqlalchemy import or_

        def _count(*extra):
            q = select(func.count(AutomationTask.id)).where(
                AutomationTask.status.in_(statuses)
            )
            for clause in extra:
                q = q.where(clause)
            return db.scalar(q)

        total_count = await _count() or 0
        direct_count = await _count(or_(
            AutomationTask.queue_traffic_type == "direct",
            AutomationTask.queue_traffic_type.is_(None),
        )) or 0
        called_count = await _count(
            AutomationTask.queue_traffic_type == "called"
        ) or 0
        scheduled_count = await _count(
            AutomationTask.queue_traffic_type == "scheduled"
        ) or 0

        oldest = await db.scalar(
            select(func.min(AutomationTask.created_at))
            .where(AutomationTask.status.in_(statuses))
        )
        oldest_age = None
        if oldest:
            # created_at is DateTime(timezone=True), so the DB returns a tz-aware
            # value; compare against an aware "now" (and coerce just in case a
            # naive value slips through from a different driver).
            if oldest.tzinfo is None:
                oldest = oldest.replace(tzinfo=timezone.utc)
            oldest_age = int((datetime.now(timezone.utc) - oldest).total_seconds())

        return {
            'queued_direct': direct_count,
            'queued_called': called_count,
            'queued_scheduled': scheduled_count,
            'queued_total': total_count,
            'oldest_queued_age_seconds': oldest_age,
        }

    @staticmethod
    async def get_queue_stats(db: AsyncSession) -> dict:
        """Cloud-QUEUE depth (``status == "queued"`` only).

        This is the capacity-pressure signal used by the autoscaler and the
        /queue/status endpoint: how many runs are waiting for a cloud recorder
        slot. It deliberately excludes ``pending`` (desktop-bound, waiting for a
        local agent to poll) and ``assigned`` (already claimed) — counting those
        would spuriously trigger cloud scale-ups. For the admin "what's waiting
        right now" view use :meth:`get_waiting_stats`.
        """
        return await WorkflowQueue._depth_stats(db, ("queued",))

    @staticmethod
    async def get_waiting_stats(db: AsyncSession) -> dict:
        """All runs a user perceives as "queued" — i.e. submitted but not yet
        running: ``queued`` + ``pending`` + ``assigned``.

        Mirrors the user-facing run-status predicate (see ``run_workflow`` in
        routers/automation.py, which reports ``queued = status in (queued,
        pending, assigned)``) so the admin live-ops view doesn't show 0 while a
        user's run is plainly waiting. NOT a capacity/autoscale signal.
        """
        return await WorkflowQueue._depth_stats(db, ("queued", "pending", "assigned"))


async def queue_processor_loop():
    """Background task: dequeue and dispatch tasks as recorder slots free up."""
    from database import AsyncSessionLocal
    from routers.automation import (
        _push_workflow_to_recorder,
        build_execute_workflow_msg,
        bind_relay_exit_for_task,
        dispatch_ws_workflow_task,
    )

    logger.info("Queue processor started")

    # Serve classes in this order each cycle. Each class respects its own capacity
    # reservation but borrows idle slots (acquire_slot → class_can_start); a class
    # with no admissible slot is SKIPPED so it can't head-of-line-block another.
    CLASS_ORDER = (TrafficType.DIRECT, TrafficType.CALLED, TrafficType.SCHEDULED)

    while True:
        try:
            await asyncio.sleep(7)

            async with AsyncSessionLocal() as db:
                expired = await WorkflowQueue.expire_stale(db)

                cap_mgr = RecorderCapacityManager(db)
                capacity = await cap_mgr.get_total_capacity()
                # Per-cycle safety cap: never schedule beyond currently-free slots
                # (the registry's active-session counts lag a few seconds).
                slots_left = min(capacity['available_slots'], 25)
                if slots_left <= 0:
                    await db.commit()
                    continue
                # Compute the running-task fairness tally ONCE per cycle and carry
                # it in memory, incrementing as each task dispatches — instead of
                # acquire_slot re-running get_total_capacity (Agent scan + gateway
                # registry read) + the running-count COUNT GROUP BY once PER task.
                running_tally = await cap_mgr.seed_running_tally(capacity)
                agent_tier_map = capacity.get('agent_tier') or {}
                dispatched = 0

                for traffic in CLASS_ORDER:
                    if slots_left <= 0:
                        break
                    tasks = await WorkflowQueue.dequeue_batch(db, traffic.value, limit=slots_left)
                    for task in tasks:
                        if slots_left <= 0:
                            break
                        workflow = await db.get(AutomationWorkflow, task.workflow_id)
                        if not workflow:
                            task.status = "cancelled"
                            task.error_message = "Workflow not found"
                            await _release_queue_usage(db, task)
                            continue

                        _ctx0 = task.trigger_context or {}
                        # Required isolation tier so a sensitive queued run prefers
                        # a gVisor (isolated) box. Computed pre-inversion from the
                        # workflow's own credentials + recipe manifest; a consumer
                        # run's creator-creds here only ever err TOWARD isolated
                        # (safe — isolated boxes run anything), and the agent's
                        # actual tier is still re-derived post-inversion in
                        # build_execute_workflow_msg.
                        _req_tier = None
                        try:
                            from services.workflow_router import WorkflowRouter
                            _req_tier = WorkflowRouter.classify_sensitivity(
                                has_credentials=bool(getattr(workflow, "credentials_encrypted", None)),
                                has_persona=bool(getattr(workflow, "default_persona_id", None)),
                                workflow=workflow,
                                trigger_context=_ctx0,
                            ).value
                        except Exception:
                            _req_tier = None
                        # Residential-intent run → steer to the CLOUD fleet so a
                        # per-run PROXY_SERVER can point at the broker chokepoint. The
                        # inline path's in-memory steering (workflow._runtime_execution
                        # _target = 'cloud') was lost when this task requeued + the
                        # workflow reloaded fresh, so re-apply it here. Read the
                        # persisted target explicitly (it's a DEFERRED column) and
                        # never override an explicit 'local' pin. Mirrors
                        # _dispatch_to_recorder_or_queue; _pick_recorder honors
                        # _runtime_execution_target.
                        _wants_residential_q = (
                            (_ctx0.get("_marketplace") or {}).get("sku") == "premium"
                            or bool(_ctx0.get("use_residential_proxy"))
                        )
                        if _wants_residential_q:
                            try:
                                _et_res = await db.execute(
                                    select(AutomationWorkflow.execution_target)
                                    .where(AutomationWorkflow.id == workflow.id)
                                )
                                _persisted_et = _et_res.scalar_one_or_none() or "auto"
                            except Exception:
                                _persisted_et = "auto"
                            if _persisted_et != "local":
                                workflow._runtime_execution_target = "cloud"
                        # A requeued crawl shard carries the agents whose IP the host
                        # just refused (`_avoid_agents`, stamped by the crawl
                        # orchestrator's block-requeue). Retrying those URLs from the
                        # same IP is what the requeue exists to avoid — so steer the
                        # retry elsewhere when anything else is free.
                        _avoid_agents = {
                            str(a) for a in (_ctx0.get("_avoid_agents") or []) if a
                        }
                        recorder = await cap_mgr.acquire_slot(
                            workflow, traffic,
                            required_tier=_req_tier,
                            capacity=capacity,
                            running_override=cap_mgr.running_override_for(
                                running_tally, capacity, _req_tier
                            ),
                            exclude_agents=_avoid_agents or None,
                        )
                        if not recorder:
                            break  # class saturated / no agent right now → next class

                        task.status = "running"
                        task.started_at = datetime.utcnow()
                        task.executor_agent_id = recorder['agent_id']

                        ctx = task.trigger_context or {}
                        form_data = ctx.pop('_queued_form_data', workflow.form_data or {})

                        # Single-user coordinator: no marketplace consumer-run
                        # inversion. Every queued run is the owner's own run against
                        # the live workflow (own steps + own credentials).
                        persona_cfg = None
                        session_state = None
                        # BYO residential proxy resolved from the RUN persona. Whether
                        # it's applied is decided by build_execute_workflow_msg's
                        # money-safe precedence.
                        byo_proxy_cfg = None
                        # A DRAGNET crawl shard authenticates via its crawl's persona: the
                        # shard task carries `_crawl_id`, and its synthetic workflow's
                        # default_persona_id points at the persona. Restore that persona's
                        # WARM SESSION so the fleet fetches logged-IN — the agent applies the
                        # session cookies on its HTTP lane (see crawl shard runner). Other
                        # queued runs keep the historical own-run behavior (no session).
                        _is_crawl_shard = bool((ctx or {}).get("_crawl_id"))
                        if getattr(workflow, "default_persona_id", None):
                            # Own queued run with a default persona: resolve that
                            # persona's acked BYO proxy. Best-effort — a proxy
                            # resolution failure must never block the run (fail toward
                            # direct egress, never toward injecting a doubtful proxy).
                            try:
                                from services.persona_service import PersonaService
                                _own_persona = await PersonaService.get_owned(
                                    db, workflow.default_persona_id
                                )
                                if _own_persona:
                                    byo_proxy_cfg = PersonaService.resolve_proxy(_own_persona)
                                    if _is_crawl_shard:
                                        # Warm session for the authenticated crawl — cookies,
                                        # DOM storage and any captured auth headers. Rides
                                        # config.session_state to the agent, which applies it
                                        # on BOTH its HTTP and browser lanes.
                                        session_state = PersonaService.load_session(_own_persona)
                                        # MID-CRAWL EXPIRY. The seeder verified the session
                                        # before fan-out, but a long crawl outlives it: by the
                                        # time this shard dispatches, load_session can return
                                        # None (absent OR expired). This used to degrade to a
                                        # logged-OUT fetch, which banks a batch of login walls
                                        # as if it were page content — silently, with the
                                        # crawl still reporting success. Fail the shard
                                        # instead, so the crawl surfaces a real error.
                                        if not session_state:
                                            logger.warning(
                                                f"[Queue] crawl shard {task.id}: persona "
                                                f"{_own_persona.id} session expired mid-crawl "
                                                f"— refusing to fetch logged-out"
                                            )
                                            await _release_queue_usage(db, task)
                                            await db.commit()
                                            # Settle through the crawl-native path: the
                                            # orchestrator incremented this crawl's in-flight
                                            # counter at mint and only complete_shard_task
                                            # decrements it and re-pumps.
                                            from services.crawl_orchestrator import (
                                                complete_shard_task,
                                            )
                                            await complete_shard_task(
                                                task.id, int(ctx["_crawl_id"]),
                                                success=False,
                                                error=(
                                                    "The login session for this crawl's persona "
                                                    "expired while it was running. Re-link the "
                                                    "login and start the crawl again."
                                                ),
                                            )
                                            continue
                            except Exception as _bp_e:
                                logger.warning(
                                    f"[Queue] persona resolve failed for task {task.id}: {_bp_e}"
                                )
                                byo_proxy_cfg = None

                        # FILE ASSETS (§4.1): resolve the run-level files map on the
                        # QUEUE path too. Resolved async here, then snapshotted into
                        # the SYNC builder below. Fail-closed: a bad file id 404s and
                        # fails the task.
                        _run_files_map = None
                        try:
                            from routers.automation import _resolve_run_files_map
                            _req_files = (ctx or {}).get("files") if isinstance(ctx, dict) else None
                            _run_files_map = await _resolve_run_files_map(
                                db, workflow,
                                request_files=_req_files,
                                ttl_seconds=max(
                                    int(getattr(settings, "file_signed_url_ttl_seconds", 600) or 600),
                                    int((getattr(workflow, "timeout_ms", None) or 0) // 1000) + 60,
                                ),
                            )
                        except Exception as _files_e:
                            # A bad file reference must fail the run, not silently drop
                            # the file (an upload step would then run with no file).
                            logger.error(
                                f"[Queue] file-map resolution failed for task {task.id} "
                                f"(workflow {workflow.id}): {_files_e}",
                                exc_info=True,
                            )
                            task.status = "failed"
                            task.error_message = f"File resolution failed: {_files_e}"
                            task.completed_at = datetime.utcnow()
                            await _release_queue_usage(db, task)
                            continue

                        # wants_residential = a residential-intent run (premium SKU or a
                        # the owner's use_residential_proxy opt-in). Resolved from the
                        # post-inversion trigger_context (the dispatch synthesis already
                        # stamped the proxy's _marketplace.sku before the task was queued).
                        _wants_residential = (
                            ((ctx or {}).get("_marketplace") or {}).get("sku") == "premium"
                            or bool((ctx or {}).get("use_residential_proxy"))
                        )
                        # Bind a residential exit on the QUEUE path too (the inline
                        # dispatch isn't the only route). Same helper / one source of
                        # truth as _dispatch_to_recorder_or_queue; returns None unless
                        # this run landed on the cloud fleet AND asked for an exit.
                        # MUST run before the synchronous build so relay_proxy (with its
                        # routing_token) rides the message. If None, clause (c) of the
                        # builder's money-safe precedence still blocks a BYO proxy from
                        # substituting for a residential-intent run.
                        relay_proxy_cfg = await bind_relay_exit_for_task(
                            db,
                            task=task,
                            workflow=workflow,
                            trigger_context=ctx if ctx else None,
                            executor_role=recorder.get('role'),
                            wants_residential=_wants_residential,
                        )
                        # Snapshot the (possibly inverted) workflow into the WS message
                        # SYNCHRONOUSLY now — before any further await — so the deferred
                        # dispatch can't read a later task's in-memory mutation.
                        ws_msg = build_execute_workflow_msg(
                            task_id=task.id, workflow=workflow, form_data=form_data,
                            session_state=session_state, persona_cfg=persona_cfg,
                            trigger_context=ctx if ctx else None,
                            executor_role=recorder.get('role'),
                            relay_proxy=relay_proxy_cfg,
                            byo_proxy=byo_proxy_cfg,
                            wants_residential=_wants_residential,
                            files=_run_files_map,
                        )

                        # The coordinator does not meter or bill runs.

                        # Dispatch over the shared WS path (direct WS + gateway/cloud
                        # fleet). A legacy HTTP recorder (recorder_url set) keeps the
                        # old HTTP push.
                        if recorder.get('via') == 'http' and recorder.get('recorder_url'):
                            asyncio.create_task(
                                _push_workflow_to_recorder(
                                    task_id=task.id,
                                    workflow=workflow,
                                    form_data=form_data,
                                    recorder_url=recorder['recorder_url'],
                                    db_url=str(settings.database_url),
                                    trigger_context=ctx if ctx else None,
                                    session_state=session_state,
                                    persona=persona_cfg,
                                )
                            )
                        else:
                            # Reserve the coordinator-owned capacity slot for this
                            # drained run so active_sessions reflects it (released on
                            # completion in _process_task_completion). Keeps a direct
                            # run dispatched during drain from over-subscribing this
                            # agent.
                            try:
                                from routers.user_recorder_ws import reserve_agent_slot
                                reserve_agent_slot(recorder['agent_id'], task.id)
                            except Exception:
                                pass
                            asyncio.create_task(
                                dispatch_ws_workflow_task(
                                    task_id=task.id,
                                    agent_id=recorder['agent_id'],
                                    message=ws_msg,
                                    trigger_context=ctx if ctx else None,
                                )
                            )
                        slots_left -= 1
                        dispatched += 1
                        # Reflect this just-started run in the in-memory fairness
                        # tally so the next task's admission sees it (mirrors the
                        # autoflushed status="running" the per-task DB re-query used
                        # to see). Bucket by the EXECUTOR's tier, exactly as
                        # _running_by_class_and_tier would.
                        _exec_tier = agent_tier_map.get(recorder['agent_id'], "shared")
                        cap_mgr.tally_increment(
                            running_tally, traffic.value, _exec_tier,
                        )
                        # Shrink the per-tier free-slot snapshot too, so the
                        # tier-aware admission path sees this dispatch within the
                        # cycle (the flat slots_left ceiling above only bounds the
                        # flat path). Prevents over-piling one pool when the
                        # registry's active-session counts lag intra-cycle.
                        cap_mgr.tier_slot_decrement(capacity, _exec_tier)
                        logger.info(
                            f"Dequeued task {task.id} ({traffic.value}) → recorder {recorder['agent_id']}"
                        )

                await db.commit()

                if dispatched:
                    logger.info(f"Queue processor: dispatched {dispatched} tasks")

        except asyncio.CancelledError:
            logger.info("Queue processor stopped")
            break
        except Exception as e:
            logger.error(f"Queue processor error: {e}", exc_info=True)
