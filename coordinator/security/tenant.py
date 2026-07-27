"""
Ownership utilities (single-owner coordinator).

The OSS self-host coordinator is single-user: there is exactly one owner and one
implicit workspace, so there is no tenant/organization scoping. The helpers here
keep the names/signatures their router callers import, but resolve to the single
owner's context — no cross-tenant checks remain.
"""
from fastapi import HTTPException, status


async def verify_target_ownership(db, target_id: int):
    """Verify a target exists. Raises 404 if not found."""
    from sqlalchemy import select
    from models.target import Target
    result = await db.execute(
        select(Target).where(Target.id == target_id)
    )
    target = result.scalar_one_or_none()
    if not target:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Target not found")
    return target


async def verify_selector_ownership(db, selector_id: int):
    """Verify a selector exists. Raises 404 if not found."""
    from sqlalchemy import select
    from models.target_selector import TargetSelector
    result = await db.execute(
        select(TargetSelector).where(
            TargetSelector.id == selector_id,
        )
    )
    sel = result.scalar_one_or_none()
    if not sel:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Selector not found")
    return sel


async def agent_may_report_target(db, *, agent, target) -> bool:
    """Authorize an agent to report results for a target.

    Single-owner coordinator: every target and every agent belong to the one
    owner, so any resolved agent may report for any resolved target. Returns
    False only when either argument is missing.
    """
    return agent is not None and target is not None


async def assert_agent_may_report_target(db, *, agent, target) -> None:
    """Raise 403 unless the agent is authorized to report for the target
    (see ``agent_may_report_target``)."""
    if not await agent_may_report_target(db, agent=agent, target=target):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Agent is not authorized to report results for this target.",
        )
