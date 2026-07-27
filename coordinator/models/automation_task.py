"""
AutomationTask model - queued automation tasks to be executed.
"""
from datetime import datetime
from typing import Optional
from sqlalchemy import (
    JSON,
    Column,
    Integer,
    String,
    DateTime,
    Text,
    Boolean,
    ForeignKey,
    Index,
)
from sqlalchemy.orm import relationship
from database import Base


class AutomationTask(Base):
    """
    AutomationTask table - queued automation tasks.

    When a target change is detected and on_change automation is enabled,
    a task is created and picked up by an automation executor (desktop agent).
    """
    __tablename__ = "automation_tasks"

    id = Column(Integer, primary_key=True, index=True)
    target_id = Column(
        Integer,
        ForeignKey("targets.id", ondelete="CASCADE"),
        nullable=True,  # Nullable for scheduled workflows not tied to a target
        index=True,
        comment="Target that triggered this task (null for scheduled workflows)"
    )
    workflow_id = Column(
        Integer,
        ForeignKey("automation_workflows.id", ondelete="CASCADE"),
        nullable=True,  # Nullable for AI navigation tasks that don't use workflows
        index=True,
        comment="Workflow to execute (null for AI navigation tasks)"
    )
    detected_change_id = Column(
        Integer,
        ForeignKey("detected_changes.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="Change that triggered this task (for on_change triggers)"
    )
    api_key_id = Column(
        Integer,
        ForeignKey("api_keys.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="API key that dispatched this task (null for JWT/dashboard runs)"
    )
    trigger_rule_id = Column(
        Integer,
        ForeignKey("trigger_rules.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="Unified trigger rule that created this task"
    )
    scrape_job_id = Column(
        Integer,
        ForeignKey("scrape_jobs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="Scrape job that created this task (for trigger_type='scraping')"
    )

    # Task status
    # pending -> assigned -> running -> success/failed/timeout/cancelled
    status = Column(
        String(20),
        nullable=False,
        default="pending",
        index=True,
        comment="Task status: pending, assigned, running, success, failed, timeout, cancelled"
    )

    # Trigger info
    trigger_type = Column(
        String(20),
        nullable=False,
        default="on_change",
        comment="What triggered this task: on_change, manual, scheduled, on_auth_fail, ai_session, scraping"
    )

    # Execution details
    started_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="When task execution started"
    )
    completed_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="When task execution completed"
    )
    request_received_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the consumer's call entered the platform (run dispatch / MCP boundary). "
                "NOT created_at (queue-insert) and NOT started_at (slot-acquired). "
                "Stamped into trigger_context._latency to survive the async hop, read back here."
    )
    response_latency_ms = Column(
        Integer,
        nullable=True,
        comment="Consumer-facing wait in ms = completed_at - request_received_at "
                "(queue wait + slot acquisition + warm/cold start + execution + return). "
                "Distinct from the duration_ms @property (execution-only)."
    )
    executor_agent_id = Column(
        String(100),
        nullable=True,
        index=True,
        comment="Agent ID that is executing/executed this task"
    )

    # Trigger context - additional context from unified triggers
    trigger_context = Column(
        JSON,
        nullable=True,
        comment="Additional context from trigger (extracted data, rendered templates, etc.)"
    )

    # Results
    success = Column(
        Boolean,
        nullable=True,
        comment="Whether task completed successfully"
    )
    result_data = Column(
        JSON,
        nullable=True,
        comment="Extracted data, confirmation details, etc."
    )
    error_message = Column(
        Text,
        nullable=True,
        comment="Error message if task failed"
    )
    # Failure classification (set only on failure; drives billing — see
    # services/failure_classifier.py). fault_domain ∈ creator|infra|buyer|unknown
    # decides whether the BUYER is charged compute (creator/infra = platform
    # absorbs; buyer/unknown = buyer pays). failure_category is the fine-grained
    # signature for analytics/tuning. Both backend-determined, never agent-reported.
    fault_domain = Column(
        String(20),
        nullable=True,
        index=True,
        comment="Billing fault domain on failure: creator|infra|buyer|unknown",
    )
    failure_category = Column(
        String(40),
        nullable=True,
        comment="Fine-grained failure signature (e.g. selector_not_found, nav_timeout)",
    )
    screenshots = Column(
        JSON,
        nullable=True,
        default=[],
        comment="Array of screenshot references [{step: 1, path: 's3://...'}]"
    )

    # AI Repair
    ai_repair_attempted = Column(
        Boolean, nullable=False, default=False,
        comment="Whether AI repair was attempted for this task"
    )
    ai_repair_result = Column(
        JSON, nullable=True,
        comment="AI repair result data"
    )

    # Retry tracking
    attempt_count = Column(
        Integer,
        nullable=False,
        default=0,
        comment="Number of execution attempts"
    )
    max_attempts = Column(
        Integer,
        nullable=False,
        default=3,
        comment="Maximum retry attempts"
    )

    # Queue management
    queue_priority = Column(
        Integer,
        nullable=True,
        comment="Queue priority: 0=direct (high), 10=scheduled (low)"
    )
    queue_expires_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="When this queued task expires and should be cancelled"
    )
    queue_traffic_type = Column(
        String(20),
        nullable=True,
        comment="Traffic type: direct | scheduled"
    )

    # Timestamps
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=datetime.utcnow,
        index=True,
        comment="When task was queued"
    )

    # Relationships
    target = relationship("Target", back_populates="automation_tasks")
    workflow = relationship("AutomationWorkflow", back_populates="automation_tasks")
    detected_change = relationship("DetectedChange")
    trigger_rule = relationship("TriggerRule")
    scrape_job = relationship("ScrapeJob", back_populates="automation_tasks")

    # Indexes
    __table_args__ = (
        Index('ix_automation_tasks_status_created', 'status', 'created_at'),
        Index('ix_automation_tasks_target_status', 'target_id', 'status'),
        Index('ix_automation_tasks_queued_priority', 'status', 'queue_priority', 'created_at'),
        # Per-traffic-class queue dequeue (workflow_queue.dequeue_batch): filter by
        # status + queue_traffic_type, order by queue_priority then created_at.
        Index('ix_automation_tasks_queue_dequeue',
              'status', 'queue_traffic_type', 'queue_priority', 'created_at'),
        # Autoscaler / per-agent concurrency scans (autoscale_controller, earnings,
        # agent_errors): filter by executor_agent_id + status, order/filter on completed_at.
        Index('ix_automation_tasks_executor_status_completed',
              'executor_agent_id', 'status', 'completed_at'),
    )

    def __repr__(self) -> str:
        return (
            f"<AutomationTask(id={self.id}, target_id={self.target_id}, "
            f"workflow_id={self.workflow_id}, status='{self.status}')>"
        )

    @property
    def can_retry(self) -> bool:
        """Check if task can be retried."""
        return self.attempt_count < self.max_attempts and self.status in ('failed', 'timeout')

    @property
    def duration_ms(self) -> Optional[int]:
        """Calculate execution duration in milliseconds."""
        if self.started_at and self.completed_at:
            delta = self.completed_at - self.started_at
            return int(delta.total_seconds() * 1000)
        return None
