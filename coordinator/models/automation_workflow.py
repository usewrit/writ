"""
AutomationWorkflow model - defines reusable browser automation workflows.
"""
from datetime import datetime
from typing import Optional
from uuid import UUID as _UUID
from sqlalchemy import (
    Column,
    Integer,
    BigInteger,
    String,
    DateTime,
    Text,
    Boolean,
    Index,
    ForeignKey,
    select,
    text,
    JSON,
)
from sqlalchemy.orm import deferred, relationship
from database import Base


class AutomationWorkflow(Base):
    """
    AutomationWorkflow table - reusable browser automation workflows.

    Workflows define a sequence of steps (navigate, click, fill, AI fill form, etc.)
    that can be assigned to targets for:
    - Pre-check authentication (login before monitoring)
    - On-change reactive automation (auto-book, add to cart, submit form)
    """
    __tablename__ = "automation_workflows"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(
        String(100),
        nullable=False,
        index=True,
        comment="Human-readable workflow name"
    )
    description = Column(
        Text,
        nullable=True,
        comment="Workflow description"
    )
    workflow_type = Column(
        String(20),
        nullable=False,
        default="pre_check",
        index=True,
        comment="Type: 'pre_check' for auth, 'on_change' for reactive actions"
    )

    # Workflow definition (JSON array of steps)
    # Example:
    # [
    #   {"id": "1", "type": "navigate", "config": {"url": "https://example.com/login"}},
    #   {"id": "2", "type": "ai_fill_form", "config": {"use_workflow_data": true}},
    #   {"id": "3", "type": "click", "config": {"selector": "button[type=submit]"}},
    #   {"id": "4", "type": "wait", "config": {"condition": "url_contains", "value": "/dashboard"}},
    # ]
    steps = Column(
        JSON,
        nullable=False,
        default=[],
        comment="Array of workflow steps"
    )

    # Raw replay steps for fallback automation
    # When smart selectors fail, automation can fall back to exact coordinate-based replay
    # Example:
    # [
    #   {"type": "click", "x": 400, "y": 200, "viewport": {"width": 1280, "height": 720}, "wait_before": 100},
    #   {"type": "type", "text": "user@example.com", "viewport": {"width": 1280, "height": 720}},
    # ]
    raw_replay = Column(
        JSON,
        nullable=True,
        default=[],
        comment="Raw coordinate-based replay steps for fallback automation"
    )

    # Form data for AI agent to fill (key-value pairs)
    # Example:
    # {
    #   "email": "user@example.com",
    #   "first_name": "John",
    #   "last_name": "Doe",
    #   "phone": "+1234567890"
    # }
    form_data = Column(
        JSON,
        nullable=True,
        default={},
        comment="Form field data for AI to use when filling forms"
    )

    # Encrypted credentials (Fernet encrypted JSON)
    # Stored encrypted, decrypted only during execution
    # Example (decrypted):
    # {"password": "secret123", "api_key": "xyz"}
    credentials_encrypted = Column(
        Text,
        nullable=True,
        comment="Fernet-encrypted credentials JSON"
    )

    # Creator-authored per-input VALIDATION rules (regex guards) for a marketplace
    # listing. Maps a manifest slot key (an input OR secret NAME) -> a regex rule
    # the passed value must FULLY match. Set by the creator in the publish/edit
    # modal; stamped onto the data manifest (slot["validation"]) by
    # workflow_manifest.derive_data_manifest, and ENFORCED on every called/consumer
    # run in automation._apply_consumer_run_inversion — a value that does not match
    # its rule fails the run BEFORE dispatch (fail-closed). NULL = no rules.
    # Shape: {"version": 1, "rules": {"<slot_key>": {"pattern": str,
    #         "flags": "<subset of ims>", "message": str|None}}}.
    # A pattern is creator-authored metadata (not a creator secret VALUE), so it is
    # safe to ship in the data-less recipe/manifest. Patterns are ReDoS-validated
    # at save time and matched under a hard timeout at run time (services.input_rules).
    input_rules = Column(
        JSON,
        nullable=True,
        comment="Creator-set per-input regex validation rules ({version,rules:{slot_key:{pattern,flags,message}}}); enforced on consumer runs.",
    )

    # Entry and exit points
    entry_url = Column(
        Text,
        nullable=True,
        comment="URL where workflow starts (navigates here first)"
    )
    exit_condition = Column(
        JSON,
        nullable=True,
        comment="Success condition: {type: 'url_contains'|'url_equals'|'element_exists'|'element_text', value: '...'}"
    )

    # Execution settings
    timeout_ms = Column(
        Integer,
        nullable=False,
        default=30000,
        comment="Workflow timeout in milliseconds"
    )
    retry_count = Column(
        Integer,
        nullable=False,
        default=2,
        comment="Number of retries on failure"
    )
    headless = Column(
        Boolean,
        nullable=False,
        default=True,
        comment="Run browser in headless mode"
    )
    fast_mode = Column(
        Boolean,
        nullable=False,
        default=True,
        comment="Fast execution (True) vs human-like with anti-bot delays (False)"
    )

    # Workflow status
    is_active = Column(
        Boolean,
        nullable=False,
        default=True,
        index=True,
        comment="Whether workflow is active and can be executed"
    )
    is_verified = Column(
        Boolean,
        nullable=False,
        default=False,
        index=True,
        comment="Whether AI-generated workflow has been validated"
    )

    # Captcha handling
    captcha_blocked = Column(
        Boolean,
        nullable=False,
        default=False,
        index=True,
        comment="Workflow is blocked by captcha, routes only to captcha-trusted agents"
    )
    last_captcha_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="Last time captcha was detected for this workflow"
    )

    # Agent trust restriction
    trusted_agents_only = Column(
        Boolean,
        nullable=False,
        default=False,
        index=True,
        comment="Only allow execution on agents marked as trusted"
    )

    # Scheduled execution (for standalone workflows not attached to targets)
    schedule_enabled = Column(
        Boolean,
        nullable=False,
        default=False,
        index=True,
        comment="Whether workflow runs on a schedule"
    )
    schedule_interval_ms = Column(
        Integer,
        nullable=True,
        comment="Interval between scheduled executions in milliseconds"
    )
    # Structured recurrence (SPEC §1a). 'interval' preserves the existing
    # schedule_interval_ms behaviour byte-for-byte; 'daily'/'weekly' fire at a
    # local wall-clock time in schedule_tz. See services.schedule_recurrence.
    schedule_kind = Column(
        String(16),
        nullable=False,
        default="interval",
        server_default="interval",
        comment="Recurrence kind: 'interval' | 'daily' | 'weekly'"
    )
    schedule_time = Column(
        String(5),
        nullable=True,
        comment="'HH:MM' 24-hour local wall-clock time (daily/weekly)"
    )
    schedule_days = Column(
        JSON,
        nullable=True,
        comment="ISO weekday ints 1=Mon..7=Sun (weekly only)"
    )
    schedule_tz = Column(
        String(64),
        nullable=True,
        comment="IANA tz name (daily/weekly); NULL treated as UTC"
    )
    last_scheduled_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="When workflow was last scheduled for execution"
    )
    next_scheduled_at = Column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
        comment="When workflow should next be scheduled"
    )

    # AI Repair
    ai_repair_enabled = Column(
        Boolean, nullable=False, default=False, index=True,
        comment="Enable AI auto-repair on workflow failure"
    )
    ai_repair_history = Column(
        JSON, nullable=True,
        comment="Array of repair entries with old/new steps for diff"
    )
    last_repaired_at = Column(
        DateTime(timezone=True), nullable=True,
        comment="When the last AI repair was performed"
    )
    repair_count = Column(
        Integer, nullable=False, default=0,
        comment="Total number of successful AI repairs"
    )

    # Persona link: default authenticated identity supplying login + 2FA.
    # A run can override via persona_id; presence of a persona forces execution_target=cloud.
    default_persona_id = Column(
        Integer,
        ForeignKey("personas.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="Default persona supplying auth identity; a run can override"
    )

    # API Recorder: function definitions for api_recorded workflows
    api_functions = Column(
        JSON,
        nullable=True,
        default=None,
        comment="API function definitions for api_recorded workflows (function_name -> {request, response_extractions, parameters})"
    )

    # Metadata
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=datetime.utcnow,
        index=True,
        comment="When workflow was created"
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=True,
        onupdate=datetime.utcnow,
        comment="When workflow was last updated"
    )
    last_run_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="When workflow was last executed"
    )
    usage_count = Column(
        Integer,
        nullable=False,
        default=0,
        comment="Number of times workflow has been executed"
    )

    # Failure tracking
    total_run_count = Column(
        Integer,
        nullable=False,
        default=0,
        comment="Total number of executions"
    )
    total_failure_count = Column(
        Integer,
        nullable=False,
        default=0,
        comment="Total number of failed executions"
    )
    consecutive_failures = Column(
        Integer,
        nullable=False,
        default=0,
        comment="Current consecutive failure streak"
    )
    last_failure_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the last failure occurred"
    )
    last_failure_error = Column(
        Text,
        nullable=True,
        comment="Error message from the last failure"
    )

    # Execution stats
    estimated_duration_ms = Column(
        Integer,
        nullable=True,
        comment="Rolling average execution time in ms (updated on task completion)",
    )

    # Session persistence
    session_persistence = Column(
        Boolean,
        nullable=False,
        default=False,
        comment="Save browser session after execution and reuse on subsequent runs",
    )
    session_ttl_seconds = Column(
        Integer,
        nullable=True,
        comment="Max session age in seconds before forced re-login (null = cookie expiry only)",
    )
    login_url_patterns = Column(
        JSON,
        nullable=True,
        default=[],
        comment="URL patterns indicating a login page redirect (session expired)",
    )
    relogin_max_retries = Column(
        Integer,
        nullable=False,
        default=1,
        comment="Max re-login attempts when session is detected as expired",
    )

    # Browserless HTTP execution lane
    auth_config = Column(
        JSON,
        nullable=True,
        comment="Declarative AuthRecipe (login/refresh/probe/challenge steps + token map) for the "
        "browserless HTTP lane. Null = infer from steps.",
    )
    http_capable = Column(
        Boolean,
        nullable=True,
        comment="Runtime hint: did the last run complete over the browserless HTTP lane? Null=unknown.",
    )

    # Streaming mode config
    streaming_config = Column(
        JSON,
        nullable=True,
        comment="Streaming mode config: setup_steps_count, handlers, advanced_script, openai_compat",
    )

    # Callable functions — exposed as MCP tools or API handlers
    # [{name, type (steps|script|extraction), description, step_range, input_variables, output_fields, code, selector, ...}]
    functions = Column(
        JSON,
        nullable=True,
        comment="Callable functions: step-groups, script handlers, extraction queries",
    )

    execution_target = deferred(Column(
        String(20),
        nullable=False,
        default="auto",
        server_default="auto",
        comment="Where to execute: 'auto' (prefer local), 'local' (user recorder only), 'cloud' (SaaS only)",
    ))

    # The coordinator library holds plain local workflows only.

    # Relationships
    automation_tasks = relationship(
        "AutomationTask",
        back_populates="workflow",
        cascade="all, delete-orphan"
    )
    default_persona = relationship(
        "Persona",
        foreign_keys=[default_persona_id]
    )
    webhook_triggers = relationship(
        "WebhookTrigger",
        back_populates="workflow",
        cascade="all, delete-orphan"
    )
    streaming_sessions = relationship(
        "StreamingSession",
        back_populates="workflow",
        cascade="all, delete-orphan",
    )

    # Indexes
    __table_args__ = (
        Index('ix_automation_workflows_type_name', 'workflow_type', 'name'),
        # Due-workflows scan (central_scheduler): WHERE schedule_enabled AND is_active
        # AND schedule_interval_ms IS NOT NULL ORDER BY next_scheduled_at. Partial
        # index on next_scheduled_at keeps it tiny (only schedulable rows) and
        # already-ordered for the <= now range scan.
        Index(
            'ix_aw_due_schedule',
            'next_scheduled_at',
            postgresql_where=text(
                'schedule_enabled AND is_active AND schedule_interval_ms IS NOT NULL'
            ),
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<AutomationWorkflow(id={self.id}, name='{self.name}', "
            f"type='{self.workflow_type}', steps={len(self.steps or [])})>"
        )
