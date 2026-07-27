"""
Twilio SMS recipient model.
"""
from datetime import datetime
from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey
from database import Base


class TwilioRecipient(Base):
    """
    Twilio SMS recipients table.

    Stores phone numbers that should receive SMS notifications.
    """
    __tablename__ = "twilio_recipients"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False, comment="Recipient display name")
    phone_number = Column(String, nullable=False, unique=True, comment="Phone number (E.164 format)")
    enabled = Column(Boolean, nullable=False, default=True, comment="Enable notifications for this recipient")
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    last_notified_at = Column(DateTime(timezone=True), nullable=True, comment="Last notification sent timestamp")

    def __repr__(self) -> str:
        return f"<TwilioRecipient(name='{self.name}', phone='{self.phone_number}', enabled={self.enabled})>"
