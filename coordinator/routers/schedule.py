"""
Schedule router - scheduling configuration and management endpoints.
"""
import logging
from typing import Dict, Any, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, func

from database import get_db
from models.config import Config
# There is no persistent audit-log store in this build. `EventsAudit(...)`
# returns None so the `if audit is not None:` guards on db.add() skip persistence
# without touching a real mapper.
def EventsAudit(*args, **kwargs):  # noqa: N802 - stand-in for the absent model
    return None
from models.target import Target
from models.target_assignment import TargetAssignment
from security.api_key import get_current_api_key
from security.dependencies import require_platform_admin, AuthContext
from config import settings
from datetime import datetime

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/schedule", tags=["Scheduling"])


# Pydantic models
class ScheduleInfo(BaseModel):
    """Response model for schedule information."""
    global_period_ms: int = Field(..., serialization_alias="globalPeriodMs")
    quorum: int
    agent_count: int = Field(..., serialization_alias="agentCount")
    online_agents: int = Field(..., serialization_alias="onlineAgents")
    distribution: Dict[str, Any]
    time_slot_mode: str = Field(default="distributed", serialization_alias="timeSlotMode")
    redistribution_interval_hours: float = Field(default=12.0, serialization_alias="redistributionIntervalHours")

    class Config:
        populate_by_name = True


class UpdateScheduleConfigRequest(BaseModel):
    """Request model for updating schedule configuration."""
    global_period_ms: Optional[int] = Field(None, ge=100, le=3600000, validation_alias="globalPeriodMs")
    time_slot_mode: Optional[str] = Field(None, validation_alias="timeSlotMode")  # "distributed" | "rolling"
    quorum: Optional[int] = Field(None, ge=1, le=10)
    redistribution_interval_hours: Optional[float] = Field(None, ge=1.0, le=168.0, validation_alias="redistributionIntervalHours")  # 1 hour to 1 week

    @field_validator("time_slot_mode")
    @classmethod
    def _valid_mode(cls, v):
        if v is not None and v not in ("distributed", "rolling"):
            raise ValueError("time_slot_mode must be 'distributed' or 'rolling'")
        return v

    class Config:
        populate_by_name = True


@router.get(
    "",
    response_model=ScheduleInfo,
    summary="Get Schedule Info",
    description="Get current scheduling information and agent distribution.",
)
async def get_schedule_info(
    db: AsyncSession = Depends(get_db),
    _auth: AuthContext = Depends(require_platform_admin),
):
    """
    Get current schedule information.

    Returns global period, quorum, agent counts, and distribution.

    Platform-admin only: this exposes the platform-wide fleet topology
    (agent counts and distribution), which is an ops-console view.
    """
    try:
        from models.agent import Agent, AgentStatus
        from datetime import datetime, timedelta

        # Get config values
        config_result = await db.execute(
            select(Config).where(Config.key.in_(["global_period_ms", "quorum", "time_slot_mode", "redistribution_interval_hours"]))
        )
        configs = {c.key: c.value for c in config_result.scalars().all()}

        global_period_ms = configs.get("global_period_ms", settings.global_period_ms)
        quorum = configs.get("quorum", settings.quorum)
        time_slot_mode = configs.get("time_slot_mode", "distributed")
        redistribution_interval_hours = float(configs.get("redistribution_interval_hours", 12.0))

        # Aggregate agent counts by status in the DB (no full-row hydration).
        status_rows = await db.execute(
            select(Agent.status, func.count(Agent.id)).group_by(Agent.status)
        )
        status_counts = {row[0]: row[1] for row in status_rows.all()}

        distribution = {
            "active": status_counts.get(AgentStatus.ACTIVE, 0),
            "inactive": status_counts.get(AgentStatus.INACTIVE, 0),
            "revoked": status_counts.get(AgentStatus.REVOKED, 0),
            "suspended": status_counts.get(AgentStatus.SUSPENDED, 0),
        }
        agent_count = sum(status_counts.values())

        # Count online agents (active + seen in last 5 minutes) directly in the DB
        from datetime import timezone
        threshold = datetime.now(timezone.utc) - timedelta(minutes=5)
        online_agents = (await db.execute(
            select(func.count(Agent.id)).where(
                Agent.status == AgentStatus.ACTIVE,
                Agent.last_seen_at.is_not(None),
                Agent.last_seen_at > threshold,
            )
        )).scalar() or 0

        return ScheduleInfo(
            global_period_ms=global_period_ms,
            quorum=quorum,
            agent_count=agent_count,
            online_agents=online_agents,
            distribution=distribution,
            time_slot_mode=time_slot_mode,
            redistribution_interval_hours=redistribution_interval_hours,
        )

    except Exception as e:
        logger.error(f"Error getting schedule info: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get schedule info",
        )


@router.get(
    "/distribution",
    summary="Get Agent Distribution",
    description="Get agent distribution by status and time slots.",
)
async def get_distribution(
    db: AsyncSession = Depends(get_db),
    _auth: AuthContext = Depends(require_platform_admin),
):
    """
    Get agent distribution statistics.

    Returns:
    - Agent counts by status
    - Time slot distribution (if time-wheel scheduler is active)

    Platform-admin only: this aggregates the platform-wide fleet (an ops-console
    view).
    """
    try:
        from models.agent import Agent, AgentStatus

        # Aggregate by status in the DB instead of hydrating every Agent row.
        status_rows = await db.execute(
            select(Agent.status, func.count(Agent.id)).group_by(Agent.status)
        )
        status_counts = {row[0]: row[1] for row in status_rows.all()}

        by_status = {
            "active": status_counts.get(AgentStatus.ACTIVE, 0),
            "inactive": status_counts.get(AgentStatus.INACTIVE, 0),
            "revoked": status_counts.get(AgentStatus.REVOKED, 0),
            "suspended": status_counts.get(AgentStatus.SUSPENDED, 0),
        }

        # Distribution by platform (grouped in the DB)
        platform_rows = await db.execute(
            select(Agent.platform, func.count(Agent.id)).group_by(Agent.platform)
        )
        by_platform = {
            (p.value if hasattr(p, "value") else p): c
            for p, c in platform_rows.all()
        }

        # Time slots distribution (placeholder — a full implementation would
        # query the TimeWheelScheduler's time slots)
        time_slots = []
        total_agents = sum(status_counts.values())
        if total_agents > 0:
            # Create sample distribution across 24 hour slots
            slots_per_day = 24
            agents_per_slot = total_agents // slots_per_day
            remainder = total_agents % slots_per_day

            for i in range(slots_per_day):
                slot_count = agents_per_slot + (1 if i < remainder else 0)
                time_slots.append({
                    "slot": f"{i:02d}:00-{i+1:02d}:00",
                    "agentCount": slot_count,
                })

        return {
            "byStatus": by_status,
            "byPlatform": by_platform,
            "timeSlots": time_slots,
        }

    except Exception as e:
        logger.error(f"Error getting distribution: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get agent distribution",
        )


@router.post(
    "/rebalance",
    summary="Rebalance Time Slots",
    description="Redistribute time slots across all active agents. Requires admin role.",
)
async def rebalance_schedule(
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """
    Rebalance agent time slots.

    Redistributes all active agents evenly across the global period.
    Sets force_config_update flag on all agents to notify them.
    Requires admin role.
    """
    # These routes mutate GLOBAL scheduling config / every agent, so they
    # require a platform admin.
    if not current_api_key.get("is_platform_admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can rebalance schedule",
        )

    try:
        # Get global period
        config_result = await db.execute(
            select(Config).where(Config.key == "global_period_ms")
        )
        config = config_result.scalar_one_or_none()
        global_period_ms = int(config.value) if config else settings.global_period_ms

        # Import and call redistribute_time_slots from agents router
        from routers.agents import redistribute_time_slots
        stats = await redistribute_time_slots(db, global_period_ms)

        # Audit log
        audit = EventsAudit(
            actor=current_api_key.get("label"),
            action="schedule.rebalance",
            details=stats,
            at=datetime.utcnow(),
        )
        if audit is not None:  # no audit store in this build → EventsAudit() is None
            db.add(audit)
        await db.commit()

        logger.info(
            f"Time slots rebalanced by {current_api_key.get('label')}: "
            f"{stats.get('agents_updated', 0)} agents updated"
        )

        return {
            "status": "success",
            "message": f"Redistributed {stats.get('agents_updated', 0)} agents across {stats.get('period_ms', 0)}ms period",
            "stats": stats,
        }

    except Exception as e:
        logger.error(f"Error rebalancing schedule: {e}")
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to rebalance schedule",
        )


@router.post(
    "/config",
    summary="Update Schedule Config",
    description="Update scheduling configuration. Requires admin role.",
)
async def update_schedule_config(
    request: UpdateScheduleConfigRequest,
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """
    Update scheduling configuration.

    Can update global_period_ms, quorum, and redistribution_interval_hours. Requires admin role.
    Triggers automatic rebalance when global_period_ms changes.
    """
    # These routes mutate GLOBAL scheduling config / every agent, so they
    # require a platform admin.
    if not current_api_key.get("is_platform_admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can update schedule config",
        )

    try:
        updates = request.model_dump(exclude_unset=True)

        if not updates:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No updates provided",
            )

        # Update each config value
        for key, value in updates.items():
            # Check if config exists
            result = await db.execute(
                select(Config).where(Config.key == key)
            )
            config = result.scalar_one_or_none()

            if config:
                config.value = value
            else:
                config = Config(key=key, value=value)
                db.add(config)

        # Audit log
        audit = EventsAudit(
            actor=current_api_key.get("label"),
            action="schedule.config_update",
            details=updates,
            at=datetime.utcnow(),
        )
        if audit is not None:  # no audit store in this build → EventsAudit() is None
            db.add(audit)

        await db.commit()

        logger.info(
            f"Schedule config updated by {current_api_key.get('label')}: {updates}"
        )

        # A change to the global period OR the schedule mode must re-run a full
        # capacity-aware redistribution (recomputes per-period offsets for the new
        # mode AND pushes assign_targets to connected recorders over the gateway).
        redistribution_stats = None
        if "global_period_ms" in updates or "time_slot_mode" in updates:
            from models.agent import Agent
            from services.capacity_aware_distributor import CapacityAwareDistributor

            # Force HTTP/desktop agents to re-poll their config on next cycle.
            await db.execute(update(Agent).values(force_config_update=True))
            await db.commit()

            gp = updates.get("global_period_ms")
            if gp is None:
                _gp = (await db.execute(
                    select(Config).where(Config.key == "global_period_ms")
                )).scalar_one_or_none()
                gp = int(_gp.value) if _gp else 60000
            redistribution_stats = await CapacityAwareDistributor(db).distribute_timeslots_and_targets(int(gp))
            logger.info(f"Redistributed after schedule config change: {redistribution_stats}")

        return {
            "status": "success",
            "message": "Schedule configuration updated",
            "updates": updates,
            "redistribution": redistribution_stats,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating schedule config: {e}")
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update schedule config",
        )


@router.get(
    "/monitor",
    summary="Get Real-time Monitoring Data",
    description="Get real-time monitoring data including agent time slots and sync status.",
)
async def get_monitoring_data(
    db: AsyncSession = Depends(get_db),
    _auth: AuthContext = Depends(require_platform_admin),
):
    """
    Get real-time monitoring data for the monitoring dashboard.

    Returns:
    - Current time slot assignments
    - Agent synchronization status
    - Load distribution
    - Global period configuration
    - Capacity metrics per agent
    - Hardware information
    - Detailed timeslot info per agent

    Platform-admin only: this returns target URLs and the full fleet/agent
    infrastructure topology (hardware, capacity, per-agent slots). It is an
    ops-console view.
    """
    try:
        from models.agent import Agent, AgentStatus
        from models.target_assignment import TargetAssignment
        from sqlalchemy import func
        from datetime import timezone, timedelta

        # Get global period and time slot mode
        config_result = await db.execute(
            select(Config).where(Config.key.in_(["global_period_ms", "time_slot_mode"]))
        )
        configs = {c.key: c.value for c in config_result.scalars().all()}
        global_period_ms = int(configs.get("global_period_ms", settings.global_period_ms))
        time_slot_mode = configs.get("time_slot_mode", "distributed")

        # Get all active agents with time slots
        # Order by agent_id for stable sorting (matches distributor sort order)
        agents_result = await db.execute(
            select(Agent)
            .where(Agent.status == AgentStatus.ACTIVE)
            .order_by(Agent.agent_id)
        )
        active_agents = agents_result.scalars().all()

        # Calculate distribution stats
        agent_count = len(active_agents)

        # Calculate slot duration (time between agent offsets)
        if time_slot_mode == "rolling":
            slot_duration = global_period_ms if agent_count > 0 else 0
        else:
            slot_duration = global_period_ms // agent_count if agent_count > 0 else global_period_ms

        # Calculate agent period (how often each agent checks)
        agent_period_ms = global_period_ms if time_slot_mode == "distributed" else (global_period_ms * agent_count if agent_count > 0 else global_period_ms)

        # Online detection threshold
        offline_threshold = timedelta(milliseconds=(agent_period_ms * 2) + 60000)
        threshold = datetime.now(timezone.utc) - offline_threshold

        # OPTIMIZATION: Fetch all assignments with targets in ONE query using JOIN
        from sqlalchemy.orm import selectinload

        # Get agent IDs - note the schema inconsistency:
        # - TargetAssignment.agent_id = VARCHAR (references Agent.agent_id string)
        agent_string_ids = [a.agent_id for a in active_agents]  # STRING IDs like "pll"

        assignments_query = await db.execute(
            select(TargetAssignment)
            .options(selectinload(TargetAssignment.target))
            .where(TargetAssignment.agent_id.in_(agent_string_ids))  # Use STRING for assignments
        )
        all_assignments = assignments_query.scalars().all()

        # Group assignments by agent_id (STRING)
        assignments_by_agent = {}
        for assignment in all_assignments:
            if assignment.agent_id not in assignments_by_agent:
                assignments_by_agent[assignment.agent_id] = []
            assignments_by_agent[assignment.agent_id].append(assignment)

        # There is no per-run reports store, so there is no per-(agent,target)
        # change-detection last-check to aggregate. The uptime_checks lookup below
        # still supplies a last-check for uptime targets; change-detection
        # last-check falls back to None.
        last_reports_lookup: dict = {}

        # Fetch last uptime checks for ALL agents/targets in ONE query (using STRING agent IDs for uptime checks)
        from models.uptime_check import UptimeCheck
        last_uptime_query = await db.execute(
            select(
                UptimeCheck.agent_id,
                UptimeCheck.target_id,
                func.max(UptimeCheck.checked_at).label('last_check')
            )
            .where(UptimeCheck.agent_id.in_(agent_string_ids))  # Use STRING for uptime checks
            .group_by(UptimeCheck.agent_id, UptimeCheck.target_id)
        )
        last_uptime_lookup = {
            (row.agent_id, row.target_id): row.last_check
            for row in last_uptime_query.all()
        }

        # NEW: Calculate target-centric capacity metrics based on ACTUAL assignments
        from models.target import Target
        targets_result = await db.execute(
            select(Target).where(Target.enabled == True)
        )
        all_enabled_targets = targets_result.scalars().all()

        # Calculate theoretical capacity needs
        ANTI_DETECTION_THRESHOLD_MS = 60000  # 60 seconds (rate limit protection)
        import math

        target_capacity_breakdown = {}
        total_agent_slots_ideal = 0  # Ideal if we had unlimited agents

        for target in all_enabled_targets:
            target_period = target.check_period_ms or global_period_ms
            # Ideal agents needed for this target (use ceil to round up)
            ideal_agents_needed = math.ceil(ANTI_DETECTION_THRESHOLD_MS / target_period)
            ideal_agents_needed = max(1, ideal_agents_needed)

            total_agent_slots_ideal += ideal_agents_needed

            # Track breakdown by period
            if target_period not in target_capacity_breakdown:
                target_capacity_breakdown[target_period] = {
                    'count': 0,
                    'ideal_agents_per_target': ideal_agents_needed,
                    'total_slots_used': 0
                }
            target_capacity_breakdown[target_period]['count'] += 1

        # Calculate actual metrics based on target-agent assignments.
        # Reuse all_assignments fetched above (same agent_string_ids filter) instead
        # of re-querying TargetAssignment a second time.
        all_current_assignments = all_assignments

        # Count unique targets that are assigned
        unique_targets_assigned = len(set(a.target_id for a in all_current_assignments))

        # Count unique agents with at least one target
        agents_with_assignments = len(set(a.agent_id for a in all_current_assignments))

        # Total agents available
        total_agents_available = agent_count

        # Capacity headroom depends on the per-target intervals; report simple
        # aggregate metrics here.
        agents_idle = total_agents_available - agents_with_assignments

        # Calculate capacity using the bottleneck agent (Option B)
        # With round-robin distribution, the agent with smallest capacity limits the system

        # Calculate total hardware capacity and find bottleneck
        total_hardware_capacity = sum(a.total_capacity for a in active_agents)
        min_agent_capacity = min((a.total_capacity for a in active_agents), default=0)

        # Calculate current load on the most-loaded agent
        # Simplified: assume even distribution (actual may vary)
        # Each target uses (agents_per_target / total_agents) slots per agent
        current_load_on_bottleneck = 0
        for target in all_enabled_targets:
            target_period = target.check_period_ms or global_period_ms
            min_agents = math.ceil(ANTI_DETECTION_THRESHOLD_MS / target_period)
            min_agents = max(1, min_agents)
            # Load per agent for this target
            load_per_agent = min_agents / total_agents_available if total_agents_available > 0 else 0
            current_load_on_bottleneck += load_per_agent

        remaining_capacity_bottleneck = max(0, min_agent_capacity - current_load_on_bottleneck)

        capacity_for_intervals = {}
        for interval_ms in [10000, 20000, 30000, 60000, 120000]:  # 10s, 20s, 30s, 1min, 2min
            min_agents_per_target = math.ceil(ANTI_DETECTION_THRESHOLD_MS / interval_ms)
            min_agents_per_target = max(1, min_agents_per_target)
            max_agents_per_target = min_agents_per_target * 6  # 6x rule

            # Check if we have enough agents to support this interval
            can_add = total_agents_available >= min_agents_per_target
            agents_needed = max(0, min_agents_per_target - total_agents_available)

            # Calculate max additional targets limited by bottleneck agent
            # Formula: max_targets = remaining_capacity × (total_agents / agents_per_target)
            if can_add and remaining_capacity_bottleneck > 0:
                # Load per agent for each target of this interval
                load_per_agent = min_agents_per_target / total_agents_available
                # How many targets before bottleneck is full
                max_new_targets = int(remaining_capacity_bottleneck / load_per_agent)
            else:
                max_new_targets = 0

            capacity_for_intervals[interval_ms] = {
                'interval_seconds': interval_ms / 1000,
                'min_agents_per_target': min_agents_per_target,
                'max_agents_per_target': max_agents_per_target,
                'can_add_more': can_add,
                'agents_needed': agents_needed,
                'max_additional_targets': max_new_targets,
                # Legacy field for backward compat
                'agents_per_target': min_agents_per_target
            }

        # Update capacity metrics
        current_capacity_used = int(current_load_on_bottleneck)
        remaining_capacity = int(remaining_capacity_bottleneck)

        # NEW: Build detailed agent info with capacity and timeslots
        agent_details = []
        total_capacity = 0
        total_used_slots = 0
        total_available_slots = 0
        time_slots = []  # Legacy format for backward compat

        for agent in active_agents:
            is_online = (
                agent.last_seen_at is not None and
                agent.last_seen_at.replace(tzinfo=timezone.utc) > threshold
            )

            # Parse capacity and hardware from meta
            meta = agent.meta or {}
            capacity_info = meta.get('capacity', {})
            hardware_info = meta.get('hardware', {})

            # Get assigned time slot offsets
            # Check for period-specific offsets first (new capacity-aware system)
            assigned_offsets_by_period = meta.get('assigned_time_slot_offsets_by_period')
            if assigned_offsets_by_period:
                # Using period-specific offsets - will extract per period below
                using_period_specific = True
                assigned_offsets = None  # Not used in period-specific mode
            else:
                # Legacy mode - single set of offsets for all periods
                using_period_specific = False
                assigned_offsets = meta.get('assigned_time_slot_offsets', [agent.time_slot_offset_ms])

            # OPTIMIZED: Get assignments from pre-fetched data (use STRING agent_id)
            assignments = assignments_by_agent.get(agent.agent_id, [])
            target_count = len(assignments)

            # OPTIMIZED: Group targets by period using pre-fetched data
            period_groups = {}
            for assignment in assignments:
                target = assignment.target
                if target:
                    period = target.check_period_ms or global_period_ms
                    if period not in period_groups:
                        period_groups[period] = []
                    period_groups[period].append({
                        'id': target.id,
                        'url': target.url,
                        'checkPeriodMs': target.check_period_ms,
                    })

            # Build timeslot info - one entry PER PERIOD GROUP
            slots_info = []
            capacity_per_slot = capacity_info.get('targets_per_timeslot', 0)
            global_slot_index = 0  # Sequential slot numbering across all period groups

            for period_idx, (period_ms, targets) in enumerate(sorted(period_groups.items())):
                # Get offsets for this specific period
                if using_period_specific:
                    # Use period-specific offsets from meta
                    period_offsets_list = assigned_offsets_by_period.get(str(period_ms), [0])
                    period_offsets = period_offsets_list if isinstance(period_offsets_list, list) else [period_offsets_list]
                else:
                    # Legacy mode - calculate staggered offsets
                    num_period_groups = len(period_groups)
                    if num_period_groups > 1 and len(assigned_offsets) > 0:
                        # Calculate slot duration
                        if len(assigned_offsets) > 1:
                            slot_duration = assigned_offsets[1] - assigned_offsets[0]
                        else:
                            slot_duration = global_period_ms
                        offset_shift = period_idx * (slot_duration // (num_period_groups + 1))
                        period_offsets = [offset + offset_shift for offset in assigned_offsets]
                    else:
                        period_offsets = assigned_offsets if assigned_offsets else [0]

                # Add timeslot entry for each offset in this period group
                for idx, offset_ms in enumerate(period_offsets):
                    # Calculate targets per slot for this period group
                    targets_per_slot_in_period = len(targets) // len(period_offsets) if len(period_offsets) > 0 else 0

                    # Distribute targets to this specific slot (round-robin)
                    slot_targets = [targets[i] for i in range(idx, len(targets), len(period_offsets))]

                    # Find the most recent check for targets in THIS specific slot
                    # Check BOTH reports (change detection) AND uptime_checks (uptime monitoring)
                    last_check_at = None
                    if slot_targets:
                        max_time = None
                        for target in slot_targets:
                            # Check reports table (uses INTEGER agent.id)
                            check_time_report = last_reports_lookup.get((agent.id, target['id']))
                            if check_time_report and (max_time is None or check_time_report > max_time):
                                max_time = check_time_report

                            # Check uptime_checks table (uses STRING agent.agent_id)
                            check_time_uptime = last_uptime_lookup.get((agent.agent_id, target['id']))
                            if check_time_uptime and (max_time is None or check_time_uptime > max_time):
                                max_time = check_time_uptime
                        last_check_at = max_time.isoformat() if max_time else None

                    # Calculate load percentage for this timeslot
                    load_percent = (targets_per_slot_in_period / capacity_per_slot * 100) if capacity_per_slot > 0 else 0

                    slots_info.append({
                        'slotIndex': global_slot_index,  # Use sequential numbering
                        'offsetMs': offset_ms,
                        'periodMs': period_ms,  # NEW: indicate which period this slot belongs to
                        'targetsAssigned': targets_per_slot_in_period,
                        'capacityPerSlot': capacity_per_slot,
                        'loadPercent': round(load_percent, 2),
                        'lastCheckAt': last_check_at,
                        'type': 'target',  # Normal target check slot
                    })
                    global_slot_index += 1  # Increment for next slot

            # Add scheduled workflows for Playwright-capable agents
            if agent.has_playwright:
                from models.automation_workflow import AutomationWorkflow

                # Get scheduled workflows
                scheduled_workflows_result = await db.execute(
                    select(AutomationWorkflow)
                    .where(
                        AutomationWorkflow.schedule_enabled == True,
                        AutomationWorkflow.schedule_interval_ms.isnot(None)
                    )
                )
                scheduled_workflows = scheduled_workflows_result.scalars().all()

                # Calculate offset for this agent (same as done in agents.py distribution)
                playwright_agents = [a for a in active_agents if a.has_playwright]
                playwright_count = len(playwright_agents)
                agent_index = 0
                for i, a in enumerate(playwright_agents):
                    if a.agent_id == agent.agent_id:
                        agent_index = i
                        break

                for workflow in scheduled_workflows:
                    # Calculate time offset for anti-detection
                    if playwright_count > 1:
                        offset_ms = (workflow.schedule_interval_ms // playwright_count) * agent_index
                    else:
                        offset_ms = 0

                    slots_info.append({
                        'slotIndex': global_slot_index,
                        'offsetMs': offset_ms,
                        'periodMs': workflow.schedule_interval_ms,
                        'targetsAssigned': 0,
                        'capacityPerSlot': 1,  # Workflows are single execution
                        'loadPercent': 0,
                        'lastCheckAt': workflow.last_scheduled_at.isoformat() if workflow.last_scheduled_at else None,
                        'type': 'playwright',  # Playwright workflow slot
                        'workflowId': workflow.id,
                        'workflowName': workflow.name,
                    })
                    global_slot_index += 1

            # Calculate capacity metrics
            agent_total_capacity = agent.total_capacity
            timeslots_needed = capacity_info.get('timeslots_needed', 1)

            # Calculate actual number of slots assigned (from slots_info built above)
            num_slots_assigned = len(slots_info)

            total_capacity += agent_total_capacity
            total_used_slots += num_slots_assigned
            total_available_slots += timeslots_needed

            # Add to agent details
            agent_details.append({
                'agentId': agent.agent_id,
                'platform': agent.platform.value,
                'status': agent.status.value,
                'lastSeenAt': agent.last_seen_at.isoformat() if agent.last_seen_at else None,
                'online': is_online,

                # Capacity metrics
                'capacity': {
                    'totalCapacity': agent_total_capacity,
                    'timeslotsNeeded': timeslots_needed,
                    'targetsPerSlot': capacity_info.get('targets_per_timeslot', 0),
                    'checkMode': capacity_info.get('check_mode', 'http'),
                    'avgLatencyMs': capacity_info.get('avg_check_latency_ms', 0),
                    'parallelWorkers': capacity_info.get('parallel_workers', 0),
                },

                # Hardware info
                'hardware': {
                    'cpuCores': hardware_info.get('cpu_cores', 0),
                    'cpuThreads': hardware_info.get('cpu_threads', 0),
                    'ramTotalGb': hardware_info.get('ram_total_gb', 0),
                    'ramAvailableGb': hardware_info.get('ram_available_gb', 0),
                    'platform': hardware_info.get('platform', 'unknown'),
                },

                # Timeslot details
                'timeslots': slots_info,
                'timeslotsAssigned': num_slots_assigned,
                'targetsAssigned': target_count,
            })

            # Legacy time_slots format
            time_slots.append({
                "agentId": agent.agent_id,
                "platform": agent.platform.value,
                "offsetMs": agent.time_slot_offset_ms,
                "syncEnabled": agent.sync_enabled,
                "online": is_online,
                "lastSeenAt": agent.last_seen_at.isoformat() if agent.last_seen_at else None,
            })

        # Calculate system load percentage
        system_load_percent = (total_used_slots / total_available_slots * 100) if total_available_slots > 0 else 0

        # Calculate sync status
        sync_enabled_count = sum(1 for agent in active_agents if agent.sync_enabled)
        sync_disabled_count = agent_count - sync_enabled_count

        return {
            "globalPeriodMs": global_period_ms,
            "slotDurationMs": slot_duration,
            "agentPeriodMs": agent_period_ms,
            "timeSlotMode": time_slot_mode,
            "activeAgents": agent_count,
            "onlineAgents": sum(1 for slot in time_slots if slot["online"]),

            # NEW: System capacity metrics (TARGET-CENTRIC MODEL)
            "systemCapacity": {
                # Clear target-centric metrics
                "agentSlotsTotal": total_agents_available,
                "agentSlotsUsed": agents_with_assignments,
                "agentSlotsIdle": agents_idle,
                "agentSlotsIdeal": total_agent_slots_ideal,
                "agentSlotsRemaining": agents_idle,
                "uniqueTargetsActive": unique_targets_assigned,
                "uniqueTargetsEnabled": len(all_enabled_targets),
                "hasCapacityWarning": total_agent_slots_ideal > total_agents_available,
                "agentDeficit": max(0, total_agent_slots_ideal - total_agents_available),
                "targetCapacityBreakdown": target_capacity_breakdown,
                "capacityForIntervals": capacity_for_intervals,

                # Hardware capacity metrics
                "totalHardwareCapacity": total_hardware_capacity,
                "currentCapacityUsed": current_capacity_used,
                "remainingCapacity": remaining_capacity,
                "capacityUtilization": round((current_capacity_used / total_hardware_capacity * 100) if total_hardware_capacity > 0 else 0, 2),

                # Legacy metrics for backward compat
                "totalCapacity": total_capacity,
                "totalAvailableSlots": total_available_slots,
                "totalUsedSlots": total_used_slots,
                "systemLoadPercent": round((agents_with_assignments / total_agents_available * 100) if total_agents_available > 0 else 0, 2),
                "avgCapacityPerAgent": round(total_capacity / agent_count, 2) if agent_count > 0 else 0,
            },

            # NEW: Detailed agent information
            "agentDetails": agent_details,

            # Legacy fields for backward compatibility
            "timeSlots": time_slots,
            "syncStatus": {
                "enabled": sync_enabled_count,
                "disabled": sync_disabled_count,
            },
        }

    except Exception as e:
        logger.error(f"Error getting monitoring data: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get monitoring data",
        )


@router.post(
    "/redistribute",
    summary="Redistribute Targets",
    description="Redistribute all targets across active agents. Requires admin role.",
)
async def redistribute_targets(
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """
    Redistribute all enabled targets across active agents.

    Uses weighted allocation based on agent weights:
    - Clears existing assignments
    - Distributes targets using weighted round-robin
    - Agents with higher weights get more targets
    - Sets force_config_update flag on all agents

    Requires admin role.
    """
    # These routes mutate GLOBAL scheduling config / every agent, so they
    # require a platform admin.
    if not current_api_key.get("is_platform_admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can redistribute targets",
        )

    try:
        from services.capacity_aware_distributor import CapacityAwareDistributor
        from models.config import Config

        # Get global period for capacity calculations
        global_period_result = await db.execute(
            select(Config).where(Config.key == "global_period_ms")
        )
        global_period_config = global_period_result.scalar_one_or_none()
        global_period_ms = int(global_period_config.value) if global_period_config else 10000

        distributor = CapacityAwareDistributor(db)
        stats = await distributor.distribute_timeslots_and_targets(global_period_ms)

        # Audit log
        audit = EventsAudit(
            actor=current_api_key.get("label"),
            action="schedule.redistribute",
            details=stats,
            at=datetime.utcnow(),
        )
        if audit is not None:  # no audit store in this build → EventsAudit() is None
            db.add(audit)
        await db.commit()

        logger.info(
            f"Target redistribution completed by {current_api_key.get('label')}: "
            f"{stats.get('targets_assigned', 0)} assignments across {stats.get('agents_total', 0)} agents"
        )

        return {
            "status": "success",
            "message": f"Redistributed {stats.get('targets_assigned', 0)} assignments across {stats.get('agents_total', 0)} agents",
            "stats": stats,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error redistributing targets: {e}")
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to redistribute targets",
        )


@router.post(
    "/time-slot-mode",
    summary="Set Time Slot Mode",
    description="Switch between distributed and rolling time slot modes. Requires admin role.",
)
async def set_time_slot_mode(
    mode: str,
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """
    Set time slot distribution mode.

    Modes:
    - distributed: Each agent checks every global_period_ms (redundancy/quorum)
    - rolling: Each agent checks every (global_period_ms × agent_count) (load spreading)

    Automatically redistributes time slots when mode changes.
    """
    # These routes mutate GLOBAL scheduling config / every agent, so they
    # require a platform admin.
    if not current_api_key.get("is_platform_admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can change time slot mode",
        )

    # Validate mode
    if mode not in ["distributed", "rolling"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Mode must be 'distributed' or 'rolling'",
        )

    try:
        # Update or create config
        result = await db.execute(
            select(Config).where(Config.key == "time_slot_mode")
        )
        config = result.scalar_one_or_none()

        old_mode = config.value if config else "distributed"

        if config:
            config.value = mode
        else:
            config = Config(key="time_slot_mode", value=mode)
            db.add(config)

        # Audit log
        audit = EventsAudit(
            actor=current_api_key.get("label"),
            action="schedule.set_time_slot_mode",
            details={"old_mode": old_mode, "new_mode": mode},
            at=datetime.utcnow(),
        )
        if audit is not None:  # no audit store in this build → EventsAudit() is None
            db.add(audit)

        await db.commit()

        logger.info(f"Time slot mode changed from {old_mode} to {mode} by {current_api_key.get('label')}")

        # Get global period for redistribution
        period_result = await db.execute(
            select(Config).where(Config.key == "global_period_ms")
        )
        period_config = period_result.scalar_one_or_none()
        global_period_ms = int(period_config.value) if period_config else settings.global_period_ms

        # Trigger time slot redistribution with new mode
        from routers.agents import redistribute_time_slots
        stats = await redistribute_time_slots(db, global_period_ms)

        logger.info(f"Time slots redistributed in {mode} mode: {stats}")

        return {
            "status": "success",
            "message": f"Time slot mode set to '{mode}' and agents redistributed",
            "old_mode": old_mode,
            "new_mode": mode,
            "redistribution_stats": stats,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error setting time slot mode: {e}")
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to set time slot mode",
        )
