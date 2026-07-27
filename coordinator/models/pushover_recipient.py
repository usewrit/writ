"""
Pushover Recipient Model - stores multiple Pushover user keys for notifications.
"""
from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime

from database import Base


class PushoverRecipient(Base):
    """
    Pushover recipient table.

    Stores multiple Pushover user keys to send notifications to multiple accounts.
    """
    __tablename__ = "pushover_recipients"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)

    # Recipient details
    name = Column(
        String(255),
        nullable=False,
        comment="Friendly name for this recipient (e.g., 'John's Phone', 'Team Group')"
    )

    user_key = Column(
        String(255),
        nullable=False,
        unique=True,
        comment="Pushover user or group key"
    )

    # Status
    enabled = Column(
        Boolean,
        nullable=False,
        default=True,
        comment="Whether to send notifications to this recipient"
    )

    # Metadata
    created_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        comment="When this recipient was added"
    )

    last_notified_at = Column(
        DateTime,
        nullable=True,
        comment="Last time a notification was sent to this recipient"
    )

    # Relationships
    target_assignments = relationship(
        "TargetNotification",
        cascade="all, delete-orphan",
        foreign_keys="TargetNotification.recipient_id"
    )

    def __repr__(self):
        return f"<PushoverRecipient(id={self.id}, name='{self.name}', enabled={self.enabled})>"
