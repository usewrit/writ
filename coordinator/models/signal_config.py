"""
Signal configuration model.
"""
from datetime import datetime
from sqlalchemy import Column, Integer, String, Boolean, DateTime
from database import Base


class SignalConfig(Base):
    """
    Signal configuration table.

    Stores Signal API credentials for notifications.
    """
    __tablename__ = "signal_config"

    id = Column(Integer, primary_key=True, index=True)
    api_url = Column(String, nullable=False, comment="signal-cli REST API URL")
    sender_number = Column(String, nullable=False, comment="Signal sender phone number (E.164)")
    enabled = Column(Boolean, nullable=False, default=False, comment="Enable Signal notifications")
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self) -> str:
        return f"<SignalConfig(sender='{self.sender_number}', enabled={self.enabled})>"
