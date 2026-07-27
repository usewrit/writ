"""
DetectedChange model - represents unique content changes.
"""
from datetime import datetime
from sqlalchemy import (
    Column,
    Integer,
    String,
    DateTime,
    Text,
    ForeignKey,
    Index,
)
from sqlalchemy.orm import relationship
from database import Base


class DetectedChange(Base):
    """
    DetectedChange table - unique content changes.

    Stores one record per unique content_hash change. Multiple agent reports
    can reference the same detected change, avoiding data duplication.
    """
    __tablename__ = "detected_changes"

    id = Column(Integer, primary_key=True, index=True)
    target_id = Column(
        Integer,
        ForeignKey("targets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Reference to monitored target"
    )
    target_selector_id = Column(
        Integer,
        ForeignKey("target_selectors.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="Which selector detected this change (null for legacy single-selector)"
    )
    content_hash = Column(
        String(64),
        nullable=False,
        index=True,
        comment="SHA-256 hash of new content"
    )
    previous_hash = Column(
        String(64),
        nullable=True,
        comment="SHA-256 hash of previous content"
    )
    diff_snippet = Column(
        Text,
        nullable=True,
        comment="Diff preview (first 1000 chars)"
    )
    content_before = Column(
        Text,
        nullable=True,
        comment="Previous content snapshot"
    )
    content_after = Column(
        Text,
        nullable=True,
        comment="New content snapshot"
    )
    screenshot_before = Column(
        Text,
        nullable=True,
        comment="For visual checks: base64 PNG of the region before the change"
    )
    screenshot_after = Column(
        Text,
        nullable=True,
        comment="For visual checks: base64 PNG of the region after the change"
    )
    screenshot_diff = Column(
        Text,
        nullable=True,
        comment="For visual checks: storage ref of the before/after pixel-delta overlay"
    )
    first_detected_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=datetime.utcnow,
        index=True,
        comment="When this change was first detected"
    )
    last_detected_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=datetime.utcnow,
        index=True,
        comment="When this change was last detected (updated each time same hash is seen)"
    )
    agent_count = Column(
        Integer,
        nullable=False,
        default=1,
        comment="Number of agents that confirmed this change"
    )

    # Relationships
    target = relationship("Target", back_populates="detected_changes")
    target_selector = relationship("TargetSelector", back_populates="detected_changes")

    # Indexes
    __table_args__ = (
        Index('ix_detected_changes_target_hash', 'target_id', 'content_hash'),
    )

    def __repr__(self) -> str:
        return (
            f"<DetectedChange(id={self.id}, target_id={self.target_id}, "
            f"hash={self.content_hash[:8]}..., agents={self.agent_count})>"
        )
