"""LocalWorkflow model — a cloud-callable handle to a workflow that lives ENTIRELY
on a linked desktop daemon.

This is the metadata substrate for the "coordinator-callable LOCAL workflows"
feature: the owner links a desktop daemon (a light agent), and that daemon
advertises its OWN local workflows by id. The coordinator stores ONLY metadata
about each local workflow — name, description, declared input/output fields, a
recipe hash, and a ``cloud_callable`` flag. The recipe (steps) and the
credentials NEVER leave the device; a caller dispatches a run by ``local_id``
and the daemon executes it locally, returning extracted data the same way a
normal task_result does.

SECURITY INVARIANT (feedback_never_trust_byo_agents): identity/metering/
success-truth live in the coordinator + ws-gateway, NEVER the agent. The
``agent_id`` column is stamped from the AUTHENTICATED, gateway-stamped agent
identity (resolved via the Agent row), NEVER from any value in the daemon's
message body. ``handle_local_catalog`` upserts rows scoped to that trusted
``agent_id`` only.

The ``input_schema`` column holds METADATA ONLY (declared inputs / output_fields).
It must NEVER contain steps/recipe/credentials.
"""
from datetime import datetime

from sqlalchemy import (
    JSON,
    Column,
    Integer,
    String,
    Text,
    Boolean,
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    UniqueConstraint,
)
from sqlalchemy.sql import func

from database import Base


class LocalWorkflow(Base):
    """A cloud-callable handle to a daemon-resident (local) workflow.

    UNIQUE (agent_id, local_id): a given daemon (agent) advertises each of its
    local workflows exactly once. The upsert in
    ``services.local_workflow_catalog.handle_local_catalog`` keys on this pair.
    """
    __tablename__ = "local_workflows"

    id = Column(Integer, primary_key=True, index=True)
    agent_id = Column(
        String(255),
        nullable=False,
        index=True,
        comment="Owning Agent.agent_id (the linked daemon). Trusted/authenticated; "
                "never taken from the catalog payload.",
    )
    local_id = Column(
        String(255),
        nullable=False,
        comment="Daemon-side opaque workflow id (the value the daemon runs by). "
                "Opaque to the cloud.",
    )
    name = Column(
        String(500),
        nullable=False,
        comment="Display name advertised by the daemon (metadata only).",
    )
    description = Column(
        Text,
        nullable=True,
        comment="Display description advertised by the daemon (metadata only).",
    )
    input_schema = Column(
        JSON,
        nullable=True,
        comment="Declared inputs / output_fields — METADATA ONLY. NEVER steps/"
                "recipe/credentials.",
    )
    cloud_callable = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
        comment="Whether this local workflow may be invoked from the cloud.",
    )
    recipe_hash = Column(
        String(255),
        nullable=True,
        comment="Hex digest of the daemon-side recipe; drift detection only "
                "(the recipe itself stays on the device).",
    )
    source_workflow_id = Column(
        Integer,
        ForeignKey("automation_workflows.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="Coordinator AutomationWorkflow this local handle was mirrored/"
                "moved from; NULL if recorded directly on the agent. Used to derive "
                "the merged-list origin marker (mirrored vs local:<agent>).",
    )
    price_per_run_micros = Column(
        BigInteger,
        nullable=False,
        default=0,
        server_default="0",
        comment="Flat price-per-run set for cloud callers (micro-USD); 0 = free.",
    )
    status = Column(
        String(32),
        nullable=False,
        default="active",
        server_default="active",
        index=True,
        comment="active | withdrawn. A workflow absent from a fresh catalog is "
                "marked withdrawn (never invokable).",
    )
    last_advertised_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the daemon last advertised this workflow in a catalog frame.",
    )
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        default=datetime.utcnow,
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    __table_args__ = (
        UniqueConstraint(
            "agent_id", "local_id",
            name="uq_local_workflows_agent_local",
        ),
        Index("ix_local_workflows_callable", "cloud_callable", "status"),
    )

    def __repr__(self) -> str:
        return (
            f"<LocalWorkflow(id={self.id}, "
            f"agent='{self.agent_id}', local_id='{self.local_id}', "
            f"status='{self.status}', cloud_callable={self.cloud_callable})>"
        )

    def to_dict(self) -> dict:
        """Metadata-only public representation (never exposes recipe/creds)."""
        return {
            "id": self.id,
            "agent_id": self.agent_id,
            "local_id": self.local_id,
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema or {},
            "cloud_callable": bool(self.cloud_callable),
            "recipe_hash": self.recipe_hash,
            "source_workflow_id": self.source_workflow_id,
            "price_per_run_micros": int(self.price_per_run_micros or 0),
            "status": self.status,
            "last_advertised_at": (
                self.last_advertised_at.isoformat() if self.last_advertised_at else None
            ),
        }
