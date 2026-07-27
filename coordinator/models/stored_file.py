"""
StoredFile model — a first-class file asset.

A StoredFile is the substrate for the "file assets" feature: a stable, OpenAI
Files-API-shaped handle (``file_<uuid4hex>``) pointing at object-storage bytes
held in the ``files`` bucket. It has four ORIGINS (``source``):

  - ``upload``          — manual dashboard upload.
  - ``api``             — developer ``POST /v1/files`` upload.
  - ``workflow_output`` — a file a replay captured from a site download.
  - ``ai_session`` / ``streaming`` — an attachment passed into an AI/streaming run.

and is consumed by browser file-inputs (upload steps), workflow→workflow
automations, AI/LLM context and API responses.

Single-owner coordinator: every file belongs to the one owner; resolves are
fail-closed (see file_service). Marketplace/consumer runs resolve only the
owner's files (creators declare a file SLOT, never ship a concrete id).
"""
from uuid import uuid4

from sqlalchemy import (
    Column,
    String,
    Integer,
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    JSON,
    Uuid as PG_UUID,
)
from sqlalchemy.sql import func

from database import Base


class StoredFile(Base):
    """A stored file asset (OpenAI Files-API shape)."""
    __tablename__ = "stored_files"

    # OpenAI-style public handle is the PK: "file_<uuid4hex>".
    id = Column(
        String,
        primary_key=True,
        default=lambda: "file_" + uuid4().hex,
    )
    created_by_user_id = Column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        comment="User who created the file (NULL once that user is deleted)",
    )
    storage_key = Column(
        String,
        nullable=False,
        comment='Opaque storage reference, e.g. "minio:files/{id}"',
    )
    # A plain nullable column retained for data-shape compatibility; the
    # coordinator uses local-disk storage (NULL provider).
    storage_provider_id = Column(
        PG_UUID(as_uuid=True),
        nullable=True,
        comment="(legacy) storage provider id; NULL = local-disk fallback",
    )
    filename = Column(
        String,
        nullable=False,
        comment="Original / suggested filename (sanitized)",
    )
    content_type = Column(
        String,
        nullable=False,
        default="application/octet-stream",
        server_default="application/octet-stream",
    )
    size_bytes = Column(
        BigInteger,
        nullable=False,
        default=0,
        server_default="0",
    )
    sha256 = Column(
        String(64),
        nullable=True,
        index=True,
        comment="Hex SHA-256 of the stored bytes (dedup / integrity)",
    )
    source = Column(
        String,
        nullable=False,
        default="upload",
        server_default="upload",
        comment="upload | api | workflow_output | ai_session | streaming",
    )
    source_run_id = Column(
        Integer,
        ForeignKey("automation_tasks.id", ondelete="SET NULL"),
        nullable=True,
        comment="Capturing run (for source=workflow_output); NULL otherwise",
    )
    purpose = Column(
        String,
        nullable=True,
        comment='OpenAI "purpose"-style tag, e.g. "assistants", "user_data"',
    )
    status = Column(
        String,
        nullable=False,
        default="ready",
        server_default="ready",
        comment="processing | ready | error",
    )
    expires_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="Ephemeral TTL; NULL = library (persistent) until deleted",
    )
    deleted_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="Soft-delete marker; resolves filter deleted_at IS NULL (fail-closed)",
    )
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    meta = Column(
        JSON,
        nullable=True,
        comment="Arbitrary extra metadata (e.g. download trigger info)",
    )

    __table_args__ = (
        Index("ix_stored_files_deleted", "deleted_at"),
        Index("ix_stored_files_source", "source"),
    )

    def __repr__(self) -> str:
        return (
            f"<StoredFile(id='{self.id}', "
            f"filename='{self.filename}', source='{self.source}')>"
        )
