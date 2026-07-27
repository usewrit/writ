"""
Publishes agent configs and secrets to Redis for relay nodes.

Called by the coordinator whenever:
- Agent registers (secrets + initial FULL config snapshot + version)
- Target distribution changes (republish ONLY the affected agents' snapshots)
- A detected change re-signals other agents (delta distribution = republish them)

Nodes read these values to serve agents without DB access. The poll path
(node OR coordinator-fallback) NEVER touches Postgres — it reads the version
(``ps:agent_config_ver``) and, only on a version miss, the FULL snapshot
(``ps:agent_configs``) from Redis.

See the AGENT POLL CONTRACT (§2, §3, §4.1) — this module is the coordinator-side
publisher of that contract.
"""
import hashlib
import json
import logging
from typing import Optional, List
import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

AGENT_SECRETS_KEY = "ps:agent_secrets"
AGENT_CONFIGS_KEY = "ps:agent_configs"
AGENT_CONFIG_VER_KEY = "ps:agent_config_ver"
AGENT_NODE_MAP_KEY = "ps:agent_node_map"


def compute_config_version(snapshot: dict) -> str:
    """Content-addressed version for a FULL config snapshot (contract §4.1).

    ``config_version = "v" + sha256(canonical_json(snapshot_without_version))[:16]``

    Idempotent: identical config ⇒ identical version, so a redundant republish
    never spuriously bumps the agent (which would cost it a free full-snapshot
    download on its next poll).
    """
    body = {k: v for k, v in snapshot.items() if k != "config_version"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return "v" + digest[:16]


class NodeConfigPublisher:
    """Publish agent configuration and secrets to Redis for node consumption."""

    def __init__(self, redis_client: aioredis.Redis):
        self._redis = redis_client

    async def publish_secret(self, agent_id: str, encrypted_secret: str):
        """Store agent's encrypted secret in Redis for node HMAC verification."""
        await self._redis.hset(AGENT_SECRETS_KEY, agent_id, encrypted_secret)

    async def publish_config(self, agent_id: str, snapshot: dict) -> str:
        """Write a FULL config snapshot + its version to Redis in ONE pipeline.

        The snapshot's ``config_version`` field is (re)computed here so the two
        Redis keys (``ps:agent_configs`` / ``ps:agent_config_ver``) can never
        skew. Returns the version written.

        The node compares ``ps:agent_config_ver[agent_id]`` against the agent's
        sent ``config_version`` and only fetches the big snapshot on a miss.
        """
        version = compute_config_version(snapshot)
        snapshot = dict(snapshot)
        snapshot["config_version"] = version

        pipe = self._redis.pipeline(transaction=True)
        pipe.hset(AGENT_CONFIGS_KEY, agent_id, json.dumps(snapshot))
        pipe.hset(AGENT_CONFIG_VER_KEY, agent_id, version)
        await pipe.execute()
        return version

    async def get_config(self, agent_id: str) -> Optional[dict]:
        """Read the FULL snapshot for an agent (poll-path: version miss only)."""
        raw = await self._redis.hget(AGENT_CONFIGS_KEY, agent_id)
        if not raw:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode()
        return json.loads(raw)

    async def get_config_version(self, agent_id: str) -> Optional[str]:
        """Read the current server-side config version (single HGET, poll-path)."""
        ver = await self._redis.hget(AGENT_CONFIG_VER_KEY, agent_id)
        if ver is None:
            return None
        return ver.decode() if isinstance(ver, bytes) else ver

    async def publish_redirect(self, agent_id: str, new_node_url: str, reason: str = "rebalance"):
        """Tell a node to redirect an agent to a different node."""
        await self._redis.publish(
            f"ps:redirect:{agent_id}",
            json.dumps({"new_node_url": new_node_url, "reason": reason}),
        )

    async def revoke_agent(self, agent_id: str):
        """Remove agent from Redis (revoked or destroyed)."""
        pipe = self._redis.pipeline(transaction=True)
        pipe.hdel(AGENT_SECRETS_KEY, agent_id)
        pipe.hdel(AGENT_CONFIGS_KEY, agent_id)
        pipe.hdel(AGENT_CONFIG_VER_KEY, agent_id)
        pipe.hdel(AGENT_NODE_MAP_KEY, agent_id)
        await pipe.execute()
        await self._redis.publish(f"ps:agent_revoked:{agent_id}", "revoked")

    async def assign_to_node(self, agent_id: str, node_id: str):
        """Record which node an agent is assigned to."""
        await self._redis.hset(AGENT_NODE_MAP_KEY, agent_id, node_id)

    async def get_assigned_node_id(self, agent_id: str) -> Optional[str]:
        """Return the node_id an agent is mapped to (or None)."""
        nid = await self._redis.hget(AGENT_NODE_MAP_KEY, agent_id)
        if nid is None:
            return None
        return nid.decode() if isinstance(nid, bytes) else nid

    async def get_node_status(self, node_id: str) -> Optional[dict]:
        """Read a node's status hash (decoded), or None if expired/missing."""
        status = await self._redis.hgetall(f"ps:node:{node_id}:status")
        if not status:
            return None
        decoded = {}
        for sk, sv in status.items():
            sk = sk.decode() if isinstance(sk, bytes) else sk
            sv = sv.decode() if isinstance(sv, bytes) else sv
            decoded[sk] = sv
        return decoded

    async def get_healthy_node_url_for_agent(self, agent_id: str) -> Optional[str]:
        """Return a reachable node URL to drain a fallback agent back onto (§4.2.3).

        Prefer the agent's already-assigned node if it is still healthy
        (status hash present, has a URL, not over capacity); otherwise pick the
        globally best node.
        """
        node_id = await self.get_assigned_node_id(agent_id)
        if node_id:
            status = await self.get_node_status(node_id)
            if status and status.get("url"):
                try:
                    if int(status.get("agent_count", "0")) < int(status.get("max_agents", "100")):
                        return status["url"]
                except (TypeError, ValueError):
                    return status["url"]
        best = await self.get_best_node()
        return best["url"] if best else None

    async def get_best_node(self) -> Optional[dict]:
        """Find the node with most available capacity.

        Returns {"node_id": str, "url": str} or None if no nodes available.
        """
        best_node = None
        best_available = -1

        async for key in self._redis.scan_iter(match="ps:node:*:status"):
            k = key.decode() if isinstance(key, bytes) else key
            # Extract node_id from key pattern ps:node:{node_id}:status
            parts = k.split(":")
            if len(parts) != 4:
                continue
            node_id = parts[2]

            status = await self._redis.hgetall(k)
            # Decode bytes
            decoded = {}
            for sk, sv in status.items():
                sk = sk.decode() if isinstance(sk, bytes) else sk
                sv = sv.decode() if isinstance(sv, bytes) else sv
                decoded[sk] = sv

            agent_count = int(decoded.get("agent_count", "0"))
            max_agents = int(decoded.get("max_agents", "100"))
            url = decoded.get("url", "")
            available = max_agents - agent_count

            if available > best_available and url:
                best_available = available
                best_node = {"node_id": node_id, "url": url}

        return best_node if best_node and best_available > 0 else None
