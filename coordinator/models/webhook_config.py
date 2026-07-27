"""
Webhook configuration model.

Stores the coordinator's webhook settings (single-owner).
"""
from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey
from sqlalchemy.sql import func
from database import Base


class WebhookConfig(Base):
    """
    Webhook notification configuration (single-owner coordinator).

    Stores default settings for webhook notifications.
    """
    __tablename__ = "webhook_configs"

    id = Column(Integer, primary_key=True, index=True)
    enabled = Column(Boolean, default=False, nullable=False)

    # Default settings
    default_timeout = Column(Integer, default=30, nullable=False)  # seconds
    default_retries = Column(Integer, default=3, nullable=False)
    default_method = Column(String(10), default="POST", nullable=False)

    # Global secret for signing (can be overridden per-recipient)
    global_secret = Column(String(255), nullable=True)

    # Rate limiting
    rate_limit_enabled = Column(Boolean, default=False)
    rate_limit_per_minute = Column(Integer, default=60)

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    def __repr__(self):
        return f"<WebhookConfig enabled={self.enabled}>"
