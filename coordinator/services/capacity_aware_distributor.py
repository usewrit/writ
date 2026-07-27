"""
Capacity-Aware Target Distribution
Distributes targets to ALL agents based on their reported hardware capacity.
Ensures maximum dilution of checks across agents for anti-detection.
"""

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import List, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete
from sqlalchemy.orm import undefer
from models.agent import Agent, AgentStatus
from models.target import Target
from models.target_assignment import TargetAssignment
from models.config import Config
import time

logger = logging.getLogger(__name__)

# Serialize distribution across the whole process. distribute_timeslots_and_targets
# does DELETE-all-then-bulk-INSERT of target_assignments; two concurrent runs (e.g.
# a recorder spamming monitor_register on reconnect + a reactivation re-push) race
# on the (agent_id, target_id) unique constraint → IntegrityError → the whole
# redistribution rolls back and the agent gets ZERO targets. One at a time.
_distribution_lock = asyncio.Lock()

# No single agent may check a target faster than this. A fast target's cadence is
# achieved by a FLEET of staggered agents (≈ ceil(60000 / interval) of them), not
# one server hammering. Used to size min-agents AND to floor each agent's
# effective check period. Mirrored agent-side as MIN_CHECK_INTERVAL_MS.
ANTI_DETECTION_THRESHOLD_MS = 60000  # 60 seconds (rate-limit / detection protection)

# A recorder runs BOTH target checks AND browser SESSIONS (workflow runs,
# recordings, streaming) on the same hardware. The two schedulers are independent
# (this distributor sizes checks; RecorderCapacityManager sizes sessions), so
# without cross-accounting an agent maxed on sessions could still be handed its
# full check load → overload. We therefore shrink each recorder's EFFECTIVE check
# capacity by its live session utilization AND always hold back this fraction of
# its hardware for sessions (covers the lag between redistribution cycles, since
# sessions are bursty). Check-only agents (serverless/virtual, no sessions) keep
# full capacity. 0..0.9; tune via env.
SESSION_HEADROOM_RESERVE = max(0.0, min(0.9, float(os.getenv("CHECK_SESSION_HEADROOM_RESERVE", "0.25"))))


class CapacityAwareDistributor:
    """
    Distributes targets to agents proportionally based on capacity.

    Key principles:
    1. Distribute to ALL active agents (maximum dilution)
    2. Proportional assignment based on reported capacity
    3. No agent gets duplicate targets across their timeslots
    4. Verify no target is assigned to multiple agents
    """

    def __init__(self, db: AsyncSession):
        self.db = db

    async def distribute_timeslots_and_targets(
        self,
        global_period_ms: int
    ) -> Dict[str, Any]:
        """Serialized entrypoint — only one redistribution runs at a time across
        the process (prevents concurrent DELETE+INSERT races on
        target_assignments). See `_distribution_lock`."""
        async with _distribution_lock:
            return await self._distribute_impl(global_period_ms)

    async def _distribute_impl(
        self,
        global_period_ms: int
    ) -> Dict[str, Any]:
        """
        Distribute timeslots AND targets in one operation.

        Algorithm:
        1. Get all active agents with their reported capacity
        2. Calculate total capacity across all agents
        3. Assign timeslots to each agent based on their capacity
        4. Distribute targets proportionally across ALL agents

        Args:
            global_period_ms: Global check period

        Returns:
            Distribution statistics
        """
        # Get all active agents (undefer user_hosted for the local/shared split).
        result = await self.db.execute(
            select(Agent)
            .options(undefer(Agent.user_hosted))
            .where(Agent.status == AgentStatus.ACTIVE)
        )
        agents = list(result.scalars().all())

        # Distribute ONLY to LIVE agents. An ACTIVE DB row can be stale — the agent
        # disconnected (e.g. process died / coordinator bounced) but its row lingers.
        # Assigning checks to it is dead coverage (its checks never run) and it
        # crowds real agents out of the per-target redundancy budget. Liveness =
        # an open WS socket (direct fleet) OR a recent heartbeat (HTTP-pool
        # recorders), mirroring RecorderCapacityManager.get_total_capacity.
        try:
            from routers.user_recorder_ws import get_connected_recorders
            _connected = {r.get("agent_id") for r in get_connected_recorders()}
        except Exception:
            _connected = set()
        _now = datetime.now(timezone.utc)

        def _is_live(a) -> bool:
            if a.agent_id in _connected:
                return True
            ls = a.last_seen_at
            if ls is None:
                return False
            if ls.tzinfo is None:
                ls = ls.replace(tzinfo=timezone.utc)
            return (_now - ls).total_seconds() <= 120

        agents = [a for a in agents if _is_live(a)]

        if not agents:
            logger.warning("No live agents to distribute to")
            return {'agents': 0, 'total_slots': 0, 'targets_assigned': 0}

        # Parse agent capacities
        agent_infos = []
        total_capacity = 0
        total_slots_requested = 0

        for agent in agents:
            meta = agent.meta or {}
            # Cross-account browser-session load so checks never overload a recorder.
            # max_sessions>0 ⇒ this agent runs sessions (a recorder); 0/absent ⇒
            # check-only (serverless/virtual) → no reduction.
            max_sessions = meta.get('max_sessions') or meta.get('maxSessions') or 0
            active_sessions = meta.get('active_sessions') or meta.get('activeSessions') or 0
            if max_sessions and max_sessions > 0:
                session_util = max(0.0, min(1.0, active_sessions / max_sessions))
                # Hold back at least the static reserve; shrink further as sessions grow.
                capacity_factor = max(0.0, 1.0 - max(session_util, SESSION_HEADROOM_RESERVE))
            else:
                capacity_factor = 1.0  # check-only agent — full check capacity

            # Floor at 1 (never 0): a fully-loaded recorder still takes a token
            # check load, and 0 would divide-by-zero in the Phase 4 overflow math
            # and zero out the min-capacity slot sizing.
            capacity_per_slot = max(1, int(round(agent.capacity_per_timeslot * capacity_factor)))
            slots_requested = agent.timeslots_requested
            total_cap = max(capacity_per_slot, int(round(agent.total_capacity * capacity_factor)))
            avg_latency = agent.avg_check_latency_ms
            parallel_workers = meta.get('capacity', {}).get('parallel_workers', 10)

            if capacity_factor < 1.0:
                logger.info(
                    f"  {agent.agent_id}: session load {active_sessions}/{max_sessions} "
                    f"→ check capacity ×{capacity_factor:.2f} "
                    f"({agent.capacity_per_timeslot}→{capacity_per_slot}/slot)"
                )

            agent_infos.append({
                'agent': agent,
                'capacity_per_slot': capacity_per_slot,
                'slots_requested': slots_requested,
                'total_capacity': total_cap,
                'avg_latency_ms': avg_latency,
                'parallel_workers': parallel_workers,
                'benchmark_period_ms': global_period_ms  # The period used for capacity calculation
            })

            total_capacity += total_cap
            total_slots_requested += slots_requested

        logger.info(
            f"Distribution planning: {len(agents)} agents, "
            f"{total_slots_requested} total slots, "
            f"{total_capacity} total capacity"
        )

        # Get time slot mode from config
        mode_result = await self.db.execute(
            select(Config).where(Config.key == "time_slot_mode")
        )
        mode_config = mode_result.scalar_one_or_none()
        time_slot_mode = mode_config.value if mode_config else "distributed"

        # Get all unique periods from targets
        result = await self.db.execute(
            select(Target).where(Target.enabled == True)
        )
        all_targets = list(result.scalars().all())

        # Collect unique periods
        unique_periods = set()
        for target in all_targets:
            period = target.check_period_ms or global_period_ms
            unique_periods.add(period)

        unique_periods = sorted(list(unique_periods))

        num_agents = len(agent_infos)

        # Calculate maximum slots actually needed based on target count
        # Don't create more slots than necessary for the actual targets
        import math
        max_slots_needed = 1  # Default minimum
        if num_agents > 0 and len(all_targets) > 0:
            # Calculate based on smallest agent capacity (bottleneck)
            min_capacity_per_slot = min(info['capacity_per_slot'] for info in agent_infos)
            if min_capacity_per_slot > 0:
                # Max slots needed = ceil(total_targets / min_capacity_per_slot)
                max_slots_needed = math.ceil(len(all_targets) / min_capacity_per_slot)
                # Cap at 20 to prevent excessive slots
                max_slots_needed = min(20, max(1, max_slots_needed))

        logger.info(
            f"Slot calculation: {len(all_targets)} targets, "
            f"max {max_slots_needed} slots needed (distributed mode only)"
        )

        # Sort agents by agent_id for consistent assignment
        agent_infos.sort(key=lambda x: x['agent'].agent_id)

        # Initialize empty offset dictionaries for all agents
        # Offsets will be calculated per-target after assignment
        for info in agent_infos:
            agent = info['agent']
            if agent.meta is None:
                agent.meta = {}
            agent.meta['assigned_time_slot_offsets_by_period'] = {}
            # REMOVED: force_config_update = True
            # Let AgentStateManager detect changes naturally and send incremental updates
            # Only set force_config_update if offsets actually change (handled by offset comparison)
            # agent.force_config_update = True
            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(agent, 'meta')

        logger.info(f"Initialized {len(agent_infos)} agents, offsets will be calculated per-target")

        await self.db.commit()

        # Now distribute targets to all agents (redundancy mode)
        targets_stats = await self._distribute_targets_to_all_agents(agent_infos)

        # Distribute scheduled workflows to Playwright-capable agents
        workflow_stats = await self._distribute_scheduled_workflows(agent_infos)

        # Republish FULL snapshots for every agent this distribution touched so the
        # new targets/offsets/workflow assignments reach Redis (ps:agent_configs +
        # ps:agent_config_ver). Without this a pure redistribution that is not
        # followed by a change report leaves the published snapshots stale and
        # polling agents see config_updated=false forever (contract §6/§9.2-4).
        try:
            from routers.agents import publish_agent_snapshots
            affected_ids = [a.agent_id for a in agents]
            await publish_agent_snapshots(self.db, affected_ids)
        except Exception as e:  # pragma: no cover - best-effort republish
            logger.warning(f"Failed to republish snapshots after distribution: {e}")

        # Recorders do NOT poll — they receive an `assign_targets` frame over their
        # DIRECT socket. publish_agent_snapshots above only reaches POLL agents
        # (desktop/HTTP). Here we push the fresh assignment to any DIRECTLY-CONNECTED
        # recorder so a redistribution actually reaches them — previously they were
        # pushed ONLY on connect/ingest, so a target added after connect never
        # arrived. Best-effort + only for connected recorders (no live socket ⇒
        # skip, so poll agents don't get a wasted dispatch). Connectivity is read
        # from the in-process _connections registry (no external ws-gateway).
        try:
            from services.monitoring_dispatch import push_assign_targets
            from routers.user_recorder_ws import _connections
            for a in agents:
                if a.agent_id in _connections:  # holds a live socket → push the frame
                    await push_assign_targets(self.db, a)
        except Exception as e:  # pragma: no cover - best-effort
            logger.warning(f"Failed to push assign_targets to recorders after distribution: {e}")

        return {
            'agents': len(agents),
            'total_slots': total_slots_requested,
            'total_capacity': total_capacity,
            'mode': time_slot_mode,
            **targets_stats,
            **workflow_stats
        }

    async def _distribute_targets_to_all_agents(
        self,
        agent_infos: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        Distribute ALL targets to ALL active agents (redundancy mode).

        Each agent gets all targets up to their capacity limit.
        This provides redundancy and anti-detection through multiple check sources.

        Args:
            agent_infos: List of agent info dicts with capacity

        Returns:
            Distribution statistics
        """
        # Get all enabled targets
        result = await self.db.execute(
            select(Target).where(Target.enabled == True)
        )
        targets = list(result.scalars().all())

        if not targets:
            logger.warning("No enabled targets to distribute")
            return {'targets_total': 0, 'targets_assigned': 0}

        # ── Coalescing: collapse identical anonymous checks (same fetch_key) into
        # ONE physical fetch for the needs/capacity math + assignment selection, so
        # many targets watching the same page cost ONE slot. Assignment rows are
        # still written for EVERY member (at write time below), so each target stays
        # assigned and all members co-locate on the same agents (their per-target
        # configs merge into a single fetch on the agent — see monitor_coalescing).
        from services.monitor_coalescing import _group_key
        _fetch_groups = {}
        for _t in targets:
            _fetch_groups.setdefault(_group_key(_t), []).append(_t)

        def _pick_rep(members):
            return min(members, key=lambda x: x.id)

        rep_members = {}   # rep.id -> [all member targets in its fetch_key group]
        rep_targets = []
        for _members in _fetch_groups.values():
            _rep = _pick_rep(_members)
            rep_targets.append(_rep)
            rep_members[_rep.id] = _members
        if len(rep_targets) != len(targets):
            logger.info(
                f"Coalescing: {len(targets)} logical targets → {len(rep_targets)} "
                f"physical fetches ({len(targets) - len(rep_targets)} coalesced away)"
            )
        targets = rep_targets

        # Clear existing assignments
        await self.db.execute(
            delete(TargetAssignment)
        )

        # Calculate total capacity
        total_capacity = sum(info['total_capacity'] for info in agent_infos)

        if total_capacity == 0:
            logger.warning("Total agent capacity is 0, cannot distribute")
            return {'targets_total': len(targets), 'targets_assigned': 0}

        logger.info(
            f"Distributing {len(targets)} targets to ALL {len(agent_infos)} agents "
            f"(redundancy/anti-detection mode - period-aware)"
        )

        # Get global period for normalization
        config_result = await self.db.execute(
            select(Config).where(Config.key == "global_period_ms")
        )
        config = config_result.scalar_one_or_none()
        global_period_ms = int(config.value) if config else 10000

        # NEW STRATEGY: Target-centric distribution based on rate-limit avoidance
        # Each target needs N agents to ensure no agent checks it more than once per 60s
        # Formula: agents_needed = 60s / target_interval
        # Targets can be distributed to 1x-6x their minimum for load balancing
        #
        # Examples:
        # - 20s interval → min 3 agents, max 18 agents (60/20 × 6)
        # - 10s interval → min 6 agents, max 36 agents (60/10 × 6)
        # - 60s interval → min 1 agent, max 6 agents (60/60 × 6)
        # - 5s interval → min 12 agents, max 72 agents (60/5 × 6)

        # (module-level ANTI_DETECTION_THRESHOLD_MS — 60s rate-limit protection)

        # Calculate how many agents each target needs (min and max)
        target_agent_needs = {}  # minimum required (ideal)
        target_agent_actual = {} # actual agents available to assign
        target_agent_max = {}    # maximum allowed (2x minimum)
        insufficient_targets = [] # targets with insufficient agents

        for target in targets:
            target_period = target.check_period_ms or global_period_ms
            # Calculate IDEAL minimum number of agents for this target's interval
            # Use ceil to ensure we always have enough (e.g., 60s/20s = 3, not 2.x)
            import math
            ideal_min_agents = math.ceil(ANTI_DETECTION_THRESHOLD_MS / target_period)
            ideal_min_agents = max(1, ideal_min_agents)

            # Actual agents we can assign (capped by available)
            actual_agents = min(ideal_min_agents, len(agent_infos))

            # Maximum is 6x minimum (but capped at available agents)
            max_agents = min(ideal_min_agents * 6, len(agent_infos))

            target_agent_needs[target.id] = ideal_min_agents
            target_agent_actual[target.id] = actual_agents
            target_agent_max[target.id] = max_agents

            # Track if we can't meet minimum requirements
            if actual_agents < ideal_min_agents:
                insufficient_targets.append({
                    'target': target,
                    'period': target_period,
                    'needed': ideal_min_agents,
                    'available': actual_agents,
                    'deficit': ideal_min_agents - actual_agents
                })

        logger.info("Target anti-detection requirements:")
        for target in targets:
            period = target.check_period_ms or global_period_ms
            ideal_min = target_agent_needs[target.id]
            actual_min = target_agent_actual[target.id]
            max_allowed = target_agent_max[target.id]

            if actual_min < ideal_min:
                logger.warning(
                    f"  ⚠ {target.url}: {period}ms interval → needs {ideal_min} agents, "
                    f"only {actual_min} available ({max_allowed} max) - INSUFFICIENT!"
                )
            else:
                logger.info(f"  {target.url}: {period}ms interval → {actual_min}-{max_allowed} agents (min-max)")

        # Log overall capacity warning if any targets have insufficient agents
        if insufficient_targets:
            total_deficit = sum(t['deficit'] for t in insufficient_targets)
            logger.warning(
                f"\n⚠ CAPACITY WARNING: {len(insufficient_targets)} targets have insufficient agents!\n"
                f"  - Total agent deficit: {total_deficit} agents needed\n"
                f"  - Current agents: {len(agent_infos)}\n"
                f"  - Recommended: Add {total_deficit} more agents for optimal anti-detection"
            )
            for t in insufficient_targets:
                logger.warning(
                    f"    • {t['target'].url}: needs {t['needed']}, has {t['available']} (-{t['deficit']})"
                )

        # Distribute each target to agents (minimum first, then expand up to 6x if capacity allows)
        agent_assignments = {info['agent'].agent_id: [] for info in agent_infos}
        # Parallel per-agent set of assigned target ids, kept in lock-step with the
        # lists above, used ONLY for O(1) membership tests (the lists stay
        # authoritative for ordering). Replaces `target in agent_assignments[...]`
        # list scans in the expansion phase.
        agent_assignment_ids = {info['agent'].agent_id: set() for info in agent_infos}

        # Separate agents by capability
        playwright_agents = [info for info in agent_infos if info['agent'].has_playwright]
        regular_agents = agent_infos  # Regular targets can go to any agent

        # Separate agents by platform type for execution_mode filtering
        SERVERLESS_PLATFORMS = {'cloudflare', 'lambda'}
        vps_agents = [
            info for info in agent_infos
            if info['agent'].platform.value not in SERVERLESS_PLATFORMS
            and not (info['agent'].meta or {}).get('provider_type')
        ]
        serverless_agents = [
            info for info in agent_infos
            if info['agent'].platform.value in SERVERLESS_PLATFORMS
            or (info['agent'].meta or {}).get('provider_type')
        ]

        logger.info(
            f"Agent capabilities: {len(playwright_agents)} Playwright, "
            f"{len(vps_agents)} VPS, {len(serverless_agents)} serverless, "
            f"{len(agent_infos)} total"
        )

        # Phase 1: Assign actual available agents to each target (may be less than ideal minimum)
        for target in targets:
            actual_agents = target_agent_actual[target.id]

            # Check if target needs pre-check auth but doesn't have it yet
            # In this case, only assign to ONE Playwright agent to run the pre-check
            needs_precheck = (
                getattr(target, 'pre_check_workflow_id', None) and
                not getattr(target, 'auth_session_encrypted', None)
            )

            if needs_precheck:
                if not playwright_agents:
                    logger.warning(f"Target {target.url} needs pre-check but no Playwright agents!")
                    continue
                # Assign to only ONE Playwright agent for pre-check
                agent_info = playwright_agents[0]  # First available Playwright agent
                agent_assignments[agent_info['agent'].agent_id].append(target)
                agent_assignment_ids[agent_info['agent'].agent_id].add(target.id)
                logger.info(f"Target {target.url} needs pre-check - assigned to single agent {agent_info['agent'].agent_id}")
                continue

            # Filter agents by execution_mode
            exec_mode = getattr(target, 'execution_mode', 'both') or 'both'
            if exec_mode == 'vps':
                mode_eligible = vps_agents
            elif exec_mode == 'cloudflare':
                mode_eligible = serverless_agents
            else:
                mode_eligible = agent_infos  # 'both' → all agents

            if not mode_eligible:
                logger.warning(f"Target {target.url} (mode={exec_mode}) has no eligible agents!")
                mode_eligible = agent_infos  # Fallback to all agents

            # Further filter by Playwright requirement
            if getattr(target, 'requires_playwright', False):
                eligible_agents = [a for a in mode_eligible if a['agent'].has_playwright]
                if not eligible_agents:
                    logger.warning(f"Target {target.url} requires Playwright but no capable agents in mode={exec_mode}!")
                    continue
            else:
                eligible_agents = mode_eligible

            if not eligible_agents:
                logger.info(
                    f"Target {target.url}: no eligible agent for its check mode "
                    f"— skipping this cycle"
                )
                continue

            # Region preference: sort agents so region-matching agents come first
            preferred_region = getattr(target, 'preferred_region', None)
            if preferred_region:
                eligible_agents = sorted(
                    eligible_agents,
                    key=lambda a: (0 if a['agent'].region == preferred_region else 1)
                )

            # Assign this target to the actual N agents available (round-robin for fairness)
            for idx in range(min(actual_agents, len(eligible_agents))):
                agent_info = eligible_agents[idx % len(eligible_agents)]
                _aid = agent_info['agent'].agent_id
                agent_assignments[_aid].append(target)
                agent_assignment_ids[_aid].add(target.id)

        # Phase 2: Expand targets that have their minimum to use remaining capacity
        # Each target that has its minimum can expand independently (up to 6x)
        # This happens even if other targets don't have enough agents
        logger.info("Phase 2: Expanding targets with available capacity...")

        # Sort targets by interval (shortest first = highest priority for expansion)
        # Shorter intervals need more redundancy and load balancing
        targets_by_priority = sorted(
            targets,
            key=lambda t: t.check_period_ms or global_period_ms
        )

        expanded_count = 0
        for target in targets_by_priority:
            # Skip targets that need pre-check (they're only assigned to one agent)
            needs_precheck = (
                getattr(target, 'pre_check_workflow_id', None) and
                not getattr(target, 'auth_session_encrypted', None)
            )
            if needs_precheck:
                continue

            current_agent_count = target_agent_actual[target.id]
            minimum_agent_count = target_agent_needs[target.id]
            max_agent_count = target_agent_max[target.id]

            # Only expand if this target has met its minimum requirement
            if current_agent_count < minimum_agent_count:
                logger.debug(
                    f"  Skipping expansion for {target.url}: needs {minimum_agent_count}, "
                    f"only has {current_agent_count} agents"
                )
                continue

            # Try to expand to more agents (up to 6x minimum)
            if current_agent_count < max_agent_count:
                # Filter by execution_mode first
                exec_mode = getattr(target, 'execution_mode', 'both') or 'both'
                if exec_mode == 'vps':
                    mode_pool = vps_agents
                elif exec_mode == 'cloudflare':
                    mode_pool = serverless_agents
                else:
                    mode_pool = agent_infos

                # Then filter by Playwright requirement
                if getattr(target, 'requires_playwright', False):
                    eligible_for_expansion = [a for a in mode_pool if a['agent'].has_playwright]
                else:
                    eligible_for_expansion = mode_pool

                # Find agents with the least targets (for load balancing)
                # Sort agents by current target count
                agents_by_load = sorted(
                    eligible_for_expansion,
                    key=lambda info: len(agent_assignments[info['agent'].agent_id])
                )

                # Try to assign to additional agents
                assigned_count = 0
                for agent_info in agents_by_load:
                    agent_id = agent_info['agent'].agent_id

                    # Skip if target already assigned to this agent (O(1) set test,
                    # was an O(n) `target in list` scan).
                    if target.id in agent_assignment_ids[agent_id]:
                        continue

                    # Check if agent has capacity
                    current_targets = len(agent_assignments[agent_id])
                    agent_capacity = agent_info['capacity_per_slot']

                    if current_targets < agent_capacity:
                        # Assign target to this agent
                        agent_assignments[agent_id].append(target)
                        agent_assignment_ids[agent_id].add(target.id)
                        assigned_count += 1

                        # Stop if we've reached the maximum for this target
                        if current_agent_count + assigned_count >= max_agent_count:
                            break

                if assigned_count > 0:
                    expanded_count += 1
                    logger.info(
                        f"  Expanded {target.url}: {current_agent_count} → "
                        f"{current_agent_count + assigned_count} agents "
                        f"(max {max_agent_count})"
                    )

        if expanded_count > 0:
            logger.info(f"Phase 2 complete: expanded {expanded_count} targets to use available capacity")
        else:
            logger.info("Phase 2 complete: no targets expanded (all at maximum or insufficient capacity)")

        # Phase 3: Calculate per-target offsets and track target-to-slot mapping
        # Each target gets its own independent rotation based on assigned agents
        logger.info("Phase 3: Calculating per-target offsets...")

        # Get time slot mode
        mode_result = await self.db.execute(
            select(Config).where(Config.key == "time_slot_mode")
        )
        mode_config = mode_result.scalar_one_or_none()
        time_slot_mode = mode_config.value if mode_config else "distributed"

        # Track targets per offset per agent per period
        # Format: {agent_id: {period_ms: {offset: [target1, target2, ...]}}}
        agent_slot_assignments = {}
        for info in agent_infos:
            agent_slot_assignments[info['agent'].agent_id] = {}

        # Reverse map target_id -> [agent_ids] built in ONE pass over the agent
        # assignment lists (was an O(agents) `target in list` scan PER target).
        # Iterating agent_assignments.items() in dict order preserves the exact
        # per-target agent ordering the prior comprehension produced; a per-target
        # seen-set dedups defensively to match the membership test's at-most-once.
        target_to_agents: Dict[int, List[str]] = {}
        _seen_pair = {}  # target_id -> set(agent_id) already recorded
        for agent_id, targets_list in agent_assignments.items():
            for t in targets_list:
                seen = _seen_pair.setdefault(t.id, set())
                if agent_id in seen:
                    continue
                seen.add(agent_id)
                target_to_agents.setdefault(t.id, []).append(agent_id)

        # For each target, calculate offsets based on position in target's agent list
        for target in targets:
            target_period = target.check_period_ms or global_period_ms

            # Get agents assigned to THIS target (reverse-map lookup, O(1))
            agents_with_target = target_to_agents.get(target.id, [])

            if not agents_with_target:
                continue

            logger.info(
                f"  {target.url} ({target_period}ms): "
                f"{len(agents_with_target)} agents → calculating offsets"
            )

            # Calculate offset for each agent based on position in THIS target's rotation
            for position, agent_id in enumerate(agents_with_target):
                # Offset = position × target_period
                offset = position * target_period

                # Add target to this agent's slot at this offset
                if target_period not in agent_slot_assignments[agent_id]:
                    agent_slot_assignments[agent_id][target_period] = {}
                if offset not in agent_slot_assignments[agent_id][target_period]:
                    agent_slot_assignments[agent_id][target_period][offset] = []

                agent_slot_assignments[agent_id][target_period][offset].append(target)

        logger.info("Phase 3 complete: per-target offsets calculated and mapped to slots")

        # Phase 4: Detect capacity overflow and create additional slots
        logger.info("Phase 4: Checking for capacity overflow per slot...")

        import math
        overflow_detected = False

        for info in agent_infos:
            agent = info['agent']
            agent_id = agent.agent_id
            capacity_per_slot = info['capacity_per_slot']

            if agent_id not in agent_slot_assignments:
                continue

            for period_ms, slots in agent_slot_assignments[agent_id].items():
                for offset, targets_at_slot in list(slots.items()):
                    target_count = len(targets_at_slot)

                    if target_count > capacity_per_slot:
                        overflow_detected = True
                        overflow = target_count - capacity_per_slot
                        additional_slots_needed = math.ceil(overflow / capacity_per_slot)
                        total_slots = 1 + additional_slots_needed

                        logger.warning(
                            f"  ⚠ Overflow: {agent_id} period {period_ms}ms offset {offset}ms: "
                            f"{target_count} targets > {capacity_per_slot} capacity"
                        )
                        logger.info(
                            f"    Creating {additional_slots_needed} additional slot(s) "
                            f"(total: {total_slots} slots)"
                        )

                        # Calculate spacing between slots
                        # Use period / total_slots to distribute evenly
                        spacing = period_ms // total_slots

                        # Create new slots with evenly spaced offsets
                        new_slots = {}
                        for slot_idx in range(total_slots):
                            new_offset = offset + (slot_idx * spacing)
                            new_slots[new_offset] = []

                        # Redistribute targets round-robin across all slots
                        for idx, target in enumerate(targets_at_slot):
                            slot_idx = idx % total_slots
                            slot_offset = list(new_slots.keys())[slot_idx]
                            new_slots[slot_offset].append(target)

                        # Replace the overflowing slot with new slots
                        del slots[offset]
                        for new_offset, new_targets in new_slots.items():
                            slots[new_offset] = new_targets
                            logger.info(
                                f"      Slot {new_offset}ms: {len(new_targets)} targets "
                                f"({len(new_targets)*100//capacity_per_slot}% utilization)"
                            )

        if overflow_detected:
            logger.info("Phase 4 complete: overflow slots created and targets redistributed")
        else:
            logger.info("Phase 4 complete: no overflow detected")

        # Phase 5: Convert slot assignments to offset dictionaries and update agents
        logger.info("Phase 5: Finalizing slot assignments...")

        # Get total agent count for effective_period calculation (rolling mode)
        total_agent_count = len(agent_infos)

        # Per-period agent coverage: how many agents actually have a slot for each
        # period. The rolling effective_period must be period × (agents covering
        # THIS period), NOT the whole fleet — otherwise Playwright/JS targets
        # (which only land on Playwright-capable agents) get period × total_agents
        # with no second agent to fill the staggered slot, so they check at 2×+
        # their interval. Counting per-period coverage keeps JS targets on cadence.
        period_agent_counts: Dict[int, int] = {}
        for _aid, _periods in agent_slot_assignments.items():
            for _pm in _periods.keys():
                period_agent_counts[_pm] = period_agent_counts.get(_pm, 0) + 1

        # Get or create shared sync_epoch_ms for all agents
        # All agents must share the same sync_epoch_ms for proper staggering
        sync_epoch_config = await self.db.execute(
            select(Config).where(Config.key == "sync_epoch_ms")
        )
        sync_epoch_record = sync_epoch_config.scalar_one_or_none()

        if sync_epoch_record:
            # Reuse existing epoch
            # Handle both old string format and new int format
            shared_sync_epoch_ms = int(sync_epoch_record.value) if isinstance(sync_epoch_record.value, str) else sync_epoch_record.value
            logger.info(f"Using existing sync_epoch_ms: {shared_sync_epoch_ms}")
        else:
            # Create new epoch (first time)
            shared_sync_epoch_ms = int(time.time() * 1000)
            # Round down to nearest minute for cleaner timing
            shared_sync_epoch_ms = (shared_sync_epoch_ms // 60000) * 60000

            # Store in config (as integer, not string, since column is JSON type)
            new_sync_epoch = Config(key="sync_epoch_ms", value=shared_sync_epoch_ms)
            self.db.add(new_sync_epoch)
            await self.db.commit()
            logger.info(f"Created new sync_epoch_ms: {shared_sync_epoch_ms}")

        for info in agent_infos:
            agent = info['agent']
            agent_id = agent.agent_id

            offsets_by_period = {}

            if agent_id in agent_slot_assignments:
                for period_ms, slots in agent_slot_assignments[agent_id].items():
                    # Get all offsets for this period, sorted
                    offsets = sorted(list(slots.keys()))

                    # Use new format: {"offset_ms": X, "effective_period_ms": Y}
                    # For rolling mode: effective_period = period × total_agent_count,
                    # FLOORED at the 60s anti-detection threshold so no single agent
                    # ever checks faster than 60s. A fast (e.g. 1s) target therefore
                    # needs ~60 servers to reach its interval at the FLEET level; with
                    # fewer it degrades gracefully (each agent stays at 60s) instead of
                    # one server hammering the site every 1s.
                    period_coverage = period_agent_counts.get(period_ms, 1) or 1
                    effective_period_ms = max(
                        period_ms * period_coverage, ANTI_DETECTION_THRESHOLD_MS
                    )
                    offsets_by_period[period_ms] = {
                        "offset_ms": offsets[0] if offsets else 0,  # First/only offset
                        "effective_period_ms": effective_period_ms,
                    }

                    logger.info(
                        f"    {agent_id} period {period_ms}ms: "
                        f"offset={offsets[0] if offsets else 0}ms, "
                        f"effective_period={effective_period_ms}ms "
                        f"(rolling mode: {period_coverage} agents cover this period, 60s floor)"
                    )

            # Update agent metadata
            agent.meta['assigned_time_slot_offsets_by_period'] = offsets_by_period
            agent.meta['global_period_ms'] = global_period_ms
            agent.meta['sync_epoch_ms'] = shared_sync_epoch_ms

            # REMOVED: force_config_update = True
            # Let AgentStateManager + offset comparison handle incremental updates
            # agent.force_config_update = True

            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(agent, 'meta')

        await self.db.commit()
        logger.info("Phase 5 complete: all agent slot assignments finalized")

        # Build assignment stats and store targets in agent_infos for slot calculation
        formatted_assignments = []
        for info in agent_infos:
            agent = info['agent']
            assigned_targets = agent_assignments[agent.agent_id]

            # Store targets in info for slot calculation above
            info['targets'] = assigned_targets

            formatted_assignments.append({
                'agent': agent,
                'agent_id': agent.agent_id,
                'target_count': len(assigned_targets),
                'targets': assigned_targets,
            })

        # Create database assignments
        total_assigned = 0

        for assignment in formatted_assignments:
            agent = assignment['agent']
            assigned_targets = assignment['targets']

            # Verify no duplicates (sanity check)
            target_ids = [t.id for t in assigned_targets]
            if len(target_ids) != len(set(target_ids)):
                raise ValueError(f"Duplicate targets assigned to agent {agent.agent_id}")

            # Create assignments in database. Each assigned target is a coalesced
            # representative — expand it back to EVERY member of its fetch_key group
            # so all members co-locate on this agent (its config-build then merges
            # them into one physical fetch). Falls back to [target] when uncoalesced.
            for target in assigned_targets:
                for member in rep_members.get(target.id, [target]):
                    db_assignment = TargetAssignment(
                        target_id=member.id,
                        agent_id=agent.agent_id,
                        assigned_by="capacity-aware-distributor",
                        assigned_at=datetime.utcnow()
                    )
                    self.db.add(db_assignment)
                    total_assigned += 1

            logger.info(
                f"Agent {agent.agent_id}: {len(assigned_targets)} targets assigned "
                f"(anti-detection distribution)"
            )

        await self.db.commit()

        # Verification: count redundancy (targets should be on multiple agents)
        all_assignments_result = await self.db.execute(
            select(TargetAssignment)
        )
        all_assignments = all_assignments_result.scalars().all()

        target_assignment_counts = {}
        for assignment in all_assignments:
            target_id = assignment.target_id
            target_assignment_counts[target_id] = target_assignment_counts.get(target_id, 0) + 1

        avg_redundancy = sum(target_assignment_counts.values()) / len(target_assignment_counts) if target_assignment_counts else 0

        # Calculate NEW metrics based on target-centric model with min/max
        total_agents_available = len(agent_infos)

        # Calculate system load based on ACTUAL assignments
        total_agent_slots_actual = sum(target_agent_actual[t.id] for t in targets)
        # Calculate IDEAL minimum requirements
        total_agent_slots_ideal = sum(target_agent_needs[t.id] for t in targets)
        # Calculate maximum possible load (if all targets expanded to 2x)
        total_agent_slots_max = sum(target_agent_max[t.id] for t in targets)

        # System load percentage (based on actual assignments)
        system_load_actual_percent = (total_agent_slots_actual / total_agents_available * 100) if total_agents_available > 0 else 0
        # Ideal load if we had enough agents
        system_load_ideal_percent = (total_agent_slots_ideal / total_agents_available * 100) if total_agents_available > 0 else 0
        # Maximum theoretical load if all targets expanded to 2x
        system_load_max_percent = (total_agent_slots_max / total_agents_available * 100) if total_agents_available > 0 else 0

        # Calculate capacity warnings
        has_capacity_warning = len(insufficient_targets) > 0
        total_agent_deficit = sum(t['deficit'] for t in insufficient_targets) if insufficient_targets else 0

        # Calculate how many more 10s-interval targets we could handle (at minimum)
        agents_per_10s_target = int(ANTI_DETECTION_THRESHOLD_MS / 10000)  # 1000s / 10s = 100 agents
        remaining_agent_capacity = total_agents_available - total_agent_slots_actual
        max_additional_10s_targets = max(0, remaining_agent_capacity // agents_per_10s_target) if agents_per_10s_target > 0 else 0

        agents_with_targets = sum(1 for a in formatted_assignments if len(a['targets']) > 0)
        distribution_quality = (agents_with_targets / len(agent_infos) * 100) if agent_infos else 0

        logger.info(
            f"✓ Anti-detection distribution complete:\n"
            f"  - {len(targets)} targets using {total_agent_slots_actual} agent slots ({system_load_actual_percent:.1f}% load)\n"
            f"  - Ideal: {total_agent_slots_ideal} slots ({system_load_ideal_percent:.1f}% load), "
            f"Max: {total_agent_slots_max} slots ({system_load_max_percent:.1f}% load)\n"
            f"  - Average {avg_redundancy:.1f}x agent redundancy per target\n"
            f"  - {agents_with_targets}/{len(agent_infos)} agents active ({distribution_quality:.1f}% coverage)\n"
            f"  - Can handle {max_additional_10s_targets} more 10s-interval targets"
        )

        return {
            'targets_total': len(targets),
            'targets_assigned': total_assigned,
            'agents_total': total_agents_available,
            'agents_utilized': agents_with_targets,
            'agent_slots_actual': total_agent_slots_actual,
            'agent_slots_ideal': total_agent_slots_ideal,
            'agent_slots_max': total_agent_slots_max,
            'agent_slots_available': total_agents_available,
            'system_load_actual_percent': system_load_actual_percent,
            'system_load_ideal_percent': system_load_ideal_percent,
            'system_load_max_percent': system_load_max_percent,
            'agent_deficit': total_agent_deficit,
            'has_capacity_warning': has_capacity_warning,
            'insufficient_targets_count': len(insufficient_targets),
            'max_additional_10s_targets': max_additional_10s_targets,
            'distribution_quality_percent': distribution_quality,
            'avg_redundancy': avg_redundancy,
            'target_weights_ideal': {t.id: target_agent_needs[t.id] for t in targets},
            'target_weights_actual': {t.id: target_agent_actual[t.id] for t in targets},
            'target_weights_max': {t.id: target_agent_max[t.id] for t in targets}
        }

    async def get_agent_targets(self, agent_id: str) -> List[Target]:
        """
        Get all targets assigned to a specific agent.

        Args:
            agent_id: The agent ID (agent_id field, not database id)

        Returns:
            List of Target objects assigned to this agent
        """
        # Get agent's database id first
        agent_result = await self.db.execute(
            select(Agent).where(Agent.agent_id == agent_id)
        )
        agent = agent_result.scalar_one_or_none()

        if not agent:
            return []

        # Get assignments for this agent. TargetAssignment.agent_id is the STRING
        # agent_id FK (agents.agent_id), NOT the integer PK — comparing to
        # agent.id (int) matches nothing.
        assignments_result = await self.db.execute(
            select(TargetAssignment).where(TargetAssignment.agent_id == agent.agent_id)
        )
        assignments = assignments_result.scalars().all()

        # Fetch full target details
        target_ids = [a.target_id for a in assignments]
        if not target_ids:
            return []

        targets_result = await self.db.execute(
            select(Target).where(Target.id.in_(target_ids))
        )
        return targets_result.scalars().all()

    async def _distribute_scheduled_workflows(
        self,
        agent_infos: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        Distribute scheduled workflows to Playwright-capable agents.

        Similar to target distribution:
        - Only assign to agents with Playwright capability
        - Calculate time offsets for anti-detection (spread across agents)
        - Store assignments in agent.meta['assigned_scheduled_workflows']

        Args:
            agent_infos: List of agent info dicts

        Returns:
            Distribution statistics
        """
        from models.automation_workflow import AutomationWorkflow

        # Get Playwright-capable agents
        playwright_agents = [info for info in agent_infos if info['agent'].has_playwright]

        if not playwright_agents:
            logger.info("No Playwright-capable agents for workflow distribution")
            return {'scheduled_workflows_total': 0, 'scheduled_workflows_assigned': 0}

        # Get scheduled workflows
        result = await self.db.execute(
            select(AutomationWorkflow)
            .where(
                AutomationWorkflow.schedule_enabled == True,
                AutomationWorkflow.schedule_interval_ms.isnot(None)
            )
        )
        all_workflows = list(result.scalars().all())

        # Skip AI workflows — those are handled by the CentralScheduler on recorders.
        # Also skip INSTALLED marketplace proxies: a desktop agent self-creates and
        # self-reports the task, so its completion arrives with no _marketplace /
        # _data_source=consumer context — _bill_task_running_time would skip billing
        # and _apply_consumer_run_inversion would never run (paid recipe delivered
        # free + un-inverted). Installed proxies are dispatched through the billed
        # _dispatch_to_recorder_or_queue choke point by the CentralScheduler instead.
        from services.workflow_router import WorkflowRouter
        workflows = [
            wf for wf in all_workflows
            if not WorkflowRouter.requires_ai(wf)
            and not getattr(wf, "is_installed", False)
        ]

        if not workflows:
            logger.info("No non-AI scheduled workflows to distribute")
            # Clear any stale workflow assignments and notify agents
            for info in playwright_agents:
                agent = info['agent']
                if agent.meta:
                    agent.meta['assigned_scheduled_workflows'] = []
                    from sqlalchemy.orm.attributes import flag_modified
                    flag_modified(agent, 'meta')
                # Ensure agent receives empty workflow list
                agent.force_config_update = True
            await self.db.commit()
            return {'scheduled_workflows_total': 0, 'scheduled_workflows_assigned': 0}

        logger.info(
            f"Distributing {len(workflows)} scheduled workflows to "
            f"{len(playwright_agents)} Playwright agents"
        )

        # Sort agents by agent_id for consistent distribution
        playwright_agents.sort(key=lambda x: x['agent'].agent_id)
        playwright_count = len(playwright_agents)

        # Initialize workflow lists for each agent
        for info in playwright_agents:
            agent = info['agent']
            if agent.meta is None:
                agent.meta = {}
            agent.meta['assigned_scheduled_workflows'] = []

        # Distribute workflows to all Playwright agents (all agents get all workflows)
        # Each agent gets a different time offset for anti-detection
        total_assigned = 0

        for workflow in workflows:
            interval_ms = workflow.schedule_interval_ms

            # Decrypt credentials for sending to agents
            credentials = None
            if workflow.credentials_encrypted:
                try:
                    from cryptography.fernet import Fernet
                    from security.hmac import get_fernet_key
                    import json
                    fernet = Fernet(get_fernet_key())
                    credentials = json.loads(
                        fernet.decrypt(workflow.credentials_encrypted.encode()).decode()
                    )
                except Exception as e:
                    logger.error(f"Failed to decrypt workflow {workflow.id} credentials: {e}")

            # Filter agents based on workflow restrictions
            if workflow.captcha_blocked:
                # Only send to captcha-trusted agents
                eligible_agents = [
                    info for info in playwright_agents
                    if info['agent'].captcha_trusted
                ]
                if not eligible_agents:
                    logger.warning(
                        f"Workflow '{workflow.name}' is captcha-blocked but no "
                        f"captcha-trusted agents available! Skipping distribution."
                    )
                    continue
            elif getattr(workflow, 'trusted_agents_only', False):
                # Only send to trusted agents
                eligible_agents = [
                    info for info in playwright_agents
                    if getattr(info['agent'], 'is_trusted', False)
                ]
                if not eligible_agents:
                    logger.warning(
                        f"Workflow '{workflow.name}' requires trusted agents but none "
                        f"available! Skipping distribution."
                    )
                    continue
            else:
                # Send to all Playwright agents
                eligible_agents = playwright_agents

            eligible_count = len(eligible_agents)

            # Assign to eligible agents with staggered offsets
            for idx, info in enumerate(eligible_agents):
                agent = info['agent']

                # Calculate time offset for this agent (spread across interval)
                if eligible_count > 1:
                    offset_ms = (interval_ms // eligible_count) * idx
                else:
                    offset_ms = 0

                workflow_data = {
                    "id": workflow.id,
                    "name": workflow.name,
                    "workflow_type": workflow.workflow_type,
                    "steps": workflow.steps,
                    "form_data": workflow.form_data,
                    "credentials": credentials,
                    "entry_url": workflow.entry_url,
                    "exit_condition": workflow.exit_condition,
                    "timeout_ms": workflow.timeout_ms,
                    "headless": workflow.headless,
                    "fast_mode": workflow.fast_mode,
                    "captcha_blocked": workflow.captcha_blocked,
                    "schedule_interval_ms": interval_ms,
                    "time_slot_offset_ms": offset_ms,
                }

                agent.meta['assigned_scheduled_workflows'].append(workflow_data)
                total_assigned += 1

                trusted_marker = " [captcha-trusted]" if workflow.captcha_blocked else ""
                logger.info(
                    f"  Workflow '{workflow.name}' → {agent.agent_id}{trusted_marker} "
                    f"(offset={offset_ms}ms, interval={interval_ms}ms)"
                )

        # Commit changes and flag agents for config update
        for info in playwright_agents:
            agent = info['agent']
            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(agent, 'meta')
            # Ensure agent receives updated workflow list
            agent.force_config_update = True

        await self.db.commit()

        logger.info(
            f"✓ Workflow distribution complete: "
            f"{len(workflows)} workflows × {playwright_count} agents = "
            f"{total_assigned} assignments"
        )

        return {
            'scheduled_workflows_total': len(workflows),
            'scheduled_workflows_assigned': total_assigned,
            'playwright_agents': playwright_count
        }
