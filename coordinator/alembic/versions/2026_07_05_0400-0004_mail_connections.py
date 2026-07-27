"""mail_connections — IMAP mailbox for persona email-OTP (self-host)

Create ``mail_connections``: a connected IMAP mailbox a persona reads email-OTP /
magic-links from. Self-host is IMAP-only (no OAuth mailbox, no inbound relay), so
this table holds just the IMAP connection fields; the app password is stored
Fernet-encrypted. Not tenant-scoped (single-owner coordinator). ``personas`` already
carries the nullable ``mail_connection_id`` (provisioned in the baseline), so this
migration only adds the table it points at.

DDL is check-first so re-running is a no-op even where schema_sync created the table
at startup.

Apply forward with:  alembic upgrade 0004_mail_connections

Revision ID: 0004_mail_connections
Revises: 0003_local_workflow_source
Create Date: 2026-07-05 04:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0004_mail_connections'
down_revision: Union[str, None] = '0003_local_workflow_source'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "mail_connections"


def _has_table(table: str) -> bool:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    try:
        return table in set(insp.get_table_names())
    except Exception:
        return False


def upgrade() -> None:
    if _has_table(_TABLE):
        return
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("provider", sa.String(length=50), nullable=False, server_default="imap"),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("imap_host", sa.String(length=255), nullable=True),
        sa.Column("imap_port", sa.Integer(), nullable=True),
        sa.Column("imap_username", sa.String(length=320), nullable=True),
        sa.Column("imap_password_encrypted", sa.Text(), nullable=True),
        sa.Column("imap_use_ssl", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("imap_mailbox", sa.String(length=120), nullable=True),
        sa.Column("is_active", sa.String(length=10), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("provider", "email", name="uq_mail_connection_provider_email"),
    )


def downgrade() -> None:
    if not _has_table(_TABLE):
        return
    op.drop_table(_TABLE)
