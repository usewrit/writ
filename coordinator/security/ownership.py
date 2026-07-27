"""
Ownership utilities (single-owner coordinator).

The self-host coordinator is single-user: there is exactly one owner and one
implicit workspace, so there is no tenant/organization scoping. The helpers here
keep the names/signatures their router callers import, and resolve against the
single owner's context.
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

    A trusted/infrastructure agent (the owner's own fleet) may report for any
    target. An untrusted / user-hosted (BYO) agent may only report for a target
    it is actually ASSIGNED to — otherwise a foreign agent could inject results
    (e.g. an auth-session blob) into an arbitrary victim target (audit #20).
    """
    if agent is None or target is None:
        return False
    if getattr(agent, "is_trusted", False):
        return True
    from sqlalchemy import select
    from models.target_assignment import TargetAssignment
    row = (await db.execute(
        select(TargetAssignment.id).where(
            TargetAssignment.agent_id == agent.agent_id,
            TargetAssignment.target_id == target.id,
        )
    )).first()
    return row is not None


async def assert_agent_may_report_target(db, *, agent, target) -> None:
    """Raise 403 unless the agent is authorized to report for the target
    (see ``agent_may_report_target``)."""
    if not await agent_may_report_target(db, agent=agent, target=target):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Agent is not authorized to report results for this target.",
        )
