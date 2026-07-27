"""user_notification_preferences — owner platform notification matrix (self-host)

Single-tenant carve of the cloud user_notification_preferences migration:
  * Keyed on user_id ONLY (unique — one row per user; NO organization_id).
  * Generic SQLite-friendly types (sa.Uuid, sa.JSON — no JSONB).

Holds the event × channel preference matrix for platform-wide notifications
(runs.run_failed, agents.agent_connected — see notifications/catalog.py) plus
the owner's personal contact points (phone_number, pushover_user_key).

Create-first idempotent: a no-op if the table already exists (e.g. a fresh DB
built directly from the models), so re-running upgrade is safe.

Revision ID: 0011_user_notification_preferences
Revises: 0010_crawl_render_ocr
Create Date: 2026-07-18 03:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0011_user_notification_preferences'
down_revision: Union[str, None] = '0010_crawl_render_ocr'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "user_notification_preferences"


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
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False,
                  comment="The owner the preferences belong to (one row per user)"),
        sa.Column("preferences", sa.JSON(), nullable=False,
                  comment='Event → channel matrix, e.g. {"runs.run_failed": '
                          '{"email": true, "in_app": true}}. Missing keys fall '
                          "back to catalog defaults."),
        sa.Column("phone_number", sa.String(length=32), nullable=True,
                  comment="E.164 phone for SMS/WhatsApp/Signal platform notifications"),
        sa.Column("pushover_user_key", sa.String(length=64), nullable=True,
                  comment="Owner's own Pushover user key for platform notifications"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("user_id", name="uq_user_notification_prefs_user"),
    )
    op.create_index("ix_user_notification_preferences_user_id", _TABLE, ["user_id"])


def downgrade() -> None:
    if not _has_table(_TABLE):
        return
    try:
        op.drop_index("ix_user_notification_preferences_user_id", table_name=_TABLE)
    except Exception:
        pass
    op.drop_table(_TABLE)
