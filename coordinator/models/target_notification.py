"""
Target Notification Model - maps targets to pushover recipients for customized alerts.
"""
from sqlalchemy import Column, Integer, ForeignKey, DateTime, Index
from sqlalchemy.orm import relationship
from datetime import datetime

from database import Base


class TargetNotification(Base):
    """
    Target notification junction table.

    Maps which Pushover recipients should be notified when a specific target changes.
    If no recipients are assigned to a target, notifications are sent to all enabled recipients.
    """
    __tablename__ = "target_notifications"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)

    target_id = Column(
        Integer,
        ForeignKey("targets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Target that triggers notifications"
    )

    recipient_id = Column(
        Integer,
        ForeignKey("pushover_recipients.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Pushover recipient to notify"
    )

    created_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        comment="When this assignment was created"
    )

    # Relationships
    target = relationship("Target", foreign_keys=[target_id], overlaps="notification_assignments")
    recipient = relationship("PushoverRecipient", foreign_keys=[recipient_id], overlaps="target_assignments")

    # Unique constraint: each target can only be assigned to each recipient once
    __table_args__ = (
        Index('ix_target_recipient_unique', 'target_id', 'recipient_id', unique=True),
    )

    def __repr__(self):
        return f"<TargetNotification(target_id={self.target_id}, recipient_id={self.recipient_id})>"
