"""
Feature gating — ALWAYS-ON (single-owner coordinator).

The self-host coordinator is single-user with no plans/tiers, so every feature
is available to the one owner. The functions here keep the names/signatures
their router callers import, but every gate resolves to ALLOWED.

Usage in routers is unchanged:
    from security.feature_gate import require_feature

    @router.post("/ai-navigate")
    async def ai_navigate(..., _gate = Depends(require_feature("ai_workflows"))):
        ...
"""
import logging
from typing import Callable, Optional

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from security.dependencies import get_auth_context, AuthContext

logger = logging.getLogger(__name__)


async def refresh_flag_cache(db: AsyncSession) -> None:
    """No-op: there are no feature flags on the single-owner coordinator."""
    return None


async def check_feature_access(
    feature_id: str,
    db: AsyncSession,
    auth: AuthContext,
) -> bool:
    """Single-owner coordinator: every feature is available."""
    return True


async def check_feature_access_with_parent(
    feature_id: str,
    db: AsyncSession,
    auth: AuthContext,
) -> bool:
    """Single-owner coordinator: every feature is available."""
    return True


async def feature_killed_for_background(feature_id: str, db: AsyncSession) -> bool:
    """Single-owner coordinator: nothing is ever killed for background runs."""
    return False


async def evaluate_all_for_org(
    db: AsyncSession,
    org_id: Optional[str],
    plan: Optional[str],
) -> dict[str, bool]:
    """Every registry feature is enabled for the single owner."""
    from security.feature_defs import FEATURE_DEFS

    return {fid: True for fid in FEATURE_DEFS}


def require_feature(feature_id: str) -> Callable:
    """FastAPI dependency that would gate an endpoint behind a feature flag.

    Single-owner coordinator: always allows. Kept as a dependency (rather than a
    no-op) so it still resolves auth, matching the previous router wiring.
    """
    async def _check(
        auth: AuthContext = Depends(get_auth_context),
        db: AsyncSession = Depends(get_db),
    ):
        return None
    return _check
