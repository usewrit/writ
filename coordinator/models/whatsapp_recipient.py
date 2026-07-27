"""
WhatsApp recipient model.
"""
from datetime import datetime
from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey
from database import Base


class WhatsAppRecipient(Base):
    """
    WhatsApp recipients table.

    Stores WhatsApp numbers that should receive notifications.
    """
    __tablename__ = "whatsapp_recipients"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False, comment="Recipient display name")
    phone_number = Column(String, nullable=False, unique=True, comment="WhatsApp number (whatsapp:+1234567890 or +1234567890)")
    enabled = Column(Boolean, nullable=False, default=True, comment="Enable notifications for this recipient")
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    last_notified_at = Column(DateTime(timezone=True), nullable=True, comment="Last notification sent timestamp")

    def __repr__(self) -> str:
        return f"<WhatsAppRecipient(name='{self.name}', phone='{self.phone_number}', enabled={self.enabled})>"
