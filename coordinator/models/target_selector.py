"""
Target Selector model - represents individual CSS selectors for a target.
Each selector is monitored independently with its own baseline.
"""
from datetime import datetime
from sqlalchemy import (
    Column,
    Integer,
    String,
    Boolean,
    DateTime,
    Text,
    ForeignKey,
    Index,
    UniqueConstraint,
    JSON,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from database import Base


class TargetSelector(Base):
    """
    Individual CSS selectors for a target.

    Each selector is monitored independently with its own baseline hash,
    allowing multiple elements on the same page to be tracked separately.
    """
    __tablename__ = "target_selectors"

    id = Column(Integer, primary_key=True, index=True)
    target_id = Column(
        Integer,
        ForeignKey("targets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Target this selector belongs to"
    )

    # Selector identification
    name = Column(
        String(255),
        nullable=False,
        comment="Human-readable name for this selector"
    )
    selector = Column(
        String(512),
        nullable=False,
        comment="CSS selector for content extraction"
    )
    description = Column(
        Text,
        nullable=True,
        comment="Optional description of what this selector monitors"
    )

    # Selector-specific settings
    enabled = Column(
        Boolean,
        default=True,
        index=True,
        comment="Whether this selector is actively monitored"
    )
    content_type = Column(
        String(20),
        default='text',
        comment="Content type: 'text' (hash of text), 'html' (structured extraction), or 'visual' (screenshot comparison)"
    )
    visual_region = Column(
        JSON,
        nullable=True,
        comment="For visual type: region coordinates {x, y, width, height} for screenshot comparison"
    )
    ignore_regex = Column(
        Text,
        nullable=True,
        comment="Regex pattern for content to ignore (overrides target-level)"
    )
    priority = Column(
        Integer,
        default=0,
        comment="Order for processing (higher = first)"
    )

    # Baseline tracking (per-selector)
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
    baseline_screenshot = Column(
        Text,
        nullable=True,
        comment="For visual checks: base64 PNG of the baseline region screenshot"
    )
    baseline_fetched_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="When baseline was fetched"
    )

    # Change tracking
    last_content_hash = Column(
        String(64),
        nullable=True,
        comment="Most recent content hash"
    )
    last_checked_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="When this selector was last checked"
    )
    change_count = Column(
        Integer,
        default=0,
        comment="Number of changes detected for this selector"
    )

    # Timestamps
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        comment="When this selector was created"
    )
    updated_at = Column(
        DateTime(timezone=True),
        onupdate=func.now(),
        comment="When this selector was last updated"
    )

    # Relationships
    target = relationship(
        "Target",
        back_populates="selectors"
    )
    detected_changes = relationship(
        "DetectedChange",
        back_populates="target_selector",
        cascade="all, delete-orphan"
    )
    extractors = relationship(
        "SelectorExtractor",
        back_populates="target_selector",
        cascade="all, delete-orphan",
        order_by="SelectorExtractor.id"
    )

    # Indexes and constraints
    __table_args__ = (
        Index("ix_target_selectors_target_enabled", "target_id", "enabled"),
        UniqueConstraint("target_id", "selector", name="uq_target_selector"),
    )

    def __repr__(self) -> str:
        return f"<TargetSelector(id={self.id}, name='{self.name}', selector='{self.selector[:30]}...')>"

    def to_dict(self) -> dict:
        """Convert selector to dictionary representation."""
        return {
            "id": self.id,
            "target_id": self.target_id,
            "name": self.name,
            "selector": self.selector,
            "description": self.description,
            "enabled": self.enabled,
            "content_type": self.content_type,
            "visual_region": self.visual_region,
            "ignore_regex": self.ignore_regex,
            "priority": self.priority,
            "baseline_hash": self.baseline_hash,
            "baseline_fetched_at": self.baseline_fetched_at.isoformat() if self.baseline_fetched_at else None,
            "last_content_hash": self.last_content_hash,
            "last_checked_at": self.last_checked_at.isoformat() if self.last_checked_at else None,
            "change_count": self.change_count,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def to_agent_dict(self) -> dict:
        """Serialize selector for agent configuration."""
        return {
            "id": self.id,
            "name": self.name,
            "selector": self.selector,
            "content_type": self.content_type,
            "visual_region": self.visual_region,
            "ignore_regex": self.ignore_regex,
            "baseline_hash": self.baseline_hash,
            "baseline_content": self.baseline_content,
        }
