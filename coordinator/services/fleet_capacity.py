"""Fleet capacity advisory — explains the agent-count → min-check-interval limit.

Anti-detection floors each agent at one check per ``ANTI_DETECTION_THRESHOLD_MS``
(60s) per target. A target's configured interval is only achieved when a FLEET of
staggered agents cover it: to check every T (< 60s) you need ``ceil(60000/T)``
agents. So with N connected agents the FASTEST any monitor can effectively run is
``60000/N`` ms, and a monitor configured faster than that quietly runs slower
until more agents connect (the distributor computes ``effective = max(configured,
60000/coverage)``). This module turns that relationship into numbers + copy the UI
advertises, so users understand the limit instead of silently falling behind.
"""
import math
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.target import Target
from services.capacity_aware_distributor import ANTI_DETECTION_THRESHOLD_MS

# Intervals the creation UI offers as presets — reported with their agent needs.
PRESET_INTERVALS_MS = [10000, 30000, 60000, 300000, 900000, 3600000, 86400000]


def agents_required_for(interval_ms: int) -> int:
    """Staggered agents needed to actually achieve `interval_ms` for one target.

    Intervals at/above the 60s per-agent floor need just one agent; below it, one
    agent per 60s of speed-up (``ceil(60000 / interval)``)."""
    if interval_ms >= ANTI_DETECTION_THRESHOLD_MS:
        return 1
    return math.ceil(ANTI_DETECTION_THRESHOLD_MS / max(1, interval_ms))


def min_interval_for(agents_online: int) -> int:
    """Fastest effective interval a single target can reach with this many agents."""
    return math.ceil(ANTI_DETECTION_THRESHOLD_MS / max(1, agents_online))


def effective_interval_ms(configured_ms: int, agents_online: int) -> int:
    """Cadence a monitor actually runs at given the fleet — mirrors the
    distributor's ``max(configured, 60000/coverage)`` with coverage bounded by the
    number of connected agents."""
    return max(configured_ms, min_interval_for(agents_online))


def _fmt(ms: int) -> str:
    if ms % 86400000 == 0:
        return f"{ms // 86400000}d"
    if ms % 3600000 == 0:
        return f"{ms // 3600000}h"
    if ms % 60000 == 0:
        return f"{ms // 60000}m"
    return f"{ms // 1000}s"


def _explain(agents_online: int, min_iv: int) -> str:
    if agents_online == 0:
        return (
            "No agents are connected, so no checks can run yet. Connect an agent "
            "from the Fleet page — each agent can check a target up to once every "
            "60s, and staggering more agents unlocks faster intervals."
        )
    return (
        f"{agents_online} agent(s) connected. A single agent checks a target at "
        f"most once per 60s (anti-detection), so faster intervals need a fleet of "
        f"staggered agents — roughly one more agent per 60s of speed-up. Your "
        f"fleet supports checks about every {_fmt(min_iv)}; a monitor set faster "
        f"than that runs at {_fmt(min_iv)} until you add more agents."
    )


def capacity_warning_for(
    configured_ms: Optional[int],
    agents_online: int,
    global_period_ms: int = 60000,
) -> Optional[str]:
    """Soft warning when a monitor's configured interval can't be met by the
    current fleet — surfaced on target create/update. ``None`` when it's fine."""
    p = configured_ms or global_period_ms
    if agents_online == 0:
        return (
            "No agents are connected yet, so this monitor won't run until you "
            "connect one from the Fleet page."
        )
    eff = effective_interval_ms(p, agents_online)
    if eff > p:
        need = agents_required_for(p)
        return (
            f"Your fleet of {agents_online} agent(s) supports checks about every "
            f"{_fmt(eff)}. This monitor is set to {_fmt(p)}, which needs {need} "
            f"staggered agents — it will run every {_fmt(eff)} until you add more."
        )
    return None


def current_agents_online() -> int:
    """Number of live WS-connected fleet agents (source of truth for capacity)."""
    from routers.user_recorder_ws import get_all_connected_recorders
    return len(get_all_connected_recorders())


async def compute_capacity(db: AsyncSession) -> dict:
    """Full fleet-capacity advisory for the UI."""
    from services.monitoring_dispatch import _config_int

    agents_online = current_agents_online()
    floor = ANTI_DETECTION_THRESHOLD_MS
    min_iv = min_interval_for(agents_online)
    global_period_ms = await _config_int(db, "global_period_ms", 60000)

    rows = (
        await db.execute(select(Target.check_period_ms).where(Target.enabled == True))  # noqa: E712
    ).all()
    active_monitors = len(rows)
    under_provisioned = 0
    for (period,) in rows:
        p = period or global_period_ms
        if agents_online == 0 or effective_interval_ms(p, agents_online) > p:
            under_provisioned += 1

    presets = [
        {
            "interval_ms": ms,
            "agents_required": agents_required_for(ms),
            "feasible": agents_online >= agents_required_for(ms),
        }
        for ms in PRESET_INTERVALS_MS
    ]

    # Best-effort enrichment from the scheduler's last redistribution (authoritative
    # slot capacity / headroom). May be None before the first reconcile.
    fleet_stats = None
    try:
        from services.scheduler import get_last_reconcile_stats
        fleet_stats = get_last_reconcile_stats()
    except Exception:
        fleet_stats = None

    return {
        "agents_online": agents_online,
        "per_agent_floor_ms": floor,
        "min_interval_ms": min_iv,
        "active_monitors": active_monitors,
        "under_provisioned_monitors": under_provisioned,
        "presets": presets,
        "fleet_stats": fleet_stats,
        "explanation": _explain(agents_online, min_iv),
    }
