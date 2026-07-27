"""
Unified per-agent error collection.

Agents don't have a single error table — failures land in three places:
- AgentError        (monitoring/check errors reported by agents)
- AutomationTask    (workflow executions that failed/timed out, via executor_agent_id)
- StreamingSession  (streaming sessions that ended with error_message)

This module merges them into one chronological list so both audiences read
the same data with different scopes:
- platform admins: every agent (cloud/infrastructure AND user-hosted)
- end users: only their own user-hosted (BYO) agents — full detail, since
  the agent runs on their machine against their own targets.
"""
import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


def _iso(dt) -> Optional[str]:
    return dt.isoformat() if dt else None


async def collect_agent_errors(
    db: AsyncSession,
    *,
    since: datetime,
    agent_id: Optional[str] = None,
    user_hosted: Optional[bool] = None,
    limit: int = 100,
    redact: bool = False,
):
    """
    Returns (errors, agents_meta).

    errors: merged list, newest first, each item:
        {source, agent_id, error_type, error_message, http_status,
         occurred_at, target_url, workflow_id, task_id, user_hosted, platform}
    agents_meta: {agent_id: {"user_hosted": bool, "platform": str}}
    """
    from models.agent import Agent
    from models.automation_task import AutomationTask
    from models.streaming_session import StreamingSession

    # Resolve the agent scope first; every error source is filtered to it.
    agent_q = select(Agent.agent_id, Agent.user_hosted, Agent.platform)
    if agent_id:
        agent_q = agent_q.where(Agent.agent_id == agent_id)
    if user_hosted is not None:
        agent_q = agent_q.where(Agent.user_hosted == user_hosted)
    agent_rows = (await db.execute(agent_q)).all()
    agents_meta = {
        row.agent_id: {
            "user_hosted": bool(row.user_hosted),
            "platform": row.platform.value if hasattr(row.platform, "value") else str(row.platform),
        }
        for row in agent_rows
    }
    if not agents_meta:
        return [], {}
    ids = list(agents_meta.keys())

    errors = []

    # Workflow-execution + streaming failures below populate the feed; there is
    # no dedicated check/monitoring error source.

    # Failed workflow executions
    task_q = (
        select(AutomationTask)
        .where(
            AutomationTask.executor_agent_id.in_(ids),
            AutomationTask.status.in_(["failed", "timeout"]),
            AutomationTask.created_at >= since,
        )
        .order_by(AutomationTask.created_at.desc())
        .limit(limit)
    )
    for t in (await db.execute(task_q)).scalars().all():
        errors.append({
            "source": "workflow",
            "agent_id": t.executor_agent_id,
            "error_type": t.status,
            "error_message": t.error_message or "(no error message recorded)",
            "http_status": None,
            "occurred_at": _iso(t.completed_at or t.created_at),
            "target_url": None,
            "workflow_id": t.workflow_id,
            "task_id": t.id,
        })

    # Streaming sessions that ended in error
    stream_q = (
        select(StreamingSession)
        .where(
            StreamingSession.agent_id.in_(ids),
            StreamingSession.error_message.isnot(None),
            StreamingSession.created_at >= since,
        )
        .order_by(StreamingSession.created_at.desc())
        .limit(limit)
    )
    for s in (await db.execute(stream_q)).scalars().all():
        errors.append({
            "source": "streaming",
            "agent_id": s.agent_id,
            "error_type": "streaming_session",
            "error_message": s.error_message,
            "http_status": None,
            "occurred_at": _iso(s.created_at),
            "target_url": s.target_url,
            "workflow_id": s.workflow_id,
            "task_id": None,
        })

    if redact:
        # End-user-facing view: keep the error detail (the owner's own
        # runs/checks) but strip internal-infra identifiers, mirroring TaskResponse.
        from security.infra_redaction import redact_infra
        for e in errors:
            e["error_message"] = redact_infra(e["error_message"])

    for e in errors:
        meta = agents_meta.get(e["agent_id"], {})
        e["user_hosted"] = meta.get("user_hosted", False)
        e["platform"] = meta.get("platform")

    errors.sort(key=lambda e: e["occurred_at"] or "", reverse=True)
    return errors[:limit], agents_meta
