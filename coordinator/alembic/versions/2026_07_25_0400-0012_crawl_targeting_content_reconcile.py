"""crawl_targeting_content_reconcile — intent targeting, content spec, reconcile totals

Single-owner carve of the cloud 0092 (targeting) + 0094 (content spec) migrations
plus the reconcile columns that shipped with the original crawl_jobs table there
but were left out of the self-host baseline (0008).

Adds to crawl_jobs:
  Targeting (drives the RANKED frontier — see services/crawl_targeting):
    - intent              TEXT     plain-English crawl goal
    - seed_urls           JSON     operator-picked seeds from the /crawl/map step
    - relevance_threshold FLOAT    drop URLs scoring below this (0 = keep all)
    - derived_scope       JSON     audit of the derived {include,exclude,depth,reason}
  Content selection (which page ELEMENTS the scrape keeps):
    - content_spec        JSON     {preset, include_comments, exclude_selectors,
                                    include_selectors, keep:{images,tables,links}}
  End-of-crawl reconciliation (dedup shard records by URL at finalize):
    - records_total       INTEGER
    - duplicates_removed  INTEGER
    - reconciled_at       DATETIME

Every add-column is guarded by an inspector check, so re-runs are idempotent and
a partially-migrated database converges. SQLite-safe: all new columns are
nullable or carry a scalar server_default (no table rebuild required).

Revision ID: 0012_crawl_targeting_content_reconcile
Revises: 0011_user_notification_preferences
Create Date: 2026-07-25 04:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0012_crawl_targeting_content_reconcile'
down_revision: Union[str, None] = '0011_user_notification_preferences'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "crawl_jobs"


def _cols() -> set:
    insp = sa.inspect(op.get_bind())
    try:
        return {c["name"] for c in insp.get_columns(_TABLE)}
    except Exception:
        return set()


def upgrade() -> None:
    cc = _cols()
    if not cc:
        # No crawl_jobs table (fresh install runs 0008 first) — nothing to do.
        return

    # --- Targeting ----------------------------------------------------------
    if "intent" not in cc:
        op.add_column(_TABLE, sa.Column("intent", sa.Text(), nullable=True))
    if "seed_urls" not in cc:
        op.add_column(_TABLE, sa.Column("seed_urls", sa.JSON(), nullable=True))
    if "relevance_threshold" not in cc:
        op.add_column(_TABLE, sa.Column(
            "relevance_threshold", sa.Float(), nullable=False, server_default="0"))
    if "derived_scope" not in cc:
        op.add_column(_TABLE, sa.Column("derived_scope", sa.JSON(), nullable=True))

    # --- Content selection --------------------------------------------------
    if "content_spec" not in cc:
        op.add_column(_TABLE, sa.Column("content_spec", sa.JSON(), nullable=True))

    # --- Reconciliation -----------------------------------------------------
    if "records_total" not in cc:
        op.add_column(_TABLE, sa.Column("records_total", sa.Integer(), nullable=True))
    if "duplicates_removed" not in cc:
        op.add_column(_TABLE, sa.Column("duplicates_removed", sa.Integer(), nullable=True))
    if "reconciled_at" not in cc:
        op.add_column(_TABLE, sa.Column(
            "reconciled_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    cc = _cols()
    for col in ("reconciled_at", "duplicates_removed", "records_total",
                "content_spec", "derived_scope", "relevance_threshold",
                "seed_urls", "intent"):
        if col in cc:
            op.drop_column(_TABLE, col)
