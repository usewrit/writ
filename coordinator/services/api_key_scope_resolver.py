"""
API key scope resolution + per-key run-limit enforcement.

Centralizes two concerns that the raw `APIKey.scopes` dict can't express on its own:

1. **Automation → workflow run cascade.** A key scoped to an *automation* (TriggerRule)
   may RUN the workflows that automation references — without those workflows being
   listed in the key's `workflows` scope, and WITHOUT gaining write/delete on them.
   This module resolves the child workflow ids and answers "can this key run workflow X?".

2. **Per-key run limits.** Execution-count, concurrency, and budget-window checks that
   gate dispatch through a key.

The cascade is consulted ONLY on run/read paths — it never widens
`APIKey.has_permission(..., "write"/"delete")`, so automation-only keys stay run-only.
"""
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, Set

from fastapi import HTTPException, status
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from models.api_key import APIKey
from models.trigger_rule import TriggerRule
from models.automation_task import AutomationTask

logger = logging.getLogger(__name__)

# Task states considered "in flight" for concurrency limiting
# (AutomationTask.status: pending, assigned, running, success, failed, timeout, cancelled).
_IN_FLIGHT_STATES = ("pending", "assigned", "running")


def _extract_workflow_ids_from_rule(rule: TriggerRule) -> Set[int]:
    """Collect every workflow id a TriggerRule can execute."""
    ids: Set[int] = set()
    if rule.workflow_id:
        ids.add(int(rule.workflow_id))

    def _maybe_add(cfg):
        if not isinstance(cfg, dict):
            return
        wid = cfg.get("workflow_id")
        if isinstance(wid, int):
            ids.add(wid)
        elif isinstance(wid, str) and wid.isdigit():
            ids.add(int(wid))
        for w in (cfg.get("workflow_ids") or []):
            if isinstance(w, int):
                ids.add(w)
            elif isinstance(w, str) and w.isdigit():
                ids.add(int(w))

    for action in (rule.actions or []):
        if isinstance(action, dict) and action.get("type") == "workflow":
            _maybe_add(action.get("config"))

    for block in (rule.blocks or []):
        if isinstance(block, dict) and block.get("blockType") == "workflow":
            _maybe_add(block.get("config"))

    return ids


async def resolve_automation_child_workflow_ids(
    db: AsyncSession,
    automation_ids: Optional[list],
) -> Set[int]:
    """Union of workflow ids reachable from the given automations (TriggerRules).

    `automation_ids = None` means "all automations" (the key's `automations`
    scope grants all). An empty list means none.
    """
    query = select(TriggerRule)
    if automation_ids is not None:
        if not automation_ids:
            return set()
        query = query.where(TriggerRule.id.in_(automation_ids))

    result = await db.execute(query)
    rules = result.scalars().all()

    workflow_ids: Set[int] = set()
    for rule in rules:
        workflow_ids |= _extract_workflow_ids_from_rule(rule)
    return workflow_ids


async def key_can_run_workflow(db: AsyncSession, key: APIKey, workflow_id: int) -> bool:
    """True if the key may EXECUTE this workflow, directly or via automation cascade.

    Direct: an explicit `workflows` scope (read = baseline access/run) covering the id.
    Cascade: the workflow is reachable from an automation the key is scoped to.
    """
    if key is None:
        return False
    # Direct workflow scope (read is the baseline "may access/run" verb).
    if key.has_permission("workflows", "read", workflow_id):
        return True
    # Cascade from automations scope (run-only).
    automations_scope = (key.scopes or {}).get("automations")
    if automations_scope and automations_scope.get("permissions"):
        automation_ids = automations_scope.get("ids")  # None = all
        child_ids = await resolve_automation_child_workflow_ids(db, automation_ids)
        if workflow_id in child_ids:
            return True
    return False


def key_can_run_automation(key: APIKey, automation_id: int) -> bool:
    """True if the key may RUN this automation (presence of automations scope)."""
    if key is None:
        return False
    return key.has_permission("automations", "read", automation_id)


async def enforce_key_run_limits(db: AsyncSession, key: APIKey) -> None:
    """Raise HTTPException if this key has exhausted a per-key run limit.

    Honors the budget reset window (resets the run counter when due). Checks the
    per-window execution limit and the concurrent-browser cap. Caller persists the
    counter increment after a successful dispatch.
    """
    if key is None:
        return

    now = datetime.now(timezone.utc)
    if key.maybe_reset_budget(now):
        await db.flush()

    # Per-window execution cap.
    if key.execution_limit is not None and (key.runs_used or 0) >= key.execution_limit:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"API key execution limit reached "
                f"({key.runs_used}/{key.execution_limit}) for the current period."
            ),
        )

    # Concurrent browser cap (in-flight tasks attributed to this key).
    if key.max_concurrent_browsers is not None:
        result = await db.execute(
            select(func.count(AutomationTask.id)).where(
                AutomationTask.api_key_id == key.id,
                AutomationTask.status.in_(_IN_FLIGHT_STATES),
            )
        )
        in_flight = result.scalar() or 0
        if in_flight >= key.max_concurrent_browsers:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=(
                    f"API key concurrent run limit reached "
                    f"({in_flight}/{key.max_concurrent_browsers})."
                ),
            )


async def check_key_hourly_session_limit(db: AsyncSession, key: APIKey) -> None:
    """Raise if the key exceeded its per-hour session/run cap (last 60 min of tasks)."""
    if key is None or key.sessions_per_hour_limit is None:
        return
    one_hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
    result = await db.execute(
        select(func.count(AutomationTask.id)).where(
            AutomationTask.api_key_id == key.id,
            AutomationTask.created_at >= one_hour_ago,
        )
    )
    count = result.scalar() or 0
    if count >= key.sessions_per_hour_limit:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"API key hourly limit reached ({count}/{key.sessions_per_hour_limit}).",
        )
