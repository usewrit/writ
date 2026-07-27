"""
SignalGroup model - groups AI sessions for signal collection.

A SignalGroup defines a geographic area and signal types to collect,
spawning multiple AI sessions to browse the web and extract signals
for predictive analysis (e.g., events, news, transit disruptions).
"""
from datetime import datetime
from sqlalchemy import (
    JSON,
    Column,
    Integer,
    String,
    Float,
    DateTime,
    Text,
    Boolean,
    Index,
    ForeignKey,
)
from sqlalchemy.sql import func
from database import Base


class SignalGroup(Base):
    __tablename__ = "signal_groups"

    id = Column(Integer, primary_key=True, index=True)

    name = Column(
        String(150),
        nullable=False,
        comment="Human-readable name (e.g., 'Paris 6th Arrondissement')"
    )
    description = Column(
        Text,
        nullable=True,
        comment="Optional description of what this group monitors"
    )

    # Geographic area
    latitude = Column(Float, nullable=False)
    longitude = Column(Float, nullable=False)
    radius_km = Column(Float, default=2.0, nullable=False)
    city = Column(
        String(200),
        nullable=True,
        comment="Resolved or user-provided city name"
    )

    # Which signal types to collect
    signal_types = Column(
        JSON,
        default=list,
        nullable=False,
        comment='Signal types: ["events","news","transit","social","trending"]'
    )

    # Research depth controls
    min_findings = Column(
        Integer,
        default=5,
        nullable=False,
        comment="Minimum findings per signal type before AI can stop"
    )
    max_findings = Column(
        Integer,
        default=10,
        nullable=False,
        comment="Maximum findings per signal type (AI stops browsing after this)"
    )

    # Status of last collection run
    status = Column(
        String(20),
        default="idle",
        nullable=False,
        comment="idle | collecting | scoring | completed | failed"
    )
    error_message = Column(Text, nullable=True)

    # Scored results from last run (WritResponse format)
    last_results = Column(
        JSON,
        nullable=True,
        comment="Scored signal results from the last completed run"
    )
    last_run_at = Column(DateTime(timezone=True), nullable=True)
    run_count = Column(Integer, default=0, nullable=False)

    # Links to spawned AI session IDs for current/last run
    ai_session_ids = Column(
        JSON,
        default=list,
        nullable=False,
        comment="List of AI session IDs for this group's current/last run"
    )

    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(
        DateTime(timezone=True),
        default=func.now(),
        nullable=False,
    )
    updated_at = Column(
        DateTime(timezone=True),
        onupdate=func.now(),
        nullable=True,
    )

    __table_args__ = (
        Index('ix_signal_groups_status', 'status'),
        Index('ix_signal_groups_is_active', 'is_active'),
    )

    def __repr__(self) -> str:
        return (
            f"<SignalGroup(id={self.id}, name='{self.name}', "
            f"city='{self.city}', status='{self.status}')>"
        )
