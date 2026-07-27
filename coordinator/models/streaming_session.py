"""
StreamingSession model - tracks persistent browser sessions for streaming mode.

Streaming sessions keep a browser alive on a target site and expose callable
handlers (step groups or JS scripts) that external systems invoke via
SSE+POST, WebSocket, or OpenAI-compatible API.
"""
from datetime import datetime
from sqlalchemy import (
    JSON,
    Column,
    Integer,
    String,
    DateTime,
    Text,
    Index,
    ForeignKey,
)
from sqlalchemy.orm import relationship
from database import Base


class StreamingSession(Base):
    """
    StreamingSession table - persistent browser sessions.

    Unlike AutomationTask (which executes and finishes), streaming sessions
    stay alive until the user explicitly ends them or they time out.
    Handlers are invoked via API during the session lifetime.
    """
    __tablename__ = "streaming_sessions"

    id = Column(Integer, primary_key=True, index=True)
    workflow_id = Column(
        Integer,
        ForeignKey("automation_workflows.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Source streaming workflow",
    )

    # Session identity
    session_key = Column(
        String(64),
        unique=True,
        nullable=False,
        index=True,
        comment="Unique key for API routing (UUID)",
    )

    # Connection state
    status = Column(
        String(20),
        nullable=False,
        default="starting",
        index=True,
        comment="queued, starting, running, ending, ended, failed",
    )
    target_url = Column(
        Text,
        nullable=False,
        comment="URL the browser is connected to",
    )
    current_url = Column(
        Text,
        nullable=True,
        comment="Current page URL (may differ from target after navigation)",
    )
    agent_id = Column(
        String(255),
        nullable=True,
        index=True,
        comment="Agent/recorder executing this session",
    )

    # Counters
    events_emitted = Column(
        Integer,
        nullable=False,
        default=0,
        comment="Number of autonomous events emitted by scripts",
    )
    commands_received = Column(
        Integer,
        nullable=False,
        default=0,
        comment="Number of handler invocations received",
    )

    # Lifecycle timestamps
    started_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="When browser connected and setup completed",
    )
    last_activity_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="Last command or event timestamp",
    )
    ended_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="When session was closed",
    )
    end_reason = Column(
        String(50),
        nullable=True,
        comment="user_ended, timeout, agent_lost, error",
    )

    # Safety
    max_duration_seconds = Column(
        Integer,
        nullable=False,
        default=3600,
        comment="Max session duration before auto-end (default 1hr)",
    )

    # Error info
    error_message = Column(
        Text,
        nullable=True,
        comment="Failure details",
    )
    queued_config = Column(
        JSON,
        nullable=True,
        comment="For status=queued: original start params (headless, execution_target, "
                "max_duration_seconds, distribute_ips) so the queue processor can dispatch "
                "this session faithfully once an agent is free.",
    )

    # Timestamps
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=datetime.utcnow,
        index=True,
        comment="When session record was created",
    )

    # Relationships
    workflow = relationship("AutomationWorkflow", back_populates="streaming_sessions")

    __table_args__ = (
        # `status` already carries an index from its column definition
        # (index=True → ix_streaming_sessions_status); only the composite
        # index needs to be declared explicitly here.
        Index("ix_streaming_sessions_agent", "agent_id", "status"),
    )

    def __repr__(self) -> str:
        return (
            f"<StreamingSession(id={self.id}, key='{self.session_key}', "
            f"status='{self.status}', url='{self.target_url}')>"
        )
