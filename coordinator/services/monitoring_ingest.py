"""Ingest monitoring frames that recorder agents send back over the ws-gateway.

These are the agent → backend frames from `MONITORING_GATEWAY_CONTRACT.md`:
`target_check_batch`, `precheck_complete`, `scheduled_workflow_complete`.

The batch path runs the reports through the shared report-ingest core
(``services/report_ingest.submit_reports_internal``) — the same core the HTTP
``/agents/report`` route uses — for change detection, baseline update,
notifications, and triggers. The gateway channel is already authenticated, so the
coordinator server-signs the batch with the agent's stored secret on its behalf
(defense-in-depth self-check) instead of requiring a per-agent HMAC round-trip.
"""
import logging
from datetime import datetime, timezone
from typing import Any, Dict

from sqlalchemy import select

logger = logging.getLogger(__name__)


async def _load_agent(db, agent_id: str):
    from models.agent import Agent
    return (await db.execute(select(Agent).where(Agent.agent_id == agent_id))).scalar_one_or_none()


def _frame_timestamp_fresh(raw_ts: Any, *, max_age_seconds: int = 600) -> bool:
    """Return True if an agent frame timestamp is parseable AND within
    ``max_age_seconds`` of now (UTC). Accepts ISO-8601 (optional trailing Z) or
    numeric epoch seconds (int/float/numeric-string). Replay protection at the
    gateway-ingest boundary. Returns False on a missing/unparseable/stale value.
    """
    if raw_ts is None or isinstance(raw_ts, bool):
        return False
    msg_time = None
    try:
        if isinstance(raw_ts, (int, float)):
            msg_time = datetime.fromtimestamp(float(raw_ts), tz=timezone.utc)
        elif isinstance(raw_ts, str):
            ts_str = raw_ts.strip()
            try:
                msg_time = datetime.fromtimestamp(float(ts_str), tz=timezone.utc)
            except (ValueError, OverflowError, OSError):
                parsed = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                msg_time = parsed
    except (ValueError, OverflowError, OSError):
        return False
    if msg_time is None:
        return False
    delta = abs((datetime.now(timezone.utc) - msg_time).total_seconds())
    return delta <= max_age_seconds


async def ingest_monitor_register(data: Dict[str, Any], reporting_agent_id: str) -> None:
    """A recorder advertising monitoring capability AFTER the gateway assigned it
    an id (the `/connect` POST can't, because the id isn't known yet on first
    connect). Persist capacity + check_modes into the Agent row the gateway just
    created, mint an HMAC secret if missing, then (re)distribute + push targets."""
    from database import AsyncSessionLocal
    from sqlalchemy.orm.attributes import flag_modified

    agent_id = reporting_agent_id or data.get("agent_id") or ""
    capacity = data.get("capacity")
    check_modes = data.get("check_modes")
    if not agent_id or not check_modes:
        return

    async with AsyncSessionLocal() as db:
        agent = await _load_agent(db, agent_id)
        if not agent:
            # The gateway creates the row on WS-connect, BEFORE it relays this
            # frame, so it should exist. If not (race), skip — the recorder
            # re-registers on its next connect.
            logger.warning(f"monitor_register from unknown agent {agent_id}; skipping")
            return
        meta = dict(agent.meta or {})
        # Track whether the distribution-relevant fields (capacity drives an
        # agent's slot sizing; check_modes drives playwright eligibility) actually
        # changed. A pure reconnect re-sends identical values → the redistribution
        # can be debounced; a first registration / capacity change must NOT be
        # (the new agent/capacity must be observed immediately).
        _prev_capacity = meta.get("capacity")
        _prev_check_modes = meta.get("check_modes")
        if capacity:
            meta["capacity"] = dict(capacity)
        meta["check_modes"] = list(check_modes)
        dist_state_changed = (
            meta.get("capacity") != _prev_capacity
            or meta.get("check_modes") != _prev_check_modes
        )
        agent.meta = meta
        flag_modified(agent, "meta")
        if not getattr(agent, "encrypted_secret", None):
            try:
                import secrets as _secrets
                from security.encryption import SecretEncryption
                agent.encrypted_secret = SecretEncryption.encrypt_secret(_secrets.token_hex(32))
            except Exception as e:
                logger.warning(f"could not mint monitoring secret for {agent_id}: {e}")
        await db.commit()
        logger.info(f"monitor_register: {agent_id} registered as monitoring agent")

    # Distribute + push the assignment so it starts checking immediately. Force a
    # fresh (non-debounced) redistribution only when this register changed the
    # agent's capacity/check_modes; an identical-state reconnect is debounced.
    try:
        from services.monitoring_dispatch import distribute_and_push_to_agent
        await distribute_and_push_to_agent(agent_id, force=dist_state_changed)
    except Exception as e:
        logger.warning(f"monitor_register distribution failed for {agent_id}: {e}")


async def ingest_target_check_batch(data: Dict[str, Any], reporting_agent_id: str) -> None:
    """Run the agent-reported batch through the shared report-ingest core
    (`submit_reports_internal`), then push refreshed assignments to any agents
    flagged for hot-reload by a detected change.

    The ws-gateway channel already authenticated the reporting agent, so the
    reports don't need a per-agent HMAC round-trip. We still (a) re-verify the
    agent has a stored secret and (b) enforce replay-freshness on the agent's
    stamped timestamp before ingesting, and re-sign the batch with the agent's
    secret as a defense-in-depth self-check that the payload matches what the
    agent would have signed."""
    from database import AsyncSessionLocal
    from security.encryption import SecretEncryption
    from security.hmac import HMACAuth, verify_fresh_signed_payload
    from services.report_ingest import submit_reports_internal

    agent_id = reporting_agent_id or data.get("agent_id") or ""
    reports = data.get("reports") or []
    if not agent_id or not reports:
        return

    async with AsyncSessionLocal() as db:
        agent = await _load_agent(db, agent_id)
        if not agent:
            logger.warning(f"target_check_batch from unknown agent {agent_id}")
            return
        if not getattr(agent, "encrypted_secret", None):
            logger.warning(f"target_check_batch: agent {agent_id} has no secret; dropping")
            return

        secret = SecretEncryption.decrypt_secret(agent.encrypted_secret)

        # Replay protection: if the agent stamped the frame, reject a stale one
        # before we re-sign + process it. Frames without a timestamp fall back to
        # the server clock below (the gateway channel is already authenticated).
        raw_ts = data.get("timestamp")
        if raw_ts is None or not _frame_timestamp_fresh(raw_ts):
            # Mandatory freshness: a frame with no/invalid timestamp is replayable,
            # so drop it rather than falling back to the server clock (audit #28).
            logger.warning(
                f"target_check_batch: missing/stale/invalid timestamp from {agent_id}; dropping"
            )
            return
        timestamp = raw_ts

        # HMAC/agent-secret re-sign + verify (defense-in-depth). Build the same
        # signature payload the agent's batch signer builds, sign it with the
        # agent's stored secret, then verify it round-trips + is fresh. A mismatch
        # would only mean a coding/clock drift here (the channel is authenticated),
        # so we log-and-drop rather than trust an inconsistent batch.
        sig_reports = []
        for r in reports:
            ct = r.get("check_type") or "content"
            item = {"target_url": r.get("target_url")}
            if ct == "uptime":
                item["is_up"] = bool(r.get("is_up", False))
            else:
                item["content_hash"] = r.get("content_hash") or ("0" * 64)
            sig_reports.append(item)
        sig_payload = {
            "agent_id": agent_id,
            "timestamp": timestamp,
            "report_count": len(reports),
            "reports": sig_reports,
        }
        signature = HMACAuth.sign_request(sig_payload, secret)
        if not verify_fresh_signed_payload(sig_payload, secret, signature, max_age_seconds=600):
            logger.warning(
                f"target_check_batch: re-signed batch failed self-verify for {agent_id}; dropping"
            )
            return

        # Hand the reports (agent already trusted) to the shared ingest core.
        try:
            await submit_reports_internal(
                {"agent_id": agent_id, "timestamp": timestamp, "reports": reports},
                db,
            )
        except Exception as e:
            logger.error(f"target_check_batch processing failed for {agent_id}: {e}")
            return

    # A detected change flags OTHER monitoring agents for hot-reload — push fresh
    # assignments to them over the gateway (the desktop-agent gets this by polling).
    await _push_to_flagged_agents()


async def _push_to_flagged_agents() -> None:
    from database import AsyncSessionLocal
    from models.agent import Agent, AgentStatus
    from services.monitoring_dispatch import push_assign_targets

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(Agent).where(
                Agent.status == AgentStatus.ACTIVE,
                Agent.force_config_update.is_(True),
            )
        )).scalars().all()
        for agent in rows:
            meta = agent.meta or {}
            if not meta.get("check_modes"):
                continue  # only recorders get gateway pushes; desktop-agents poll
            try:
                await push_assign_targets(db, agent)
            except Exception as e:
                logger.warning(f"hot-reload push failed for {agent.agent_id}: {e}")


async def ingest_precheck_complete(data: Dict[str, Any], reporting_agent_id: str) -> None:
    """Persist a refreshed auth session harvested by a browser/pre-check run,
    then redistribute + push so the (now-authenticated) target gets checked."""
    from database import AsyncSessionLocal
    from models.target import Target
    from routers.automation import encrypt_credentials

    agent_id = reporting_agent_id or data.get("agent_id") or ""
    target_id = data.get("target_id")
    auth_session = data.get("auth_session")
    if not target_id or not auth_session:
        return

    async with AsyncSessionLocal() as db:
        target = (await db.execute(select(Target).where(Target.id == target_id))).scalar_one_or_none()
        if not target:
            logger.warning(f"precheck_complete for unknown target {target_id}")
            return

        # Auth-session injection guard: only an agent that owns this target (the
        # owner) or is actively assigned to it may overwrite its stored auth
        # session. Otherwise a foreign/BYO agent could inject a session blob into
        # an arbitrary victim target. Load the reporting agent and authorize.
        from security.ownership import agent_may_report_target
        agent = await _load_agent(db, agent_id) if agent_id else None
        if agent is None or not await agent_may_report_target(db, agent=agent, target=target):
            logger.warning(
                f"precheck_complete: agent {agent_id!r} not authorized to set auth "
                f"session for target {target_id}; ignoring"
            )
            return

        try:
            target.auth_session_encrypted = encrypt_credentials(auth_session)
            await db.commit()
            logger.info(f"precheck_complete: saved auth session for target {target_id}")
        except Exception as e:
            logger.error(f"precheck_complete save failed for target {target_id}: {e}")
            return

    # Redistribute + push to the reporting agent so it starts the authed checks.
    # force=True: we just saved a NEW auth session that flips this target out of
    # precheck-only single-agent mode, so the redistribution must observe it now
    # (not be debounced away by a recent reconnect-driven distribution).
    if agent_id:
        try:
            from services.monitoring_dispatch import distribute_and_push_to_agent
            await distribute_and_push_to_agent(agent_id, force=True)
        except Exception as e:
            logger.warning(f"precheck_complete redistribution failed: {e}")


async def ingest_scheduled_complete(data: Dict[str, Any], reporting_agent_id: str) -> None:
    """Record a scheduled-workflow run. Server-side completion time is the source
    of truth (agent-reported timestamps are not trusted)."""
    from database import AsyncSessionLocal
    from models.automation_workflow import AutomationWorkflow

    workflow_id = data.get("workflow_id")
    if workflow_id is None:
        return
    success = bool(data.get("success"))

    agent_id = reporting_agent_id or data.get("agent_id") or ""

    async with AsyncSessionLocal() as db:
        wf = (await db.execute(
            select(AutomationWorkflow).where(AutomationWorkflow.id == workflow_id)
        )).scalar_one_or_none()
        if not wf:
            logger.warning(f"scheduled_workflow_complete for unknown workflow {workflow_id}")
            return

        # Scope the workflow to the reporting agent before bumping usage/last-run.
        # An untrusted agent must not be able to inflate usage_count (which feeds
        # billing/ranking) for an arbitrary foreign workflow. Authorized when the
        # workflow belongs to the owner OR the agent is actively assigned
        # to a target owned by the owner.
        authorized = await _agent_may_report_workflow(db, agent_id, wf)
        if not authorized:
            logger.warning(
                f"scheduled_workflow_complete: agent {agent_id!r} not authorized "
                f"for workflow {workflow_id}; skipping usage/last-run bump"
            )
            return

        now = datetime.utcnow()
        if hasattr(wf, "last_scheduled_at"):
            wf.last_scheduled_at = now
        if hasattr(wf, "last_run_at"):
            wf.last_run_at = now
        if success and hasattr(wf, "usage_count"):
            wf.usage_count = (wf.usage_count or 0) + 1
        try:
            await db.commit()
            logger.info(f"scheduled_workflow_complete: workflow {workflow_id} success={success}")
        except Exception as e:
            logger.error(f"scheduled_workflow_complete persist failed for {workflow_id}: {e}")


async def _agent_may_report_workflow(db, agent_id: str, workflow) -> bool:
    """Authorize an agent to report completion for a scheduled workflow.

    Every workflow and every connected agent belong to the one owner, so any known
    agent may report. We still require a real, loadable agent so a bogus/unknown
    agent_id is rejected. Never raises; returns False on any error.
    """
    if not agent_id or workflow is None:
        return False
    try:
        agent = await _load_agent(db, agent_id)
        return agent is not None
    except Exception as e:
        logger.debug(f"_agent_may_report_workflow check failed: {e}")
        return False
