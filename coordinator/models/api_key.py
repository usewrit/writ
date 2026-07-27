"""
API Key model - authentication tokens for API access.

Scopes are stored as JSON in the vocabulary defined by ``security.api_scopes``::

    {"v": 2,
     "scopes": ["workflows:read", "workflows:execute", "datasets:read"],
     "ids": {"workflows": [12, 15]}}

- A scope absent from the list = no access to that resource/action.
- ``ids`` is optional and per-resource: absent = every object, list = those ids.

This replaced a per-resource CRUD matrix over
``workflows|checks|automations|targets|signals|streaming`` that named six of the
reachable surfaces, spelled the same surface several ways, and had no way to say
"execute" — so the routers asking for ``workflows:run`` could never be satisfied.
"""
import enum
from datetime import datetime, timezone, timedelta, date
from typing import Optional
from sqlalchemy import (
    Column,
    Integer,
    BigInteger,
    String,
    Enum,
    DateTime,
    Date,
    Boolean,
    ForeignKey,
    Index,
    Uuid,
    JSON,
)
from database import Base

BUDGET_RESET_PERIODS = ("none", "daily", "weekly", "monthly")

# The resource/action vocabulary lives in security.api_scopes and is NOT mirrored
# here — the old copy drifted from what the routers actually enforced.


class Role(str, enum.Enum):
    VIEWER = "viewer"
    OPERATOR = "operator"
    ADMIN = "admin"
    CLIENT = "client"


class APIKey(Base):
    __tablename__ = "api_keys"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"),
                     nullable=True, index=True)
    label = Column(String(255), nullable=False)
    key_hash = Column(String(255), unique=True, nullable=False, index=True)
    key_prefix = Column(String(8), nullable=True, index=True)  # SHA-256 prefix for O(1) lookup

    # Legacy — kept for DB compat, not used by new code
    role = Column(
        Enum(Role, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        default=Role.CLIENT,
        index=True,
    )
    workflow_id = Column(Integer, ForeignKey("automation_workflows.id", ondelete="SET NULL"), nullable=True, index=True)
    webhook_trigger_id = Column(Integer, ForeignKey("webhook_triggers.id", ondelete="SET NULL"), nullable=True, index=True)

    scopes = Column(JSON, nullable=True)

    # ---- AI / credits ----
    # Dedicated AI toggle: a key may consume credits only if this is True.
    ai_enabled = Column(Boolean, nullable=False, default=False, server_default="false")
    # Per-key wallet budget in MICRO-USD (per reset window). NULL = unlimited
    # (draws only org balance). Column name kept; unit is now micro-dollars.
    credit_budget = Column(BigInteger, nullable=True)
    # Micro-USD consumed in the current window.
    credit_used = Column(BigInteger, nullable=False, default=0, server_default="0")
    # Window reset cadence: none/daily/weekly/monthly.
    budget_reset_period = Column(String(16), nullable=False, default="none", server_default="none")
    # End of the current budget window (recomputed forward on reset).
    budget_reset_at = Column(DateTime(timezone=True), nullable=True)

    # ---- Per-key limits (all NULL = unlimited / inherit org) ----
    rate_limit_per_min = Column(Integer, nullable=True)
    rate_limit_per_hour = Column(Integer, nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True, index=True)
    # Per-key daily cost cap (cents).
    daily_cost_cap_usd = Column(Integer, nullable=True)
    daily_cost_accumulated = Column(Integer, nullable=False, default=0, server_default="0")  # micro-dollars
    daily_cost_date = Column(Date, nullable=True)
    sessions_per_hour_limit = Column(Integer, nullable=True)
    max_concurrent_browsers = Column(Integer, nullable=True)
    execution_limit = Column(Integer, nullable=True)
    # Run counter (executions dispatched through this key in the current window).
    runs_used = Column(Integer, nullable=False, default=0, server_default="0")

    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    revoked_at = Column(DateTime(timezone=True), nullable=True, index=True)
    last_used_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_api_keys_role_revoked", "role", "revoked_at"),
    )

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None

    # ------------------------------------------------------------------
    # Per-key metering helpers
    # ------------------------------------------------------------------
    def can_use_ai(self) -> bool:
        return bool(self.ai_enabled)

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        if self.expires_at is None:
            return False
        now = now or datetime.now(timezone.utc)
        exp = self.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        return now >= exp

    @staticmethod
    def _period_window_end(period: str, anchor: datetime) -> Optional[datetime]:
        """Compute the end of the current window starting from `anchor` (UTC)."""
        if period == "none" or not period:
            return None
        if anchor.tzinfo is None:
            anchor = anchor.replace(tzinfo=timezone.utc)
        if period == "daily":
            base = anchor.replace(hour=0, minute=0, second=0, microsecond=0)
            return base + timedelta(days=1)
        if period == "weekly":
            base = anchor.replace(hour=0, minute=0, second=0, microsecond=0)
            # Monday = 0; days until next Monday (at least 1, at most 7)
            days_ahead = 7 - base.weekday()
            return base + timedelta(days=days_ahead)
        if period == "monthly":
            year = anchor.year + (1 if anchor.month == 12 else 0)
            month = 1 if anchor.month == 12 else anchor.month + 1
            return anchor.replace(
                year=year, month=month, day=1,
                hour=0, minute=0, second=0, microsecond=0,
            )
        return None

    def maybe_reset_budget(self, now: Optional[datetime] = None) -> bool:
        """Reset the per-key credit/run counters if the current window has elapsed.

        Idempotent: recomputes `budget_reset_at` forward from `now`, so a key left
        untouched across several periods resets correctly on next use. Returns True
        if a reset occurred (caller should persist).
        """
        period = self.budget_reset_period or "none"
        if period == "none":
            return False
        now = now or datetime.now(timezone.utc)
        reset_at = self.budget_reset_at
        if reset_at is not None and reset_at.tzinfo is None:
            reset_at = reset_at.replace(tzinfo=timezone.utc)
        if reset_at is None or now >= reset_at:
            self.credit_used = 0
            self.runs_used = 0
            self.budget_reset_at = self._period_window_end(period, now)
            return True
        return False

    def has_credit_headroom(self, amount: int) -> bool:
        if self.credit_budget is None:
            return True
        return (self.credit_used or 0) + amount <= self.credit_budget

    def consume_key_credits(self, amount: int) -> None:
        self.credit_used = (self.credit_used or 0) + amount

    @property
    def is_scoped(self) -> bool:
        """True when the key is pinned to specific objects, not just specific verbs."""
        ids = (self.scopes or {}).get("ids")
        return bool(isinstance(ids, dict) and ids)

    @property
    def trigger_ids(self) -> list:
        ids = []
        if self.webhook_trigger_id:
            ids.append(self.webhook_trigger_id)
        if self.scopes and isinstance(self.scopes, dict):
            scope_ids = self.scopes.get('trigger_ids', [])
            if isinstance(scope_ids, list):
                ids.extend(scope_ids)
        return list(set(ids))

    def has_permission(self, resource_type: str, action: str, resource_id: int = None) -> bool:
        """Does this key grant (resource, action) — and object `resource_id`?

        A scope-less key is NOT full access any more. That shortcut existed to
        rescue keys the old UI let you create with nothing selected; creation now
        requires at least one scope, so an empty grant means nothing.
        """
        from security.api_scopes import check
        return check(self.scopes, resource_type, action, resource_id)

    def permitted_ids(self, resource_type: str) -> Optional[list]:
        """Object ids this key may see; None = no id restriction, [] = nothing."""
        from security.api_scopes import allowed_ids
        return allowed_ids(self.scopes, resource_type)

    def granted_scopes(self) -> list:
        """The scope strings this key holds, sorted — what the UI renders."""
        from security.api_scopes import granted_scopes
        return sorted(granted_scopes(self.scopes))

    def scope_summary(self) -> str:
        from security.api_scopes import summarize
        return summarize(self.scopes)

    def to_dict(self, include_hash: bool = False) -> dict:
        result = {
            "id": self.id,
            "label": self.label,
            "scopes": self.scopes,
            "created_at": self.created_at.isoformat(),
            "revoked_at": self.revoked_at.isoformat() if self.revoked_at else None,
            "last_used_at": self.last_used_at.isoformat() if self.last_used_at else None,
            "is_active": self.is_active,
            "is_scoped": self.is_scoped,
            # AI / credits
            "ai_enabled": self.ai_enabled,
            "credit_budget": self.credit_budget,
            "credit_used": self.credit_used or 0,
            "budget_reset_period": self.budget_reset_period or "none",
            "budget_reset_at": self.budget_reset_at.isoformat() if self.budget_reset_at else None,
            # Limits
            "rate_limit_per_min": self.rate_limit_per_min,
            "rate_limit_per_hour": self.rate_limit_per_hour,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "daily_cost_cap_usd": self.daily_cost_cap_usd,
            "sessions_per_hour_limit": self.sessions_per_hour_limit,
            "max_concurrent_browsers": self.max_concurrent_browsers,
            "execution_limit": self.execution_limit,
            "runs_used": self.runs_used or 0,
        }
        if include_hash:
            result["key_hash"] = self.key_hash
        return result
