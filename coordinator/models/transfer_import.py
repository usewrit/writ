"""
Transfer Import — one staged `.writ` package on its way into this install.

Spec: `DATA_PORTABILITY_SPEC.md` §10. This row is the state machine behind the
import wizard: the user unlocks a package once (`staged`), walks the middle steps
building a plan (`planned`), confirms (`committing` → `committed`), and can undo
(`undone`). Without it, every wizard step would re-upload and re-decrypt the file.

WHY THE PAYLOAD IS NOT IN THIS TABLE
------------------------------------
`summary_json` / `requirements_json` are what the wizard's steps 3-6 render, and
they are small and bounded — names, counts, collisions, slots. The staged asset +
data payload can be hundreds of megabytes, so it goes to the filesystem under
`writ_files_dir/transfers/` (`payload_ref`) and is streamed back at commit; only a
small payload (< 256 KiB, which is the overwhelming majority — a config-only
package) is inlined. A 400 MiB `TEXT` value in SQLite would be read and rewritten
whole on every row update.

Wherever staged bytes live they are **Fernet-encrypted**: this is a decrypted copy
of a file the user considered secret enough to encrypt, so it must not sit in the
clear.

`secrets_ref` — the sealed-credentials lane — is deleted at commit and at expiry,
accepted or not. A staged credential blob has no reason to outlive the import.

UNDO records `created_ids_json` and nothing else: undo deletes exactly what this
import created, in reverse dependency order, and refuses rows that have since run
or collected data.
"""
from sqlalchemy import (
    JSON, BigInteger, Column, DateTime, ForeignKey, Index, Integer, String, Text,
)
from sqlalchemy.sql import func

from database import Base

#: Lifecycle. `staged` and `planned` hold decrypted bytes and are swept at
#: `expires_at`; the terminal states keep history with the payload scrubbed.
STATUSES = ("staged", "planned", "committing", "committed", "failed", "undone", "expired")

#: A staged import holds tenant plaintext, so it is short-lived by design.
STAGE_TTL_MINUTES = 60

#: How long an import stays undoable. Also ends early on the first successful run
#: of an imported asset — after that, "undo" would destroy real results.
UNDO_TTL_HOURS = 24

#: Payloads at or below this size skip the object-store round trip.
INLINE_PAYLOAD_MAX_BYTES = 256 * 1024


class TransferImport(Base):
    __tablename__ = "transfer_imports"

    # String, not a native UUID type: SQLite has none. Values are still uuid4
    # hex strings, so an id minted here is meaningful in a cloud install's logs too.
    id = Column(String(36), primary_key=True)
    created_by_user_id = Column(
        String(36),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    # ── provenance, straight from the package's cleartext header ──
    bundle_id = Column(
        String(36), nullable=True, index=True,
        comment="Package's own id — warns the user when they re-import the same file",
    )
    label = Column(String(120), nullable=True, comment="User-supplied package label")
    producer_app = Column(String(20), nullable=True, comment="cloud|desktop|selfhost")
    producer_version = Column(String(40), nullable=True)
    producer_edition = Column(String(20), nullable=True, comment="managed|oss")

    status = Column(String(20), nullable=False, default="staged", index=True)

    # ── small, always inline: what the wizard renders ──
    header_json = Column(JSON(), nullable=False, comment="The package's cleartext header")
    counts_json = Column(JSON(), nullable=True, comment="Per-kind asset counts")
    summary_json = Column(
        JSON(), nullable=True,
        comment="Asset names + collisions + capability blocks: steps 3-6 read only this",
    )
    requirements_json = Column(JSON(), nullable=True, comment="Slot list the user must bind")

    # ── large, out of line ──
    payload_ref = Column(
        Text(), nullable=True,
        comment="Object-store key for the Fernet-wrapped staged body (large packages)",
    )
    payload_inline = Column(
        Text(), nullable=True,
        comment="Fernet-wrapped staged body, for payloads under INLINE_PAYLOAD_MAX_BYTES",
    )
    payload_bytes = Column(BigInteger(), nullable=False, default=0, comment="Staged size, for quotas")
    secrets_ref = Column(
        Text(), nullable=True,
        comment="Sealed-credentials lane, Fernet-wrapped; DELETED at commit and at expiry",
    )

    # ── plan / outcome ──
    plan_json = Column(JSON(), nullable=True, comment="Collision resolutions + slot bindings")
    result_json = Column(JSON(), nullable=True, comment="Per-asset outcome after commit")
    created_ids_json = Column(JSON(), nullable=True, comment="{kind: [id]} — exactly what undo deletes")
    progress_json = Column(JSON(), nullable=True, comment="{done,total,phase} for a backgrounded commit")

    failed_unlock_count = Column(Integer(), nullable=False, default=0)
    idempotency_key = Column(
        String(80), nullable=True,
        comment="Commit de-duplication: a retried commit returns the first result",
    )

    expires_at = Column(DateTime(timezone=True), nullable=True, index=True)
    committed_at = Column(DateTime(timezone=True), nullable=True)
    undone_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        # History listing, newest first.
        Index("ix_transfer_imports_created", "created_at"),
        # The expiry sweep's access path.
        Index("ix_transfer_imports_status_expires", "status", "expires_at"),
        # Idempotent commit lookup.
        Index("ix_transfer_imports_idem", "idempotency_key"),
    )

    # ── derived helpers (no I/O) ──

    @property
    def holds_plaintext(self) -> bool:
        """True while decrypted tenant data is staged — what the sweep looks for."""
        return self.status in ("staged", "planned", "committing") and bool(
            self.payload_ref or self.payload_inline or self.secrets_ref
        )

    @property
    def has_sealed_credentials(self) -> bool:
        secrets = (self.header_json or {}).get("secrets") or {}
        return int(secrets.get("count") or 0) > 0

    def scrub_payload(self) -> None:
        """Forget the staged bytes, keeping the row as history. Called at commit, at
        expiry, and on discard. Does NOT delete the file itself — the caller owns
        that, and `transfer_store.delete_import` reclaims anything left behind (a
        crash between write and row commit must not leave decrypted work on disk
        forever)."""
        self.payload_ref = None
        self.payload_inline = None
        self.secrets_ref = None
