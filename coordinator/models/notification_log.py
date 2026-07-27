"""
Notification Log model - tracks sent notifications for rate limiting.
"""
from datetime import datetime
from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey, Index
from database import Base


class NotificationLog(Base):
    """
    Notification Log table - tracks sent notifications.

    Used for rate limiting enforcement.
    """
    __tablename__ = "notification_logs"

    id = Column(Integer, primary_key=True, index=True)
    target_id = Column(Integer, ForeignKey("targets.id"), nullable=False, index=True)
    provider = Column(String(20), nullable=False)  # pushover, email, twilio, whatsapp, signal
    sent_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow, index=True)
    success = Column(Boolean, nullable=False, default=True)

    # Indexes for efficient rate limit queries
    __table_args__ = (
        Index("ix_notification_logs_target_time", "target_id", "sent_at"),
    )

    def __repr__(self) -> str:
        return f"<NotificationLog(target_id={self.target_id}, provider='{self.provider}', sent_at='{self.sent_at}')>"
