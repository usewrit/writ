"""AiSession model — a coordinator-side record of ONE autonomous AI session that
runs ENTIRELY on a connected fleet agent.

The self-host coordinator is a dispatch PROXY for AI sessions: it sends a single
``ai_session_start`` frame to a connected ``writ-agent`` (built ``--features
fleet,local``), the agent runs the whole autonomous loop locally, and replies
with ``ai_session_complete`` / ``ai_session_failed``. There is NO coordinator-side
brain/runner — the agent owns the loop; this row just records the request + the
returned outcome so the UI can list past sessions and link to the workflow the
agent recorded.

The recorded workflow itself lives on the agent and surfaces through the EXISTING
``local_catalog`` mirror path (LocalWorkflow rows); this row only stores the
returned ``workflow_id`` / ``workflow_name`` for display.

Tenant-stripped (single-owner coordinator): no tenant/organization column. The
``agent_id`` is the AUTHENTICATED agent the session was dispatched to (chosen by
the endpoint from the live in-process fleet registry), never a value taken from
the agent's reply body.

Secret handling mirrors the fleet deploy path: any secret fill values are
re-sealed under the agent's per-agent channel key into ``credentials_encrypted``
on the wire frame — they are NEVER persisted on this row and never leave the
process unsealed. This row holds only non-secret request metadata + the outcome.
"""
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.sql import func

from database import Base


class AiSession(Base):
    """A coordinator record of one agent-run autonomous AI session (dispatch proxy)."""

    __tablename__ = "ai_sessions"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(
        String(64),
        nullable=False,
        unique=True,
        index=True,
        comment="External correlation handle (uuid4). Also the ai_session_start "
                "correlate_by key and the agent's external session handle.",
    )
    agent_id = Column(
        String(255),
        nullable=False,
        index=True,
        comment="Authenticated Agent.agent_id the session was dispatched to. "
                "Chosen from the live fleet registry; never from a reply payload.",
    )
    name = Column(
        String(500),
        nullable=True,
        comment="Display name for the session (e.g. 'AI: <goal>').",
    )
    goal = Column(
        Text,
        nullable=False,
        comment="The natural-language goal the agent pursued.",
    )
    entry_url = Column(
        String(2048),
        nullable=True,
        comment="Optional starting URL the agent opened before the loop.",
    )
    status = Column(
        String(32),
        nullable=False,
        default="running",
        server_default="running",
        index=True,
        comment="running | complete | blocked | max_steps | stuck | error | "
                "cancelled. Starts 'running'; updated from the agent's reply.",
    )
    workflow_id = Column(
        Integer,
        nullable=True,
        comment="The LOCAL workflow id the agent recorded (agent-side id), or "
                "NULL if generate_workflow was off / nothing was recorded.",
    )
    workflow_name = Column(
        String(500),
        nullable=True,
        comment="Display name of the recorded workflow, if any.",
    )
    generate_workflow = Column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
        comment="Whether the agent was asked to record+save a workflow at the end.",
    )
    steps = Column(
        Integer,
        nullable=True,
        comment="Iterations the agent took (reported in the completion frame).",
    )
    # LOGIN-RECORDING session: set when this session exists to RECORD how the named
    # persona signs in ("Let AI record the login"). On the terminal frame the
    # coordinator materializes the returned recipe as its OWN AutomationWorkflow and
    # points personas.login_workflow_id at it — the agent-side workflow id is a
    # different namespace and can never be used for that link. Durable so the wiring
    # survives a coordinator restart between dispatch and reply.
    login_for_persona_id = Column(
        Integer,
        ForeignKey("personas.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="Persona whose sign-in this session records; completion wires login_workflow_id",
    )
    error = Column(
        Text,
        nullable=True,
        comment="Error string when the session failed / could not be dispatched.",
    )
    message = Column(
        Text,
        nullable=True,
        comment="Human-readable outcome message reported by the agent.",
    )
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        default=datetime.utcnow,
    )
    completed_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="When a terminal reply (complete/failed) landed or the dispatch "
                "gave up (timeout / agent disconnect).",
    )

    def __repr__(self) -> str:
        return (
            f"<AiSession(id={self.id}, session_id='{self.session_id}', "
            f"agent='{self.agent_id}', status='{self.status}')>"
        )

    def to_dict(self) -> dict:
        """Public representation (never exposes any secret material — none is stored)."""
        return {
            "id": self.id,
            "session_id": self.session_id,
            "agent_id": self.agent_id,
            "name": self.name,
            "goal": self.goal,
            "entry_url": self.entry_url,
            "status": self.status,
            "workflow_id": self.workflow_id,
            "workflow_name": self.workflow_name,
            "generate_workflow": bool(self.generate_workflow),
            "steps": self.steps,
            "error": self.error,
            "message": self.message,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }
