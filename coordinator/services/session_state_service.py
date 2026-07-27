"""
SessionStateService — per-workflow browser session cache.

The coordinator does not pin sessions to a fleet agent, so there is no
workflow+agent session cache.

This is a NO-OP stub kept so the call sites (automation runs, streaming,
triggers) keep working: saves are dropped, loads return None, and no preferred
agent is ever reported. Persona-scoped warm sessions still work — they live on
the Persona row and are handled by ``PersonaService`` (which reuses the pure
``compute_cookie_expiry``/``is_expired`` helpers below).
"""
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


class SessionStateService:

    @staticmethod
    async def get_preferred_affinity(db: AsyncSession, workflow_id: int) -> None:
        """No preferred agent — this build does not pin sessions. Always None."""
        return None

    @staticmethod
    async def save_session(
        db: AsyncSession,
        workflow_id: int,
        agent_id: str,
        auth_session: Optional[dict] = None,
        ttl_seconds: Optional[int] = None,
    ) -> None:
        """No-op: this build does not pin workflow+agent sessions."""
        return None

    @staticmethod
    async def load_session(
        db: AsyncSession,
        workflow_id: int,
        agent_id: str,
        ttl_seconds: Optional[int] = None,
    ) -> Optional[dict]:
        """No cached workflow+agent session exists anymore. Always None."""
        return None

    @staticmethod
    async def mark_expired(
        db: AsyncSession,
        workflow_id: int,
        agent_id: str,
        reason: str,
    ) -> None:
        """No-op — nothing to expire."""
        return None

    @staticmethod
    async def mark_validated(
        db: AsyncSession, workflow_id: int, agent_id: str
    ) -> None:
        """No-op — nothing to validate."""
        return None

    @staticmethod
    async def delete_sessions(db: AsyncSession, workflow_id: int) -> int:
        """No cached workflow+agent sessions to delete. Always 0."""
        return 0

    @staticmethod
    def compute_cookie_expiry(cookies: list) -> Optional[datetime]:
        """Compute earliest expiry from a Playwright cookie list.

        Playwright cookies carry 'expires' as a Unix timestamp (float); -1 means
        a session cookie (no fixed expiry). Pure helper — still used by
        ``PersonaService`` for identity-scoped warm sessions.
        """
        earliest = None
        for cookie in cookies or []:
            expires = cookie.get("expires", -1)
            if expires == -1 or expires <= 0:
                continue
            try:
                expiry_dt = datetime.fromtimestamp(expires, tz=timezone.utc)
                if earliest is None or expiry_dt < earliest:
                    earliest = expiry_dt
            except (ValueError, OSError, OverflowError):
                continue
        return earliest

    @staticmethod
    def is_expired(affinity, ttl_seconds: Optional[int] = None) -> bool:
        """Whether a session-bearing row is expired.

        Defensive against a falsy row (treated as expired). The caller pattern is
        ``if row and not is_expired(row, ttl)``; persona rows expose the
        ``validation_status``/``expires_at``/``created_at`` attributes this reads.
        """
        if not affinity:
            return True
        if getattr(affinity, "validation_status", None) == "expired":
            return True

        now = datetime.now(timezone.utc)

        expires_at = getattr(affinity, "expires_at", None)
        if expires_at:
            exp = expires_at
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if now >= exp:
                return True

        created_at = getattr(affinity, "created_at", None)
        if ttl_seconds and created_at:
            created = created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if now >= created + timedelta(seconds=ttl_seconds):
                return True

        return False
