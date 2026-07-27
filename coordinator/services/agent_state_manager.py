"""
Agent State Manager - Track agent assignments and detect changes

FIXED ISSUES:
- ✅ Uses Redis (not agent.meta JSON) for scalability
- ✅ Persistent version counter in Redis
- ✅ Fast hash-based change detection
- ✅ Fallback to database if Redis unavailable
"""

import logging
import hashlib
import json
from typing import List, Dict, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete
import redis.asyncio as redis

from models.target import Target
from models.agent import Agent

logger = logging.getLogger(__name__)


class AgentStateManager:
    """
    Manages agent state for incremental update detection.

    Uses Redis for fast state storage and change detection.
    Falls back to database if Redis unavailable.
    """

    def __init__(self, db: AsyncSession, redis_client: Optional[redis.Redis] = None):
        """
        Initialize state manager.

        Args:
            db: Database session
            redis_client: Optional Redis client (recommended)
        """
        self.db = db
        self.redis = redis_client
        self._use_redis = redis_client is not None

    async def compute_incremental_update(
        self,
        agent_id: str,
        new_assignments: List[Target]
    ) -> Optional[Dict]:
        """
        Compare current agent state with new assignments.

        Returns:
            Dict with incremental update, or None for full rebuild:
            {
                'version': int,
                'changes': [
                    {'operation': 'add', 'target_id': int, 'target_data': {...}},
                    {'operation': 'remove', 'target_id': int},
                    {'operation': 'modify', 'target_id': int, 'target_data': {...}}
                ],
                'full_rebuild_required': False
            }
        """
        # Get current state
        current_state = await self._get_agent_state(agent_id)

        logger.debug(f"[HOTRELOAD] Agent {agent_id} state: {len(current_state)} targets in Redis")

        # Build new state (hash-based for fast comparison)
        new_state = {
            str(t.id): self._hash_target(t)
            for t in new_assignments
        }

        current_ids = set(current_state.keys())
        new_ids = set(new_state.keys())

        # Detect changes
        added_ids = new_ids - current_ids
        removed_ids = current_ids - new_ids

        # Check for modifications (hash mismatch)
        modified_ids = set()
        for target_id in (current_ids & new_ids):
            if current_state[target_id] != new_state[target_id]:
                modified_ids.add(target_id)

        total_changes = len(added_ids) + len(removed_ids) + len(modified_ids)

        # Decision logic: incremental or full rebuild?
        if not current_state:
            # First sync for this agent
            logger.info(f"[HOTRELOAD] Agent {agent_id}: first sync, full rebuild (no Redis state)")
            await self._update_agent_state(agent_id, new_state)
            return None

        # Allow incremental updates for small target counts
        # For <= 20 targets: allow up to 3 changes
        # For > 20 targets: allow up to 10% changes
        max_allowed_changes = max(3, int(len(new_assignments) * 0.10))

        if total_changes > max_allowed_changes:
            # Too many changes
            logger.info(
                f"[HOTRELOAD] Agent {agent_id}: too many changes "
                f"({total_changes}/{len(new_assignments)}), full rebuild (threshold: {max_allowed_changes})"
            )
            await self._update_agent_state(agent_id, new_state)
            return None

        if total_changes == 0:
            # No changes
            logger.info(f"[HOTRELOAD] Agent {agent_id}: no changes detected")
            return {
                'version': await self._get_next_version(),
                'changes': [],
                'full_rebuild_required': False
            }

        # Build incremental update
        changes = []
        target_map = {t.id: t for t in new_assignments}

        # Additions
        for target_id in added_ids:
            target = target_map[int(target_id)]
            changes.append({
                'operation': 'add',
                'target_id': target.id,
                'target_data': self._serialize_target(target)
            })

        # Removals
        for target_id in removed_ids:
            changes.append({
                'operation': 'remove',
                'target_id': int(target_id),
                'target_data': None
            })

        # Modifications
        for target_id in modified_ids:
            target = target_map[int(target_id)]
            changes.append({
                'operation': 'modify',
                'target_id': target.id,
                'target_data': self._serialize_target(target),
                'changed_fields': self._detect_changed_fields(
                    agent_id,
                    target_id,
                    target
                )
            })

        # Get version
        version = await self._get_next_version()

        logger.info(
            f"[HOTRELOAD] Agent {agent_id}: incremental update v{version} "
            f"(+{len(added_ids)} -{len(removed_ids)} ~{len(modified_ids)})"
        )

        # Update stored state
        await self._update_agent_state(agent_id, new_state)
        logger.debug(f"[HOTRELOAD] Updated Redis state for {agent_id} with {len(new_state)} targets")

        return {
            'version': version,
            'changes': changes,
            'full_rebuild_required': False
        }

    async def _get_agent_state(self, agent_id: str) -> Dict[str, str]:
        """
        Get agent's target state (hash map).

        Returns:
            Dict mapping target_id -> target_hash
        """
        if self._use_redis:
            try:
                key = f"agent_state:{agent_id}"
                state = await self.redis.hgetall(key)

                # Handle both bytes and str (Redis client behavior varies)
                decoded = {}
                for k, v in state.items():
                    key_str = k.decode() if isinstance(k, bytes) else k
                    val_str = v.decode() if isinstance(v, bytes) else v
                    decoded[key_str] = val_str

                logger.debug(f"[HOTRELOAD] Redis get for {agent_id}: {len(decoded)} targets found (key: {key})")
                return decoded
            except Exception as e:
                logger.warning(f"[HOTRELOAD] Redis get failed for {agent_id}: {e}, falling back to empty state")
                return {}
        else:
            # Fallback: use agent.meta (slower but works)
            result = await self.db.execute(
                select(Agent).where(Agent.agent_id == agent_id)
            )
            agent = result.scalar_one_or_none()
            if agent and agent.meta:
                return agent.meta.get('target_state_hashes', {})
            return {}

    async def _update_agent_state(self, agent_id: str, state: Dict[str, str]):
        """
        Update agent's target state.

        Args:
            agent_id: Agent ID
            state: Dict mapping target_id -> target_hash
        """
        if self._use_redis:
            try:
                key = f"agent_state:{agent_id}"

                # Atomic update: delete + set + expire
                pipe = self.redis.pipeline()
                pipe.delete(key)
                if state:
                    pipe.hset(key, mapping=state)
                    pipe.expire(key, 86400)  # 24 hour TTL
                result = await pipe.execute()

                logger.info(f"[HOTRELOAD] Redis write for {agent_id}: {len(state)} targets stored (key: {key}, result: {result})")
            except Exception as e:
                logger.error(f"[HOTRELOAD] Redis update failed for {agent_id}: {e}")
                # Continue even if Redis fails
        else:
            # Fallback: update agent.meta (slower)
            result = await self.db.execute(
                select(Agent).where(Agent.agent_id == agent_id)
            )
            agent = result.scalar_one_or_none()
            if agent:
                if not agent.meta:
                    agent.meta = {}
                agent.meta['target_state_hashes'] = state

                # Mark as modified for SQLAlchemy
                from sqlalchemy.orm.attributes import flag_modified
                flag_modified(agent, 'meta')
                await self.db.commit()

    async def _get_next_version(self) -> int:
        """
        Get next version number (atomic increment).

        Uses Redis INCR for atomic increment.
        Falls back to timestamp if Redis unavailable.

        Returns:
            Next version number
        """
        if self._use_redis:
            try:
                version = await self.redis.incr('agent_update_version')
                return version
            except Exception as e:
                logger.warning(f"Redis INCR failed: {e}, using timestamp")

        # Fallback: use timestamp (not ideal but works)
        import time
        return int(time.time() * 1000)

    def _hash_target(self, target: Target) -> str:
        """
        Hash target for change detection.

        Only includes fields that affect agent behavior.

        Args:
            target: Target to hash

        Returns:
            SHA-256 hash (hex)
        """
        # Build selectors hash (includes baseline_hash for each)
        selectors_data = []
        if hasattr(target, 'selectors') and target.selectors:
            for s in target.selectors:
                if s.enabled:
                    selectors_data.append({
                        'id': s.id,
                        'selector': s.selector,
                        'content_type': s.content_type,
                        'ignore_regex': s.ignore_regex,
                        'baseline_hash': s.baseline_hash,
                        'priority': s.priority,
                    })

        data = {
            'url': target.url,
            'check_period_ms': target.check_period_ms,
            'check_type': target.check_type,
            'enabled': target.enabled,
            # Include selectors with their baseline hashes
            'selectors': selectors_data,
            # Don't include: baseline_content (too large), created_at, updated_at
        }

        # Canonical JSON (sorted keys)
        json_str = json.dumps(data, sort_keys=True)
        return hashlib.sha256(json_str.encode()).hexdigest()

    def _serialize_target(self, target: Target) -> Dict:
        """
        Serialize target for agent.

        Args:
            target: Target to serialize

        Returns:
            Dict with all fields agent needs
        """
        # Build selectors array
        selectors_list = []
        if hasattr(target, 'selectors') and target.selectors:
            for s in target.selectors:
                if s.enabled:
                    selectors_list.append({
                        'id': s.id,
                        'name': s.name,
                        'selector': s.selector,
                        'content_type': s.content_type or 'text',
                        'visual_region': s.visual_region,
                        'ignore_regex': s.ignore_regex,
                        'baseline_hash': s.baseline_hash,
                        'baseline_content': s.baseline_content,
                        'priority': s.priority,
                    })

        return {
            'id': target.id,
            'url': target.url,
            'check_period_ms': target.check_period_ms,
            'check_type': target.check_type or 'content',
            'enabled': target.enabled,
            # Uptime-specific fields
            'expected_status_code': target.expected_status_code if hasattr(target, 'expected_status_code') else None,
            'timeout_ms': target.timeout_ms if hasattr(target, 'timeout_ms') else None,
            'max_response_time_ms': target.max_response_time_ms if hasattr(target, 'max_response_time_ms') else None,
            'check_ssl': target.check_ssl if hasattr(target, 'check_ssl') else None,
            # Selectors with baselines
            'selectors': selectors_list,
        }

    def _detect_changed_fields(self, agent_id: str, target_id: str, target: Target) -> List[str]:
        """
        Detect which fields changed (for debugging).

        Args:
            agent_id: Agent ID
            target_id: Target ID
            target: New target data

        Returns:
            List of changed field names
        """
        # This is optional metadata for logging
        # Could be extended to store old values in Redis for detailed comparison
        return ['unknown']  # Placeholder

    async def clear_agent_state(self, agent_id: str):
        """
        Clear agent state (force full rebuild on next update).

        Args:
            agent_id: Agent ID
        """
        if self._use_redis:
            try:
                key = f"agent_state:{agent_id}"
                await self.redis.delete(key)
                logger.info(f"Cleared Redis state for {agent_id}")
            except Exception as e:
                logger.error(f"Redis delete failed for {agent_id}: {e}")
        else:
            result = await self.db.execute(
                select(Agent).where(Agent.agent_id == agent_id)
            )
            agent = result.scalar_one_or_none()
            if agent and agent.meta:
                agent.meta.pop('target_state_hashes', None)
                from sqlalchemy.orm.attributes import flag_modified
                flag_modified(agent, 'meta')
                await self.db.commit()

    async def get_stats(self) -> Dict:
        """
        Get statistics about state storage.

        Returns:
            Dict with stats
        """
        stats = {
            'using_redis': self._use_redis,
            'total_agents': 0,
            'total_targets_tracked': 0
        }

        if self._use_redis:
            try:
                # Count agents with state
                keys = await self.redis.keys('agent_state:*')
                stats['total_agents'] = len(keys)

                # Count total targets
                for key in keys:
                    count = await self.redis.hlen(key)
                    stats['total_targets_tracked'] += count

            except Exception as e:
                logger.error(f"Failed to get Redis stats: {e}")

        return stats
