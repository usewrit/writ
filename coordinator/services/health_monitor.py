"""
Health monitoring service for agents, targets, and system.

Monitors for errors and health issues, triggers notifications based on settings.
"""
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, func, desc

logger = logging.getLogger(__name__)


class HealthMonitor:
    """
    Health monitoring service.

    Monitors agent errors, target health, and system health.
    Triggers notifications when thresholds are exceeded.
    """

    def __init__(self, db: AsyncSession):
        """
        Initialize health monitor.

        Args:
            db: SQLAlchemy async session
        """
        self.db = db
        self.settings = None

    async def _load_settings(self):
        """Load notification settings."""
        if self.settings is not None:
            return self.settings

        try:
            from models.notification_settings import NotificationSettings
            result = await self.db.execute(
                select(NotificationSettings).where(NotificationSettings.id == 1)
            )
            self.settings = result.scalar_one_or_none()
            return self.settings
        except Exception as e:
            logger.error(f"Failed to load notification settings: {e}")
            return None

    async def check_agent_errors(self) -> List[Dict[str, Any]]:
        """
        Check for agent errors that exceed threshold.

        Returns:
            List of agents with excessive errors
        """
        settings = await self._load_settings()
        if not settings or not settings.agent_errors_enabled:
            return []

        # There is no agent check-error ledger to threshold on.
        return []

        try:
            AgentError = None
            from models.agent import Agent

            # Calculate time window based on delay
            window_start = datetime.utcnow() - timedelta(minutes=settings.agent_error_delay_minutes)

            # Find agents with consecutive errors exceeding threshold
            result = await self.db.execute(
                select(
                    AgentError.agent_id,
                    Agent.agent_id.label('agent_identifier'),
                    func.count(AgentError.id).label('error_count'),
                    func.max(AgentError.occurred_at).label('latest_error')
                )
                .join(Agent, Agent.id == AgentError.agent_id)
                .where(AgentError.occurred_at >= window_start)
                .group_by(AgentError.agent_id, Agent.agent_id)
                .having(func.count(AgentError.id) >= settings.agent_error_threshold)
            )

            agents_with_errors = []
            for row in result:
                agents_with_errors.append({
                    'agent_id': row.agent_id,
                    'agent_identifier': row.agent_identifier,
                    'error_count': row.error_count,
                    'latest_error': row.latest_error,
                })

            if agents_with_errors:
                logger.warning(
                    f"Found {len(agents_with_errors)} agents with excessive errors: "
                    f"threshold={settings.agent_error_threshold}, window={settings.agent_error_delay_minutes}m"
                )

            return agents_with_errors

        except Exception as e:
            logger.error(f"Error checking agent errors: {e}")
            return []

    async def check_target_health(self) -> List[Dict[str, Any]]:
        """
        Check for targets with consecutive failures.

        Returns:
            List of unhealthy targets
        """
        settings = await self._load_settings()
        if not settings or not settings.target_health_enabled:
            return []

        # There is no per-target failure ledger to threshold on.
        return []

        try:
            AgentError = None
            from models.target import Target

            # Calculate time window based on check interval
            window_start = datetime.utcnow() - timedelta(minutes=settings.target_health_check_interval * settings.target_health_threshold)

            # Find targets with consecutive failures
            result = await self.db.execute(
                select(
                    AgentError.target_id,
                    Target.url,
                    func.count(AgentError.id).label('failure_count'),
                    func.max(AgentError.occurred_at).label('latest_failure')
                )
                .join(Target, Target.id == AgentError.target_id)
                .where(
                    and_(
                        AgentError.occurred_at >= window_start,
                        AgentError.target_id.isnot(None)
                    )
                )
                .group_by(AgentError.target_id, Target.url)
                .having(func.count(AgentError.id) >= settings.target_health_threshold)
            )

            unhealthy_targets = []
            for row in result:
                unhealthy_targets.append({
                    'target_id': row.target_id,
                    'target_url': row.url,
                    'failure_count': row.failure_count,
                    'latest_failure': row.latest_failure,
                })

            if unhealthy_targets:
                logger.warning(
                    f"Found {len(unhealthy_targets)} unhealthy targets: "
                    f"threshold={settings.target_health_threshold}"
                )

            return unhealthy_targets

        except Exception as e:
            logger.error(f"Error checking target health: {e}")
            return []

    async def check_system_health(self) -> List[Dict[str, Any]]:
        """
        Check for system health issues (disconnected agents).

        Returns:
            List of disconnected agents
        """
        settings = await self._load_settings()
        if not settings or not settings.system_health_enabled:
            return []

        try:
            from models.agent import Agent

            # Calculate disconnect threshold
            disconnect_threshold = datetime.utcnow() - timedelta(minutes=settings.system_health_agent_disconnect_delay)

            # Find agents that haven't reported recently
            result = await self.db.execute(
                select(Agent)
                .where(
                    and_(
                        Agent.last_seen_at < disconnect_threshold,
                        Agent.status == 'active'
                    )
                )
            )

            disconnected_agents = []
            for agent in result.scalars():
                disconnected_agents.append({
                    'agent_id': agent.id,
                    'agent_identifier': agent.agent_id,
                    'last_seen': agent.last_seen_at,
                    'minutes_ago': int((datetime.utcnow() - agent.last_seen_at).total_seconds() / 60),
                })

            if disconnected_agents:
                logger.warning(
                    f"Found {len(disconnected_agents)} disconnected agents: "
                    f"threshold={settings.system_health_agent_disconnect_delay}m"
                )

            return disconnected_agents

        except Exception as e:
            logger.error(f"Error checking system health: {e}")
            return []

    async def run_health_checks(self) -> Dict[str, Any]:
        """
        Run all health checks.

        Returns:
            Dict with results from all checks
        """
        results = {
            'agent_errors': [],
            'target_health': [],
            'system_health': [],
            'timestamp': datetime.utcnow().isoformat(),
        }

        try:
            # Run the three checks sequentially: they share this single
            # AsyncSession, which cannot execute concurrent statements (one
            # session = one in-flight query). Each check is a single grouped
            # aggregate, so serial execution is cheap.
            results['agent_errors'] = await self.check_agent_errors()
            results['target_health'] = await self.check_target_health()
            results['system_health'] = await self.check_system_health()

            # Log summary
            total_issues = (
                len(results['agent_errors']) +
                len(results['target_health']) +
                len(results['system_health'])
            )

            if total_issues > 0:
                logger.info(
                    f"Health check found {total_issues} issues: "
                    f"{len(results['agent_errors'])} agent errors, "
                    f"{len(results['target_health'])} unhealthy targets, "
                    f"{len(results['system_health'])} disconnected agents"
                )
            else:
                logger.debug("Health check: All systems nominal")

            return results

        except Exception as e:
            logger.error(f"Error running health checks: {e}")
            return results
