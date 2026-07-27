"""
Notification model - in-app notification primitive.

The first-class notification record powering the bell + inbox.
Distinct from `NotificationLog` (which tracks outbound provider sends for rate
limiting). One row per delivered in-app notification; `read_at` flips when the
user opens it. Created best-effort via `services.notification_service`.

`type` is a free-form string matching the NotificationType values:
    review_approved, review_rejected, payout_paid, payout_failed,
    refund_issued, clawback_applied, run_failed, low_balance,
    update_available, account_security
"""
import uuid
from datetime import datetime
from sqlalchemy import Column, String, Text, DateTime, ForeignKey, Index, Uuid, JSON
from sqlalchemy.sql import func
from database import Base


class Notification(Base):
    """In-app notification (bell + inbox)."""
    __tablename__ = "notifications"

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # Optional targeting at a specific member; NULL = owner-wide.
    user_id = Column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        comment="Optional specific recipient user. NULL = owner-wide.",
    )

    type = Column(
        String(50),
        nullable=False,
        comment="NotificationType value, e.g. review_approved, payout_paid, run_failed.",
    )
    title = Column(String(255), nullable=False)
    body = Column(Text, nullable=True)
    payload = Column(
        JSON,
        nullable=True,
        comment="Structured context (ids, amounts) for the frontend to render/link.",
    )
    link = Column(
        String(500),
        nullable=True,
        comment="Optional in-app deep link the notification points to.",
    )

    read_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the recipient marked this read (NULL = unread).",
    )

    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )

    __table_args__ = (
        # Inbox list/unread-count predicate: most-recent-first.
        Index("ix_notifications_created", "created_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<Notification(id={self.id}, "
            f"type='{self.type}', read={self.read_at is not None})>"
        )

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "user_id": str(self.user_id) if self.user_id else None,
            "type": self.type,
            "title": self.title,
            "body": self.body,
            "payload": self.payload,
            "link": self.link,
            "read_at": self.read_at.isoformat() if self.read_at else None,
            "is_read": self.read_at is not None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
