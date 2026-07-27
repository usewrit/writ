"""
Agents router - agent registration and management endpoints.
"""
import json
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List, Union
from fastapi import APIRouter, Depends, HTTPException, status, Request, Query
from fastapi.security import HTTPAuthorizationCredentials
from security.api_key import security as _bearer_scheme  # HTTPBearer(auto_error=False)
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, delete, func, and_, case, distinct
from argon2 import PasswordHasher

from database import get_db
from dependencies import get_redis_client
import redis.asyncio as redis
from utils.redis_client import get_redis, get_redis_bytes
from models.agent import Agent, AgentStatus, Platform
from models.target import Target
Report = None  # no per-run reports store in this build
AgentError = None  # no per-target check-error store in this build
from models.pushover_recipient import PushoverRecipient
# There is no persistent audit-log store in this build. `EventsAudit(...)`
# returns None so the existing `if audit is not None:` guards on db.add() skip
# persistence without touching a real mapper.
def EventsAudit(*args, **kwargs):  # noqa: N802 - stand-in for the absent model
    return None
from models.automation_workflow import AutomationWorkflow
from security.api_key import get_current_api_key
from security.hmac import verify_hmac_signature, construct_message, verify_fresh_signed_payload
from config import settings
from utils.user_agents import USER_AGENTS, get_user_agent_by_index, get_total_user_agents
from services.agent_state_manager import AgentStateManager

logger = logging.getLogger(__name__)
ph = PasswordHasher()

router = APIRouter(prefix="/agents", tags=["Agents"])

# Keys in agent.meta that disclose internal network topology. These are kept in
# the DB for server-side load-balancing/routing but MUST be stripped from any
# response reachable by a non-platform-admin caller. Platform-admin/ops
# endpoints (require_platform_admin) may show full meta and should NOT use this.
_INFRA_META_KEYS = frozenset({
    "recorder_url",
    "recorderUrl",
    "gateway_instance",
    "gatewayInstance",
    "internal_url",
    "internalUrl",
    "node_id",
    "nodeId",
    "ip_address",
    "ipAddress",
    "reachable_url",
    "reachableUrl",
    "droplet_id",
    "droplet_name",
    "provisioned_id",
    "private_ip",
    "public_ip",
})


def _strip_infra_meta(meta: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Return a shallow copy of agent.meta with infra-address keys removed.

    Never mutates the input (which may be a live SQLAlchemy JSON attribute).
    """
    if not meta:
        return meta
    return {k: v for k, v in meta.items() if k not in _INFRA_META_KEYS}


# ============================================================================
# Helper: Build target dict with workflow and auth data
# ============================================================================

def decrypt_auth_session(encrypted: str) -> Optional[dict]:
    """Decrypt auth session from target."""
    if not encrypted:
        return None
    try:
        from cryptography.fernet import Fernet
        from security.hmac import get_fernet_key
        import json
        fernet = Fernet(get_fernet_key())
        return json.loads(fernet.decrypt(encrypted.encode()).decode())
    except Exception as e:
        logger.error(f"Failed to decrypt auth session: {e}")
        return None


def _decrypt_workflow_credentials(workflow: AutomationWorkflow) -> Optional[dict]:
    """Decrypt a workflow's stored credentials (returns None on absence/failure)."""
    if not workflow.credentials_encrypted:
        return None
    try:
        from cryptography.fernet import Fernet
        from security.hmac import get_fernet_key
        import json
        fernet = Fernet(get_fernet_key())
        return json.loads(fernet.decrypt(workflow.credentials_encrypted.encode()).decode())
    except Exception as e:
        logger.error(f"Failed to decrypt workflow credentials: {e}")
        return None


def _assemble_target_dict(
    target: Target,
    selectors: list,
    workflows_by_id: Dict[int, AutomationWorkflow],
    include_workflow: bool,
    include_baseline: bool,
) -> dict:
    """Build the agent-facing target dict from already-loaded selectors + workflows.

    This is the shared core of ``build_target_dict`` (single) and
    ``build_target_dicts`` (batched). It performs NO database I/O — the caller
    supplies the enabled selectors (ordered priority desc, id asc) for this fetch
    and a {workflow_id: AutomationWorkflow} lookup covering the target's
    pre_check/on_change workflow ids. Output is byte-for-byte identical to the
    previous per-target query path.
    """
    result = {
        "id": str(target.id),
        "url": target.url,
        "check_type": target.check_type or "content",
        "selector": target.selector,
        "ignore_regex": target.ignore_regex,
        "check_period_ms": target.check_period_ms,
        "baseline_hash": target.baseline_hash,
        # Uptime monitoring fields
        "expected_status_code": target.expected_status_code,
        "timeout_ms": target.timeout_ms,
        "max_response_time_ms": target.max_response_time_ms,
        "check_ssl": target.check_ssl,
        # Playwright requirement
        "requires_playwright": target.requires_playwright,
    }

    if include_baseline and target.check_type == 'content':
        result["baseline_content"] = target.baseline_content

    # Include multi-selectors if available. For a coalesced group these are the
    # unioned selectors of EVERY member target on this one fetch (each selector
    # keeps its own id + baseline, so the agent reports per selector_id and the
    # backend fans each result back to the owning target).
    if selectors:
        result["selectors"] = [
            {
                "id": s.id,
                "name": s.name,
                "selector": s.selector,
                "content_type": s.content_type or 'text',
                "visual_region": s.visual_region,
                "ignore_regex": s.ignore_regex,
                "baseline_hash": s.baseline_hash,
                "baseline_content": s.baseline_content if include_baseline else None,
                "priority": s.priority,
            }
            for s in selectors
        ]

    # Inline setup-steps manifest (recorded expand/scroll/click steps from the
    # monitor wizard, JSON {steps, credentials}). The browser checker replays the
    # interaction steps before extraction so a visual zone / selector that only
    # appears after interaction is captured in the right state.
    _setup_raw = getattr(target, "setup_steps", None)
    if _setup_raw and str(_setup_raw).strip() not in ("", "null"):
        try:
            import json as _json
            result["setup_steps"] = _json.loads(_setup_raw)
        except (ValueError, TypeError):
            logger.warning("agent config: ignoring malformed setup_steps for target %s", target.id)

    # Include pre_check workflow if target has one
    if include_workflow and target.pre_check_workflow_id:
        workflow = workflows_by_id.get(target.pre_check_workflow_id)

        if workflow:
            # Decrypt credentials before sending to agent
            credentials = _decrypt_workflow_credentials(workflow)

            result["pre_check_workflow"] = {
                "id": workflow.id,
                "name": workflow.name,
                "steps": workflow.steps,
                "form_data": workflow.form_data,
                "credentials": credentials,  # Decrypted credentials
                "timeout_ms": workflow.timeout_ms,
                "headless": workflow.headless,
            }

    # Include the on_change workflow IN the payload when the target opts into running it
    # inside the live check session (same browser, no fresh dispatch). Capable agents run
    # these steps right after detecting a change and report it via on_change_handled_in_session.
    if (
        include_workflow
        and getattr(target, "on_change_in_session", False)
        and target.on_change_enabled
        and target.on_change_workflow_id
    ):
        oc_workflow = workflows_by_id.get(target.on_change_workflow_id)
        if oc_workflow:
            oc_credentials = _decrypt_workflow_credentials(oc_workflow)

            result["on_change_workflow"] = {
                "id": oc_workflow.id,
                "name": oc_workflow.name,
                "steps": oc_workflow.steps,
                "form_data": oc_workflow.form_data,
                "credentials": oc_credentials,
                "timeout_ms": oc_workflow.timeout_ms,
                "headless": oc_workflow.headless,
                # Conditions the agent should evaluate before running in-session.
                "conditions": target.on_change_conditions,
                "run_in_session": True,
            }

    # Include decrypted auth session if available
    if target.auth_session_encrypted:
        auth_session = decrypt_auth_session(target.auth_session_encrypted)
        if auth_session:
            result["auth_session"] = auth_session

    return result


async def _load_selectors_by_target(db: AsyncSession, target_ids: list) -> Dict[int, list]:
    """Batch-load enabled selectors for many targets in ONE query.

    Returns {target_id: [TargetSelector, ...]} with each list ordered exactly as
    the per-target query did (priority desc, id asc). Target ids with no enabled
    selectors are absent from the dict.
    """
    if not target_ids:
        return {}
    from models.target_selector import TargetSelector
    rows = (await db.execute(
        select(TargetSelector)
        .where(TargetSelector.target_id.in_(target_ids))
        .where(TargetSelector.enabled == True)
        .order_by(TargetSelector.priority.desc(), TargetSelector.id)
    )).scalars().all()
    by_target: Dict[int, list] = {}
    for s in rows:
        by_target.setdefault(s.target_id, []).append(s)
    return by_target


async def _load_workflows_by_id(db: AsyncSession, workflow_ids: set) -> Dict[int, AutomationWorkflow]:
    """Batch-load AutomationWorkflows for the given ids in ONE query."""
    if not workflow_ids:
        return {}
    rows = (await db.execute(
        select(AutomationWorkflow).where(AutomationWorkflow.id.in_(workflow_ids))
    )).scalars().all()
    return {w.id: w for w in rows}


async def build_target_dict(
    target: Target,
    db: AsyncSession,
    include_workflow: bool = True,
    include_baseline: bool = True,
    selector_target_ids: Optional[list] = None,
) -> dict:
    """
    Build target dict with optional workflow and auth data.

    Args:
        target: Target model instance
        db: Database session
        include_workflow: Include pre_check_workflow data
        include_baseline: Include baseline content
        selector_target_ids: When this target represents a COALESCED group
            (several checks sharing one physical fetch — same fetch_key),
            the ids of every member target whose selectors ride on this single
            fetch. Defaults to ``[target.id]`` (no coalescing). Each selector keeps
            its own id + baseline, so reports fan out per selector_id back to the
            owning target.

    Returns:
        Target dict ready for agent response
    """
    _sel_target_ids = selector_target_ids or [target.id]
    selectors_by_target = await _load_selectors_by_target(db, _sel_target_ids)
    selectors = [s for tid in _sel_target_ids for s in selectors_by_target.get(tid, [])]
    # Preserve the single-query ordering (priority desc, id asc) across members.
    selectors.sort(key=lambda s: (-s.priority, s.id))

    workflow_ids = set()
    if include_workflow and target.pre_check_workflow_id:
        workflow_ids.add(target.pre_check_workflow_id)
    if (
        include_workflow
        and getattr(target, "on_change_in_session", False)
        and target.on_change_enabled
        and target.on_change_workflow_id
    ):
        workflow_ids.add(target.on_change_workflow_id)
    workflows_by_id = await _load_workflows_by_id(db, workflow_ids)

    return _assemble_target_dict(
        target, selectors, workflows_by_id, include_workflow, include_baseline
    )


async def build_target_dicts(
    coalesced: list,
    db: AsyncSession,
    include_workflow: bool = True,
    include_baseline: bool = True,
) -> List[dict]:
    """Batched build of many target dicts with at most 2 extra queries total.

    Args:
        coalesced: list of (representative Target, member_target_ids) pairs, as
            produced by ``coalesce_assigned_targets``.

    Pre-loads every group's enabled selectors and every referenced
    pre_check/on_change workflow via two ``IN(...)`` queries, then assembles each
    dict from in-memory lookups. Output is identical (and in the same order) to
    calling ``build_target_dict`` per group, but without the per-target N+1.
    """
    if not coalesced:
        return []

    # All member ids whose selectors ride on any of these fetches.
    all_member_ids: List[int] = []
    for _rep, member_ids in coalesced:
        all_member_ids.extend(member_ids)
    selectors_by_target = await _load_selectors_by_target(db, all_member_ids)

    # Every referenced workflow id (pre_check + opted-in on_change).
    workflow_ids: set = set()
    if include_workflow:
        for rep, _member_ids in coalesced:
            if rep.pre_check_workflow_id:
                workflow_ids.add(rep.pre_check_workflow_id)
            if (
                getattr(rep, "on_change_in_session", False)
                and rep.on_change_enabled
                and rep.on_change_workflow_id
            ):
                workflow_ids.add(rep.on_change_workflow_id)
    workflows_by_id = await _load_workflows_by_id(db, workflow_ids)

    results: List[dict] = []
    for rep, member_ids in coalesced:
        selectors = [s for tid in member_ids for s in selectors_by_target.get(tid, [])]
        selectors.sort(key=lambda s: (-s.priority, s.id))
        results.append(
            _assemble_target_dict(
                rep, selectors, workflows_by_id, include_workflow, include_baseline
            )
        )
    return results


# ============================================================================
# FULL config snapshot (AGENT POLL CONTRACT §2) + Redis publish helpers
# ============================================================================

async def _scheduling_block(db: AsyncSession, agent) -> dict:
    """Compute the agent's scheduling fields for the snapshot (contract §2).

    Mirrors the scheduling math in register_agent / get_agent_config so the
    snapshot is a superset of RegisterAgentResponse. Reads Config + agent.meta;
    no per-poll cost (this runs only on a distribution change, not on the hot
    poll path).
    """
    import time as _time
    from models.config import Config

    # Fetch all scheduling Config keys in ONE query, resolved from a dict.
    cfg = {
        c.key: c
        for c in (await db.execute(
            select(Config).where(
                Config.key.in_(["global_period_ms", "time_slot_mode", "sync_epoch_ms"])
            )
        )).scalars().all()
    }

    # global_period_ms
    gp = cfg.get("global_period_ms")
    global_period_ms = int(gp.value) if gp else settings.global_period_ms

    # time_slot_mode
    mode = cfg.get("time_slot_mode")
    time_slot_mode = mode.value if mode else "distributed"

    # active agent count (for rolling effective period)
    active_agent_count = (await db.execute(
        select(func.count(Agent.id)).where(Agent.status == AgentStatus.ACTIVE)
    )).scalar() or 0

    if time_slot_mode == "rolling":
        effective_period_ms = global_period_ms * active_agent_count
    else:
        effective_period_ms = global_period_ms

    # sync_epoch_ms
    se = cfg.get("sync_epoch_ms")
    if se:
        sync_epoch_ms = int(se.value) if isinstance(se.value, str) else se.value
    else:
        sync_epoch_ms = int(_time.time() * 1000)

    # offsets (period-specific dict form is canonical; legacy list form preserved)
    assigned_offsets_by_period = agent.meta.get('assigned_time_slot_offsets_by_period') if agent.meta else None
    if assigned_offsets_by_period:
        time_slot_offsets = {}
        assigned_slot_count = 0
        for period_str, offsets_data in assigned_offsets_by_period.items():
            period_ms = int(period_str)
            if isinstance(offsets_data, dict):
                time_slot_offsets[str(period_ms)] = {
                    "offset_ms": offsets_data.get("offset_ms", 0),
                    "effective_period_ms": offsets_data.get("effective_period_ms", period_ms),
                }
                assigned_slot_count += 1
            else:
                offset_ms = offsets_data[0] if offsets_data else 0
                eff = period_ms * active_agent_count if time_slot_mode == "rolling" else period_ms
                time_slot_offsets[str(period_ms)] = {"offset_ms": offset_ms, "effective_period_ms": eff}
                assigned_slot_count += len(offsets_data)
    else:
        # Legacy single-offset form (kept for back-compat; agents still accept it)
        legacy = agent.meta.get('assigned_time_slot_offsets', [agent.time_slot_offset_ms]) if agent.meta else [agent.time_slot_offset_ms]
        time_slot_offsets = legacy
        assigned_slot_count = len(legacy) if isinstance(legacy, list) else 1

    return {
        "global_period_ms": global_period_ms,
        "effective_period_ms": effective_period_ms,
        "time_slot_mode": time_slot_mode,
        "time_slot_offset_ms": agent.time_slot_offset_ms,
        "time_slot_offsets": time_slot_offsets,
        "assigned_slot_count": assigned_slot_count,
        "sync_epoch_ms": sync_epoch_ms,
        "sync_enabled": agent.sync_enabled,
    }


async def build_agent_snapshot(db: AsyncSession, agent) -> dict:
    """Build the FULL per-agent config snapshot (AGENT POLL CONTRACT §2).

    This is the single source of truth for what the coordinator publishes to
    ``ps:agent_configs``. It reconciles RegisterAgentResponse's scheduling/
    identity fields with the agent's complete assigned target set (built by the
    EXISTING ``build_target_dict`` helper — credentials/auth decrypted at publish
    time). The ``config_version`` is filled in by NodeConfigPublisher.publish_config.
    """
    from models.target_assignment import TargetAssignment
    # Assigned targets (FULL set, replaces incremental target deltas)
    assignments = (await db.execute(
        select(TargetAssignment).where(TargetAssignment.agent_id == agent.agent_id)
    )).scalars().all()
    target_ids = [a.target_id for a in assignments]

    targets: List[dict] = []
    if target_ids:
        rows = (await db.execute(
            select(Target).where(Target.id.in_(target_ids)).where(Target.enabled == True)
        )).scalars().all()
        # Coalesce: same-fetch_key targets become ONE fetch with unioned selectors.
        from services.monitor_coalescing import coalesce_assigned_targets
        coalesced = list(coalesce_assigned_targets(rows))
        targets = await build_target_dicts(coalesced, db, include_workflow=True, include_baseline=True)

    sched = await _scheduling_block(db, agent)

    # Scheduled workflows (Playwright-capable agents only)
    scheduled_workflows = None
    if getattr(agent, "has_playwright", False) and agent.meta:
        scheduled_workflows = agent.meta.get('assigned_scheduled_workflows', [])

    snapshot = {
        # config_version is set by publish_config (content hash). Placeholder here.
        "config_version": None,
        **sched,
        "targets": targets,
        "user_agent": agent.user_agent,
        "scheduled_workflows": scheduled_workflows,
        "emergency_pushover_app_token": settings.pushover_app_token or None,
        "emergency_pushover_user_key": settings.pushover_user_key or None,
    }
    return snapshot


async def publish_agent_snapshots(db: AsyncSession, agent_ids: List[str]) -> int:
    """Republish FULL snapshots + bumped versions for the given agents (delta
    DISTRIBUTION, contract §6/§7). Returns count published.

    This is how a distribution change propagates: only the AFFECTED agents are
    rebuilt and rewritten to Redis; their next poll sees a version miss and
    pulls the new snapshot. No ``force_config_update`` DB flag is involved.
    Best-effort: Redis errors are logged, never raised into the caller's flow.
    """
    if not agent_ids:
        return 0
    try:
        from services.node_config_publisher import NodeConfigPublisher
        redis_client = get_redis()
    except Exception as e:  # pragma: no cover - redis wiring
        logger.debug(f"publish_agent_snapshots skipped (no redis): {e}")
        return 0

    publisher = NodeConfigPublisher(redis_client)
    rows = (await db.execute(
        select(Agent).where(Agent.agent_id.in_(agent_ids))
    )).scalars().all()

    published = 0
    for agent in rows:
        try:
            snapshot = await build_agent_snapshot(db, agent)
            await publisher.publish_config(agent.agent_id, snapshot)
            published += 1
        except Exception as e:
            logger.warning(f"Failed to publish snapshot for agent {agent.agent_id}: {e}")
    if published:
        logger.info(f"Published FULL config snapshots for {published} agent(s) (delta distribution)")
    return published


async def trigger_auto_redistribution(db: AsyncSession, reason: str):
    """
    Trigger automatic target redistribution using capacity-aware distributor.

    Called when topology changes (agents join/leave).
    """
    try:
        from services.capacity_aware_distributor import CapacityAwareDistributor
        from models.config import Config
        from sqlalchemy import select

        # Get global period for capacity calculations
        global_period_result = await db.execute(
            select(Config).where(Config.key == "global_period_ms")
        )
        global_period_config = global_period_result.scalar_one_or_none()
        global_period_ms = int(global_period_config.value) if global_period_config else 10000

        distributor = CapacityAwareDistributor(db)
        stats = await distributor.distribute_timeslots_and_targets(global_period_ms)
        logger.info(f"Auto-redistribution triggered ({reason}): {stats}")
        return stats
    except Exception as e:
        logger.error(f"Auto-redistribution failed ({reason}): {e}")
        return None


async def redistribute_time_slots(db: AsyncSession, global_period_ms: int, reset_epoch: bool = False):
    """
    Redistribute time slots across all active agents.

    Supports two modes:
    - DISTRIBUTED: Each agent checks every global_period_ms (redundancy/quorum)
    - ROLLING: Each agent checks every (global_period_ms × agent_count) (load spreading)

    Sets force_config_update flag on all agents to notify them of the change.

    Args:
        db: Database session
        global_period_ms: Global check period in milliseconds
        reset_epoch: If True, resets sync_epoch_ms to force immediate resynchronization
                     (use when changing modes or periods, NOT for normal agent join/leave)

    Returns:
        Dict with redistribution stats
    """
    try:
        # Get time slot mode from config (default to "distributed")
        from models.config import Config
        import time

        mode_result = await db.execute(
            select(Config).where(Config.key == "time_slot_mode")
        )
        mode_config = mode_result.scalar_one_or_none()
        time_slot_mode = mode_config.value if mode_config else "distributed"

        # Handle sync_epoch_ms
        sync_epoch_result = await db.execute(
            select(Config).where(Config.key == "sync_epoch_ms")
        )
        sync_epoch_config = sync_epoch_result.scalar_one_or_none()

        if reset_epoch or not sync_epoch_config:
            # Reset or initialize sync epoch
            current_time_ms = int(time.time() * 1000)

            if sync_epoch_config:
                sync_epoch_config.value = current_time_ms
                logger.warning(f"Sync epoch RESET to {current_time_ms}ms (forced resynchronization - all agents will realign)")
            else:
                sync_epoch_config = Config(key="sync_epoch_ms", value=current_time_ms)
                db.add(sync_epoch_config)
                logger.info(f"Sync epoch initialized to {current_time_ms}ms (absolute reference for time slot alignment)")
        else:
            # Preserve existing epoch - CRITICAL for maintaining synchronization
            # All agents must use the same epoch to stay aligned
            logger.debug(f"Sync epoch preserved at {sync_epoch_config.value}ms (agents stay synchronized)")

        # Get all active agents
        result = await db.execute(
            select(Agent)
            .where(Agent.status == AgentStatus.ACTIVE)
        )
        active_agents = list(result.scalars().all())

        agent_count = len(active_agents)

        if agent_count == 0:
            logger.info("No active agents to redistribute time slots")
            return {"agents_updated": 0, "slot_duration": 0, "mode": time_slot_mode}

        # Check if agents have period-specific offsets (new capacity-aware system)
        # If so, skip this legacy redistribution - capacity-aware distributor handles it
        if active_agents[0].meta and 'assigned_time_slot_offsets_by_period' in active_agents[0].meta:
            logger.info(f"Skipping legacy time slot redistribution - {agent_count} agents use capacity-aware period-specific offsets")
            await db.commit()
            return {"agents_updated": 0, "slot_duration": 0, "mode": time_slot_mode, "skipped": "using_period_specific_offsets"}

        if agent_count == 1:
            # Single agent gets offset 0
            active_agents[0].time_slot_offset_ms = 0
            active_agents[0].force_config_update = True
            await db.commit()
            # Republish the affected agent's snapshot so the new offset reaches Redis
            # (contract §6/§9.2-4); force_config_update alone never reaches the poll path.
            await publish_agent_snapshots(db, [active_agents[0].agent_id])
            logger.info("Single agent assigned time slot 0ms")
            return {"agents_updated": 1, "slot_duration": global_period_ms, "mode": time_slot_mode}

        # Calculate slot duration (time between each agent's offset)
        # - DISTRIBUTED: agents spread across global_period (e.g., 0ms, 30ms for 60ms period, 2 agents)
        # - ROLLING: agents spread across (global_period × agent_count) (e.g., 0ms, 60ms for 60ms period, 2 agents)
        if time_slot_mode == "rolling":
            # In rolling mode, spread agents across the entire effective period
            slot_duration = global_period_ms
        else:
            # In distributed mode, spread agents within the global period
            slot_duration = global_period_ms // agent_count

        # Assign time slots evenly
        for idx, agent in enumerate(active_agents):
            new_offset = idx * slot_duration
            old_offset = agent.time_slot_offset_ms

            agent.time_slot_offset_ms = new_offset
            agent.force_config_update = True  # Signal agent to fetch new config

            logger.info(
                f"Agent {agent.agent_id}: time slot updated from {old_offset}ms to {new_offset}ms"
            )

        await db.commit()

        # Republish FULL snapshots for every agent whose offset just changed so the
        # new offsets reach Redis (ps:agent_configs/ps:agent_config_ver); the legacy
        # force_config_update flag never reaches the poll path (contract §6/§9.2-4).
        await publish_agent_snapshots(db, [a.agent_id for a in active_agents])

        # Log mode-specific info
        if time_slot_mode == "rolling":
            effective_period = global_period_ms * agent_count
            logger.info(
                f"Time slots redistributed (ROLLING mode): {agent_count} agents, "
                f"{slot_duration}ms apart, global period {global_period_ms}ms, "
                f"effective period {effective_period}ms (each agent checks every {effective_period}ms)"
            )
        else:
            logger.info(
                f"Time slots redistributed (DISTRIBUTED mode): {agent_count} agents, "
                f"{slot_duration}ms apart, period {global_period_ms}ms "
                f"(each agent checks every {global_period_ms}ms)"
            )

        return {
            "agents_updated": agent_count,
            "slot_duration": slot_duration,
            "period_ms": global_period_ms,
            "mode": time_slot_mode
        }

    except Exception as e:
        logger.error(f"Failed to redistribute time slots: {e}")
        await db.rollback()
        raise


# Pydantic models
class RegisterAgentRequest(BaseModel):
    """Request model for agent registration."""
    agent_id: str = Field(..., min_length=1, max_length=255)
    platform: Platform
    meta: Optional[Dict[str, Any]] = None
    # Optional ownership proof for RE-registration of an existing agent_id (#31).
    # An agent that still holds its current secret proves ownership by HMAC-signing
    # {"agent_id", "timestamp"} with it; without proof (and without an owner match)
    # a collision returns 409 rather than rotating the secret for the caller.
    timestamp: Optional[str] = Field(
        None, description="ISO 8601 / epoch timestamp for re-registration ownership proof"
    )
    signature: Optional[str] = Field(
        None,
        description="HMAC-SHA256 over {agent_id,timestamp} with the current secret (re-registration ownership proof)",
    )


class RegisterAgentResponse(BaseModel):
    """Response model for agent registration."""
    secret: str = Field(..., description="Agent secret for HMAC signing - save this securely")
    schedule: Dict[str, Any]
    global_period_ms: int
    effective_period_ms: int = Field(..., description="Actual period agent should use for checking (equals global_period with redundancy distribution)")
    target_set: list
    time_slot_offset_ms: int = Field(default=0, description="Millisecond offset for synchronized checking (legacy - single offset)")
    time_slot_offsets: Union[Dict[int, Dict[str, int]], List[int]] = Field(default=[0], description="Period-specific time slot offsets {period_ms: {offset_ms, effective_period_ms}} or legacy list [offsets]")
    assigned_slot_count: int = Field(default=1, description="Number of time slots assigned")
    sync_epoch_ms: int = Field(..., description="Absolute timestamp (epoch ms) for time slot synchronization")
    sync_enabled: bool = Field(default=True, description="Whether agent should sync to time slot")
    user_agent: str = Field(..., description="Assigned user agent string for this agent")
    emergency_pushover_app_token: Optional[str] = Field(None, description="Pushover app token for emergency notifications")
    emergency_pushover_user_key: Optional[str] = Field(None, description="Pushover user key for emergency notifications")
    # NOTE: Platform LLM keys are NEVER pushed to agents. Agents obtain managed AI
    # only via the backend-mediated AI gateway proxy (billed per call); BYO agents
    # use their own locally-stored keys. See settings.py:250.
    # Scheduled workflows (for Playwright-capable agents)
    scheduled_workflows: Optional[List[Dict[str, Any]]] = Field(None, description="Scheduled workflows assigned to this agent")
    # Relay node assignment
    node_url: Optional[str] = Field(None, description="Relay node URL for subsequent agent traffic (null = use coordinator directly)")
    # Initial config snapshot version (AGENT POLL CONTRACT §5). The agent stores
    # this and sends it on its first /poll so that first poll returns
    # config_updated=false (it already has the snapshot's contents via the
    # superset fields above). null means the agent should treat its first poll
    # as a full-config fetch.
    config_version: Optional[str] = Field(None, description="Version of the FULL config snapshot published to Redis at registration")


class HeartbeatRequest(BaseModel):
    """Request model for agent heartbeat."""
    agent_id: str
    timestamp: str = Field(..., description="ISO 8601 timestamp")
    signature: Optional[str] = None
    # Recorder-specific load info (optional, for recorder agents)
    active_sessions: Optional[int] = Field(None, description="Current active browser sessions (recorder agents)")
    max_sessions: Optional[int] = Field(None, description="Max concurrent sessions (recorder agents)")
    recorder_url: Optional[str] = Field(None, description="Recorder's reachable URL (recorder agents)")
    # Capacity/fastness profile (recorder agents). Refreshed on heartbeat so the
    # admin Agents page + HTTP-pool dispatcher track spec changes (e.g. resize).
    speed_class: Optional[str] = Field(None, description="fast | throughput | balanced")
    perf_score: Optional[int] = Field(None, description="Normalised single-core benchmark")
    cpu_cores: Optional[int] = None
    cpu_threads: Optional[int] = None
    cpu_clock_mhz: Optional[int] = None
    ram_mb: Optional[int] = None


class ReportRequest(BaseModel):
    """Request model for agent report submission."""
    agent_id: str
    target_url: str
    content_hash: str = Field(..., min_length=64, max_length=64, description="SHA-256 hash")
    diff_snippet: Optional[str] = None
    content: Optional[str] = Field(None, max_length=5000, description="New content for storage")
    previous_content: Optional[str] = Field(None, max_length=5000, description="Previous content for storage")
    headers: Optional[Dict[str, str]] = None
    timestamp: str = Field(..., description="ISO 8601 timestamp")
    signature: str = Field(..., description="HMAC-SHA256 signature")
    status_code: Optional[int] = None

    # Multi-selector support
    selector_id: Optional[int] = Field(None, description="ID of the selector checked")
    selector_name: Optional[str] = Field(None, description="Name of the selector")

    # Optional error information (can be included in same report)
    error_type: Optional[str] = Field(None, description="Type of error if any occurred")
    error_message: Optional[str] = Field(None, description="Error message if any occurred")

    # In-session on_change handling: when the agent ran the target's on_change_workflow
    # inside its live check browser (because the target opted into on_change_in_session),
    # it reports it here so the backend records the result and skips a duplicate fresh dispatch.
    on_change_handled_in_session: bool = Field(
        False,
        description="True if the agent already ran the on_change_workflow in its live check session",
    )
    on_change_result: Optional[Dict[str, Any]] = Field(
        None,
        description="Result data (extracted_data, success, error) from the in-session on_change run",
    )


class UptimeReportRequest(BaseModel):
    """Request model for uptime check report submission."""
    agent_id: str
    target_url: str
    # Id of the target checked (the representative when this was a coalesced
    # fetch). Lets the result fan out to the exact fetch_key group; falls back to
    # first-uptime-target-by-url when absent (older agents).
    target_id: Optional[int] = Field(None, description="ID of the target being checked")
    check_type: str = Field("uptime", description="Report type: uptime")
    timestamp: str = Field(..., description="ISO 8601 timestamp")
    signature: str = Field(..., description="HMAC-SHA256 signature")

    # Uptime check data
    is_up: bool = Field(..., description="Whether the site is up")
    status_code: Optional[int] = Field(None, description="HTTP status code")
    response_time_ms: Optional[int] = Field(None, description="Total response time in milliseconds")
    error_message: Optional[str] = Field(None, description="Error message if check failed")

    # Timing breakdown
    dns_time_ms: Optional[int] = Field(None, description="DNS resolution time")
    connect_time_ms: Optional[int] = Field(None, description="TCP connection time")
    tls_time_ms: Optional[int] = Field(None, description="TLS handshake time")

    # SSL certificate info
    ssl_cert_valid: Optional[bool] = Field(None, description="SSL certificate validity")
    ssl_cert_expires_at: Optional[str] = Field(None, description="SSL expiry date (ISO 8601)")
    ssl_cert_days_until_expiry: Optional[int] = Field(None, description="Days until SSL expires")
    ssl_cert_issuer: Optional[str] = Field(None, description="SSL certificate issuer")
    ssl_cert_subject: Optional[str] = Field(None, description="SSL certificate subject")
    ssl_error: Optional[str] = Field(None, description="SSL validation error")


class ErrorReportRequest(BaseModel):
    """Request model for agent error reporting."""
    agent_id: str
    target_url: Optional[str] = None
    target_id: Optional[int] = None
    error_type: str = Field(..., description="Type of error (rate_limited, page_check_error, connection_error, etc.)")
    error_message: str = Field(..., description="Error message or description")
    http_status: Optional[int] = Field(None, description="HTTP status code if applicable")
    timestamp: str = Field(..., description="ISO 8601 timestamp when error occurred")
    signature: str = Field(..., description="HMAC-SHA256 signature")


# ----------------------------------------------------------------------------
# Unified /api/agents/poll models (AGENT POLL CONTRACT §1). These are
# byte-for-byte identical to the node's models (the hosted node's agents endpoints)
# so the coordinator can host the SAME endpoint as a fallback (§8).
# ----------------------------------------------------------------------------

class PollChecked(BaseModel):
    """A lightweight no-change heartbeat entry: what was checked this cycle."""
    target_id: int
    selector_id: Optional[int] = None


class OnChangeResult(BaseModel):
    """In-session on_change handling result (mirrors node ChangeReport's typed
    field — kept byte-for-byte identical with the hosted node's agents endpoints)."""
    success: bool = True
    extracted_data: Optional[dict] = None
    error: Optional[str] = None


class PollChangeReport(BaseModel):
    """A change/error report (only populated when something changed this cycle)."""
    target_url: str
    target_id: Optional[int] = None
    check_type: str = "content"
    selector_id: Optional[int] = None
    selector_name: Optional[str] = None
    content_hash: Optional[str] = None
    diff_snippet: Optional[str] = None
    content: Optional[str] = None
    previous_content: Optional[str] = None
    # Visual checks: base64 PNG of the region (first run seeds baseline image; on
    # change it's the "after"). Powers the before/after image diff.
    screenshot: Optional[str] = None
    headers: Optional[dict] = None
    status_code: Optional[int] = None
    error_type: Optional[str] = None
    error_message: Optional[str] = None
    check_period_ms: Optional[int] = None
    # Uptime fields
    is_up: Optional[bool] = None
    response_time_ms: Optional[float] = None
    dns_time_ms: Optional[float] = None
    connect_time_ms: Optional[float] = None
    tls_time_ms: Optional[float] = None
    ssl_info: Optional[dict] = None
    # In-session on_change handling (capable agents)
    on_change_handled_in_session: Optional[bool] = None
    on_change_result: Optional[OnChangeResult] = None


class AgentPollRequest(BaseModel):
    """Unified heartbeat + optional change reports (contract §1.1)."""
    agent_id: str
    timestamp: str
    signature: str
    # The agent's CURRENTLY-APPLIED snapshot version; null/"" => "send full".
    config_version: Optional[str] = None
    checked: List[PollChecked] = Field(default_factory=list)
    change_reports: List[PollChangeReport] = Field(default_factory=list)


class AgentPollResponse(BaseModel):
    """Unified poll response (contract §1.2)."""
    status: str = "ok"
    config_updated: bool = False
    config: Optional[dict] = None
    # ALWAYS present (current server version) so the agent can self-heal drift.
    config_version: Optional[str] = None
    redirect_url: Optional[str] = None
    redirect_reason: Optional[str] = None
    processed: int = 0


class AgentInfo(BaseModel):
    """Response model for agent information."""
    id: int
    agent_id: str = Field(..., serialization_alias="agentId")
    platform: str
    status: str
    connected: bool = Field(..., description="Whether agent is currently connected (seen in last 5 minutes)")
    last_seen_at: Optional[str] = Field(None, serialization_alias="lastSeenAt")
    created_at: str = Field(..., serialization_alias="createdAt")
    meta: Optional[Dict[str, Any]] = None
    check_count: int = Field(default=0, serialization_alias="checkCount")
    assigned_targets: int = Field(default=0, serialization_alias="assignedTargets")
    avg_latency_ms: Optional[float] = Field(None, serialization_alias="avgLatencyMs")
    last_report_at: Optional[str] = Field(None, serialization_alias="lastReportAt")
    uptime_hours: Optional[float] = Field(None, serialization_alias="uptimeHours")
    time_slot_offset_ms: int = Field(default=0, serialization_alias="timeSlotOffsetMs")
    sync_enabled: bool = Field(default=True, serialization_alias="syncEnabled")
    # --- Capacity / fastness profile (admin Agents page + dispatch) ---
    speed_class: str = Field(default="balanced", serialization_alias="speedClass")
    perf_score: int = Field(default=0, serialization_alias="perfScore")
    max_sessions: int = Field(default=0, serialization_alias="maxSessions")
    active_sessions: int = Field(default=0, serialization_alias="activeSessions")
    cpu_cores: int = Field(default=0, serialization_alias="cpuCores")
    cpu_threads: int = Field(default=0, serialization_alias="cpuThreads")
    cpu_clock_mhz: int = Field(default=0, serialization_alias="cpuClockMhz")
    ram_mb: int = Field(default=0, serialization_alias="ramMb")
    role: Optional[str] = Field(default=None, serialization_alias="role")
    # Isolation pool this agent serves on Writ Cloud (Phase 3a): 'isolated' = the
    # gVisor/ephemeral pool that may run SENSITIVE (credentialed) runs; 'shared' =
    # the warm-browser pool for non-sensitive cloud runs. Sourced from
    # Agent.meta['tier'] (HTTP recorders) or the live ws-gateway registry blob's
    # 'tier' field (gateway agents); absent ⇒ None (own-BYO agents never tier).
    tier: Optional[str] = Field(default=None, serialization_alias="tier")
    # Geographic region detected via IP geolocation at registration (e.g. us-east).
    region: Optional[str] = Field(default=None, serialization_alias="region")
    # --- Capabilities (what this agent can actually run) ---
    check_modes: List[str] = Field(default_factory=list, serialization_alias="checkModes")
    has_playwright: bool = Field(default=False, serialization_alias="hasPlaywright")
    captcha_trusted: bool = Field(default=False, serialization_alias="captchaTrusted")
    is_trusted: bool = Field(default=False, serialization_alias="isTrusted")
    targets_per_slot: int = Field(default=0, serialization_alias="targetsPerSlot")
    parallel_workers: int = Field(default=0, serialization_alias="parallelWorkers")

    class Config:
        populate_by_name = True


async def rate_limit_agent(request: Request, db: AsyncSession = Depends(get_db)) -> None:
    """
    Dynamic rate limiting for agent endpoints.

    Scales with infrastructure speed - faster checking = higher limits.
    Formula: max_requests = (60000 / global_period_ms) * avg_targets * buffer
    """
    from dependencies import get_redis_client
    from security.rate_limit import RateLimiter
    from models.config import Config
    from models.target_assignment import TargetAssignment
    # Get global_period_ms from config to calculate expected request rate
    config_result = await db.execute(
        select(Config).where(Config.key == "global_period_ms")
    )
    config = config_result.scalar_one_or_none()
    global_period_ms = int(config.value) if config else settings.global_period_ms

    # Get average targets per agent (simple calculation to avoid nested aggregates)
    # Count total assignments and divide by active agents
    total_assignments_result = await db.execute(
        select(func.count(TargetAssignment.id))
        .select_from(TargetAssignment)
        .join(Agent, Agent.agent_id == TargetAssignment.agent_id)
        .where(Agent.status == AgentStatus.ACTIVE)
    )
    total_assignments = total_assignments_result.scalar() or 0

    active_agents_result = await db.execute(
        select(func.count(Agent.id)).where(Agent.status == AgentStatus.ACTIVE)
    )
    active_agents = active_agents_result.scalar() or 1

    # Calculate average (avoid division by zero)
    avg_targets = total_assignments / active_agents if active_agents > 0 else 10

    # Calculate expected requests per minute
    # Formula: (60 seconds * 1000ms) / period_ms = checks per minute
    # Then multiply by avg targets and add 50% buffer for errors/retries
    checks_per_minute = (60000 / global_period_ms)
    expected_requests = int(checks_per_minute * avg_targets * 1.5)  # 50% buffer

    # Minimum of 100, maximum of 10000 to prevent extremes
    max_requests = max(100, min(10000, expected_requests))

    redis = await get_redis_client(request)
    limiter = RateLimiter(
        redis_client=redis,
        max_requests=max_requests,
        window_seconds=60,
        prefix="agent_ratelimit"
    )

    # Key on the real client IP, NOT the X-Agent-ID header. The header is fully
    # client-controlled, so keying on it let an attacker rotate it per request to
    # get an unlimited supply of fresh buckets and defeat the limiter entirely.
    # (Requires uvicorn --proxy-headers so request.client.host is the real peer,
    # not the reverse proxy.)
    client_ip = request.client.host if request.client else "unknown"
    identifier = f"agent:{client_ip}"

    allowed, info = await limiter.is_allowed(identifier)

    if not allowed:
        from fastapi import HTTPException, status
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "error": "Rate limit exceeded",
                "limit": info["limit"],
                "reset_at": info["reset_at"],
                "info": f"Your infrastructure is checking every {global_period_ms}ms with ~{int(avg_targets)} targets"
            }
        )


async def get_agent_register_auth(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Auth for agent registration.

    Accepts an INFRASTRUCTURE recorder JWT (issued by the admin-approved device-flow
    `writ-dagent link`) → registers the agent as TRUSTED infra (user_hosted=False).
    Its trust was already established when an admin approved the device flow, so the
    token alone is sufficient here. Anything else (API key / platform JWT)
    falls through to the standard auth → normal user-hosted registration.
    """
    if credentials:
        try:
            from utils.recorder_auth import validate_recorder_token
            rec = await validate_recorder_token(credentials.credentials)
        except Exception:
            rec = None
        if rec and getattr(rec, "is_trusted", False) and getattr(rec, "role", None) == "infrastructure":
            return {
                "id": None,
                "label": "infra-jwt",
                "role": "agent",
                "is_platform_admin": False,
                "user_id": None,
                "is_trusted_agent": True,
            }
    # Not an infra JWT → standard API-key / platform-JWT auth (user-hosted).
    return await get_current_api_key(request, credentials, db)


def _reregistration_ownership_ok(
    existing_agent,
    request: "RegisterAgentRequest",
    api_key: dict,
    agent_is_trusted: bool,
) -> bool:
    """Return True iff the caller has proven ownership of an already-registered
    agent_id and may therefore rotate its secret (#31).

    Re-registration mints a brand-new secret and returns it, so an unconditional
    rotation on a client-supplied agent_id lets any authenticated caller hijack
    another agent's identity (impersonate it, read its target assignments, and —
    in a device-flow fleet — steal its work). Accepted proofs of ownership:

      (a) a valid HMAC over a fresh timestamped payload signed with the agent's
          CURRENT secret (possession of the existing secret ⇒ it is this agent);
      (b) an admin-approved infra device-flow token (trust already vetted by an
          admin when the link was approved);
      (c) a first-party admin / platform-admin API key;
      (d) an authenticated principal whose user_id matches the agent's recorded
          owner (meta['user_id']);
      (e) the agent has NO recorded owner yet (unclaimed slot) — preserves the
          single-owner recovery flow and first-registration semantics.

    Anything else returns False → the caller gets 409 instead of a fresh secret.
    """
    # (a) HMAC proof-of-possession of the current secret.
    signature = getattr(request, "signature", None)
    timestamp = getattr(request, "timestamp", None)
    if signature and timestamp and existing_agent.encrypted_secret:
        try:
            from security.encryption import SecretEncryption

            current_secret = SecretEncryption.decrypt_secret(
                existing_agent.encrypted_secret
            )
            payload = {"agent_id": request.agent_id, "timestamp": timestamp}
            if verify_fresh_signed_payload(
                payload, current_secret, signature, max_age_seconds=300
            ):
                return True
        except Exception as e:
            logger.warning(
                f"Re-registration HMAC proof failed for {request.agent_id}: {e}"
            )

    # (b) Admin-approved infra device-flow token.
    if agent_is_trusted:
        return True

    # (c) First-party admin / platform-admin API key.
    if api_key.get("role") == "admin" or api_key.get("is_platform_admin"):
        return True

    # (d)/(e) Owner match, or an unclaimed slot (no recorded owner).
    try:
        owner_user_id = (existing_agent.meta or {}).get("user_id")
    except Exception:
        owner_user_id = None
    if owner_user_id is None:
        return True
    caller_user_id = api_key.get("user_id")
    if caller_user_id is not None and caller_user_id == owner_user_id:
        return True

    return False


@router.post(
    "/register",
    response_model=RegisterAgentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register Agent",
    description="Register a new monitoring agent. Auto-registration enabled.",
    dependencies=[Depends(rate_limit_agent)],
)
async def register_agent(
    request: RegisterAgentRequest,
    db: AsyncSession = Depends(get_db),
    _api_key: dict = Depends(get_agent_register_auth),
):
    """
    Register a new monitoring agent.

    Requires API key authentication to prevent unauthorized agent registration.
    Generates a secret for HMAC signing and assigns the agent to the schedule.
    The secret is only returned once - the agent must store it securely.
    """
    try:
        # Infra device-flow token (admin-approved) → register as TRUSTED infra.
        agent_is_trusted = bool(_api_key.get("is_trusted_agent"))

        # Check if the agent already exists
        result = await db.execute(
            select(Agent).where(Agent.agent_id == request.agent_id)
        )
        existing_agent = result.scalar_one_or_none()

        if existing_agent:
            # SECURITY (#31): re-registration rotates the secret and returns it, so
            # it must NOT be granted to whoever supplies a matching agent_id. Require
            # proof of ownership (HMAC with the current secret, or an owner match)
            # before rotating; otherwise 409 Conflict rather than minting a new
            # secret for the caller. Legitimate flows (owner API key, admin-approved
            # infra token, an agent that still holds its secret, or an as-yet
            # unclaimed slot) still pass — see _reregistration_ownership_ok.
            if not _reregistration_ownership_ok(
                existing_agent, request, _api_key, agent_is_trusted
            ):
                logger.warning(
                    f"Rejected re-registration of {request.agent_id}: ownership not proven"
                )
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "agent_id already registered; ownership proof required to "
                        "re-register (sign {agent_id,timestamp} with the current "
                        "secret, or authenticate as the owner)"
                    ),
                )

            # Allow re-registration of INACTIVE or REVOKED agents
            # Reactivate the agent
            logger.info(f"Reactivating agent {request.agent_id} from {existing_agent.status.value} status")

            # Generate new secret
            secret = secrets.token_urlsafe(32)
            existing_agent.secret_hash = ph.hash(secret)

            # Store encrypted secret for HMAC verification
            from security.encryption import SecretEncryption
            existing_agent.encrypted_secret = SecretEncryption.encrypt_secret(secret)

            existing_agent.status = AgentStatus.ACTIVE
            if agent_is_trusted:  # infra token re-link → promote to trusted infra
                existing_agent.is_trusted = True
                existing_agent.user_hosted = False
            existing_agent.last_seen_at = datetime.utcnow()
            existing_agent.platform = request.platform
            existing_agent.meta = request.meta or {}

            # Extract region from meta (detected by agent via IP geo)
            meta = request.meta or {}
            if meta.get('region'):
                existing_agent.region = meta['region']

            agent = existing_agent
            await db.flush()
        else:
            # Generate secret for HMAC
            secret = secrets.token_urlsafe(32)
            secret_hash = ph.hash(secret)

            # Encrypt secret for retrievable storage
            from security.encryption import SecretEncryption
            encrypted_secret = SecretEncryption.encrypt_secret(secret)

            meta = request.meta or {}

            # Trust comes from the auth: infra device-flow token → trusted infra;
            # API key → user-hosted (admin can promote later).
            agent = Agent(
                agent_id=request.agent_id,
                platform=request.platform,
                status=AgentStatus.ACTIVE,
                secret_hash=secret_hash,
                encrypted_secret=encrypted_secret,
                last_seen_at=datetime.utcnow(),
                created_at=datetime.utcnow(),
                meta=meta,
                is_trusted=agent_is_trusted,
                user_hosted=not agent_is_trusted,
                region=meta.get('region'),
            )

            db.add(agent)
            await db.flush()

        # Assign user agent if not already assigned
        if not agent.user_agent:
            # Rotate through user agents based on agent's database ID
            total_ua = get_total_user_agents()
            ua_index = agent.id % total_ua
            agent.user_agent = get_user_agent_by_index(ua_index)
            logger.info(f"Assigned User-Agent to {request.agent_id}: {agent.user_agent[:60]}...")
            await db.flush()

        # Get enabled targets
        targets_result = await db.execute(
            select(Target).where(Target.enabled == True)
        )
        targets = targets_result.scalars().all()

        target_set = [
            {
                "url": t.url,
                "selector": t.selector,
                "ignore_regex": t.ignore_regex,
            }
            for t in targets
        ]

        # Get global period from config (database override or settings default)
        from models.config import Config
        config_result = await db.execute(
            select(Config).where(Config.key == "global_period_ms")
        )
        config = config_result.scalar_one_or_none()
        global_period_ms = int(config.value) if config else settings.global_period_ms

        # Scheduling is handled by the scheduler service; return schedule info.
        schedule_info = {
            "period_ms": global_period_ms,
            "next_run_at": (datetime.utcnow() + timedelta(
                milliseconds=global_period_ms
            )).isoformat(),
        }

        # Audit log
        audit = EventsAudit(
            actor=f"agent:{request.agent_id}",
            action="agent.register",
            details={
                "agent_id": request.agent_id,
                "platform": request.platform.value,
            },
            at=datetime.utcnow(),
        )
        if audit is not None:  # no audit store in this build → EventsAudit() is None
            db.add(audit)

        await db.commit()

        logger.info(
            f"Agent registered: {request.agent_id} ({request.platform.value})"
        )

        # NEW: Capacity-aware distribution (distributes to ALL agents for maximum dilution)
        from services.capacity_aware_distributor import CapacityAwareDistributor
        distributor = CapacityAwareDistributor(db)

        distribution_stats = await distributor.distribute_timeslots_and_targets(global_period_ms)

        logger.info(
            f"Capacity-aware distribution: {distribution_stats['agents']} agents, "
            f"{distribution_stats['targets_assigned']} targets assigned, "
            f"{distribution_stats.get('distribution_quality_percent', 0):.1f}% agent coverage"
        )

        # Refresh agent to get updated time slot offsets from metadata
        await db.refresh(agent)

        # Get assigned targets for this agent after redistribution. Select the
        # Target rows directly via the assignment join — one query instead of
        # re-fetching each Target individually after the join.
        from models.target_assignment import TargetAssignment
        targets_result = await db.execute(
            select(Target)
            .join(TargetAssignment, TargetAssignment.target_id == Target.id)
            .where(TargetAssignment.agent_id == request.agent_id)
        )
        assigned_targets = list(targets_result.scalars().all())

        # Build target list with workflow and auth data (coalesce same-fetch_key
        # targets into one fetch with unioned selectors).
        from services.monitor_coalescing import coalesce_assigned_targets
        coalesced = list(coalesce_assigned_targets(assigned_targets))
        assigned_target_set = await build_target_dicts(coalesced, db, include_workflow=True, include_baseline=True)

        # Fetch the scheduling Config keys in ONE query, resolved from a dict.
        import time
        sched_cfg = {
            c.key: c
            for c in (await db.execute(
                select(Config).where(
                    Config.key.in_(["time_slot_mode", "sync_epoch_ms"])
                )
            )).scalars().all()
        }

        # Calculate effective period based on time slot mode
        mode_config = sched_cfg.get("time_slot_mode")
        time_slot_mode = mode_config.value if mode_config else "distributed"

        # Count active agents for period calculation
        active_agent_count = (await db.execute(
            select(func.count(Agent.id)).where(Agent.status == AgentStatus.ACTIVE)
        )).scalar() or 0

        # Calculate effective period
        # In rolling mode: effective_period = period × num_agents (load distribution)
        # In distributed mode: effective_period = period (redundancy, all check at same rate)
        if time_slot_mode == "rolling":
            effective_period_ms = global_period_ms * active_agent_count
        else:
            effective_period_ms = global_period_ms

        # Get sync_epoch_ms for absolute time synchronization
        sync_epoch_config = sched_cfg.get("sync_epoch_ms")
        # Handle both old string format and new int format from JSON column
        if sync_epoch_config:
            sync_epoch_ms = int(sync_epoch_config.value) if isinstance(sync_epoch_config.value, str) else sync_epoch_config.value
        else:
            sync_epoch_ms = int(time.time() * 1000)

        # Get assigned time slot offsets from agent metadata
        # Check for period-specific offsets first, fallback to legacy single offset
        assigned_offsets_by_period = agent.meta.get('assigned_time_slot_offsets_by_period') if agent.meta else None

        if assigned_offsets_by_period:
            # Period-specific offsets (rolling mode)
            # Build complete scheduling info: {period: {"offset_ms": X, "effective_period_ms": Y}}
            assigned_offsets = {}
            assigned_slot_count = 0
            for period_str, offsets_data in assigned_offsets_by_period.items():
                period_ms = int(period_str)

                # Handle both new dict format and legacy list format
                if isinstance(offsets_data, dict):
                    # New format: {"offset_ms": X, "effective_period_ms": Y}
                    offset_ms = offsets_data.get("offset_ms", 0)
                    effective_period_ms = offsets_data.get("effective_period_ms", period_ms)
                    assigned_offsets[period_ms] = {
                        "offset_ms": offset_ms,
                        "effective_period_ms": effective_period_ms
                    }
                    assigned_slot_count += 1  # One slot per period in rolling mode
                else:
                    # Legacy list format: [offset1, offset2, ...]
                    offset_ms = offsets_data[0] if offsets_data else 0
                    # In rolling mode: effective_period = period × num_agents
                    effective_period_for_this_period = period_ms * active_agent_count if time_slot_mode == "rolling" else period_ms
                    assigned_offsets[period_ms] = {
                        "offset_ms": offset_ms,
                        "effective_period_ms": effective_period_for_this_period
                    }
                    assigned_slot_count += len(offsets_data)
        else:
            # Legacy mode: single set of offsets for all periods
            assigned_offsets = agent.meta.get('assigned_time_slot_offsets', [agent.time_slot_offset_ms]) if agent.meta else [agent.time_slot_offset_ms]
            assigned_slot_count = len(assigned_offsets)

        # SECURITY: Platform LLM API keys are intentionally NOT fetched or returned
        # here. Handing the global plaintext OpenAI/Anthropic key to any registering
        # agent was an unnecessary exposure. Managed AI is served only through the
        # backend-mediated AI gateway proxy; BYO agents use their own local keys.

        # Get scheduled workflows if agent has Playwright capability
        scheduled_workflows = None
        if agent.has_playwright and agent.meta:
            scheduled_workflows = agent.meta.get('assigned_scheduled_workflows', [])
            if scheduled_workflows:
                logger.info(f"Sending {len(scheduled_workflows)} scheduled workflows to agent {request.agent_id}")

        # Publish agent secret + FULL config snapshot to Redis for relay nodes
        # (AGENT POLL CONTRACT §5). The snapshot + content-hash version are the
        # source of truth the node/coordinator-fallback /poll serves from; nothing
        # in the poll path touches Postgres.
        node_url = None
        config_version = None
        try:
            from services.node_config_publisher import NodeConfigPublisher
            # Try to get redis from app state (set in lifespan)
            redis_url = settings.redis_url if hasattr(settings, 'redis_url') else None
            if redis_url:
                # Snapshot publishing uses the text client (str hash fields);
                # the encrypted secret is an already-string Fernet token.
                publisher = NodeConfigPublisher(get_redis())
                # Publish encrypted secret for node HMAC verification
                if agent.encrypted_secret:
                    await publisher.publish_secret(request.agent_id, agent.encrypted_secret)
                # Publish the FULL config snapshot + version so the agent's first
                # poll returns config_updated=false (it is already in sync).
                try:
                    snapshot = await build_agent_snapshot(db, agent)
                    config_version = await publisher.publish_config(request.agent_id, snapshot)
                except Exception as e:
                    logger.warning(f"Initial snapshot publish failed for {request.agent_id}: {e}")
                # Assign agent to best available node
                best_node = await publisher.get_best_node()
                if best_node:
                    node_url = best_node["url"]
                    await publisher.assign_to_node(request.agent_id, best_node["node_id"])
                    agent.node_id = best_node["node_id"]
                    await db.flush()
                    await db.commit()
                    logger.info(f"Agent {request.agent_id} assigned to node {best_node['node_id']}")
        except Exception as e:
            logger.debug(f"Node assignment skipped (no nodes available or Redis error): {e}")

        return RegisterAgentResponse(
            secret=secret,
            schedule=schedule_info,
            global_period_ms=global_period_ms,
            effective_period_ms=effective_period_ms,
            target_set=assigned_target_set,
            time_slot_offset_ms=agent.time_slot_offset_ms,
            time_slot_offsets=assigned_offsets,
            assigned_slot_count=assigned_slot_count,
            sync_epoch_ms=sync_epoch_ms,
            sync_enabled=agent.sync_enabled,
            user_agent=agent.user_agent,
            emergency_pushover_app_token=settings.pushover_app_token if settings.pushover_app_token else None,
            emergency_pushover_user_key=settings.pushover_user_key if settings.pushover_user_key else None,
            scheduled_workflows=scheduled_workflows,
            node_url=node_url,
            config_version=config_version,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error registering agent: {e}")
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to register agent",
        )


# ============================================================================
# Unified POST /api/agents/poll — coordinator FALLBACK implementation (§8).
#
# Identical contract to the node's /poll. Hosted by the coordinator for two
# cases: (a) no node was available at register, (b) an agent's node died and it
# failed over. CRITICAL INVARIANT: this path is Redis-ONLY — it never reads or
# writes Postgres. Reports go onto the SAME ps:reports:* stream the node uses
# (node_id "coordinator"), drained off the hot path by the stream consumer.
# ============================================================================

_POLL_REPLAY_SKEW_MS = 300_000  # 5-minute window (matches the node)


def _poll_signing_payload(agent_id: str, timestamp: str,
                          config_version: Optional[str], report_count: int) -> dict:
    """The EXACT signed payload for /poll (contract §4.4).

    Keys are signed in sorted order by the canonicalizer (sort_keys=True);
    config_version null is normalized to "" on both sides before signing.
    """
    return {
        "agent_id": agent_id,
        "timestamp": timestamp,
        "config_version": config_version or "",
        "report_count": report_count,
    }


@router.post(
    "/poll",
    response_model=AgentPollResponse,
    summary="Agent Poll (coordinator fallback)",
    description=(
        "Unified heartbeat + reports up; versioned config + redirect down. "
        "Identical to the relay node's /poll; the coordinator hosts it as a "
        "fallback (no node available, or node failover). Redis-only hot path."
    ),
)
async def agent_poll(request: AgentPollRequest):
    """Coordinator-side fallback implementation of the unified agent poll."""
    from services.node_config_publisher import NodeConfigPublisher, AGENT_SECRETS_KEY
    from services.report_relay_coordinator import relay_reports_coordinator, bump_coordinator_agent_seen
    from security.encryption import SecretEncryption
    from security.hmac import HMACAuth

    redis_client = get_redis()
    publisher = NodeConfigPublisher(redis_client)

    # --- Replay protection (epoch-millis-as-string, ±5 min) ---
    try:
        ts_ms = int(request.timestamp)
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        if abs(now_ms - ts_ms) > _POLL_REPLAY_SKEW_MS:
            raise HTTPException(status_code=403, detail="Stale or invalid request timestamp")
    except HTTPException:
        raise
    except (TypeError, ValueError):
        raise HTTPException(status_code=403, detail="Invalid request timestamp")

    # --- HMAC verify against the Redis-published encrypted secret (no DB) ---
    encrypted = await redis_client.hget(AGENT_SECRETS_KEY, request.agent_id)
    if not encrypted:
        raise HTTPException(status_code=401, detail="Invalid signature")
    if isinstance(encrypted, bytes):
        encrypted = encrypted.decode()
    try:
        secret = SecretEncryption.decrypt_secret(encrypted)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid signature")

    payload = _poll_signing_payload(
        request.agent_id, request.timestamp, request.config_version,
        len(request.change_reports),
    )
    if not HMACAuth.verify_signature(payload, secret, request.signature):
        raise HTTPException(status_code=401, detail="Invalid signature")

    # --- Replay dedupe: a signed poll may only be accepted once. The timestamp is
    # part of the signed payload, so legitimate distinct polls carry distinct
    # signatures; only an exact replay collides. SETNX a nonce keyed on the
    # signature (TTL >= the ±5min freshness window). Fail-open on redis errors.
    try:
        nonce_key = f"agent_poll_nonce:{request.agent_id}:{request.signature}"
        was_set = await redis_client.set(nonce_key, "1", ex=300, nx=True)
        if not was_set:
            logger.warning(f"Replayed poll signature from {request.agent_id}; dropping")
            raise HTTPException(status_code=403, detail="Duplicate signature (replay)")
    except HTTPException:
        raise
    except Exception as _e:
        logger.debug(f"poll replay nonce check skipped (redis error): {_e}")

    # --- Liveness parity: bump ps:node:coordinator:agent_seen (Redis only) ---
    try:
        await bump_coordinator_agent_seen(redis_client, request.agent_id)
    except Exception as e:
        logger.debug(f"coordinator agent_seen bump failed for {request.agent_id}: {e}")

    # --- Relay change reports onto the SAME stream the consumer drains ---
    processed = 0
    if request.change_reports:
        report_dicts = [r.dict(exclude_none=True) for r in request.change_reports]
        try:
            processed = await relay_reports_coordinator(
                redis_client, request.agent_id, report_dicts, request.timestamp,
            )
        except Exception as e:
            logger.error(f"coordinator report relay failed for {request.agent_id}: {e}")

    # --- Version-gated config (Redis only; full snapshot ONLY on a miss) ---
    server_ver = await publisher.get_config_version(request.agent_id)
    config = None
    config_updated = False
    if server_ver is not None and (request.config_version or "") != server_ver:
        config = await publisher.get_config(request.agent_id)
        config_updated = config is not None
        if config is not None:
            # The snapshot already carries config_version == server_ver.
            server_ver = config.get("config_version", server_ver)

    # --- Fallback recovery: drain the agent back onto a healthy node (§4.2.3) ---
    redirect_url = None
    redirect_reason = None
    try:
        node_url = await publisher.get_healthy_node_url_for_agent(request.agent_id)
        if node_url:
            redirect_url = node_url
            redirect_reason = "fallback_recovered"
    except Exception as e:
        logger.debug(f"fallback redirect lookup failed for {request.agent_id}: {e}")

    return AgentPollResponse(
        status="ok",
        config_updated=config_updated,
        config=config,
        config_version=server_ver,
        redirect_url=redirect_url,
        redirect_reason=redirect_reason,
        processed=processed,
    )


@router.post(
    "/heartbeat",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Agent Heartbeat",
    description="Update agent last_seen_at timestamp.",
)
async def agent_heartbeat(
    request: HeartbeatRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Record agent heartbeat.

    Updates the last_seen_at timestamp to track agent health.
    """
    try:
        result = await db.execute(
            select(Agent).where(Agent.agent_id == request.agent_id)
        )
        agent = result.scalar_one_or_none()

        if not agent:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Agent not found",
            )

        # SECURITY (#6): the HMAC signature is MANDATORY. Previously verification
        # was gated on `agent.encrypted_secret and request.signature`, so simply
        # OMITTING the signature skipped auth entirely, and a decrypt/verify
        # exception only logged a warning. That let an attacker who knows an
        # agent_id reactivate the agent and poison attacker-controlled meta —
        # notably meta['recorder_url'], the internal address the coordinator dials
        # outbound to build HTTP/WS to the recorder (SSRF / session redirect).
        # We now reject unsigned/unverifiable heartbeats with 401 BEFORE
        # reactivating the agent or writing any meta below. Legitimate agents are
        # unaffected: they already sign `f"{agent_id}|{timestamp}"` (see
        # CoordinatorAPI heartbeat), which verify_hmac_signature also freshness-checks.
        from security.encryption import SecretEncryption

        if not request.signature:
            raise HTTPException(status_code=401, detail="Missing heartbeat signature")

        if not agent.encrypted_secret:
            # Legacy agent without a stored secret cannot be authenticated; it must
            # re-register (which mints and stores an encrypted secret).
            raise HTTPException(
                status_code=401,
                detail="Agent needs to re-register to use HMAC authentication",
            )

        try:
            secret = SecretEncryption.decrypt_secret(agent.encrypted_secret)
        except Exception as e:
            # A decrypt failure is an authentication failure, NOT a warning.
            logger.warning(
                f"Heartbeat secret decryption failed for {request.agent_id}: {e}"
            )
            raise HTTPException(status_code=401, detail="Invalid heartbeat signature")

        message = f"{request.agent_id}|{request.timestamp}"
        if not verify_hmac_signature(message, request.signature, secret):
            raise HTTPException(status_code=401, detail="Invalid heartbeat signature")

        # Update last_seen_at
        agent.last_seen_at = datetime.utcnow()

        # Reactivate if inactive (agent came back online)
        if agent.status == AgentStatus.INACTIVE:
            agent.status = AgentStatus.ACTIVE
            logger.info(f"Agent {request.agent_id} reactivated via heartbeat")

        # Store recorder load info in agent meta (for load-balancing)
        _has_perf = any(v is not None for v in (
            request.speed_class, request.perf_score, request.cpu_cores,
            request.cpu_threads, request.cpu_clock_mhz, request.ram_mb))
        if request.active_sessions is not None or request.max_sessions is not None or request.recorder_url or _has_perf:
            meta = dict(agent.meta or {})
            if request.active_sessions is not None:
                meta['active_sessions'] = request.active_sessions
            if request.max_sessions is not None:
                meta['max_sessions'] = request.max_sessions
            if request.recorder_url:
                meta['recorder_url'] = request.recorder_url
            if _has_perf:
                from services.agent_speed import derive_speed_class
                perf = dict(meta.get('perf') or {})
                if request.perf_score is not None:
                    perf['perf_score'] = int(request.perf_score)
                if request.cpu_cores is not None:
                    perf['cpu_cores'] = int(request.cpu_cores)
                if request.cpu_threads is not None:
                    perf['cpu_threads'] = int(request.cpu_threads)
                if request.cpu_clock_mhz is not None:
                    perf['cpu_clock_mhz'] = int(request.cpu_clock_mhz)
                if request.ram_mb is not None:
                    perf['ram_mb'] = int(request.ram_mb)
                speed_class = derive_speed_class(
                    explicit=request.speed_class,
                    perf_score=request.perf_score,
                    max_sessions=request.max_sessions or meta.get('max_sessions'),
                    clock_mhz=request.cpu_clock_mhz,
                )
                perf['speed_class'] = speed_class
                meta['perf'] = perf
                meta['speed_class'] = speed_class
            agent.meta = meta
            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(agent, 'meta')

        await db.commit()

        logger.debug(f"Heartbeat received from agent: {request.agent_id}")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error processing heartbeat: {e}")
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to process heartbeat",
        )


@router.post(
    "/report",
    status_code=status.HTTP_200_OK,
    summary="Submit Report",
    description="Submit a content report. Requires HMAC signature. Returns config_updated flag.",
    dependencies=[Depends(rate_limit_agent)],
)
async def submit_report(
    request: ReportRequest,
    db: AsyncSession = Depends(get_db),
    redis: redis.Redis = Depends(get_redis_client),
):
    """
    Submit a content report from an agent.

    Reports must be signed with HMAC-SHA256 using the agent's secret.
    Triggers quorum check for change detection.
    """
    try:
        # Get agent and verify signature
        result = await db.execute(
            select(Agent).where(Agent.agent_id == request.agent_id)
        )
        agent = result.scalar_one_or_none()

        if not agent:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Agent not found",
            )

        # Verify agent is active
        if agent.status != AgentStatus.ACTIVE:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Agent is {agent.status.value}",
            )

        # Verify HMAC signature
        from security.encryption import SecretEncryption
        from security.hmac import HMACAuth

        if not agent.encrypted_secret:
            logger.error(f"Agent {request.agent_id} has no encrypted secret - legacy agent needs re-registration")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Agent needs to re-register to use HMAC authentication",
            )

        try:
            # Decrypt agent's secret
            agent_secret = SecretEncryption.decrypt_secret(agent.encrypted_secret)

            # Build payload for verification (same as agent signs)
            payload = {
                "agent_id": request.agent_id,
                "target_url": request.target_url,
                "content_hash": request.content_hash,
                "timestamp": request.timestamp,
            }

            # Constant-time HMAC match AND timestamp freshness (replay protection).
            # Previously this used the bare verify_signature with NO freshness check,
            # so a captured report could be replayed indefinitely.
            is_valid = verify_fresh_signed_payload(
                payload, agent_secret, request.signature, max_age_seconds=300
            )

            if not is_valid:
                logger.warning(f"Invalid/stale HMAC signature from agent {request.agent_id}")
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Invalid signature - authentication failed",
                )

            # Replay dedupe: a signature may only be accepted once. SETNX a nonce
            # keyed on the signature with a TTL >= the freshness window. Fail-open
            # if redis is missing (matches the app's posture elsewhere).
            if redis is not None:
                try:
                    nonce_key = f"agent_report_nonce:{request.agent_id}:{request.signature}"
                    was_set = await redis.set(nonce_key, "1", ex=300, nx=True)
                    if not was_set:
                        logger.warning(
                            f"Replayed report signature from {request.agent_id}; dropping"
                        )
                        raise HTTPException(
                            status_code=status.HTTP_403_FORBIDDEN,
                            detail="Duplicate signature (replay)",
                        )
                except HTTPException:
                    raise
                except Exception as _e:
                    logger.debug(f"Replay nonce check skipped (redis error): {_e}")

        except ValueError as e:
            logger.error(f"Secret decryption failed for agent {request.agent_id}: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Secret decryption failed",
            )

        # Resolve the target. Under coalescing a URL maps to MANY targets
        # (different zones), so the globally-unique selector_id is the
        # authoritative key for content reports — looking up by URL alone would be
        # ambiguous (MultipleResultsFound). URL is only a legacy fallback for
        # reports that predate selector_id.
        from models.target_selector import TargetSelector

        selector = None
        selector_id = request.selector_id

        target = None
        if selector_id:
            selector = (await db.execute(
                select(TargetSelector).where(TargetSelector.id == selector_id)
            )).scalar_one_or_none()
            if selector:
                target = (await db.execute(
                    select(Target).where(Target.id == selector.target_id)
                )).scalar_one_or_none()

        if target is None:
            # Legacy / no-selector: best-effort first match by URL.
            target = (await db.execute(
                select(Target).where(Target.url == request.target_url).order_by(Target.id)
            )).scalars().first()

        if not target:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Target not found",
            )

        if not selector_id:
            # Legacy report without selector - just log and return OK
            # Agent will get updated config with selectors on next sync
            logger.warning(f"Report for {target.url} missing selector_id - agent needs config update")
            # Still update target's last_checked
            target.check_count = (target.check_count or 0) + 1
            target.last_checked_at = datetime.utcnow()
            await db.commit()

            # Signal agent needs fresh config
            return {
                "status": "ok",
                "config_updated": True,  # Force agent to fetch new config with selectors
                "message": "selector_id required - please fetch updated config"
            }

        if not selector:
            logger.warning(f"Selector {selector_id} not found")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Selector {selector_id} not found",
            )

        # Change detection + notifications run through the SHARED report-ingest
        # core (services/report_ingest.submit_reports_internal), the same core the
        # ws-gateway batch path uses. HMAC was already verified above, so we hand
        # the (single) report to the core as a trusted, pre-authenticated batch.
        # It performs the baseline/DetectedChange writes, on-change dispatch,
        # monitor-health edges, notifications, and trigger evaluation, and commits.
        from services.report_ingest import submit_reports_internal

        report_payload = {
            "target_url": request.target_url,
            "check_type": "content",
            "selector_id": request.selector_id,
            "selector_name": request.selector_name,
            "content_hash": request.content_hash,
            "diff_snippet": request.diff_snippet,
            "content": request.content,
            "previous_content": request.previous_content,
            "screenshot": getattr(request, "screenshot", None),
            "headers": request.headers,
            "status_code": request.status_code,
            "error_type": request.error_type,
            "error_message": request.error_message,
            "on_change_handled_in_session": request.on_change_handled_in_session,
            "on_change_result": request.on_change_result,
        }
        ingest_result = await submit_reports_internal(
            {
                "agent_id": request.agent_id,
                "timestamp": request.timestamp,
                "reports": [report_payload],
            },
            db,
        )
        content_changed = bool(ingest_result.get("content_changed"))
        baseline_updates = ingest_result.get("baseline_updates") or {}

        logger.info(
            f"Report received: {request.agent_id} -> {request.target_url} "
            f"(hash: {request.content_hash[:8]}..., changed: {content_changed})"
        )

        # Build the report response. In the single-user coordinator agents are
        # driven over the ws-gateway (target distribution + hot-reload live on the
        # published config snapshot / gateway pushes), so this legacy HTTP path
        # only echoes the selector baseline_updates it just wrote plus a
        # config_updated flag derived from the agent's pending hot-reload flag.
        incremental_updates = {}
        if baseline_updates:
            incremental_updates["baseline_updates"] = baseline_updates

        config_updated = bool(getattr(agent, "force_config_update", False))
        if config_updated:
            # Acknowledge the hot-reload signal; the agent refreshes via the
            # gateway-published snapshot on its next poll/reconnect.
            agent.force_config_update = False
            await db.commit()

        # Include pending automation tasks for Playwright-capable agents
        if agent.has_playwright:
            from models.automation_task import AutomationTask

            # Find pending tasks (on-change, manual, scheduled)
            # Use FOR UPDATE SKIP LOCKED to prevent duplicate assignment across concurrent requests
            pending_tasks_result = await db.execute(
                select(AutomationTask)
                .where(
                    AutomationTask.status == "pending",
                    AutomationTask.attempt_count < AutomationTask.max_attempts,
                    # Exclude AI session/navigation tasks - they use dedicated endpoints
                    AutomationTask.trigger_type.notin_(["ai_session", "ai_navigate"]),
                )
                .order_by(AutomationTask.created_at)
                .limit(5)
                .with_for_update(skip_locked=True)
            )
            pending_tasks = pending_tasks_result.scalars().all()

            if pending_tasks:
                automation_tasks = []
                for task in pending_tasks:
                    # Get workflow
                    workflow_result = await db.execute(
                        select(AutomationWorkflow).where(AutomationWorkflow.id == task.workflow_id)
                    )
                    workflow = workflow_result.scalar_one_or_none()

                    if not workflow:
                        # Mark task as failed if workflow not found
                        task.status = "failed"
                        task.error_message = "Workflow not found"
                        continue

                    # Mark as assigned to this agent
                    task.status = "assigned"
                    task.executor_agent_id = agent.agent_id
                    task.attempt_count += 1

                    # Decrypt credentials before sending to agent
                    credentials = None
                    if workflow.credentials_encrypted:
                        try:
                            from cryptography.fernet import Fernet
                            from security.hmac import get_fernet_key
                            import json
                            fernet = Fernet(get_fernet_key())
                            credentials = json.loads(fernet.decrypt(workflow.credentials_encrypted.encode()).decode())
                        except Exception as e:
                            logger.error(f"Failed to decrypt workflow credentials: {e}")

                    # Merge form_data: workflow defaults, then trigger context overrides
                    trigger_ctx = task.trigger_context or {}
                    merged_form_data = {**(workflow.form_data or {}), **(trigger_ctx.get("form_data") or {})}

                    automation_tasks.append({
                        "task_id": task.id,
                        "trigger_type": task.trigger_type,
                        "target_id": task.target_id,
                        "workflow": {
                            "id": workflow.id,
                            "name": workflow.name,
                            "workflow_type": workflow.workflow_type,
                            "steps": workflow.steps,
                            "form_data": merged_form_data,
                            "timeout_ms": workflow.timeout_ms,
                            "headless": workflow.headless,
                        },
                        "credentials": credentials,  # Decrypted credentials
                    })

                if automation_tasks:
                    incremental_updates["automation_tasks"] = automation_tasks
                    await db.commit()
                    logger.info(f"Assigned {len(automation_tasks)} automation tasks to agent {agent.agent_id}")

            # Scheduled workflows are distributed via the gateway snapshot and
            # stored in agent.meta['assigned_scheduled_workflows']; echo them here.
            if agent.meta:
                assigned_workflows = agent.meta.get('assigned_scheduled_workflows', [])
                if assigned_workflows:
                    incremental_updates["scheduled_workflows"] = assigned_workflows
                    logger.info(f"Sending {len(assigned_workflows)} scheduled workflows to agent {agent.agent_id}")

        return {
            "status": "ok",
            "config_updated": config_updated or bool(incremental_updates),
            **incremental_updates,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error processing report: {e}")
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to process report",
        )


@router.post(
    "/report-uptime",
    status_code=status.HTTP_200_OK,
    summary="Submit Uptime Report",
    description="Submit an uptime check report. Requires HMAC signature.",
    dependencies=[Depends(rate_limit_agent)],
)
async def submit_uptime_report(
    request: UptimeReportRequest,
    db: AsyncSession = Depends(get_db),
    redis: redis.Redis = Depends(get_redis_client),
):
    """
    Submit an uptime check report from an agent.

    Reports must be signed with HMAC-SHA256 using the agent's secret.
    Stores uptime history with SSL certificate monitoring.
    """
    try:
        # Get agent and verify signature
        result = await db.execute(
            select(Agent).where(Agent.agent_id == request.agent_id)
        )
        agent = result.scalar_one_or_none()

        if not agent:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Agent not found",
            )

        # Verify agent is active
        if agent.status != AgentStatus.ACTIVE:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Agent is {agent.status.value}",
            )

        # Verify HMAC signature
        from security.encryption import SecretEncryption
        from security.hmac import HMACAuth

        if not agent.encrypted_secret:
            logger.error(f"Agent {request.agent_id} has no encrypted secret")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Agent needs to re-register to use HMAC authentication",
            )

        try:
            # Decrypt agent's secret
            agent_secret = SecretEncryption.decrypt_secret(agent.encrypted_secret)

            # Build payload for verification
            payload = {
                "agent_id": request.agent_id,
                "target_url": request.target_url,
                "is_up": request.is_up,
                "timestamp": request.timestamp,
            }

            # Constant-time HMAC match AND timestamp freshness (replay protection).
            # Previously this used the bare verify_signature with NO freshness check,
            # so a captured uptime report could be replayed indefinitely.
            is_valid = verify_fresh_signed_payload(
                payload, agent_secret, request.signature, max_age_seconds=300
            )

            if not is_valid:
                logger.warning(f"Invalid/stale HMAC signature from agent {request.agent_id}")
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Invalid signature - authentication failed",
                )

            # Replay dedupe: a signature may only be accepted once. SETNX a nonce
            # keyed on the signature with a TTL >= the freshness window. Fail-open
            # if redis is missing (matches the app's posture elsewhere).
            if redis is not None:
                try:
                    nonce_key = f"agent_uptime_nonce:{request.agent_id}:{request.signature}"
                    was_set = await redis.set(nonce_key, "1", ex=300, nx=True)
                    if not was_set:
                        logger.warning(
                            f"Replayed uptime signature from {request.agent_id}; dropping"
                        )
                        raise HTTPException(
                            status_code=status.HTTP_403_FORBIDDEN,
                            detail="Duplicate signature (replay)",
                        )
                except HTTPException:
                    raise
                except Exception as _e:
                    logger.debug(f"Replay nonce check skipped (redis error): {_e}")

        except ValueError as e:
            logger.error(f"Secret decryption failed for agent {request.agent_id}: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Secret decryption failed",
            )

        # Get target. Prefer the explicit target_id (the coalesced group's
        # representative) so the fan-out below hits the exact fetch_key group —
        # critical when one URL has several intervals. Fall back to the first
        # uptime target for the URL for older agents that don't send target_id.
        target = None
        if request.target_id is not None:
            target = (await db.execute(
                select(Target).where(Target.id == request.target_id, Target.check_type == "uptime")
            )).scalar_one_or_none()
        if target is None:
            target = (await db.execute(
                select(Target)
                .where(Target.url == request.target_url, Target.check_type == "uptime")
                .order_by(Target.id)
            )).scalars().first()

        if not target:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Target not found",
            )

        # Verify target is uptime check
        if target.check_type != 'uptime':
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Target is not an uptime check",
            )

        # Parse SSL expiry date if provided
        from datetime import datetime as dt
        ssl_cert_expires_at = None
        if request.ssl_cert_expires_at:
            try:
                ssl_cert_expires_at = dt.fromisoformat(request.ssl_cert_expires_at.replace('Z', '+00:00'))
            except Exception as e:
                logger.warning(f"Could not parse SSL expiry date: {e}")

        # Change-only persistence with COALESCED fan-out: one physical uptime
        # fetch serves every target sharing this fetch_key, so record the result
        # for each of them. Redis last-check updates always; an UptimeCheck row is
        # written only when up/down or the status code CHANGED (per target).
        from services.monitoring_state import record_uptime
        from services.monitor_coalescing import uptime_group_targets
        from models.uptime_check import UptimeCheck

        pending_health: list = []  # (member, event_type) — dispatched after commit
        for member in await uptime_group_targets(db, target):
            state_changed, health_event = await record_uptime(member.id, request.is_up, request.status_code)
            if state_changed:
                db.add(UptimeCheck(
                    target_id=member.id,
                    agent_id=request.agent_id,
                    checked_at=datetime.utcnow(),
                    is_up=request.is_up,
                    status_code=request.status_code,
                    response_time_ms=request.response_time_ms,
                    error_message=request.error_message,
                    dns_time_ms=request.dns_time_ms,
                    connect_time_ms=request.connect_time_ms,
                    tls_time_ms=request.tls_time_ms,
                    ssl_cert_valid=request.ssl_cert_valid,
                    ssl_cert_expires_at=ssl_cert_expires_at,
                    ssl_cert_days_until_expiry=request.ssl_cert_days_until_expiry,
                    ssl_cert_issuer=request.ssl_cert_issuer,
                    ssl_cert_subject=request.ssl_cert_subject,
                    ssl_error=request.ssl_error,
                ))
            if health_event:
                pending_health.append((member, health_event))

        # Update agent last_seen_at
        agent.last_seen_at = datetime.utcnow()

        await db.commit()

        # Fire monitor-health automations (down / recovered) AFTER the ingest
        # commit so trigger dispatch runs in its own transaction and can never
        # leave the uptime write half-applied. Fail-soft.
        if pending_health:
            from services.monitor_health_events import dispatch_monitor_health
            for member, event_type in pending_health:
                await dispatch_monitor_health(
                    db, member, event_type,
                    {"state": "up" if request.is_up else "down", "status_code": request.status_code},
                )

        logger.info(
            f"Uptime report received: {request.agent_id} -> {request.target_url} "
            f"(status: {'UP' if request.is_up else 'DOWN'}, response_time: {request.response_time_ms}ms)"
        )

        # Build incremental config updates (same as content reports)
        from models.config import Config
        import time as time_module

        incremental_updates = {}

        if agent.force_config_update:
            from services.capacity_aware_distributor import CapacityAwareDistributor

            distributor = CapacityAwareDistributor(db)
            assigned_targets = await distributor.get_agent_targets(agent.agent_id)

            # Build target list with workflow and auth data (coalesce same-fetch_key).
            from services.monitor_coalescing import coalesce_assigned_targets
            coalesced = list(coalesce_assigned_targets(assigned_targets))
            target_list = await build_target_dicts(coalesced, db, include_workflow=True, include_baseline=True)
            target_updates = {"targets": target_list}
            incremental_updates["target_updates"] = target_updates

            # Clear the flag
            agent.force_config_update = False
            await db.commit()
            logger.info(f"Sent config updates to {request.agent_id}: {len(target_updates['targets'])} targets")

        return {
            "status": "ok",
            "config_updated": bool(incremental_updates),
            **incremental_updates
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error processing uptime report: {e}", exc_info=True)
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to process uptime report",
        )


@router.post(
    "/report-error",
    status_code=status.HTTP_200_OK,
    summary="Report Error",
    description="Report an error from an agent. Enables error correlation and alerting.",
)
async def report_error(
    request: ErrorReportRequest,
):
    """
    Accept an error report from an agent.

    The agent-error store + cross-agent correlation/alerting was a cloud feature
    backed by the (now removed) ``agent_error`` table. The single-owner
    coordinator accepts the frame for wire compatibility only: nothing is
    persisted, nothing is fanned out, and no state is mutated.

    SECURITY (#29): this endpoint declares a required ``signature`` field but
    never verified it, so anyone who knew an active agent_id could bump its
    last_seen_at and keep a dead agent looking "alive". That side effect was
    removed; real liveness now rides exclusively on the authenticated poll /
    heartbeat path.

    It is also the only unauthenticated POST under /api/agents, so the agent
    lookup that remained behind it was a free ENUMERATION ORACLE: 404 vs 403 vs
    200 told an anonymous caller whether a given agent_id existed and what
    lifecycle state it was in. With no side effect left to authorize, the lookup
    had no purpose but to leak, so it is gone — the response is now identical for
    every input. The report is logged (where an operator can correlate it) and
    acknowledged.
    """
    logger.info(
        f"Error reported: {request.agent_id} -> {request.error_type} "
        f"({request.target_url or 'no target'})"
    )

    # Constant response: no database read, nothing for a caller to distinguish.
    return {
        "status": "ok",
        "correlation": {
            "agent_count": 0,
            "notification_sent": False,
        }
    }


@router.post(
    "/deregister",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Deregister Agent",
    description="Agent self-deregistration (graceful shutdown).",
)
async def deregister_agent(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Agent self-deregistration on graceful shutdown.

    Marks agent as INACTIVE and triggers redistribution.
    Uses HMAC authentication from request body.
    """
    try:
        # Parse request body
        body = await request.json()
        agent_id = body.get('agent_id')
        reason = body.get('reason', 'Graceful shutdown')

        if not agent_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="agent_id is required",
            )

        # Get agent
        result = await db.execute(
            select(Agent).where(Agent.agent_id == agent_id)
        )
        agent = result.scalar_one_or_none()

        if not agent:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Agent not found",
            )

        # SECURITY (#11): authenticate the caller as THIS agent BEFORE mutating any
        # state. This endpoint used to trust the client-supplied agent_id alone —
        # despite the docstring claiming HMAC — so an unauthenticated attacker who
        # knew/guessed an agent_id could mark it INACTIVE, bulk-delete every one of
        # its TargetAssignment rows and force a fleet-wide redistribution (DoS).
        # The agent proves possession of its shared secret by HMAC-signing the
        # request body (X-Signature header, exactly as CoordinatorAPI.deregister →
        # HMACAuth.create_auth_header emits it) with its stored secret. When the
        # signed payload also carries a timestamp we additionally enforce freshness
        # (replay protection), reusing the same verifier as /report and /config.
        from security.encryption import SecretEncryption
        from security.hmac import HMACAuth

        if not agent.encrypted_secret:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Agent needs to re-register to use HMAC authentication",
            )

        provided_sig = request.headers.get("X-Signature") or body.get("signature")
        if not provided_sig:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing agent signature",
            )

        try:
            agent_secret = SecretEncryption.decrypt_secret(agent.encrypted_secret)
        except Exception as e:
            logger.warning(f"Deregister secret decryption failed for {agent_id}: {e}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid signature - authentication failed",
            )

        # The agent signs the exact JSON body it sends (minus any inline signature
        # field). Verify over the received body so canonicalization matches
        # HMACAuth.sign_request on both ends regardless of extra fields.
        signed_payload = {k: v for k, v in body.items() if k != "signature"}
        if "timestamp" in signed_payload:
            sig_ok = verify_fresh_signed_payload(
                signed_payload, agent_secret, provided_sig, max_age_seconds=300
            )
        else:
            sig_ok = HMACAuth.verify_signature(signed_payload, agent_secret, provided_sig)

        if not sig_ok:
            logger.warning(f"Invalid/stale deregister signature from agent {agent_id}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid signature - authentication failed",
            )

        # Mark as inactive
        agent.status = AgentStatus.INACTIVE

        # Remove all target assignments in a single bulk DELETE.
        from models.target_assignment import TargetAssignment
        delete_result = await db.execute(
            delete(TargetAssignment).where(TargetAssignment.agent_id == agent_id)
        )
        removed_count = delete_result.rowcount or 0

        if removed_count:
            logger.info(f"Removed {removed_count} target assignment(s) from deregistering agent {agent_id}")

        # Audit log
        audit = EventsAudit(
            actor=f"agent:{agent_id}",
            action="agent.deregister",
            details={"agent_id": agent_id, "reason": reason},
            at=datetime.utcnow(),
        )
        if audit is not None:  # no audit store in this build → EventsAudit() is None
            db.add(audit)

        await db.commit()

        logger.info(f"Agent deregistered: {agent_id} ({reason})")

        # Get global period for time slot redistribution
        from models.config import Config
        config_result = await db.execute(
            select(Config).where(Config.key == "global_period_ms")
        )
        config = config_result.scalar_one_or_none()
        global_period_ms = int(config.value) if config else settings.global_period_ms

        # Redistribute time slots after agent leaves
        await redistribute_time_slots(db, global_period_ms)

        # Auto-redistribute targets when agent disconnects
        await trigger_auto_redistribution(db, "agent_deregistered")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deregistering agent: {e}")
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to deregister agent",
        )


async def _load_admin_agent(db, agent_id: str, current_api_key: dict, action: str):
    """Authorize an agent-management action and load the agent.

    Requires admin role (single-owner coordinator).
    """
    if current_api_key.get("role") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Only admins can {action}",
        )

    query = select(Agent).where(Agent.agent_id == agent_id)
    agent = (await db.execute(query)).scalar_one_or_none()
    if not agent:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent not found",
        )
    return agent


@router.post(
    "/{agent_id}/revoke",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke Agent",
    description="Revoke an agent's access. Requires admin role.",
)
async def revoke_agent(
    agent_id: str,
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """
    Revoke (delete) an agent's access.

    Only admins can revoke agents. This permanently deletes the agent and all related data.
    Related data (assignments, reports, errors) are automatically deleted via CASCADE.
    """
    try:
        agent = await _load_admin_agent(db, agent_id, current_api_key, "revoke agents")

        # Audit log before deletion
        audit = EventsAudit(
            actor=current_api_key.get("label"),
            action="agent.delete",
            details={
                "agent_id": agent_id,
                "platform": agent.platform,
                "status": agent.status.value
            },
            at=datetime.utcnow(),
        )
        if audit is not None:  # no audit store in this build → EventsAudit() is None
            db.add(audit)

        # Delete the agent (CASCADE will handle related records)
        await db.delete(agent)
        await db.commit()

        logger.info(f"Agent deleted: {agent_id} by {current_api_key.get('label')}")

        # Evict the agent's Redis state (secret + snapshot + version + node map)
        # and notify nodes to drop it (AGENT POLL CONTRACT §3 — deleted on revoke).
        # Without this a node keeps serving the revoked agent_id's cached config.
        try:
            from services.node_config_publisher import NodeConfigPublisher
            await NodeConfigPublisher(get_redis()).revoke_agent(agent_id)
        except Exception as _e:
            logger.debug(f"Redis revoke for {agent_id} skipped: {_e}")

        # Auto-redistribute targets when agent is deleted
        await trigger_auto_redistribution(db, "agent_deleted")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting agent: {e}")
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete agent",
        )


@router.post(
    "/{agent_id}/rotate-secret",
    summary="Rotate Agent Secret",
    description="Rotate an agent's HMAC secret. Requires admin role.",
)
async def rotate_agent_secret(
    agent_id: str,
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """
    Rotate an agent's secret.

    Generates a new secret for HMAC signing. The agent must update its local secret.
    Only admins can rotate secrets.
    """
    try:
        agent = await _load_admin_agent(db, agent_id, current_api_key, "rotate agent secrets")

        # Generate new secret
        new_secret = secrets.token_urlsafe(32)
        agent.secret_hash = ph.hash(new_secret)

        # Audit log
        audit = EventsAudit(
            actor=current_api_key.get("label"),
            action="agent.rotate_secret",
            details={"agent_id": agent_id},
            at=datetime.utcnow(),
        )
        if audit is not None:  # no audit store in this build → EventsAudit() is None
            db.add(audit)

        await db.commit()

        logger.info(f"Agent secret rotated: {agent_id} by {current_api_key.get('label')}")

        return {
            "status": "success",
            "message": "Secret rotated successfully",
            "new_secret": new_secret,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error rotating agent secret: {e}")
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to rotate agent secret",
        )


@router.post(
    "/{agent_id}/force-check",
    summary="Force Agent Check",
    description="Force an agent to perform an immediate check. Requires admin role.",
)
async def force_agent_check(
    agent_id: str,
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """
    Force an agent to perform an immediate check.

    Sets a flag in agent metadata that the agent will check on next heartbeat
    to trigger an immediate config update and target check.
    Only admins can force checks.
    """
    try:
        agent = await _load_admin_agent(db, agent_id, current_api_key, "force agent checks")

        if agent.status != AgentStatus.ACTIVE:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Agent is {agent.status.value}, cannot force check",
            )

        # Set flag in agent metadata to force config update
        if not agent.meta:
            agent.meta = {}
        agent.meta["force_config_update"] = True
        agent.meta["force_config_update_at"] = datetime.utcnow().isoformat()

        # Mark as modified for SQLAlchemy
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(agent, "meta")

        # Audit log
        audit = EventsAudit(
            actor=current_api_key.get("label"),
            action="agent.force_check",
            details={"agent_id": agent_id},
            at=datetime.utcnow(),
        )
        if audit is not None:  # no audit store in this build → EventsAudit() is None
            db.add(audit)

        await db.commit()

        logger.info(f"Force config update set for agent: {agent_id} by {current_api_key.get('label')}")

        return {
            "status": "success",
            "message": "Force config update requested. Agent will update on next poll cycle.",
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error forcing agent check: {e}")
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to force agent check",
        )


@router.patch(
    "/{agent_id}/weight",
    summary="Update Agent Weight",
    description="Update an agent's scheduling weight. Requires admin role.",
)
async def update_agent_weight(
    agent_id: str,
    weight: int = Query(..., ge=1, le=10, description="Agent weight for scheduling (1-10)"),
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """
    Update an agent's scheduling weight.

    Weight determines how many checks this agent should perform relative to others.
    Higher weight = more checks assigned. Only admins can update weights.
    """
    try:
        agent = await _load_admin_agent(db, agent_id, current_api_key, "update agent weights")

        # Store weight in agent metadata
        if not agent.meta:
            agent.meta = {}
        agent.meta["weight"] = weight

        # Mark as modified
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(agent, "meta")

        # Audit log
        audit = EventsAudit(
            actor=current_api_key.get("label"),
            action="agent.update_weight",
            details={"agent_id": agent_id, "weight": weight},
            at=datetime.utcnow(),
        )
        if audit is not None:  # no audit store in this build → EventsAudit() is None
            db.add(audit)

        await db.commit()

        logger.info(f"Agent weight updated: {agent_id} weight={weight} by {current_api_key.get('label')}")

        return {
            "status": "success",
            "message": f"Agent weight updated to {weight}",
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating agent weight: {e}")
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update agent weight",
        )


@router.patch(
    "/{agent_id}/captcha-trusted",
    summary="Set Agent Captcha Trust",
    description="Mark agent as captcha-trusted (residential IP, less likely to trigger captchas). Requires admin role.",
)
async def set_captcha_trusted(
    agent_id: str,
    trusted: bool = Query(..., description="Whether agent is captcha-trusted"),
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """
    Mark an agent as captcha-trusted.

    Captcha-trusted agents (typically on residential IPs) are less likely to trigger
    captchas. Workflows marked as captcha_blocked will only be distributed to these agents.
    """
    try:
        agent = await _load_admin_agent(db, agent_id, current_api_key, "update agent captcha trust")

        # Store captcha_trusted in agent metadata
        if not agent.meta:
            agent.meta = {}
        agent.meta["captcha_trusted"] = trusted

        # Mark as modified
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(agent, "meta")

        # Audit log
        audit = EventsAudit(
            actor=current_api_key.get("label"),
            action="agent.set_captcha_trusted",
            details={"agent_id": agent_id, "captcha_trusted": trusted},
            at=datetime.utcnow(),
        )
        if audit is not None:  # no audit store in this build → EventsAudit() is None
            db.add(audit)

        await db.commit()

        logger.info(f"Agent captcha trust updated: {agent_id} trusted={trusted} by {current_api_key.get('label')}")

        return {
            "status": "success",
            "captcha_trusted": trusted,
            "message": f"Agent captcha trust set to {trusted}",
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating agent captcha trust: {e}")
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update agent captcha trust",
        )


@router.patch(
    "/{agent_id}/trusted",
    summary="Set Agent Trusted Status",
    description="Mark agent as trusted for sensitive workflow execution. Requires admin role.",
)
async def set_agent_trusted(
    agent_id: str,
    trusted: bool = Query(..., description="Whether agent is trusted"),
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """
    Mark an agent as trusted.

    Trusted agents can execute workflows marked with trusted_agents_only=True.
    """
    try:
        agent = await _load_admin_agent(db, agent_id, current_api_key, "update agent trusted status")

        agent.is_trusted = trusted

        audit = EventsAudit(
            actor=current_api_key.get("label"),
            action="agent.set_trusted",
            details={"agent_id": agent_id, "is_trusted": trusted},
            at=datetime.utcnow(),
        )
        if audit is not None:  # no audit store in this build → EventsAudit() is None
            db.add(audit)

        await db.commit()

        logger.info(f"Agent trusted status updated: {agent_id} trusted={trusted} by {current_api_key.get('label')}")

        return {
            "status": "success",
            "is_trusted": trusted,
            "message": f"Agent trusted status set to {trusted}",
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating agent trusted status: {e}")
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update agent trusted status",
        )


def _verify_agent_request(
    agent,
    *,
    agent_id: str,
    timestamp: str,
    signature: str,
    payload: dict,
    max_age_seconds: int = 300,
) -> None:
    """Authenticate an agent-originated request via HMAC over `payload`.

    The agent proves possession of its secret by signing `payload` (which must
    include a fresh timestamp) with HMAC-SHA256. Raises HTTPException(403) on any
    failure. Mirrors the verification used by submit_report so the same agent
    secret authenticates config reads — without this, anyone could read another
    agent's target assignments by guessing an agent_id.
    """
    from security.encryption import SecretEncryption
    from security.hmac import HMACAuth

    if not signature or not timestamp:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Missing agent signature",
        )

    if not agent.encrypted_secret:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Agent needs to re-register to use HMAC authentication",
        )

    # Replay protection: reject stale or future-dated timestamps. The agent may
    # send either a numeric epoch (seconds or ms) OR an ISO 8601 string
    # (e.g. "2026-06-15T23:58:40.794564Z") — accept both. The HMAC is computed
    # over the timestamp STRING as-sent, so we only parse it here for the age check.
    try:
        ts_raw = (timestamp or "").strip()
        try:
            ts = float(ts_raw)
            if ts > 1e12:  # milliseconds → seconds
                ts /= 1000.0
        except (TypeError, ValueError):
            # ISO 8601 — normalize a trailing 'Z' to a UTC offset for fromisoformat.
            iso = ts_raw[:-1] + "+00:00" if ts_raw.endswith("Z") else ts_raw
            parsed = datetime.fromisoformat(iso)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            ts = parsed.timestamp()
        age = datetime.now(timezone.utc).timestamp() - ts
        if age > max_age_seconds or age < -60:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Stale or invalid request timestamp",
            )
    except HTTPException:
        raise
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid request timestamp",
        )

    try:
        agent_secret = SecretEncryption.decrypt_secret(agent.encrypted_secret)
    except ValueError as e:
        logger.error(f"Secret decryption failed for agent {agent_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Secret decryption failed",
        )

    # Constant-time HMAC match AND timestamp freshness in one shot (the explicit
    # age check above is kept for the clearer error messages / future-skew clamp;
    # this adds the same freshness inside the verify path as the report endpoints).
    if not verify_fresh_signed_payload(
        payload, agent_secret, signature, max_age_seconds=max_age_seconds
    ):
        logger.warning(f"Invalid/stale HMAC signature from agent {agent_id} (config)")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid signature - authentication failed",
        )


@router.get(
    "/config",
    summary="Get Agent Config",
    description="Get updated configuration for an agent.",
)
async def get_agent_config(
    agent_id: str,
    timestamp: str,
    signature: str,
    db: AsyncSession = Depends(get_db),
):
    """
    Get configuration updates for an agent.

    Returns only targets assigned to this specific agent for coordinated distribution.

    Returns:
    - config_updated: Whether config has changed (based on force_config_update flag)
    - targets: List of targets assigned to this agent
    - global_period_ms: Check period for this agent
    - schedule: Schedule information
    """
    try:
        # Verify agent exists and update last_seen
        result = await db.execute(
            select(Agent).where(Agent.agent_id == agent_id)
        )
        agent = result.scalar_one_or_none()

        if not agent:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Agent not found",
            )

        # Authenticate the caller as this agent before disclosing target config
        # or mutating agent state (status/distribution).
        _verify_agent_request(
            agent,
            agent_id=agent_id,
            timestamp=timestamp,
            signature=signature,
            payload={"agent_id": agent_id, "timestamp": timestamp},
        )

        # Reactivate agent if it was INACTIVE
        # If an agent is fetching config, it's clearly running and should be ACTIVE
        was_inactive = False
        if agent.status == AgentStatus.INACTIVE:
            logger.info(f"Agent {agent_id} was INACTIVE, reactivating...")
            agent.status = AgentStatus.ACTIVE
            was_inactive = True

        # Check if config was force updated
        config_updated = agent.force_config_update or was_inactive

        # Clear force_config_update flag after reading
        if agent.force_config_update:
            agent.force_config_update = False

        # Update last_seen
        agent.last_seen_at = datetime.utcnow()

        await db.commit()

        # If agent was just reactivated, trigger redistribution
        if was_inactive:
            logger.info(f"Agent {agent_id} reactivated, triggering redistribution...")

            # Get global period for capacity-aware redistribution
            from models.config import Config
            from config import settings as app_settings
            config_result = await db.execute(
                select(Config).where(Config.key == "global_period_ms")
            )
            config = config_result.scalar_one_or_none()
            global_period_ms_for_dist = int(config.value) if config else app_settings.global_period_ms

            # Use capacity-aware distributor (handles both timeslots and targets)
            from services.capacity_aware_distributor import CapacityAwareDistributor
            distributor = CapacityAwareDistributor(db)
            distribution_stats = await distributor.distribute_timeslots_and_targets(global_period_ms_for_dist)

            logger.info(
                f"Capacity-aware redistribution triggered by reactivation: "
                f"{distribution_stats['agents']} agents, "
                f"{distribution_stats['targets_assigned']} targets assigned"
            )

            # Refresh agent to get updated metadata after redistribution
            await db.refresh(agent)

        # Get targets assigned to this agent using distributor
        from services.distributor import TargetDistributor
        distributor = TargetDistributor(db)
        assigned_targets = await distributor.get_agent_assignments(agent_id)

        # Coalesce same-fetch_key targets into one physical fetch, unioning the
        # selectors of every member so a single page load serves them all
        # (reports fan out per selector_id back to each target).
        from services.monitor_coalescing import coalesce_assigned_targets
        by_id = {t.id: t for t in assigned_targets}
        target_set = []
        for rep, member_ids in coalesce_assigned_targets(assigned_targets):
            # Union enabled selectors across all coalesced members.
            enabled_selectors = []
            for mid in member_ids:
                mt = by_id.get(mid)
                if mt and mt.selectors:
                    enabled_selectors.extend([s for s in mt.selectors if s.enabled])

            # Build selectors array from target_selectors table only
            selectors_list = [
                {
                    "id": s.id,
                    "name": s.name,
                    "selector": s.selector,
                    "content_type": s.content_type or 'text',
                    "visual_region": s.visual_region,
                    "ignore_regex": s.ignore_regex,
                    "baseline_hash": s.baseline_hash,
                    "baseline_content": s.baseline_content,
                    "priority": s.priority,
                }
                for s in sorted(enabled_selectors, key=lambda x: (-x.priority, x.id))
            ]

            target_data = {
                "id": str(rep.id),
                "url": rep.url,
                "check_type": rep.check_type or "content",
                "check_period_ms": rep.check_period_ms,
                # Uptime monitoring fields
                "expected_status_code": rep.expected_status_code,
                "timeout_ms": rep.timeout_ms,
                "max_response_time_ms": rep.max_response_time_ms,
                "check_ssl": rep.check_ssl,
                # All content targets must have selectors
                "selectors": selectors_list,
            }
            # Inline setup-steps manifest (recorded expand/scroll/click steps) —
            # replayed before extraction so an interaction-gated visual zone is
            # captured in the right state.
            _rep_setup = getattr(rep, "setup_steps", None)
            if _rep_setup and str(_rep_setup).strip() not in ("", "null"):
                try:
                    import json as _json
                    target_data["setup_steps"] = _json.loads(_rep_setup)
                except (ValueError, TypeError):
                    logger.warning("agent assignment: ignoring malformed setup_steps for target %s", rep.id)
            target_set.append(target_data)

        logger.info(f"Config request for agent {agent_id}: returning {len(target_set)} assigned targets (force_update={config_updated})")

        # Get config values - use database config if set, otherwise use settings.
        # Fetch all scheduling Config keys in ONE query, resolved from a dict.
        from models.config import Config
        from config import settings as app_settings

        cfg = {
            c.key: c
            for c in (await db.execute(
                select(Config).where(
                    Config.key.in_(["global_period_ms", "time_slot_mode", "sync_epoch_ms"])
                )
            )).scalars().all()
        }

        config = cfg.get("global_period_ms")
        global_period_ms = int(config.value) if config else app_settings.global_period_ms

        # Schedule info (can be enhanced with actual scheduler data)
        from config import settings
        schedule_info = {
            "period_ms": global_period_ms,
            "next_run_at": (datetime.utcnow() + timedelta(
                milliseconds=global_period_ms
            )).isoformat(),
        }

        # Calculate effective period based on time slot mode
        mode_config = cfg.get("time_slot_mode")
        time_slot_mode = mode_config.value if mode_config else "distributed"

        # Count active agents for period calculation
        active_agent_count = (await db.execute(
            select(func.count(Agent.id)).where(Agent.status == AgentStatus.ACTIVE)
        )).scalar() or 0

        # Calculate effective period
        # In rolling mode: effective_period = period × num_agents (load distribution)
        # In distributed mode: effective_period = period (redundancy, all check at same rate)
        if time_slot_mode == "rolling":
            effective_period_ms = global_period_ms * active_agent_count
        else:
            effective_period_ms = global_period_ms

        # Get sync_epoch_ms for absolute time synchronization
        import time
        sync_epoch_config = cfg.get("sync_epoch_ms")
        # Handle both old string format and new int format from JSON column
        if sync_epoch_config:
            sync_epoch_ms = int(sync_epoch_config.value) if isinstance(sync_epoch_config.value, str) else sync_epoch_config.value
        else:
            sync_epoch_ms = int(time.time() * 1000)

        # Get assigned time slot offsets from agent metadata
        # Check for period-specific offsets first, fallback to legacy single offset
        assigned_offsets_by_period = agent.meta.get('assigned_time_slot_offsets_by_period') if agent.meta else None

        if assigned_offsets_by_period:
            # Period-specific offsets (rolling mode)
            # Build complete scheduling info: {period: {"offset": ms, "effective_period": ms}}
            time_slot_offsets = {}
            for period_str, offsets_data in assigned_offsets_by_period.items():
                period_ms = int(period_str)

                # Handle both new dict format and legacy list format
                if isinstance(offsets_data, dict):
                    # New format: already has offset_ms and effective_period_ms
                    time_slot_offsets[period_ms] = {
                        "offset_ms": offsets_data.get("offset_ms", 0),
                        "effective_period_ms": offsets_data.get("effective_period_ms", period_ms)
                    }
                else:
                    # Legacy list format: [offset1, offset2, ...]
                    offset_ms = offsets_data[0] if offsets_data else 0
                    # In rolling mode: effective_period = period × num_agents
                    effective_period_for_this_period = period_ms * active_agent_count if time_slot_mode == "rolling" else period_ms
                    time_slot_offsets[period_ms] = {
                        "offset_ms": offset_ms,
                        "effective_period_ms": effective_period_for_this_period
                    }
        else:
            # Legacy mode: single set of offsets for all periods
            time_slot_offsets = agent.meta.get('assigned_time_slot_offsets', [agent.time_slot_offset_ms]) if agent.meta else [agent.time_slot_offset_ms]

        # Get scheduled workflows for Playwright-capable agents
        scheduled_workflows = None
        if agent.has_playwright and agent.meta:
            scheduled_workflows = agent.meta.get('assigned_scheduled_workflows', [])
            if scheduled_workflows:
                logger.info(f"Sending {len(scheduled_workflows)} scheduled workflows to agent {agent_id} via config endpoint")

        return {
            "config_updated": config_updated,  # True if force_config_update was set
            "targets": target_set,
            "global_period_ms": global_period_ms,
            "effective_period_ms": effective_period_ms,
            "schedule": schedule_info,
            "time_slot_offset_ms": agent.time_slot_offset_ms,
            "time_slot_offsets": time_slot_offsets,  # Period-specific {period: {offset_ms, effective_period_ms}} or legacy [offsets]
            "sync_epoch_ms": sync_epoch_ms,
            "time_slot_mode": time_slot_mode,
            "active_agent_count": active_agent_count,
            "sync_enabled": agent.sync_enabled,
            "scheduled_workflows": scheduled_workflows,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting agent config: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get agent configuration",
        )


@router.get(
    "/{agent_id}/errors",
    summary="Get Agent Errors",
    description="Recent errors for one agent (checks, workflow runs, streaming sessions).",
)
async def get_agent_errors(
    agent_id: str,
    days: int = Query(default=7, ge=1, le=30),
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """
    Per-agent error feed.

    Visibility follows the error policy:
    - User-hosted (BYO) agents: the owner sees full detail — the agent runs on
      their machine against their own targets.
    - Cloud/infrastructure agents: admins only; others get 404 so internal fleet
      errors never leak.
    """
    # user_hosted is deferred() on the Agent model, so a plain select() won't load
    # it — the first attribute access would emit a sync lazy-load (MissingGreenlet)
    # in this async context. Undefer it up front.
    from sqlalchemy.orm import undefer
    result = await db.execute(
        select(Agent)
        .where(Agent.agent_id == agent_id)
        .options(undefer(Agent.user_hosted))
    )
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    # Snapshot the scalar attributes we need NOW. collect_agent_errors below runs
    # further queries that can expire this loaded `agent`; a later attribute
    # access would then trigger a sync lazy-load (MissingGreenlet) in the async
    # context. Reading them up front avoids that refresh entirely.
    agent_user_hosted = bool(agent.user_hosted)

    is_admin = bool((current_api_key or {}).get("is_platform_admin"))
    if not is_admin and not agent_user_hosted:
        # Cloud/infrastructure agent errors are admin-only.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    from services.agent_errors import collect_agent_errors
    since = datetime.utcnow() - timedelta(days=days)
    errors, _ = await collect_agent_errors(
        db, since=since, agent_id=agent_id, limit=limit,
        redact=not is_admin,
    )

    return {
        "agent_id": agent_id,
        "user_hosted": agent_user_hosted,
        "days": days,
        "count": len(errors),
        "errors": errors,
    }


@router.get(
    "",
    response_model=list[AgentInfo],
    summary="List Agents",
    description="List all registered agents.",
)
async def list_agents(
    status_filter: Optional[AgentStatus] = None,
    platform_filter: Optional[Platform] = None,
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """
    List all registered agents.

    Can filter by status and platform.
    """
    try:
        from sqlalchemy import func
        from models.target_assignment import TargetAssignment
        from datetime import timezone

        query = select(Agent)

        if status_filter:
            query = query.where(Agent.status == status_filter)

        if platform_filter:
            query = query.where(Agent.platform == platform_filter)

        result = await db.execute(query.order_by(Agent.created_at.desc()))
        agents = result.scalars().all()

        # Platform admins (ops console) may see internal infra addresses in meta;
        # ordinary callers must not. Gate on the trustworthy is_platform_admin flag
        # — NOT on role=="admin", which is also true for admin API keys (that would
        # leak recorder URLs / IPs).
        is_admin = bool((current_api_key or {}).get("is_platform_admin"))

        # Determine connection status (seen in last 5 minutes)
        connection_threshold = datetime.now(timezone.utc) - timedelta(minutes=5)

        # Assignment counts key on the string Agent.agent_id.
        #
        # There is no per-agent check_count / last_report_at ledger to aggregate,
        # so check_count defaults to 0 and last_report_at falls back to the
        # agent's own last_seen_at below.
        agent_str_ids = [a.agent_id for a in agents]

        report_stats: Dict[int, tuple] = {}

        assigned_counts: Dict[str, int] = {}
        if agent_str_ids:
            rows = await db.execute(
                select(TargetAssignment.agent_id, func.count(TargetAssignment.id))
                .where(TargetAssignment.agent_id.in_(agent_str_ids))
                .group_by(TargetAssignment.agent_id)
            )
            for aid, cnt in rows.all():
                assigned_counts[aid] = cnt or 0

        agent_infos = []
        for agent in agents:
            # No reports ledger — default check_count to 0 and use the agent's
            # last_seen_at as the best-available "last report" signal.
            check_count, last_report_at = report_stats.get(
                agent.id, (0, agent.last_seen_at)
            )
            assigned_targets = assigned_counts.get(agent.agent_id, 0)

            # Calculate uptime (time since created)
            uptime_hours = None
            if agent.created_at:
                try:
                    created_aware = agent.created_at if agent.created_at.tzinfo else agent.created_at.replace(tzinfo=timezone.utc)
                    uptime_delta = datetime.now(timezone.utc) - created_aware
                    uptime_hours = uptime_delta.total_seconds() / 3600
                except Exception:
                    pass

            # Average latency is not tracked (no stored request/response
            # timestamps), so leave it unset.
            avg_latency_ms = None

            from services.agent_speed import profile_from_meta as _prof_from_meta
            _prof = _prof_from_meta(agent.meta)
            agent_infos.append(AgentInfo(
                id=agent.id,
                agent_id=agent.agent_id,
                platform=agent.platform.value,
                status=agent.status.value,
                connected=(
                    agent.last_seen_at is not None and
                    agent.last_seen_at.replace(tzinfo=timezone.utc if not agent.last_seen_at.tzinfo else agent.last_seen_at.tzinfo) >= connection_threshold and
                    agent.status == AgentStatus.ACTIVE
                ),
                last_seen_at=agent.last_seen_at.isoformat() if agent.last_seen_at else None,
                created_at=agent.created_at.isoformat(),
                meta=(agent.meta if is_admin else _strip_infra_meta(agent.meta)),
                check_count=check_count,
                assigned_targets=assigned_targets,
                avg_latency_ms=avg_latency_ms,
                last_report_at=last_report_at.isoformat() if last_report_at else None,
                uptime_hours=round(uptime_hours, 2) if uptime_hours else None,
                time_slot_offset_ms=agent.time_slot_offset_ms,
                sync_enabled=agent.sync_enabled,
                speed_class=_prof["speed_class"],
                perf_score=_prof["perf_score"],
                max_sessions=(agent.meta or {}).get("max_sessions", 0) or 0,
                active_sessions=(agent.meta or {}).get("active_sessions", 0) or 0,
                cpu_cores=_prof["cpu_cores"],
                cpu_threads=_prof["cpu_threads"],
                cpu_clock_mhz=_prof["cpu_clock_mhz"],
                ram_mb=_prof["ram_mb"],
                # Derive role from is_trusted (a regular column). Avoid the
                # DEFERRED user_hosted column — touching it here triggers an async
                # lazy-load (MissingGreenlet → 500). Redis enrichment below
                # overrides with the live registry role when present.
                role=("infrastructure" if getattr(agent, "is_trusted", False) else "user-hosted"),
                # Isolation pool from DB meta (recorder_proxy stamps it; only an
                # infrastructure agent is ever 'isolated'). Redis enrichment below
                # overrides with the live registry tier when the agent is WS-connected.
                tier=((agent.meta or {}).get("tier") or None),
                # Geo region detected via IP geolocation at registration.
                region=getattr(agent, "region", None),
                # Capabilities — what this agent can actually run (single source of
                # truth = Agent model properties over meta).
                check_modes=list(agent.check_modes or []),
                has_playwright=bool(agent.has_playwright),
                captcha_trusted=bool(agent.captcha_trusted),
                is_trusted=bool(getattr(agent, "is_trusted", False)),
                targets_per_slot=int((((agent.meta or {}).get("capacity") or {}).get("targets_per_timeslot")) or 0),
                parallel_workers=int((((agent.meta or {}).get("capacity") or {}).get("parallel_workers")) or 0),
            ))

        # Enrich with live status from the in-process directly-connected fleet.
        # Agents now hold a persistent socket on THIS coordinator (/ws/ai-gateway) —
        # there is no external ws-gateway Redis registry to sync from. The live
        # source of truth for "connected right now" + current load/role is
        # user_recorder_ws (_connections/_agent_meta). Fields the old registry blob
        # carried but the direct socket doesn't advertise (speed_class / perf_score /
        # tier / region / recorder_url / gateway_instance) stay at their DB-derived
        # values populated above.
        try:
            from routers.user_recorder_ws import get_connected_recorders
            live_lookup = {r["agent_id"]: r for r in get_connected_recorders()}

            for info in agent_infos:
                live = live_lookup.get(info.agent_id)
                if live:
                    info.connected = True
                    info.active_sessions = live.get("active_sessions", info.active_sessions) or 0
                    info.max_sessions = live.get("max_sessions", info.max_sessions) or 0
                    if live.get("role"):
                        info.role = live.get("role")
                    # Caller-visible meta carries only load/health — never infra
                    # routing addresses.
                    info.meta = {
                        **(info.meta or {}),
                        "active_sessions": live.get("active_sessions", 0),
                        "max_sessions": live.get("max_sessions", 5),
                        "speed_class": info.speed_class,
                        "perf_score": info.perf_score,
                        "live": True,
                    }
        except Exception as e:
            logger.debug(f"Live agent enrichment skipped: {e}")

        return agent_infos

    except Exception as e:
        logger.error(f"Error listing agents: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to list agents",
        )


# NOTE: GET /recorder-package was removed. It streamed a tarball assembled from
# a sibling source directory that is not part of this project, so the endpoint
# could only ever return 404 here. Browser work runs on the separately-installed
# `writ-agent` fleet; see docs/CONNECT_AGENT.md.


@router.get("/agent-package")
async def download_agent_package(
    current_api_key: dict = Depends(get_current_api_key),
):
    """
    Serve the pip-installable writ-agent package as a tarball.

    Used by autoscale cloud-init to install the CURRENT product agent
    (writ_agent + pyproject.toml → the `writ-agent` CLI), which then
    self-links to the gateway using its baked-in WRIT_SERVICE_TOKEN. Protected by
    API-key auth (the provisioner passes the platform key in cloud-init).
    """
    import io
    import tarfile
    from fastapi.responses import StreamingResponse

    agent_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "writ-agent"
    )
    agent_dir = os.path.abspath(agent_dir)
    if not os.path.isdir(agent_dir):
        raise HTTPException(status_code=404, detail="Agent package not found on server")

    # Skip caches/build artifacts/local state so the tarball is clean + small.
    _SKIP_DIRS = {"__pycache__", ".egg-info", "writ_agent.egg-info", ".pytest_cache",
                  "build", "dist", ".venv", "venv", ".git"}
    _SKIP_SUFFIX = (".pyc", ".log")

    def _filter(ti: tarfile.TarInfo):
        parts = ti.name.split("/")
        if any(p in _SKIP_DIRS or p.endswith(".egg-info") for p in parts):
            return None
        if ti.name.endswith(_SKIP_SUFFIX):
            return None
        return ti

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        # arcname "writ-agent" so cloud-init can `pip install ./writ-agent`.
        tar.add(agent_dir, arcname="writ-agent", filter=_filter)
    buffer.seek(0)

    return StreamingResponse(
        buffer,
        media_type="application/gzip",
        headers={"Content-Disposition": "attachment; filename=agent-package.tar.gz"},
    )
