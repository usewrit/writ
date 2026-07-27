"""
WhatsApp configuration model.
"""
from datetime import datetime
from sqlalchemy import Column, Integer, String, Boolean, DateTime
from database import Base


class WhatsAppConfig(Base):
    """
    WhatsApp configuration table (via Twilio).

    Stores only WhatsApp-specific settings. Uses Twilio credentials from twilio_config table.
    """
    __tablename__ = "whatsapp_config"

    id = Column(Integer, primary_key=True, index=True)
    # Legacy fields - kept for backwards compatibility but should be nullable
    account_sid = Column(String, nullable=True, comment="DEPRECATED: Use twilio_config instead")
    auth_token = Column(String, nullable=True, comment="DEPRECATED: Use twilio_config instead")
    from_number = Column(String, nullable=False, comment="WhatsApp-enabled number (whatsapp:+1234567890)")
    enabled = Column(Boolean, nullable=False, default=False, comment="Enable WhatsApp notifications")
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self) -> str:
        return f"<WhatsAppConfig(from='{self.from_number}', enabled={self.enabled})>"
