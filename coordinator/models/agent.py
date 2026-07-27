"""
Agent model - represents monitoring agents across different platforms.
"""
import enum
from datetime import datetime
from typing import Optional
from sqlalchemy import (
    Column,
    Integer,
    String,
    Boolean,
    Enum,
    DateTime,
    JSON,
    Index,
)
from sqlalchemy.orm import deferred, relationship
from database import Base


class Platform(str, enum.Enum):
    """Supported agent platforms."""
    IOS = "ios"
    ANDROID = "android"
    LINUX = "linux"
    MACOS = "macos"
    WINDOWS = "windows"
    DOCKER = "docker"
    LAMBDA = "lambda"
    CLOUDFLARE = "cloudflare"
    RECORDER = "recorder"


class AgentStatus(str, enum.Enum):
    """Agent lifecycle status."""
    ACTIVE = "active"
    INACTIVE = "inactive"
    REVOKED = "revoked"
    SUSPENDED = "suspended"


class Agent(Base):
    """
    Agent table - tracks all registered monitoring agents.

    Agents are distributed across different platforms and coordinate
    with the backend to perform scheduled URL checks.
    """
    __tablename__ = "agents"

    id = Column(Integer, primary_key=True, index=True)
    agent_id = Column(
        String(255),
        unique=True,
        nullable=False,
        index=True,
        comment="Unique identifier for the agent"
    )
    platform = Column(
        Enum(Platform, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        comment="Platform where agent is running"
    )
    status = Column(
        Enum(AgentStatus, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        default=AgentStatus.ACTIVE,
        index=True,
        comment="Current agent status"
    )
    secret_hash = Column(
        String(255),
        nullable=False,
        comment="Argon2 hash of agent secret (legacy - for backward compat)"
    )
    encrypted_secret = Column(
        String(500),
        nullable=True,
        comment="Fernet-encrypted agent secret for HMAC verification (retrievable)"
    )
    last_seen_at = Column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
        comment="Last heartbeat timestamp"
    )
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=datetime.utcnow,
        comment="Agent registration timestamp"
    )
    meta = Column(
        JSON,
        nullable=True,
        comment="Additional metadata (OS version, device info, etc.)"
    )
    force_config_update = Column(
        Boolean,
        nullable=False,
        default=False,
        comment="Flag to force agent to fetch config immediately"
    )
    time_slot_offset_ms = Column(
        Integer,
        nullable=False,
        default=0,
        comment="Millisecond offset within global period for synchronized checking"
    )
    sync_enabled = Column(
        Boolean,
        nullable=False,
        default=True,
        comment="Whether agent should sync checks to assigned time slot"
    )
    user_agent = Column(
        String(500),
        nullable=True,
        comment="Assigned user agent string for this agent"
    )
    is_trusted = Column(
        Boolean,
        nullable=False,
        default=False,
        index=True,
        comment="Whether this agent is trusted for sensitive workflow execution"
    )
    node_id = Column(
        String(255),
        nullable=True,
        index=True,
        comment="Assigned relay node (null = direct coordinator connection)"
    )
    region = Column(
        String(50),
        nullable=True,
        index=True,
        comment="Geographic region detected via IP geolocation (e.g. us-east, eu-west)"
    )
    # The coordinator runs a single global fleet with one implicit workspace;
    # agents are not scoped to a per-owner namespace.
    user_hosted = deferred(Column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
        index=True,
        comment="Whether this agent is user-hosted (connected via device flow + WS)",
    ))

    # Indexes for common queries
    __table_args__ = (
        Index("ix_agents_status_last_seen", "status", "last_seen_at"),
        Index("ix_agents_platform_status", "platform", "status"),
    )

    # Capacity properties (parsed from meta)
    @property
    def capacity_per_timeslot(self) -> int:
        """Get reported capacity per timeslot"""
        meta = self.meta or {}
        capacity = meta.get('capacity', {})
        return capacity.get('targets_per_timeslot', 10)

    @property
    def timeslots_requested(self) -> int:
        """Get requested number of timeslots"""
        meta = self.meta or {}
        capacity = meta.get('capacity', {})
        return capacity.get('timeslots_needed', 1)

    @property
    def total_capacity(self) -> int:
        """Get total capacity across all timeslots"""
        return self.capacity_per_timeslot * self.timeslots_requested

    @property
    def avg_check_latency_ms(self) -> float:
        """Get reported average check latency"""
        meta = self.meta or {}
        capacity = meta.get('capacity', {})
        return capacity.get('avg_check_latency_ms', 1000.0)

    @property
    def parallel_workers(self) -> int:
        """Get number of parallel workers"""
        meta = self.meta or {}
        capacity = meta.get('capacity', {})
        return capacity.get('parallel_workers', 5)

    @property
    def check_modes(self) -> list:
        """Get supported check modes (content, uptime, playwright)"""
        meta = self.meta or {}
        return meta.get('check_modes', ['content', 'uptime'])

    @property
    def has_playwright(self) -> bool:
        """Check if agent supports Playwright automation"""
        return 'playwright' in self.check_modes

    @property
    def captcha_trusted(self) -> bool:
        """Check if agent is marked as captcha-trusted (residential IP, less likely to trigger captchas)"""
        meta = self.meta or {}
        return meta.get('captcha_trusted', False)

    def __repr__(self) -> str:
        return f"<Agent(agent_id='{self.agent_id}', platform='{self.platform}', status='{self.status}')>"

    def to_dict(self) -> dict:
        """Convert agent to dictionary representation."""
        return {
            "id": self.id,
            "agent_id": self.agent_id,
            "platform": self.platform.value,
            "status": self.status.value,
            "last_seen_at": self.last_seen_at.isoformat() if self.last_seen_at else None,
            "created_at": self.created_at.isoformat(),
            "meta": self.meta,
            "is_trusted": self.is_trusted,
        }
