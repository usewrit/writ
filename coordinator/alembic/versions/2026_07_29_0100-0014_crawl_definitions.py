"""Saved, re-runnable crawl configurations — the callable crawl API surface.

Revision ID: 0014_crawl_definitions
Revises: 0013_api_key_scopes_v2
Create Date: 2026-07-29

A ``crawl_jobs`` row is one RUN: its settings live on the row and its id dies
with that run. That left a crawl with no stable handle, so it could not be
exposed as an API the way a workflow can — the URL would change on every
re-crawl.

``crawl_definitions`` is that handle. It owns the saved settings (as one
validated ``StartCrawlRequest`` blob, so a new crawl option cannot silently go
missing from a hand-maintained column mirror) plus a slug, and
``crawl_jobs.definition_id`` points every run back at the config that launched
it. Runs become the definition's history, which is what makes ``max_age``
answerable: "has this saved crawl completed recently enough to reuse?"

SQLite-safe throughout: a new table, plus one NULLABLE column on ``crawl_jobs``
— no table rebuild, no NOT NULL backfill.

On the ``definition_id`` FK: the model declares it (so a fresh ``create_all``
database gets the real constraint), but this migration adds the column WITHOUT
one, because SQLite cannot attach a foreign key to an existing table without
rebuilding it — and rebuilding ``crawl_jobs`` to gain a constraint SQLite does
not enforce by default is a poor trade. An upgraded database therefore relies on
the application for the relationship. That is safe here: a dangling
``definition_id`` only ever fails to match a freshness lookup, which falls
through to a normal crawl.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0014_crawl_definitions'
down_revision: Union[str, None] = '0013_api_key_scopes_v2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_JOBS = "crawl_jobs"
_DEFS = "crawl_definitions"


def _tables() -> set:
    try:
        return set(sa.inspect(op.get_bind()).get_table_names())
    except Exception:
        return set()


def _cols(table: str) -> set:
    insp = sa.inspect(op.get_bind())
    try:
        return {c["name"] for c in insp.get_columns(table)}
    except Exception:
        return set()


def _indexes(table: str) -> set:
    insp = sa.inspect(op.get_bind())
    try:
        return {i["name"] for i in insp.get_indexes(table)}
    except Exception:
        return set()


def upgrade() -> None:
    if _DEFS not in _tables():
        op.create_table(
            _DEFS,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("name", sa.String(length=200), nullable=False),
            sa.Column("slug", sa.String(length=120), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("config", sa.JSON(), nullable=False),
            sa.Column("seed_url", sa.Text(), nullable=False),
            sa.Column("default_max_age_seconds", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_crawl_definitions_slug", _DEFS, ["slug"], unique=True)
        op.create_index("ix_crawl_definitions_created_at", _DEFS, ["created_at"])

    if _JOBS in _tables():
        if "definition_id" not in _cols(_JOBS):
            op.add_column(_JOBS, sa.Column("definition_id", sa.Integer(), nullable=True))
        existing = _indexes(_JOBS)
        if "ix_crawl_jobs_definition_id" not in existing:
            op.create_index("ix_crawl_jobs_definition_id", _JOBS, ["definition_id"])
        # The freshness lookup: newest completed run for a definition. Without
        # it, every max_age-qualified call scans that definition's run history.
        if "ix_crawl_jobs_definition_completed" not in existing:
            op.create_index(
                "ix_crawl_jobs_definition_completed",
                _JOBS, ["definition_id", "status", "completed_at"],
            )


def downgrade() -> None:
    if _JOBS in _tables():
        existing = _indexes(_JOBS)
        if "ix_crawl_jobs_definition_completed" in existing:
            op.drop_index("ix_crawl_jobs_definition_completed", table_name=_JOBS)
        if "ix_crawl_jobs_definition_id" in existing:
            op.drop_index("ix_crawl_jobs_definition_id", table_name=_JOBS)
        if "definition_id" in _cols(_JOBS):
            op.drop_column(_JOBS, "definition_id")
    if _DEFS in _tables():
        op.drop_table(_DEFS)
