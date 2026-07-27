"""
Twilio SMS configuration model.
"""
from datetime import datetime
from sqlalchemy import Column, Integer, String, Boolean, DateTime
from database import Base


class TwilioConfig(Base):
    """
    Twilio SMS configuration table.

    Stores Twilio API credentials for SMS notifications.
    """
    __tablename__ = "twilio_config"

    id = Column(Integer, primary_key=True, index=True)
    account_sid = Column(String, nullable=False, comment="Twilio account SID")
    auth_token = Column(String, nullable=False, comment="Twilio auth token (encrypted)")
    from_phone = Column(String, nullable=False, comment="Source phone number (E.164 format)")
    enabled = Column(Boolean, nullable=False, default=False, comment="Enable Twilio SMS notifications")
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self) -> str:
        return f"<TwilioConfig(sid='{self.account_sid[:8]}...', from='{self.from_phone}', enabled={self.enabled})>"
