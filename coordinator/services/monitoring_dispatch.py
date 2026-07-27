"""Push monitoring assignments to recorder agents over the ws-gateway.

The desktop-agent learns its targets by polling `GET agents/config`; recorders
instead receive an `assign_targets` frame pushed over the gateway. This module
reuses the existing capacity-aware distributor + assignment lookup and turns the
result into the gateway frame defined in `MONITORING_GATEWAY_CONTRACT.md`.
"""
import asyncio
import logging
import time
from typing import Any, Dict, Optional

from sqlalchemy import select

logger = logging.getLogger(__name__)

# Debounce the FULL capacity-aware redistribution triggered by single-agent
# (re)connect / precheck events. distribute_timeslots_and_targets DELETEs and
# bulk re-INSERTs all of target_assignments and republishes to the whole fleet;
# a fleet of recorders reconnecting in a burst would otherwise each trigger that
# whole-fleet recompute back-to-back. Since the recompute is deterministic from
# current DB state, one run within this window already produced the assignments
# the later events would recompute identically — so we skip the redundant
# recompute but STILL push each agent its current (freshly-read) assignment.
# Explicit target-edit callers (routers) call distribute_timeslots_and_targets
# directly and are NOT debounced — they must redistribute immediately.
DISTRIBUTION_DEBOUNCE_S = 3.0
_last_distribution_at: float = 0.0
_distribution_debounce_lock = asyncio.Lock()


async def _config_int(db, key: str, default: int) -> int:
    from models.config import Config
    row = (await db.execute(select(Config).where(Config.key == key))).scalar_one_or_none()
    try:
        return int(row.value) if row else default
    except (TypeError, ValueError):
        return default


async def build_assign_targets_payload(db, agent) -> Dict[str, Any]:
    """Build the `assign_targets` frame for one agent from its assignments +
    the per-period offsets the distributor stored in `agent.meta`."""
    from services.distributor import TargetDistributor
    from config import settings

    global_period_ms = await _config_int(db, "global_period_ms", settings.global_period_ms)
    sync_epoch_ms = await _config_int(db, "sync_epoch_ms", int(time.time() * 1000))

    distributor = TargetDistributor(db)
    assigned = await distributor.get_agent_assignments(agent.agent_id)

    # Per-target agent coverage → per-target effective period. A target's fleet
    # cadence is its interval only if `K` agents stagger their checks; a single
    # agent must therefore check it every `period × K` (floored at 60s). Keying
    # this per-TARGET (not per-period) is essential: JS/Playwright targets land
    # on fewer agents than HTTP/uptime ones that share the same period, so a
    # per-period multiplier checks them at the wrong cadence.
    from sqlalchemy import func as _func
    from models.target_assignment import TargetAssignment as _TA
    _ids = [t.id for t in assigned]
    coverage: Dict[int, int] = {}
    if _ids:
        rows = (await db.execute(
            select(_TA.target_id, _func.count(_func.distinct(_TA.agent_id)))
            .where(_TA.target_id.in_(_ids)).group_by(_TA.target_id)
        )).all()
        coverage = {tid: int(c or 1) for tid, c in rows}
    floor_ms = 60000

    targets_list = []
    for t in assigned:
        _period = t.check_period_ms or global_period_ms
        _eff = max(_period * coverage.get(t.id, 1), floor_ms)
        enabled_selectors = [s for s in (t.selectors or []) if getattr(s, "enabled", True)]
        auth_session = None
        if getattr(t, "auth_session_encrypted", None):
            try:
                from routers.automation import decrypt_credentials
                auth_session = decrypt_credentials(t.auth_session_encrypted)
            except Exception:
                auth_session = None

        # Inline setup-steps manifest ({steps, credentials}): the recorder agent
        # replays these in the browser BEFORE the content read. It maps to the
        # agent's CloudTarget.pre_check_workflow field (monitor::models::Target),
        # which also forces the browser path via needs_browser(). Stored as a JSON
        # string; parse it to a dict so it serializes cleanly over the gateway.
        # Absent/blank/unparseable → a plain check (no setup), matching the daemon's
        # build_cloud_target tolerance.
        pre_check_workflow = None
        _setup_raw = getattr(t, "setup_steps", None)
        if _setup_raw and str(_setup_raw).strip() not in ("", "null"):
            try:
                import json as _json
                pre_check_workflow = _json.loads(_setup_raw)
            except (ValueError, TypeError):
                logger.warning(
                    f"target {t.id}: setup_steps is not valid JSON; dispatching without pre_check_workflow"
                )
                pre_check_workflow = None
        targets_list.append({
            "id": t.id,
            "url": t.url,
            "check_type": t.check_type or "content",
            "check_period_ms": t.check_period_ms,
            # Cadence THIS agent should run the target at (period × covering
            # agents, 60s floor). Overrides the per-period effective_period so
            # JS targets covered by fewer agents stay on interval.
            "effective_period_ms": _eff,
            "expected_status_code": t.expected_status_code,
            "timeout_ms": t.timeout_ms,
            "check_ssl": t.check_ssl,
            "requires_playwright": bool(getattr(t, "requires_playwright", False)),
            # Inline setup-steps manifest → CloudTarget.pre_check_workflow on the
            # agent (replayed in-browser before the check). Parsed from the target's
            # setup_steps JSON above; None when absent/blank/unparseable.
            "pre_check_workflow": pre_check_workflow,
            # Carry the raw manifest too so an agent that prefers an explicit
            # setup_steps key (mirroring the local daemon's column) can read it.
            "setup_steps": pre_check_workflow,
            "auth_session": auth_session,
            "selectors": [
                {
                    "id": s.id,
                    "name": s.name,
                    "selector": s.selector,
                    "content_type": getattr(s, "content_type", "text") or "text",
                    # Visual (screenshot-region) checks need the region so the agent
                    # knows what to clip + hash; without it visual checks can't run.
                    "visual_region": getattr(s, "visual_region", None),
                    "ignore_regex": s.ignore_regex,
                    "baseline_hash": s.baseline_hash,
                    "baseline_content": s.baseline_content,
                }
                for s in sorted(enabled_selectors, key=lambda x: (-(x.priority or 0), x.id))
            ],
        })

    meta = agent.meta or {}
    time_slot_offsets = meta.get("assigned_time_slot_offsets_by_period", {})

    return {
        "type": "assign_targets",
        "global_period_ms": global_period_ms,
        "sync_epoch_ms": sync_epoch_ms,
        "sync_enabled": True,
        "time_slot_offsets": time_slot_offsets,
        "targets": targets_list,
    }


async def push_assign_targets(db, agent) -> bool:
    """Build + dispatch the agent's assign_targets frame (fire-and-forget).
    Clears the agent's force_config_update flag once pushed.

    Pushes straight to the agent's DIRECT socket (it connected to /ws/ai-gateway on
    this process). Returns False without clearing force_config_update when the agent
    is not connected, so the flag survives to re-push on its next connect."""
    from routers.user_recorder_ws import push_fire_and_forget
    from sqlalchemy.orm.attributes import flag_modified

    payload = await build_assign_targets_payload(db, agent)
    try:
        sent = await push_fire_and_forget(agent.agent_id, payload)
    except Exception as e:
        logger.warning(f"assign_targets dispatch failed for {agent.agent_id}: {e}")
        return False
    if not sent:
        logger.debug(f"assign_targets not pushed: {agent.agent_id} is not connected")
        return False

    if getattr(agent, "force_config_update", False):
        agent.force_config_update = False
        flag_modified(agent, "force_config_update")
        await db.commit()
    logger.info(f"Pushed assign_targets to {agent.agent_id}: {len(payload['targets'])} targets")
    return True


async def distribute_and_push_to_agent(agent_id: str, force: bool = False) -> None:
    """(Re)run the capacity-aware distribution, then push the given agent's
    assignment over the gateway. Best-effort; safe to call on connect / change.

    The full redistribution is debounced (see DISTRIBUTION_DEBOUNCE_S): a recent
    redistribution is reused instead of recomputed back-to-back during a reconnect
    storm. The agent's assignment is ALWAYS pushed regardless, reading whatever the
    most recent distribution wrote.

    ``force=True`` bypasses the debounce — pass it when the caller just persisted
    NEW state the redistribution must observe (e.g. a precheck auth session that
    flips a target out of precheck-only single-agent mode); a pure reconnect has
    no new state and is safe to debounce."""
    from database import AsyncSessionLocal
    from models.agent import Agent
    from services.capacity_aware_distributor import CapacityAwareDistributor

    global _last_distribution_at

    async with AsyncSessionLocal() as db:
        global_period_ms = await _config_int(db, "global_period_ms", 60000)

        # Skip the whole-fleet recompute if one just ran (a deterministic recompute
        # within the window produces the same assignments). Still fall through to
        # push this agent its current assignment below. force=True always recomputes.
        async with _distribution_debounce_lock:
            now = time.monotonic()
            should_redistribute = force or (now - _last_distribution_at) >= DISTRIBUTION_DEBOUNCE_S
            if should_redistribute:
                # Reserve the window now so concurrent callers in the same burst
                # don't all redistribute (the lock serializes this decision; the
                # distribution itself runs outside the debounce lock so it doesn't
                # block the decision for the whole distribution duration).
                _last_distribution_at = now

        if should_redistribute:
            try:
                await CapacityAwareDistributor(db).distribute_timeslots_and_targets(global_period_ms)
            except Exception as e:
                logger.warning(f"distribution failed before push to {agent_id}: {e}")
        else:
            logger.debug(
                f"distribute_and_push_to_agent({agent_id}): reusing recent "
                f"distribution (debounced), pushing current assignment only"
            )

        agent = (await db.execute(select(Agent).where(Agent.agent_id == agent_id))).scalar_one_or_none()
        if not agent:
            return
        # When we just redistributed, distribute_timeslots_and_targets already
        # pushed assign_targets to every gateway-connected recorder (including this
        # one), so a trailing push here would be a redundant duplicate frame. Only
        # push when we reused a debounced distribution (no fresh push happened).
        if not should_redistribute:
            await push_assign_targets(db, agent)


async def push_to_agent_id(agent_id: str) -> None:
    """Push current assignments to one agent WITHOUT re-running distribution
    (used after a change already redistributed). Best-effort."""
    from database import AsyncSessionLocal
    from models.agent import Agent

    async with AsyncSessionLocal() as db:
        agent = (await db.execute(select(Agent).where(Agent.agent_id == agent_id))).scalar_one_or_none()
        if agent:
            await push_assign_targets(db, agent)
