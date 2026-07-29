"""
Webhook trigger model.

Stores configuration for incoming webhooks that trigger workflows or actions.
"""
import secrets
from sqlalchemy import Column, Integer, String, Boolean, DateTime, JSON, Text, ForeignKey, Index
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from database import Base


def generate_webhook_token():
    """Generate a secure random token for webhook URLs."""
    return secrets.token_urlsafe(24)  # 32 characters, URL-safe


class WebhookTrigger(Base):
    """
    Incoming webhook trigger configuration.

    Allows external systems to trigger workflows via HTTP POST.
    """
    __tablename__ = "webhook_triggers"

    id = Column(Integer, primary_key=True, index=True)
    token = Column(String(64), unique=True, nullable=False, default=generate_webhook_token, index=True)
    name = Column(String(255), nullable=False)
    enabled = Column(Boolean, default=True, nullable=False)

    # Authentication
    secret = Column(String(255), nullable=True)  # HMAC secret for verifying incoming requests

    # Action configuration
    workflow_id = Column(Integer, ForeignKey("automation_workflows.id", ondelete="SET NULL"), nullable=True)
    target_id = Column(Integer, ForeignKey("targets.id", ondelete="SET NULL"), nullable=True)
    action = Column(String(50), default="run_workflow", nullable=False)
    # Actions: run_workflow, check_target, run_ai_session

    # Payload mapping - map incoming JSON fields to workflow form_data
    # Example: {"email": "$.data.user.email", "name": "$.data.user.name"}
    payload_mapping = Column(JSON, nullable=True)

    # Conditions - only trigger if conditions match
    # Example: {"event_type": "user.created", "source": "stripe"}
    conditions = Column(JSON, nullable=True)

    # Response behavior
    wait_for_result = Column(Boolean, default=False, nullable=False,
                             comment="Wait for workflow completion and return extracted data by default")
    wait_timeout = Column(Integer, default=120, nullable=False,
                          comment="Max seconds to wait for result (10-300)")

    # Custom URL path: /api/v1/webhooks/{custom_path} as alternative to /webhooks/hook/{token}
    custom_path = Column(String(100), unique=True, nullable=True, index=True,
                         comment="Custom URL path: /api/v1/webhooks/{custom_path}")

    # API Recorder: target a specific function within an api_recorded workflow
    function_name = Column(String(100), nullable=True, index=True,
                           comment="Function name within api_recorded workflow for per-function endpoints")

    # Statistics
    last_triggered_at = Column(DateTime(timezone=True), nullable=True)
    trigger_count = Column(Integer, default=0)

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # Relationships
    workflow = relationship("AutomationWorkflow", back_populates="webhook_triggers", foreign_keys=[workflow_id])
    target = relationship("Target", back_populates="webhook_triggers", foreign_keys=[target_id])

    # Indexes — index the FK columns (previously unindexed): speeds up lookups by
    # parent and the cascade nullification on parent delete (ON DELETE SET NULL).
    __table_args__ = (
        Index("ix_webhook_triggers_workflow_id", "workflow_id"),
        Index("ix_webhook_triggers_target_id", "target_id"),
    )

    def __repr__(self):
        return f"<WebhookTrigger {self.name} (action={self.action})>"

    def to_dict(self):
        """Convert to dictionary for API responses."""
        return {
            "id": self.id,
            "token": self.token,
            "name": self.name,
            "enabled": self.enabled,
            "has_secret": bool(self.secret),
            "workflow_id": self.workflow_id,
            "target_id": self.target_id,
            "action": self.action,
            "payload_mapping": self.payload_mapping,
            "conditions": self.conditions,
            "wait_for_result": self.wait_for_result,
            "wait_timeout": self.wait_timeout,
            "custom_path": self.custom_path,
            "function_name": self.function_name,
            "last_triggered_at": self.last_triggered_at.isoformat() if self.last_triggered_at else None,
            "trigger_count": self.trigger_count,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            # Generate webhook URL using token for security
            "webhook_path": f"/api/webhooks/hook/{self.token}",
            # The readable twin, when a custom path was set. Emitted so a client can
            # DISCOVER the callable URL: the path alone was already in this payload,
            # but nothing told a caller what to prefix it with, and the two routes
            # authenticate differently (token + HMAC vs API key). None when unset, so
            # a client can render one affordance or the other without guessing.
            "custom_webhook_path": (
                f"/api/v1/webhooks/{self.custom_path}" if self.custom_path else None
            ),
        }
