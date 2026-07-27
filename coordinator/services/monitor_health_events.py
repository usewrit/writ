"""Dispatch monitor-health transitions (down / stale / recovered) as trigger events.

A thin, fail-soft wrapper around UnifiedTriggerService.process_monitor_health so the
report-ingest hot path and the background stale sweep can fire health automations
without ever breaking the check pipeline if the trigger engine hiccups.

The transition itself is detected in services.monitoring_state (record_uptime /
record_content return the edge; sweep_stale finds newly-stale targets).
"""
import logging
from typing import Any, Dict, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from models.target import Target

logger = logging.getLogger(__name__)

# Event names — kept in sync with services.monitoring_state and the FE block catalog.
MONITOR_DOWN = "monitor_down"
MONITOR_STALE = "monitor_stale"
MONITOR_RECOVERED = "monitor_recovered"
MONITOR_HEALTH_EVENTS = {MONITOR_DOWN, MONITOR_STALE, MONITOR_RECOVERED}


async def dispatch_monitor_health(
    db: AsyncSession,
    target: Target,
    event_type: str,
    health_context: Optional[Dict[str, Any]] = None,
) -> None:
    """Fire monitor-health automations for `target`. Never raises — a failure here
    must not fail the caller's check report or sweep."""
    if event_type not in MONITOR_HEALTH_EVENTS:
        return
    try:
        from services.unified_trigger_service import get_unified_trigger_service

        service = get_unified_trigger_service(db)
        executions = await service.process_monitor_health(target, event_type, health_context)
        if executions:
            logger.info(
                f"[MonitorHealth] {event_type} on target {target.id}: "
                f"dispatched {len(executions)} automation(s)"
            )
    except Exception as e:  # noqa: BLE001 — fail-soft by design
        logger.error(
            f"[MonitorHealth] Failed to dispatch {event_type} for target "
            f"{getattr(target, 'id', '?')}: {e}",
            exc_info=True,
        )
