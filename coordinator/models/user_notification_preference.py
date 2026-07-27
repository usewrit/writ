"""
UserNotificationPreference model — the owner's platform notification preferences.

One row per user (the single-owner coordinator has exactly one, but the schema
is keyed on user_id so it stays correct if that ever changes). Holds the
event × channel matrix for PLATFORM-WIDE notifications (runs, agents) plus the
owner's personal delivery contact points (phone, Pushover key).

Distinct from the per-target notification config (`target.notification_providers`,
operator recipient tables) which governs monitor-change alerts, and from
`NotificationSettings` (global delivery behavior: quiet hours, rate limits).
The event catalog and channel semantics live in `notifications/catalog.py`;
missing keys in `preferences` fall back to the catalog defaults, so an owner
with no row gets sane defaults.
"""
import uuid
from sqlalchemy import Column, String, DateTime, ForeignKey, Uuid, JSON
from sqlalchemy.sql import func
from database import Base


class UserNotificationPreference(Base):
    """Per-user platform notification preference matrix (single-owner coordinator)."""
    __tablename__ = "user_notification_preferences"

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)

    user_id = Column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
        comment="The owner the preferences belong to (one row per user)",
    )

    preferences = Column(
        JSON,
        nullable=False,
        default=dict,
        comment=(
            'Event → channel matrix, e.g. {"runs.run_failed": {"email": true, '
            '"in_app": true, "sms": false}}. Missing events/channels fall back to '
            "catalog defaults."
        ),
    )

    # Personal delivery contact points (per-user, NOT the operator recipient lists).
    phone_number = Column(
        String(32),
        nullable=True,
        comment="E.164 phone for SMS/WhatsApp/Signal platform notifications",
    )
    pushover_user_key = Column(
        String(64),
        nullable=True,
        comment="Owner's own Pushover user key for platform notifications",
    )

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(),
                        onupdate=func.now(), nullable=False)

    def __repr__(self) -> str:
        return f"<UserNotificationPreference(user={self.user_id})>"
