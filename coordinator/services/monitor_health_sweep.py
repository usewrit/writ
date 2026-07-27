"""Periodic sweep that fires `monitor_stale` when a monitored target stops reporting.

Unlike down/recovered (edge-detected inline in the report-ingest path), 'stale' has
no inbound event — the agent simply stops reporting. This loop, scoped to ONLY the
targets that actually have an enabled monitor_stale automation, compares each
target's last check against its expected cadence (services.monitoring_state.sweep_stale)
and fires monitor_stale on the crossing edge. A later fresh check clears the marker
and yields monitor_recovered via the ingest path.
"""
import asyncio
import logging
from typing import Set

from sqlalchemy import select

from models.trigger_rule import TriggerRule
from models.target import Target
from services.monitoring_state import sweep_stale
from services.monitor_health_events import dispatch_monitor_health, MONITOR_STALE

logger = logging.getLogger(__name__)

_SWEEP_INTERVAL_SECONDS = 60
_STARTUP_DELAY_SECONDS = 30


def _stale_target_ids(rules) -> Set[int]:
    """Target ids a monitor_stale rule watches — the target_id column plus any
    monitor_stale event block that pins a target in the block tree."""
    ids: Set[int] = set()
    for r in rules:
        if r.target_id:
            ids.add(int(r.target_id))
        blocks = r.blocks if isinstance(r.blocks, list) else []
        for b in blocks:
            if not isinstance(b, dict):
                continue
            if b.get("type") != "event":
                continue
            if (b.get("blockType") or b.get("block_type")) != MONITOR_STALE:
                continue
            cfg = b.get("config") or {}
            if isinstance(cfg, dict) and cfg.get("target_id"):
                ids.add(int(cfg["target_id"]))
    return ids


async def _run_once() -> None:
    from database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        rules = (await db.execute(
            select(TriggerRule).where(
                TriggerRule.enabled == True,  # noqa: E712
                TriggerRule.event_type == MONITOR_STALE,
            )
        )).scalars().all()
        target_ids = _stale_target_ids(rules)
        if not target_ids:
            return

        targets = (await db.execute(
            select(Target).where(
                Target.id.in_(target_ids),
                Target.enabled == True,  # noqa: E712 — a paused monitor isn't "stale"
            )
        )).scalars().all()
        if not targets:
            return

        newly_stale = await sweep_stale([(t.id, t.check_period_ms) for t in targets])
        if not newly_stale:
            return

        by_id = {t.id: t for t in targets}
        for tid in newly_stale:
            target = by_id.get(tid)
            if target is not None:
                await dispatch_monitor_health(db, target, MONITOR_STALE, {"state": "stale"})


async def monitor_health_sweep_loop() -> None:
    """Background loop — registered in main.lifespan."""
    await asyncio.sleep(_STARTUP_DELAY_SECONDS)
    while True:
        try:
            await _run_once()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — never let the loop die
            logger.error(f"[MonitorHealthSweep] sweep failed: {e}", exc_info=True)
        await asyncio.sleep(_SWEEP_INTERVAL_SECONDS)
