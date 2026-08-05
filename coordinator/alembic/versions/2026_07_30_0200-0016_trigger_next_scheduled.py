"""`trigger_rules.next_scheduled_at` — the scheduler's due index for TIME-DRIVEN
automations.

Revision ID: 0016_trigger_next_scheduled
Revises: 0015_transfer_imports
Create Date: 2026-07-30

The automation builder has always offered a "scheduled" root block, and the create
endpoint rejected it — `valid_event_types` never listed `scheduled`, and even had
it been accepted, nothing would have fired it: a scheduled-root automation has no
incoming event to pull it, so it needs a persisted next-fire time the scheduler
can scan for.

This column is that index, mirroring `automation_workflows.next_scheduled_at` (the
scheduled-WORKFLOW lane that already works) and the cloud's identical column on
`trigger_rules`. NULL means "not time-driven": the routers stamp it only while the
rule is enabled AND its root event block is `scheduled`, so the due scan
(`next_scheduled_at IS NOT NULL AND next_scheduled_at <= now`) never touches
event-driven rules.

SQLite-safe: one nullable column + index, no backfill needed — every existing rule
predates the `scheduled` event type being accepted, so none of them can be
scheduled-root.
"""
import sqlalchemy as sa
from alembic import op

revision = "0016_trigger_next_scheduled"
down_revision = "0015_transfer_imports"
branch_labels = None
depends_on = None

_INDEX = "ix_trigger_rules_next_scheduled_at"


def _has_column() -> bool:
    cols = sa.inspect(op.get_bind()).get_columns("trigger_rules")
    return any(c["name"] == "next_scheduled_at" for c in cols)


def upgrade() -> None:
    if _has_column():
        return
    op.add_column(
        "trigger_rules",
        sa.Column("next_scheduled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(_INDEX, "trigger_rules", ["next_scheduled_at"])


def downgrade() -> None:
    if not _has_column():
        return
    op.drop_index(_INDEX, table_name="trigger_rules")
    op.drop_column("trigger_rules", "next_scheduled_at")
