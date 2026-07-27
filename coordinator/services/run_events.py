"""Realtime run-state events.

Lightweight "a run changed" signal published on run lifecycle transitions
(start / end) so the dashboard reacts instantly instead of waiting out its
poll interval. The payload is a thin delta — the client treats it only as a
nudge to refetch the unified ``/runs`` feed.

This coordinator is single-owner, so there is one events channel for the
whole instance. Transport is the coordinator's in-process fakeredis pub/sub.
That bus is per-worker, so with several uvicorn workers a publish only
reaches SSE subscribers on the SAME worker; the frontend's while-live poll is
the cross-worker safety net. Because realtime is an accelerator over polling,
a missed publish is never fatal — and every emit is best-effort, swallowing
all errors so it can never break the run path.
"""
import json
import logging
from typing import Optional

from utils.redis_client import get_redis

logger = logging.getLogger(__name__)

# Single-owner coordinator: one instance-wide run-events channel.
RUN_EVENTS_CHANNEL = "runs:events"


async def emit_run_event(
    *,
    run_type: str,
    row_id,
    status: Optional[str] = None,
    event: str = "updated",
    extra: Optional[dict] = None,
) -> None:
    """Publish a run-state delta to the realtime channel (best-effort).

    ``extra`` merges additional fields into the payload — it carries live counters
    (e.g. a crawl's pages_done/discovered/agents-active) so a subscriber can render
    progress DIRECTLY from the push with no follow-up refetch. Keep it small; it
    rides every emit.
    """
    try:
        body = {
            "id": f"{run_type}-{row_id}",
            "run_type": run_type,
            "status": status,
            "event": event,
        }
        if extra:
            body.update(extra)
        payload = json.dumps(body, default=str)
        await get_redis().publish(RUN_EVENTS_CHANNEL, payload)
    except Exception:
        logger.debug("emit_run_event failed", exc_info=True)
