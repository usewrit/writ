"""
ScrapeJob model - defines what to scrape and how often.

A ScrapeJob represents a recurring scraping task (e.g., "Get popular times for Starbucks Montreal").
When due, it creates AutomationTask entries that get picked up by agents (worker-templates or desktop).
"""
from datetime import datetime
from sqlalchemy import (
    JSON,
    Column,
    Integer,
    String,
    DateTime,
    Text,
    Boolean,
    Index,
    ForeignKey,
)
from sqlalchemy.orm import relationship
from database import Base


class ScrapeJob(Base):
    """
    ScrapeJob table - recurring scraping job definitions.

    Supports multiple scrape types (popular_times, custom_extract) and
    distributes work across agent tiers (worker, desktop, recorder).
    """
    __tablename__ = "scrape_jobs"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(
        String(200),
        nullable=False,
        index=True,
        comment="Human-readable job name, e.g. 'Montreal Popular Times'"
    )
    description = Column(Text, nullable=True)

    # Scrape type
    scrape_type = Column(
        String(50),
        nullable=False,
        default="popular_times",
        index=True,
        comment="Type of scrape: popular_times, custom_extract"
    )

    # Target configuration (varies by scrape_type)
    # For popular_times: {place_query, place_id?, coordinates?: {lat, lng}}
    # For custom_extract: {url, selectors: {...}}
    target_config = Column(
        JSON,
        nullable=False,
        comment="Scrape target configuration (schema varies by scrape_type)"
    )

    # Scheduling
    schedule_enabled = Column(
        Boolean,
        nullable=False,
        default=True,
        index=True,
        comment="Whether this job runs on a schedule"
    )
    schedule_interval_ms = Column(
        Integer,
        nullable=False,
        default=3600000,
        comment="Interval between scrape runs in ms (default: 1 hour)"
    )
    last_scheduled_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the last scheduled run was triggered"
    )
    next_scheduled_at = Column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
        comment="When the next scheduled run should trigger"
    )

    # Tier preference
    preferred_tier = Column(
        String(20),
        nullable=False,
        default="auto",
        comment="Preferred execution tier: worker, desktop, recorder, auto"
    )

    # Anti-detection settings
    anti_detection_config = Column(
        JSON,
        nullable=True,
        default={},
        comment="Anti-detection config: {random_delay_min_ms, random_delay_max_ms, proxy_pool?}"
    )

    # Rate limiting coordination
    rate_limit_group = Column(
        String(50),
        nullable=False,
        default="google_maps",
        comment="Rate limit group -- tasks in same group are throttled together"
    )
    max_concurrent_per_group = Column(
        Integer,
        nullable=False,
        default=2,
        comment="Max concurrent tasks running in this rate limit group globally"
    )

    # Results (denormalized for quick access)
    last_result = Column(
        JSON,
        nullable=True,
        comment="Most recent scraping result (denormalized for quick access)"
    )
    last_result_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the last successful result was stored"
    )
    last_error = Column(
        Text,
        nullable=True,
        comment="Last error message (if most recent run failed)"
    )

    # Tier promotion tracking
    consecutive_worker_failures = Column(
        Integer,
        nullable=False,
        default=0,
        comment="Consecutive failures from worker tier (for auto-promotion)"
    )

    # Lifecycle
    is_active = Column(
        Boolean,
        nullable=False,
        default=True,
        index=True,
        comment="Whether this job is active"
    )
    run_count = Column(
        Integer,
        nullable=False,
        default=0,
        comment="Total number of runs"
    )
    success_count = Column(
        Integer,
        nullable=False,
        default=0,
        comment="Total successful runs"
    )
    failure_count = Column(
        Integer,
        nullable=False,
        default=0,
        comment="Total failed runs"
    )

    # Timestamps
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=datetime.utcnow,
        comment="When the job was created"
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=True,
        onupdate=datetime.utcnow,
        comment="When the job was last updated"
    )

    # Relationships
    scrape_results = relationship(
        "ScrapeResult",
        back_populates="scrape_job",
        cascade="all, delete-orphan",
    )
    automation_tasks = relationship(
        "AutomationTask",
        back_populates="scrape_job",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index('ix_scrape_jobs_type_active', 'scrape_type', 'is_active'),
        Index('ix_scrape_jobs_next_scheduled', 'next_scheduled_at'),
        Index('ix_scrape_jobs_rate_limit', 'rate_limit_group', 'is_active'),
    )

    def __repr__(self) -> str:
        return (
            f"<ScrapeJob(id={self.id}, name='{self.name}', "
            f"type='{self.scrape_type}', active={self.is_active})>"
        )

    @property
    def success_rate(self) -> float:
        """Calculate success rate as percentage."""
        if self.run_count == 0:
            return 0.0
        return round((self.success_count / self.run_count) * 100, 1)

    @property
    def effective_tier(self) -> str:
        """Determine effective tier based on preference and failure history."""
        if self.preferred_tier != "auto":
            return self.preferred_tier
        # Auto-promote: if workers failed 3+ times consecutively, use desktop
        if self.consecutive_worker_failures >= 3:
            return "desktop"
        return "worker"
