"""
Coordinator-side report relay + liveness bump for the /poll FALLBACK path.

When an agent falls back to the coordinator (no node available, or its node
died), the coordinator's /api/agents/poll must behave EXACTLY like a node:
- relay change reports onto a Redis stream the same consumer drains, and
- keep the agent's liveness fresh in Redis.

To satisfy "ONE endpoint implemented identically" (contract §8) without touching
Postgres on the hot path, the coordinator uses a node_id of ``"coordinator"``:
its stream is ``ps:reports:coordinator`` and its liveness hash is
``ps:node:coordinator:agent_seen`` — both already understood by the existing
NodeReportConsumer (it scans ``ps:reports:*``) and the Redis->DB liveness
bulk-sync (it scans ``ps:node:*:agent_seen``).

This mirrors the hosted node's report relay and the node's
HeartbeatTracker agent_seen write, but on the coordinator side.
"""
import json
import logging
import time
from typing import List, Dict, Any
import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

COORDINATOR_NODE_ID = "coordinator"
COORDINATOR_STREAM_KEY = f"ps:reports:{COORDINATOR_NODE_ID}"
COORDINATOR_SEEN_KEY = f"ps:node:{COORDINATOR_NODE_ID}:agent_seen"
FAST_REPORTS_CHANNEL = "ps:fast_reports"
FAST_THRESHOLD_MS = 30000
# Liveness hash TTL — matches the node's per-agent agent_seen TTL (§4.3).
SEEN_TTL_S = 60


async def relay_reports_coordinator(
    redis_client: aioredis.Redis,
    agent_id: str,
    reports: List[Dict[str, Any]],
    timestamp: str,
) -> int:
    """XADD change reports onto ps:reports:coordinator (durable, exactly-once via
    the coordinator consumer group). Mirrors the node ReportRelay envelope so the
    SAME NodeReportConsumer processes them. Returns count relayed.

    Also PUBLISHes to ps:fast_reports when any report is a fast target
    (check_period_ms <= 30s), identical to the node.
    """
    if not reports:
        return 0

    message = {
        "agent_id": agent_id,
        "node_id": COORDINATOR_NODE_ID,
        "timestamp": timestamp,
        "reports": reports,
        "report_count": len(reports),
        "relayed_at": time.time(),
    }
    payload = json.dumps(message)

    await redis_client.xadd(COORDINATOR_STREAM_KEY, {"data": payload})

    has_fast = any(
        r.get("check_period_ms") and r["check_period_ms"] <= FAST_THRESHOLD_MS
        for r in reports
    )
    if has_fast:
        await redis_client.publish(FAST_REPORTS_CHANNEL, payload)

    return len(reports)


async def bump_coordinator_agent_seen(redis_client: aioredis.Redis, agent_id: str) -> None:
    """Refresh the agent's last_seen in ps:node:coordinator:agent_seen (§4.3).

    The Redis->DB liveness bulk-sync (~60s) reads ps:node:*:agent_seen, so an
    agent polling the coordinator-fallback stays "online" in the Fleet view and
    is not false-reaped — without a per-poll Postgres write.
    """
    await redis_client.hset(COORDINATOR_SEEN_KEY, agent_id, str(int(time.time())))
    await redis_client.expire(COORDINATOR_SEEN_KEY, SEEN_TTL_S)
