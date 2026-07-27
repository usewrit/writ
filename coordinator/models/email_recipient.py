"""
Email recipient model.
"""
from datetime import datetime
from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey
from database import Base


class EmailRecipient(Base):
    """
    Email recipients table.

    Stores email addresses that should receive notifications.
    """
    __tablename__ = "email_recipients"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False, comment="Recipient display name")
    email = Column(String, nullable=False, unique=True, comment="Recipient email address")
    enabled = Column(Boolean, nullable=False, default=True, comment="Enable notifications for this recipient")
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    last_notified_at = Column(DateTime(timezone=True), nullable=True, comment="Last notification sent timestamp")

    def __repr__(self) -> str:
        return f"<EmailRecipient(name='{self.name}', email='{self.email}', enabled={self.enabled})>"
