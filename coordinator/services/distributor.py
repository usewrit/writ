"""
Target Distribution Service

Handles intelligent distribution of monitoring targets across available agents
based on agent weights, capacity, and availability.
"""
import logging
from typing import List, Dict
from datetime import datetime, timedelta
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete

from models.agent import Agent, AgentStatus
from models.target import Target
from models.target_assignment import TargetAssignment

logger = logging.getLogger(__name__)


class TargetDistributor:
    """
    Distributes monitoring targets across available agents using weighted allocation.
    """

    def __init__(self, db: AsyncSession):
        """
        Initialize distributor.

        Args:
            db: Database session
        """
        self.db = db

    async def get_active_agents(self) -> List[Agent]:
        """
        Get all active agents.

        Note: We include all ACTIVE agents, not just ones with recent heartbeats,
        because agents without target assignments won't send heartbeats.
        This prevents a chicken-and-egg problem where agents can't get work
        because they haven't reported recently.

        Returns:
            List of active agents
        """
        result = await self.db.execute(
            select(Agent).where(Agent.status == AgentStatus.ACTIVE)
        )
        return list(result.scalars().all())

    async def get_enabled_targets(self) -> List[Target]:
        """
        Get all enabled targets that should be monitored.

        Returns:
            List of enabled targets
        """
        result = await self.db.execute(
            select(Target).where(Target.enabled == True)
        )
        return list(result.scalars().all())

    def calculate_agent_weights(self, agents: List[Agent]) -> Dict[str, int]:
        """
        Calculate effective weights for each agent.

        Agents can have weight stored in metadata. Default weight is 1.
        Higher weight = more targets assigned.

        Args:
            agents: List of agents

        Returns:
            Dictionary mapping agent_id to weight
        """
        weights = {}
        for agent in agents:
            weight = 1  # Default weight
            if agent.meta and isinstance(agent.meta, dict):
                weight = agent.meta.get('weight', 1)
            weights[agent.agent_id] = max(1, min(weight, 10))  # Clamp between 1-10
        return weights

    async def distribute_targets(self, assigned_by: str = "auto-balancer") -> Dict:
        """
        Distribute all enabled targets across active agents with load balancing.

        Algorithm:
        1. Get all active agents and targets
        2. Calculate load for each target (inversely proportional to check_period_ms)
        3. Use greedy algorithm to balance load across agents
        4. Assign each target to ALL agents (quorum mode) or specific agents (load-balanced mode)
        5. Set force_config_update flag on all agents

        Args:
            assigned_by: Who triggered this redistribution

        Returns:
            Distribution statistics
        """
        # Get active agents and targets
        agents = await self.get_active_agents()
        targets = await self.get_enabled_targets()

        if not agents:
            logger.warning("No active agents available for distribution")
            return {
                "success": False,
                "reason": "No active agents available",
                "agents": 0,
                "targets": 0,
                "assignments": 0
            }

        if not targets:
            logger.info("No enabled targets to distribute")
            return {
                "success": True,
                "agents": len(agents),
                "targets": 0,
                "assignments": 0
            }

        # Get quorum configuration (how many agents should check each target)
        from models.config import Config
        quorum_result = await self.db.execute(
            select(Config).where(Config.key == "quorum")
        )
        quorum_config = quorum_result.scalar_one_or_none()
        quorum = int(quorum_config.value) if quorum_config else len(agents)

        # Ensure quorum doesn't exceed available agents
        quorum = min(quorum, len(agents))

        logger.info(f"Distributing {len(targets)} targets with quorum={quorum} across {len(agents)} agents")

        # Clear existing assignments
        await self.db.execute(delete(TargetAssignment))

        # Calculate load for each target and sort by load (highest first)
        # Load = checks per minute = 60000 / check_period_ms
        # Higher check frequency = higher load
        from models.config import Config
        global_period_result = await self.db.execute(
            select(Config).where(Config.key == "global_period_ms")
        )
        global_period_config = global_period_result.scalar_one_or_none()
        global_period_ms = int(global_period_config.value) if global_period_config else 60000

        target_loads = []
        for target in targets:
            period = target.check_period_ms if target.check_period_ms else global_period_ms
            load = 60000.0 / period  # Checks per minute
            target_loads.append((target, load))

        # Sort by load descending (assign high-load targets first)
        target_loads.sort(key=lambda x: x[1], reverse=True)

        # Initialize agent load tracking
        agent_load = {agent.agent_id: 0.0 for agent in agents}
        assignments = []

        # Assign each target to `quorum` agents with lowest current load
        for target, target_load in target_loads:
            # Sort agents by current load (ascending)
            sorted_agents = sorted(agents, key=lambda a: agent_load[a.agent_id])

            # Assign to the `quorum` agents with lowest load
            for agent in sorted_agents[:quorum]:
                assignment = TargetAssignment(
                    target_id=target.id,
                    agent_id=agent.agent_id,
                    assigned_at=datetime.utcnow(),
                    assigned_by=assigned_by
                )
                assignments.append(assignment)
                agent_load[agent.agent_id] += target_load

        # Save assignments
        self.db.add_all(assignments)

        # Set force_config_update flag on all agents
        for agent in agents:
            agent.force_config_update = True

        await self.db.commit()

        # Log distribution statistics
        agent_target_counts = {}
        for agent in agents:
            count = sum(1 for a in assignments if a.agent_id == agent.agent_id)
            agent_target_counts[agent.agent_id] = count

        stats = {
            "success": True,
            "agents": len(agents),
            "targets": len(targets),
            "assignments": len(assignments),
            "quorum": quorum,
            "distribution": {
                agent.agent_id: {
                    "assigned": agent_target_counts[agent.agent_id],
                    "load": round(agent_load[agent.agent_id], 2)
                }
                for agent in agents
            }
        }

        logger.info(f"Distribution completed: {len(assignments)} assignments created (quorum={quorum})")
        for agent_id, info in stats["distribution"].items():
            logger.info(f"  Agent {agent_id}: {info['assigned']} targets, load={info['load']} checks/min")

        return stats

    async def get_agent_assignments(self, agent_id: str) -> List[Target]:
        """
        Get all targets assigned to a specific agent.

        Args:
            agent_id: Agent identifier

        Returns:
            List of assigned targets with selectors eager loaded
        """
        from sqlalchemy.orm import selectinload

        result = await self.db.execute(
            select(Target)
            .options(selectinload(Target.selectors))
            .join(TargetAssignment, Target.id == TargetAssignment.target_id)
            .where(TargetAssignment.agent_id == agent_id)
            .where(Target.enabled == True)
        )
        return list(result.scalars().all())

    async def assign_target_to_agent(self, target_id: int, agent_id: str, assigned_by: str) -> bool:
        """
        Manually assign a specific target to a specific agent.

        Args:
            target_id: Target ID
            agent_id: Agent ID
            assigned_by: Who made this assignment

        Returns:
            True if successful, False otherwise
        """
        try:
            # Check if assignment already exists
            result = await self.db.execute(
                select(TargetAssignment).where(
                    TargetAssignment.target_id == target_id,
                    TargetAssignment.agent_id == agent_id
                )
            )
            existing = result.scalar_one_or_none()

            if existing:
                logger.info(f"Assignment already exists: target {target_id} -> agent {agent_id}")
                return True

            # Create new assignment
            assignment = TargetAssignment(
                target_id=target_id,
                agent_id=agent_id,
                assigned_at=datetime.utcnow(),
                assigned_by=assigned_by
            )
            self.db.add(assignment)

            # Set force_config_update flag
            result = await self.db.execute(
                select(Agent).where(Agent.agent_id == agent_id)
            )
            agent = result.scalar_one_or_none()
            if agent:
                agent.force_config_update = True

            await self.db.commit()
            logger.info(f"Target {target_id} assigned to agent {agent_id}")
            return True

        except Exception as e:
            logger.error(f"Error assigning target to agent: {e}")
            await self.db.rollback()
            return False
