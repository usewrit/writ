"""targets.last_checked_at — fleet's proof a target is being checked at its interval

Adds a nullable ``last_checked_at`` timestamp to ``targets``, stamped by
``report_ingest.submit_reports_internal`` on EVERY authorized agent check-report
(uptime or content, change or no-change). Powers "last checked N ago" in the UI and
lets operators verify the fleet is actually firing checks on schedule.

Idempotent: a no-op if the column already exists (fresh DB built from the models).

Revision ID: 0009_target_last_checked
Revises: 0008_crawl_jobs
Create Date: 2026-07-14 01:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0009_target_last_checked'
down_revision: Union[str, None] = '0008_crawl_jobs'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "targets"
_COL = "last_checked_at"


def _has_column(table: str, col: str) -> bool:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    try:
        return col in {c["name"] for c in insp.get_columns(table)}
    except Exception:
        return False


def upgrade() -> None:
    if _has_column(_TABLE, _COL):
        return
    op.add_column(_TABLE, sa.Column(_COL, sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_targets_last_checked_at", _TABLE, [_COL])


def downgrade() -> None:
    if not _has_column(_TABLE, _COL):
        return
    try:
        op.drop_index("ix_targets_last_checked_at", table_name=_TABLE)
    except Exception:
        pass
    op.drop_column(_TABLE, _COL)
