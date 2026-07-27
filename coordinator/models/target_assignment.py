"""
Target Assignment model — maps a monitored Target to the fleet Agent that checks it.

Restored for the self-hosted coordinator: the capacity-aware distributor writes one
row per (target, agent) so the monitor-dispatch scheduler can (re)build each agent's
``assign_targets`` frame and keep an assignment STICKY per target (one agent per
target; reassign only on disconnect). ``agent_id`` is the STRING ``Agent.agent_id``
(not the integer PK) so it matches the distributor / assign_targets lookups.
"""
from datetime import datetime

from sqlalchemy import Column, Integer, String, ForeignKey, DateTime, Index
from sqlalchemy.orm import relationship

from database import Base


class TargetAssignment(Base):
    """Junction row: which fleet agent is responsible for checking a target."""

    __tablename__ = "target_assignments"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)

    target_id = Column(
        Integer,
        ForeignKey("targets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Target this assignment covers",
    )

    # STRING agent id (Agent.agent_id), not the integer PK — matches how the
    # distributor + monitoring_dispatch look assignments up. No FK: the string
    # column mirrors the cloud schema and survives agent-row churn.
    agent_id = Column(
        String(255),
        nullable=False,
        index=True,
        comment="Agent.agent_id (string) responsible for checking the target",
    )

    assigned_by = Column(
        String(64),
        nullable=True,
        comment="What produced this assignment (e.g. capacity-aware-distributor)",
    )

    assigned_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=datetime.utcnow,
        comment="When the assignment was (re)written",
    )

    # Relationship used by routers/schedule.py selectinload(TargetAssignment.target).
    target = relationship("Target", foreign_keys=[target_id])

    __table_args__ = (
        Index("ix_target_assignments_target_agent", "target_id", "agent_id", unique=True),
        Index("ix_target_assignments_agent", "agent_id"),
    )

    def __repr__(self) -> str:
        return f"<TargetAssignment(target_id={self.target_id}, agent_id='{self.agent_id}')>"
