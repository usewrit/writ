"""Reusable agent-report ingest core (change detection + notifications).

`submit_reports_internal(payload, db)` is the single, transport-agnostic core that
both the HTTP `/agents/report` route (routers/agents.py) and the ws-gateway batch
ingest (services/monitoring_ingest.ingest_target_check_batch) run their reports
through. It performs ONLY the DB writes, change detection, on-change automation
dispatch, monitor-health edges, notifications, and trigger evaluation — the caller
owns authentication (HMAC / gateway channel) and any response/config building.

Single-user coordinator model: there is exactly one owner, so there is NO tenant
scoping, NO cloud metering, and NO `Report`/`AgentError`/`TargetAssignment` rows
(those were carved out of the self-host build). Change detection keys off the
globally-unique `selector_id`; a target's baseline lives on its `TargetSelector`.

The `payload` dict is `{agent_id, timestamp, reports: [ {..SingleReportData..} ]}`
where each report is the same shape the agents send in a `target_check_batch`
frame / an HTTP report body (target_url, selector_id, content_hash, content,
diff_snippet, screenshot, headers, status_code, check_type/uptime fields, and the
optional in-session on_change fields).
"""
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

logger = logging.getLogger(__name__)


async def submit_reports_internal(payload: Dict[str, Any], db: AsyncSession) -> Dict[str, Any]:
    """Process a batch of agent-reported checks.

    Args:
        payload: {agent_id, timestamp, reports: [SingleReportData-shaped dicts]}.
            The reporting agent is assumed ALREADY authenticated by the caller
            (HTTP HMAC verify, or the authenticated ws-gateway channel).
        db: async session (its own transaction — committed here).

    Returns:
        {
          "processed": int,          # reports successfully ingested
          "failed": int,             # reports dropped (unknown target / bad data)
          "content_changed": bool,   # any content report advanced a baseline
          "baseline_updates": {      # per-selector baseline snapshots for the
            "<target_id>:<selector_id>": {...}   # agent to refresh its cache
          },
        }

    Never raises for a single bad report (it is counted in ``failed``); a fatal
    error rolls back and re-raises so the caller can log/handle it.
    """
    from models.agent import Agent
    from models.target import Target
    from models.target_selector import TargetSelector
    from security.ownership import agent_may_report_target

    agent_id = payload.get("agent_id") or ""
    reports: List[Dict[str, Any]] = payload.get("reports") or []

    result = {"processed": 0, "failed": 0, "content_changed": False, "baseline_updates": {}}
    if not agent_id or not reports:
        return result

    agent = (await db.execute(
        select(Agent).where(Agent.agent_id == agent_id)
    )).scalar_one_or_none()
    if not agent:
        logger.warning(f"report ingest: unknown agent {agent_id}; dropping batch")
        return result

    agent.last_seen_at = datetime.utcnow()

    processed = 0
    failed = 0
    baseline_updates: Dict[str, Any] = {}
    # New (unseen-hash) changes to notify + evaluate triggers for AFTER commit.
    changes_to_notify: List[Dict[str, Any]] = []
    # Monitor-health edges (down / recovered) — dispatched after commit. Each is
    # (target, event_type, health_context).
    pending_health: list = []

    for report_data in reports:
        try:
            target_url = report_data.get("target_url")

            # Resolve target: prefer an explicit target_id (a coalesced group's
            # representative), else the globally-unique selector_id (selector →
            # target), else first match by URL (legacy / no selector).
            target = None
            _tid = report_data.get("target_id")
            if _tid:
                target = (await db.execute(
                    select(Target).where(Target.id == _tid)
                )).scalar_one_or_none()
            _sel_id = report_data.get("selector_id")
            if target is None and _sel_id:
                sel_row = (await db.execute(
                    select(TargetSelector).where(TargetSelector.id == _sel_id)
                )).scalar_one_or_none()
                if sel_row:
                    target = (await db.execute(
                        select(Target).where(Target.id == sel_row.target_id)
                    )).scalar_one_or_none()
            if target is None and target_url:
                target = (await db.execute(
                    select(Target).where(Target.url == target_url).order_by(Target.id)
                )).scalars().first()

            if not target:
                logger.warning(f"report ingest: target not found for {target_url!r}; dropping")
                failed += 1
                continue

            # Single-owner authorization (never trust a foreign agent). In the
            # coordinator this is True for any resolved agent+target, but it is
            # kept so the guard stays a single choke point.
            if not await agent_may_report_target(db, agent=agent, target=target):
                logger.warning(
                    f"report ingest: agent {agent_id} not authorized to report for "
                    f"target {target.id}; dropping report"
                )
                failed += 1
                continue

            # Stamp the last-checked time on EVERY authorized report (uptime or
            # content, change or no-change). This is the fleet's proof the target is
            # actually being checked at its interval, and powers "last checked N ago".
            target.last_checked_at = datetime.utcnow()

            check_type = report_data.get("check_type") or "content"

            if check_type == "uptime":
                await _ingest_uptime(db, agent, target, report_data, pending_health)
                processed += 1
                continue

            changed = await _ingest_content(
                db, agent, agent_id, target, report_data,
                baseline_updates, changes_to_notify, pending_health,
            )
            if changed:
                result["content_changed"] = True
            # A content report with no selector_id / hash is skipped (not counted).
            if changed is not None:
                processed += 1

        except Exception as e:  # noqa: BLE001 — one bad report must not sink the batch
            logger.error(
                f"report ingest: error processing report for "
                f"{report_data.get('target_url')!r}: {e}",
                exc_info=True,
            )
            failed += 1

    await db.commit()

    # Fire monitor-health automations (down / recovered) after commit. Fail-soft.
    if pending_health:
        from services.monitor_health_events import dispatch_monitor_health
        for _target, _event, _ctx in pending_health:
            await dispatch_monitor_health(db, _target, _event, _ctx)

    # Dispatch notifications for NEW changes (after commit so data is persisted).
    if changes_to_notify:
        from notifications.dispatcher import NotificationDispatcher
        for change in changes_to_notify:
            try:
                dispatcher = NotificationDispatcher(db)
                await dispatcher.dispatch_change_notification(
                    target=change["target"],
                    agent=agent,
                    diff_snippet=change.get("diff_snippet"),
                    detected_change=change.get("detected_change"),
                    selector=change.get("selector"),
                )
            except Exception as e:  # noqa: BLE001 — never fail ingest on notify error
                logger.error(
                    f"report ingest: notification dispatch failed for "
                    f"{change['target'].url}: {e}"
                )

    # Evaluate unified trigger rules for NEW changes (deduped across selectors).
    if changes_to_notify:
        try:
            from services.unified_trigger_service import get_unified_trigger_service
            trigger_service = get_unified_trigger_service(db)
            fired_triggers: set = set()
            for change in changes_to_notify:
                detected_change = change.get("detected_change")
                if not detected_change:
                    continue
                await trigger_service.process_change(
                    target=change["target"],
                    selector=change.get("selector"),
                    detected_change=detected_change,
                    content=change.get("content") or change.get("diff_snippet") or "",
                    change_context={
                        "diff_snippet": change.get("diff_snippet"),
                        "content_hash": change.get("content_hash"),
                        "previous_hash": change.get("previous_hash"),
                        "target_url": change["target"].url,
                    },
                    fired_triggers=fired_triggers,
                )
        except Exception as e:  # noqa: BLE001 — never fail ingest on trigger error
            logger.error(f"report ingest: trigger evaluation failed: {e}", exc_info=True)

    result["processed"] = processed
    result["failed"] = failed
    result["baseline_updates"] = baseline_updates
    logger.info(
        f"report ingest: {processed} processed, {failed} failed from {agent_id}, "
        f"{len(changes_to_notify)} change(s) detected"
    )
    return result


async def _ingest_uptime(db, agent, target, report_data, pending_health) -> None:
    """Record an uptime check for `target` (change-only DB persistence)."""
    from models.uptime_check import UptimeCheck
    from services.monitoring_state import record_uptime

    is_up_val = bool(report_data.get("is_up", False))
    status_code = report_data.get("status_code")

    ssl_expires_at = None
    _raw_expires = report_data.get("ssl_cert_expires_at")
    if isinstance(_raw_expires, str):
        try:
            ssl_expires_at = datetime.fromisoformat(
                _raw_expires.replace("Z", "+00:00")
            ).replace(tzinfo=None)
        except Exception:
            ssl_expires_at = None
    elif _raw_expires is not None:
        ssl_expires_at = _raw_expires

    changed, health_event = await record_uptime(target.id, is_up_val, status_code)
    if health_event:
        pending_health.append((
            target, health_event,
            {"state": "up" if is_up_val else "down", "status_code": status_code},
        ))
    if changed:
        db.add(UptimeCheck(
            target_id=target.id,
            # UptimeCheck.agent_id is the STRING agent_id column, NOT the int PK.
            agent_id=agent.agent_id,
            is_up=is_up_val,
            status_code=status_code,
            response_time_ms=report_data.get("response_time_ms"),
            dns_time_ms=report_data.get("dns_time_ms"),
            connect_time_ms=report_data.get("connect_time_ms"),
            tls_time_ms=report_data.get("tls_time_ms"),
            ssl_cert_valid=report_data.get("ssl_cert_valid"),
            ssl_cert_expires_at=ssl_expires_at,
            ssl_cert_days_until_expiry=report_data.get("ssl_cert_days_until_expiry"),
            ssl_cert_issuer=report_data.get("ssl_cert_issuer"),
            ssl_cert_subject=report_data.get("ssl_cert_subject"),
            ssl_error=report_data.get("ssl_error"),
            error_message=report_data.get("error_message"),
            checked_at=datetime.utcnow(),
        ))
        if is_up_val:
            target.last_seen_up_at = datetime.utcnow()


async def _ingest_content(
    db, agent, agent_id, target, report_data,
    baseline_updates, changes_to_notify, pending_health,
) -> Optional[bool]:
    """Record a content check for `target`; run baseline/change detection.

    Returns True if content changed, False if no change / baseline init, or None
    when the report is unusable (missing selector_id) and should be skipped
    (NOT counted as processed).
    """
    from models.agent import Agent, AgentStatus
    from models.target_selector import TargetSelector
    from models.detected_change import DetectedChange
    from services.monitoring_state import record_content

    selector_id = report_data.get("selector_id")
    content_hash = report_data.get("content_hash")
    if not selector_id:
        logger.warning(
            f"report ingest: content report for {target.url} missing selector_id; skipping"
        )
        return None

    selector = (await db.execute(
        select(TargetSelector)
        .options(selectinload(TargetSelector.extractors))
        .where(TargetSelector.id == selector_id, TargetSelector.target_id == target.id)
    )).scalar_one_or_none()
    if not selector:
        logger.warning(
            f"report ingest: selector {selector_id} not found for target {target.id}; skipping"
        )
        return None

    content = report_data.get("content")
    diff_snippet = report_data.get("diff_snippet")
    screenshot = report_data.get("screenshot")

    content_changed = False
    old_baseline = None
    old_baseline_screenshot = None
    visual_after_ref = visual_diff_ref = None

    if selector.baseline_hash is None:
        # Initialize baseline (the hash IS the baseline; the image powers diffs).
        selector.baseline_hash = content_hash
        selector.baseline_content = content
        if screenshot:
            from services.visual_storage import seed_baseline
            selector.baseline_screenshot = seed_baseline(screenshot, selector_id)
        selector.baseline_fetched_at = datetime.utcnow()
        selector.last_content_hash = content_hash
        selector.last_checked_at = datetime.utcnow()

        baseline_updates[f"{target.id}:{selector_id}"] = {
            "target_id": target.id,
            "selector_id": selector_id,
            "baseline_hash": selector.baseline_hash,
            "baseline_content": selector.baseline_content,
        }
        logger.info(
            f"report ingest: baseline initialized for selector {selector_id}: "
            f"{(selector.baseline_hash or '')[:16]}..."
        )
    elif content_hash and content_hash != selector.baseline_hash:
        content_changed = True
        old_baseline = selector.baseline_hash
        old_baseline_screenshot = selector.baseline_screenshot
        if screenshot:
            from services.visual_storage import process_change
            visual_after_ref, visual_diff_ref = process_change(
                old_baseline_screenshot, screenshot, selector_id,
            )
            selector.baseline_screenshot = visual_after_ref or old_baseline_screenshot
        selector.baseline_hash = content_hash
        selector.baseline_content = content
        selector.baseline_fetched_at = datetime.utcnow()
        selector.last_content_hash = content_hash
        selector.last_checked_at = datetime.utcnow()
        selector.change_count = (selector.change_count or 0) + 1

        baseline_updates[f"{target.id}:{selector_id}"] = {
            "target_id": target.id,
            "selector_id": selector_id,
            "baseline_hash": selector.baseline_hash,
            "baseline_content": selector.baseline_content,
        }
        logger.info(
            f"report ingest: CHANGE DETECTED for selector {selector_id} on "
            f"{target.url}: {(old_baseline or '')[:16]}... -> {(content_hash or '')[:16]}..."
        )
    else:
        # No change → Redis check-record only; no DB write this interval.
        pass

    # Record the check in Redis (cheap) regardless; only changes write the DB.
    content_health = await record_content(target.id, content_changed)
    if content_health:
        pending_health.append((target, content_health, {"state": "ok"}))

    if content_changed:
        # Signal the OTHER active agents assigned to this target to hot-reload.
        # (Coordinator has no TargetAssignment table — carved out — so this is a
        # best-effort broadcast to every OTHER active agent.)
        await db.execute(
            update(Agent)
            .where(Agent.agent_id != agent_id)
            .where(Agent.status == AgentStatus.ACTIVE)
            .values(force_config_update=True)
        )

        # Create or update the DetectedChange for this (target, hash, selector).
        existing = (await db.execute(
            select(DetectedChange)
            .where(
                DetectedChange.target_id == target.id,
                DetectedChange.content_hash == content_hash,
                DetectedChange.target_selector_id == selector_id,
            )
            .limit(1)
        )).scalar_one_or_none()

        if existing:
            # Already notified for this hash — bump count, do NOT re-notify.
            existing.agent_count += 1
            existing.last_detected_at = datetime.utcnow()
            detected_change_obj = existing
        else:
            detected_change_obj = DetectedChange(
                target_id=target.id,
                target_selector_id=selector_id,
                content_hash=content_hash,
                previous_hash=old_baseline,
                diff_snippet=diff_snippet,
                content_before=report_data.get("previous_content"),
                content_after=content,
                screenshot_before=old_baseline_screenshot,
                screenshot_after=visual_after_ref,
                screenshot_diff=visual_diff_ref,
                first_detected_at=datetime.utcnow(),
                last_detected_at=datetime.utcnow(),
                agent_count=1,
            )
            db.add(detected_change_obj)
            await db.flush()

            changes_to_notify.append({
                "target": target,
                "selector": selector,
                "selector_name": report_data.get("selector_name") or selector.name,
                "diff_snippet": diff_snippet,
                "content": content,
                "content_hash": content_hash,
                "previous_hash": old_baseline,
                "detected_change": detected_change_obj,
            })

            # Kick off the target's on-change automation for a NEW change.
            await _maybe_dispatch_on_change(db, agent, target, report_data)

    return content_changed


async def _maybe_dispatch_on_change(db, agent, target, report_data) -> None:
    """Create/queue the on-change automation task for a newly detected change,
    honoring the target's conditions and the in-session fast path."""
    if not (target.on_change_enabled and target.on_change_workflow_id):
        return

    from models.automation_task import AutomationTask
    from models.automation_workflow import AutomationWorkflow

    # Evaluate optional content conditions against the diff snippet.
    should_trigger = True
    if target.on_change_conditions:
        conditions = target.on_change_conditions
        content = report_data.get("diff_snippet") or ""
        if "content_contains" in conditions:
            should_trigger = conditions["content_contains"] in content
        if should_trigger and "content_not_contains" in conditions:
            should_trigger = conditions["content_not_contains"] not in content

    if not should_trigger:
        return

    if report_data.get("on_change_handled_in_session"):
        # The agent already ran the on_change workflow in its live check session
        # (target opted into on_change_in_session). Record the result; skip the
        # duplicate fresh-browser dispatch.
        oc_res = report_data.get("on_change_result") or {}
        oc_success = bool(oc_res.get("success", True))
        db.add(AutomationTask(
            target_id=target.id,
            workflow_id=target.on_change_workflow_id,
            trigger_type="on_change",
            status="success" if oc_success else "failed",
            max_attempts=3,
            result_data=oc_res or None,
            error_message=oc_res.get("error"),
            executor_agent_id=agent.id,
            completed_at=datetime.utcnow(),
        ))
        logger.info(
            f"report ingest: on_change handled IN-SESSION by agent {agent.id} for "
            f"target {target.id} (workflow_id={target.on_change_workflow_id}, "
            f"success={oc_success}) — skipped fresh dispatch"
        )
        return

    workflow = (await db.execute(
        select(AutomationWorkflow).where(AutomationWorkflow.id == target.on_change_workflow_id)
    )).scalar_one_or_none()

    if workflow:
        from routers.automation import _dispatch_to_recorder_or_queue
        task = await _dispatch_to_recorder_or_queue(
            db=db, workflow=workflow, target_id=target.id, trigger_type="on_change",
        )
    else:
        task = AutomationTask(
            target_id=target.id,
            workflow_id=target.on_change_workflow_id,
            trigger_type="on_change",
            status="pending",
            max_attempts=3,
        )
        db.add(task)
    logger.info(
        f"report ingest: dispatched on-change automation for target {target.id} "
        f"(workflow_id={target.on_change_workflow_id}, status={getattr(task, 'status', '?')})"
    )
