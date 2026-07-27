"""
Webhook recipient model.

Stores individual webhook endpoints to send notifications to.
"""
from sqlalchemy import Column, Integer, String, Boolean, DateTime, JSON, Text, ForeignKey
from sqlalchemy.sql import func
from database import Base


class WebhookRecipient(Base):
    """
    Webhook endpoint configuration.

    Each recipient represents a webhook URL to send notifications to.
    """
    __tablename__ = "webhook_recipients"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)  # Friendly name
    enabled = Column(Boolean, default=True, nullable=False)

    # Webhook endpoint
    url = Column(Text, nullable=False)  # Webhook URL
    method = Column(String(10), default="POST", nullable=False)  # HTTP method

    # Authentication
    secret = Column(String(255), nullable=True)  # HMAC secret for signing
    headers = Column(JSON, nullable=True)  # Custom headers {"Authorization": "Bearer xxx"}

    # Filtering
    event_types = Column(JSON, nullable=True)  # List of event types to send, null = all
    # Example: ["change_detected", "workflow_completed", "error"]

    # Retry settings (override global)
    timeout = Column(Integer, nullable=True)  # Override default timeout
    max_retries = Column(Integer, nullable=True)  # Override default retries

    # Payload customization
    payload_template = Column(Text, nullable=True)  # Custom payload template (JSON)
    include_diff = Column(Boolean, default=True)  # Include diff in payload
    include_content = Column(Boolean, default=False)  # Include full content

    # Statistics
    last_success_at = Column(DateTime(timezone=True), nullable=True)
    last_failure_at = Column(DateTime(timezone=True), nullable=True)
    success_count = Column(Integer, default=0)
    failure_count = Column(Integer, default=0)

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    def __repr__(self):
        return f"<WebhookRecipient {self.name} ({self.url[:50]})>"

    def to_dict(self):
        """Convert to dictionary for API responses."""
        return {
            "id": self.id,
            "name": self.name,
            "enabled": self.enabled,
            "url": self.url,
            "method": self.method,
            "has_secret": bool(self.secret),
            "headers": self.headers,
            "event_types": self.event_types,
            "timeout": self.timeout,
            "max_retries": self.max_retries,
            "include_diff": self.include_diff,
            "include_content": self.include_content,
            "last_success_at": self.last_success_at.isoformat() if self.last_success_at else None,
            "last_failure_at": self.last_failure_at.isoformat() if self.last_failure_at else None,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
