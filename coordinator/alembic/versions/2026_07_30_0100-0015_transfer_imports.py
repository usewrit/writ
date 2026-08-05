"""Staged `.writ` package imports — the import wizard's state machine.

Revision ID: 0015_transfer_imports
Revises: 0014_crawl_definitions
Create Date: 2026-07-30

See `DATA_PORTABILITY_SPEC.md` §10. A user unlocks a transfer package once, then
walks several wizard steps building a plan before anything is created. That needs
server-side state, or every step would re-upload and re-decrypt the file.

The staged BODY is deliberately NOT stored here. `summary_json` /
`requirements_json` (names, counts, collisions, slots) are small, bounded, and all
the wizard reads; the body goes to `writ_files_dir/transfers/` and is streamed back
at commit, with only sub-256 KiB payloads inlined. SQLite rewrites a row wholesale
on update, so a 400 MiB `TEXT` value here would be re-read and re-written on every
status change.

Both payload locations hold Fernet-wrapped bytes: a staged import is a decrypted
copy of a file the user encrypted on purpose. `secrets_ref` (the opt-in sealed
credential lane) is deleted at commit and at expiry regardless of what the user
accepted.

SQLite-safe: one new table, no existing table touched, no NOT NULL backfill.
`id` is `VARCHAR(36)` holding a uuid4 string — SQLite has no native UUID type, and
keeping the same value space as the cloud means an import id is meaningful in
either install's logs.
"""
import sqlalchemy as sa
from alembic import op

revision = "0015_transfer_imports"
down_revision = "0014_crawl_definitions"
branch_labels = None
depends_on = None


def _has_table(table: str) -> bool:
    return table in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if _has_table("transfer_imports"):
        return

    op.create_table(
        "transfer_imports",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("created_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("bundle_id", sa.String(length=36), nullable=True),
        sa.Column("label", sa.String(length=120), nullable=True),
        sa.Column("producer_app", sa.String(length=20), nullable=True),
        sa.Column("producer_version", sa.String(length=40), nullable=True),
        sa.Column("producer_edition", sa.String(length=20), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="staged"),
        sa.Column("header_json", sa.JSON(), nullable=False),
        sa.Column("counts_json", sa.JSON(), nullable=True),
        sa.Column("summary_json", sa.JSON(), nullable=True),
        sa.Column("requirements_json", sa.JSON(), nullable=True),
        sa.Column("payload_ref", sa.Text(), nullable=True,
                  comment="Filesystem path of the Fernet-wrapped staged body"),
        sa.Column("payload_inline", sa.Text(), nullable=True,
                  comment="Fernet-wrapped staged body for small packages"),
        sa.Column("payload_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("secrets_ref", sa.Text(), nullable=True,
                  comment="Sealed-credentials lane; dropped at commit and at expiry"),
        sa.Column("plan_json", sa.JSON(), nullable=True),
        sa.Column("result_json", sa.JSON(), nullable=True),
        sa.Column("created_ids_json", sa.JSON(), nullable=True),
        sa.Column("progress_json", sa.JSON(), nullable=True),
        sa.Column("failed_unlock_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("idempotency_key", sa.String(length=80), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("committed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("undone_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_index("ix_transfer_imports_bundle_id", "transfer_imports", ["bundle_id"])
    op.create_index("ix_transfer_imports_status", "transfer_imports", ["status"])
    op.create_index("ix_transfer_imports_expires_at", "transfer_imports", ["expires_at"])
    op.create_index("ix_transfer_imports_created", "transfer_imports", ["created_at"])
    # The expiry sweep's access path.
    op.create_index("ix_transfer_imports_status_expires", "transfer_imports", ["status", "expires_at"])
    # Idempotent commit lookup.
    op.create_index("ix_transfer_imports_idem", "transfer_imports", ["idempotency_key"])


def downgrade() -> None:
    if _has_table("transfer_imports"):
        op.drop_table("transfer_imports")
