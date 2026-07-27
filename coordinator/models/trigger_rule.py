"""
Trigger Rule model - unified trigger system for change events.

A TriggerRule defines:
1. What conditions must be met (on extracted data)
2. What actions to execute when conditions match
"""
from datetime import datetime
from sqlalchemy import (
    JSON,
    Column,
    Integer,
    BigInteger,
    String,
    Boolean,
    DateTime,
    Text,
    ForeignKey,
    Index,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from database import Base


class TriggerRule(Base):
    """
    Unified trigger rule that evaluates conditions and dispatches actions.

    Event types:
    - change_detected: Fires when content/selector changes (default)
    - webhook_received: Fires when a webhook is called
    - ai_session_started: Fires when an AI session starts
    - ai_session_completed: Fires when an AI session completes (success or error)
    - workflow_started: Fires when a workflow starts
    - workflow_completed: Fires when a workflow completes (success or error)

    Actions are polymorphic:
    - notification: Send email, webhook, Pushover, etc.
    - ai_session: Run one or more AI sessions
    - workflow: Execute an automation workflow
    - return_data: Return extracted data to caller
    """
    __tablename__ = "trigger_rules"

    id = Column(Integer, primary_key=True, index=True)
    target_id = Column(
        Integer,
        ForeignKey("targets.id", ondelete="CASCADE"),
        nullable=True,  # Nullable for global triggers (e.g., any AI session)
        index=True,
        comment="Target this trigger belongs to (null for global triggers)"
    )

    # Event type this trigger listens to
    event_type = Column(
        String(50),
        nullable=False,
        default="change_detected",
        index=True,
        comment="Event type: change_detected, webhook_received, ai_session_started, ai_session_completed, workflow_started, workflow_completed, monitor_down, monitor_stale, monitor_recovered"
    )

    # Optional: limit to specific selector (for change_detected events)
    target_selector_id = Column(
        Integer,
        ForeignKey("target_selectors.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="If set, only fires for changes on this selector"
    )

    # Optional: limit to specific workflow (for workflow_* events)
    workflow_id = Column(
        Integer,
        ForeignKey("automation_workflows.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="If set, only fires for this specific workflow"
    )

    # Optional: link to specific webhook trigger (for webhook_received events)
    webhook_trigger_id = Column(
        Integer,
        ForeignKey("webhook_triggers.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="If set, only fires when this specific webhook is called"
    )

    # Trigger identification
    name = Column(
        String(255),
        nullable=False,
        comment="Human-readable name"
    )
    description = Column(
        Text,
        nullable=True,
        comment="Description of what this trigger does"
    )
    enabled = Column(
        Boolean,
        default=True,
        index=True,
        comment="Whether this trigger is active"
    )
    priority = Column(
        Integer,
        default=0,
        comment="Execution order (higher = first)"
    )

    # Conditions (all must match to fire)
    conditions = Column(
        JSON,
        nullable=True,
        server_default='{}',
        comment="""Conditions to evaluate. Examples:
        {
            // On extracted data
            "extracted.price": {"operator": "lt", "value": 100},
            "extracted.url": {"operator": "changed"},
            "extracted.status": {"operator": "equals", "value": "in_stock"},

            // On change metadata
            "diff_size": {"operator": "gte", "value": 50},
            "content": {"operator": "contains", "value": "error"},
            "content": {"operator": "not_contains", "value": "success"},

            // Schedule constraints
            "schedule": {
                "time_window": {"start": "09:00", "end": "18:00"},
                "days_of_week": [1,2,3,4,5],
                "cooldown_minutes": 60
            }
        }
        """
    )

    # Actions to execute (array of action configs)
    actions = Column(
        JSON,
        nullable=False,
        server_default='[]',
        comment="""Actions to execute when conditions match. Array of:
        [
            {
                "type": "notification",
                "config": {
                    "channels": ["email", "webhook"],
                    "template": "Price dropped to {{extracted.price}}!"
                }
            },
            {
                "type": "ai_session",
                "config": {
                    "session_ids": [1, 2, 3],
                    "context_template": {"price": "{{extracted.price}}"}
                }
            },
            {
                "type": "workflow",
                "config": {
                    "workflow_id": 5,
                    "input_mapping": {"url": "{{extracted.url}}"}
                }
            }
        ]
        """
    )

    # Block-based workflow chain (new visual builder format)
    blocks = Column(
        JSON,
        nullable=True,
        server_default=None,
        comment="""Block chain for visual workflow builder. Array of blocks:
        [
            {"id": "block_1", "type": "event", "blockType": "change_detected", "config": {"selector_id": 5}},
            {"id": "block_2", "type": "condition", "blockType": "condition", "config": {"field": "extracted.price", "operator": "lt", "value": "100"}},
            {"id": "block_3", "type": "action", "blockType": "ai_session", "config": {"session_ids": [1]}},
            {"id": "block_4", "type": "event", "blockType": "ai_session_completed", "config": {}},
            {"id": "block_5", "type": "action", "blockType": "notification", "config": {"template": "Done!"}}
        ]
        When blocks is set, it takes precedence over event_type/conditions/actions for processing.
        """
    )

    # Execution tracking
    last_triggered_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="When this trigger last fired"
    )
    trigger_count = Column(
        Integer,
        default=0,
        comment="Total times this trigger has fired"
    )

    # Timestamps
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now()
    )
    updated_at = Column(
        DateTime(timezone=True),
        onupdate=func.now()
    )

    # Relationships
    target = relationship("Target", back_populates="trigger_rules")
    target_selector = relationship("TargetSelector")
    workflow = relationship("AutomationWorkflow")
    executions = relationship(
        "TriggerExecution",
        back_populates="trigger_rule",
        cascade="all, delete-orphan",
        order_by="TriggerExecution.triggered_at.desc()"
    )

    __table_args__ = (
        Index("ix_trigger_rules_target_enabled", "target_id", "enabled"),
    )

    def __repr__(self) -> str:
        return f"<TriggerRule(id={self.id}, name='{self.name}', event_type='{self.event_type}', enabled={self.enabled})>"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "target_id": self.target_id,
            "event_type": self.event_type,
            "target_selector_id": self.target_selector_id,
            "workflow_id": self.workflow_id,
            "name": self.name,
            "description": self.description,
            "enabled": self.enabled,
            "priority": self.priority,
            "conditions": self.conditions,
            "actions": self.actions,
            "blocks": self.blocks,
            "last_triggered_at": self.last_triggered_at.isoformat() if self.last_triggered_at else None,
            "trigger_count": self.trigger_count,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class TriggerExecution(Base):
    """
    Tracks each execution of a trigger for logging and debugging.
    """
    __tablename__ = "trigger_executions"

    id = Column(Integer, primary_key=True, index=True)
    trigger_rule_id = Column(
        Integer,
        ForeignKey("trigger_rules.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )
    detected_change_id = Column(
        Integer,
        ForeignKey("detected_changes.id", ondelete="SET NULL"),
        nullable=True,
        index=True
    )

    # Execution status
    status = Column(
        String(50),
        default="pending",
        index=True,
        comment="Status: pending/running/completed/failed/skipped"
    )

    # Context at trigger time
    trigger_context = Column(
        JSON,
        nullable=True,
        comment="Snapshot of extracted data and conditions at trigger time"
    )

    # Action results (array matching actions array)
    action_results = Column(
        JSON,
        nullable=True,
        server_default='[]',
        comment="Results from each action execution"
    )

    # Timing
    triggered_at = Column(
        DateTime(timezone=True),
        server_default=func.now()
    )
    completed_at = Column(
        DateTime(timezone=True),
        nullable=True
    )

    # Error handling
    error_message = Column(Text, nullable=True)

    # Relationships
    trigger_rule = relationship("TriggerRule", back_populates="executions")
    detected_change = relationship("DetectedChange")

    # Indexes already created by index=True on columns above

    def __repr__(self) -> str:
        return f"<TriggerExecution(id={self.id}, rule_id={self.trigger_rule_id}, status='{self.status}')>"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "trigger_rule_id": self.trigger_rule_id,
            "detected_change_id": self.detected_change_id,
            "status": self.status,
            "trigger_context": self.trigger_context,
            "action_results": self.action_results,
            "triggered_at": self.triggered_at.isoformat() if self.triggered_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "error_message": self.error_message,
        }
