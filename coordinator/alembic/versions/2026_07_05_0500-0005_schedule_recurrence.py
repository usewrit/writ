"""schedule_recurrence — structured recurrence (interval | daily | weekly) columns

Adds precise recurring schedules to workflows and monitors on the self-host
coordinator. The existing "every N ms" behaviour is ``schedule_kind = 'interval'``
(the default) and is byte-identical to before — these columns are purely
additive / back-compat. (No ai_workflow_sessions table on the coordinator.)

Adds to each of ``automation_workflows`` and ``targets``:
  - schedule_kind  VARCHAR(16) NOT NULL DEFAULT 'interval'
  - schedule_time  VARCHAR(5)  NULL      -- "HH:MM" local wall-clock (daily/weekly)
  - schedule_days  JSON        NULL      -- ISO weekday ints 1=Mon..7=Sun (weekly)
  - schedule_tz    VARCHAR(64) NULL      -- IANA tz name (daily/weekly); NULL => UTC

Check-first DDL: every step is guarded so re-running is a no-op even if a prior
attempt (or a schema_sync at startup) already added a column. Chains onto the
current single head.

Apply forward with:  alembic upgrade 0005_schedule_recurrence

Revision ID: 0005_schedule_recurrence
Revises: 0004_mail_connections
Create Date: 2026-07-05 05:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0005_schedule_recurrence'
down_revision: Union[str, None] = '0004_mail_connections'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = ("automation_workflows", "targets")


def _has_table(table: str) -> bool:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    try:
        return table in set(insp.get_table_names())
    except Exception:
        return False


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    try:
        return column in {c["name"] for c in insp.get_columns(table)}
    except Exception:
        return False


def upgrade() -> None:
    for table in _TABLES:
        if not _has_table(table):
            # Table absent (fresh DB created from models) — the model already
            # declares these columns, so there is nothing to backfill.
            continue
        if not _has_column(table, "schedule_kind"):
            op.add_column(
                table,
                sa.Column(
                    "schedule_kind", sa.String(length=16),
                    nullable=False, server_default="interval",
                ),
            )
        if not _has_column(table, "schedule_time"):
            op.add_column(
                table, sa.Column("schedule_time", sa.String(length=5), nullable=True)
            )
        if not _has_column(table, "schedule_days"):
            op.add_column(
                table, sa.Column("schedule_days", sa.JSON(), nullable=True)
            )
        if not _has_column(table, "schedule_tz"):
            op.add_column(
                table, sa.Column("schedule_tz", sa.String(length=64), nullable=True)
            )


def downgrade() -> None:
    for table in _TABLES:
        if not _has_table(table):
            continue
        for col in ("schedule_tz", "schedule_days", "schedule_time", "schedule_kind"):
            if _has_column(table, col):
                op.drop_column(table, col)
