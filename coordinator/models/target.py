"""
Target model - represents URLs to be monitored.
"""
from datetime import datetime
from typing import Optional
from sqlalchemy import (
    JSON,
    Column,
    Integer,
    String,
    Boolean,
    DateTime,
    Text,
    Index,
    ForeignKey,
)
from sqlalchemy.orm import relationship
from database import Base


class Target(Base):
    """
    Target table - URLs and selectors for content monitoring.

    Each target represents a web page that agents will periodically check
    for changes using the specified CSS selector.
    """
    __tablename__ = "targets"

    id = Column(Integer, primary_key=True, index=True)
    url = Column(
        String(2048),
        nullable=False,
        index=True,
        comment="URL to monitor"
    )
    check_type = Column(
        String(20),
        nullable=False,
        default='content',
        index=True,
        comment="Type of check: 'content' or 'uptime'"
    )
    execution_mode = Column(
        String(20),
        nullable=False,
        default='both',
        index=True,
        comment="Execution mode: 'vps' (traditional agents), 'cloudflare' (serverless), or 'both'"
    )
    selector = Column(
        String(512),
        nullable=True,  # Nullable for uptime checks
        comment="CSS selector for content extraction (required for content checks)"
    )
    ignore_regex = Column(
        Text,
        nullable=True,
        comment="Regex pattern for content to ignore (e.g., timestamps)"
    )
    check_period_ms = Column(
        Integer,
        nullable=True,
        comment="Custom check period for this target in milliseconds (null = use global period)"
    )
    # Structured recurrence (SPEC §1a). 'interval' preserves the existing
    # check_period_ms behaviour byte-for-byte; 'daily'/'weekly' fire at a local
    # wall-clock time in schedule_tz. See services.schedule_recurrence.
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
    # Uptime check configuration
    expected_status_code = Column(
        Integer,
        nullable=True,
        default=200,
        comment="Expected HTTP status code for uptime checks (default: 200)"
    )
    timeout_ms = Column(
        Integer,
        nullable=True,
        default=10000,
        comment="Timeout for uptime checks in milliseconds (default: 10000)"
    )
    max_response_time_ms = Column(
        Integer,
        nullable=True,
        default=5000,
        comment="Alert if response time exceeds this value (default: 5000ms)"
    )
    check_ssl = Column(
        Boolean,
        nullable=True,
        default=True,
        comment="Check SSL certificate for uptime monitors (default: True for HTTPS)"
    )
    enabled = Column(
        Boolean,
        nullable=False,
        default=True,
        index=True,
        comment="Whether this target is actively monitored"
    )
    baseline_hash = Column(
        String(64),
        nullable=True,
        comment="SHA256 hash of baseline content"
    )
    baseline_content = Column(
        Text,
        nullable=True,
        comment="Baseline content for comparison"
    )
    baseline_fetched_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="When baseline was fetched"
    )
    user_agent_hashes = Column(
        JSON,
        nullable=True,
        server_default='{}',
        comment="Content hashes for different user agents {user_agent: hash}"
    )
    notification_providers = Column(
        JSON,
        nullable=True,
        server_default='{}',
        comment="Enabled notification providers for this target {provider: true/false}"
    )
    provider_notification_settings = Column(
        JSON,
        nullable=True,
        server_default='{}',
        comment="Per-provider notification customization (title, message, priority, etc.)"
    )
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=datetime.utcnow,
        comment="Target creation timestamp"
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=True,
        onupdate=datetime.utcnow,
        comment="Target last update timestamp"
    )
    last_checked_at = Column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
        comment="When the fleet last CHECKED this target (stamped on every agent "
                "check-report batch, change or no-change) — powers 'last checked N "
                "ago' and verifies checks are firing at their interval."
    )

    # Notification customization (optional per-target overrides)
    notification_title = Column(
        String(250),
        nullable=True,
        comment="Custom notification title for this target (overrides global)"
    )
    notification_message = Column(
        String(1024),
        nullable=True,
        comment="Custom notification message for this target (overrides global)"
    )
    notification_priority = Column(
        Integer,
        nullable=True,
        comment="Custom priority for this target: -2 to 2 (overrides global)"
    )
    notification_sound = Column(
        String(50),
        nullable=True,
        comment="Custom notification sound for this target (overrides global)"
    )

    # Geo-distribution
    preferred_region = Column(
        String(50),
        nullable=True,
        index=True,
        comment="Preferred agent region for checks (null = any region)"
    )

    # Automation configuration
    requires_playwright = Column(
        Boolean,
        nullable=False,
        default=False,
        index=True,
        comment="Whether this target requires a Playwright-capable agent"
    )
    pre_check_workflow_id = Column(
        Integer,
        ForeignKey("automation_workflows.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="Workflow to run before each check (e.g., login flow)"
    )
    on_change_workflow_id = Column(
        Integer,
        ForeignKey("automation_workflows.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="Workflow to run when change detected (e.g., auto-book, add to cart)"
    )
    on_change_enabled = Column(
        Boolean,
        nullable=False,
        default=False,
        comment="Whether on-change automation is enabled"
    )
    on_change_conditions = Column(
        JSON,
        nullable=True,
        comment="Conditions for triggering on-change automation {content_contains, content_not_contains, etc.}"
    )
    on_change_in_session = Column(
        Boolean,
        nullable=False,
        default=False,
        comment="Run the on_change_workflow INSIDE the live check session (same browser) "
                "instead of dispatching a fresh AutomationTask. Capable agents receive the "
                "workflow in their target payload and report it back as handled in-session; "
                "falls back to a fresh dispatch when the agent can't reuse its session."
    )
    auth_session_encrypted = Column(
        Text,
        nullable=True,
        comment="Fernet-encrypted auth session (cookies, headers, tokens) from pre-check automation"
    )
    setup_steps = Column(
        Text,
        nullable=True,
        comment="Inline setup-steps manifest (JSON: {steps, credentials}) of recorded "
                "login/navigate/click steps replayed in the browser BEFORE the content "
                "check. Mirrors the local daemon's targets.setup_steps column. When "
                "present it is dispatched to the recorder agent as pre_check_workflow, "
                "forcing the browser path (Target::needs_browser)."
    )
    persona_id = Column(
        Integer,
        ForeignKey("personas.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="Persona supplying auth for this check (alternative to pre_check_workflow_id)"
    )
    fetch_key = Column(
        String(64),
        nullable=True,
        index=True,
        comment="Coalescing key (services.monitor_coalescing): targets sharing a "
                "key share ONE physical fetch. Anonymous checks with the same "
                "url+interval+browser+region coalesce; personalized checks are "
                "pinned to their own id and never group."
    )

    # Relationships
    detected_changes = relationship(
        "DetectedChange",
        back_populates="target",
        cascade="all, delete-orphan"
    )
    notification_assignments = relationship(
        "TargetNotification",
        cascade="all, delete-orphan",
        foreign_keys="TargetNotification.target_id"
    )
    automation_tasks = relationship(
        "AutomationTask",
        back_populates="target",
        cascade="all, delete-orphan"
    )
    pre_check_workflow = relationship(
        "AutomationWorkflow",
        foreign_keys=[pre_check_workflow_id],
        lazy="select"
    )
    on_change_workflow = relationship(
        "AutomationWorkflow",
        foreign_keys=[on_change_workflow_id],
        lazy="select"
    )
    selectors = relationship(
        "TargetSelector",
        back_populates="target",
        cascade="all, delete-orphan",
        order_by="TargetSelector.priority.desc()"
    )
    trigger_rules = relationship(
        "TriggerRule",
        back_populates="target",
        cascade="all, delete-orphan",
        order_by="TriggerRule.priority.desc()"
    )
    webhook_triggers = relationship(
        "WebhookTrigger",
        back_populates="target",
        cascade="all, delete-orphan"
    )

    # Indexes
    __table_args__ = (
        Index("ix_targets_enabled_created", "enabled", "created_at"),
    )

    @property
    def active_selectors(self):
        """Get all enabled selectors for this target."""
        return [s for s in self.selectors if s.enabled]

    @property
    def primary_selector_obj(self):
        """Get the highest priority enabled selector (for backward compatibility)."""
        active = self.active_selectors
        return active[0] if active else None

    @property
    def enabled_triggers(self):
        """Get all enabled trigger rules for this target."""
        return [t for t in self.trigger_rules if t.enabled]

    def __repr__(self) -> str:
        return f"<Target(url='{self.url[:50]}...', enabled={self.enabled})>"

    def to_dict(self) -> dict:
        """Convert target to dictionary representation."""
        return {
            "id": self.id,
            "url": self.url,
            "selector": self.selector,
            "ignore_regex": self.ignore_regex,
            "enabled": self.enabled,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
