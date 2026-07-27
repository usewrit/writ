"""crawl_jobs — Dragnet distributed-crawl control-plane table (self-host)

Adds the ``crawl_jobs`` table that owns one distributed ("Dragnet") site crawl:
scope, politeness budget, live counters, and terminal state. The live URL
frontier / visited-set lives in the coordinator's in-process Redis keyspace; this
table is the durable control-plane record the API + AI session poll.

Single-tenant carve of the cloud 0089 crawl_jobs migration:
  * NO owner/tenant column — the single-owner coordinator scopes nothing.
  * Generic SQLite-friendly types (Integer PK, JSON, no JSONB/UUID).
  * FKs to personas.id (SET NULL) and automation_workflows.id (SET NULL).

Create-first idempotent: a no-op if the table already exists (e.g. a fresh DB
built directly from the models), so re-running upgrade is safe.

Revision ID: 0008_crawl_jobs
Revises: 0007_workflow_auth_config
Create Date: 2026-07-13 08:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0008_crawl_jobs'
down_revision: Union[str, None] = '0007_workflow_auth_config'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "crawl_jobs"


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
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("seed_url", sa.Text(), nullable=False),
        # Scope
        sa.Column("include_paths", sa.JSON(), nullable=True),
        sa.Column("exclude_paths", sa.JSON(), nullable=True),
        sa.Column("max_depth", sa.Integer(), nullable=False, server_default="4"),
        sa.Column("same_domain", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("allow_subdomains", sa.Boolean(), nullable=False, server_default=sa.true()),
        # Extraction
        sa.Column("extract_mode", sa.String(length=20), nullable=False, server_default="markdown"),
        sa.Column("extract_schema", sa.JSON(), nullable=True),
        # Authenticated crawl
        sa.Column("persona_id", sa.Integer(), nullable=True),
        # Politeness / budget
        sa.Column("respect_robots", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("delay_ms", sa.Integer(), nullable=False, server_default="250"),
        sa.Column("max_concurrent_shards", sa.Integer(), nullable=False, server_default="6"),
        sa.Column("shard_size", sa.Integer(), nullable=False, server_default="25"),
        sa.Column("page_budget", sa.Integer(), nullable=False, server_default="1000"),
        # Aggregation
        sa.Column("workflow_id", sa.Integer(), nullable=True),
        sa.Column("ai_session_id", sa.Integer(), nullable=True),
        # Lifecycle + live counters
        sa.Column("status", sa.String(length=20), nullable=False, server_default="queued"),
        sa.Column("pages_discovered", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pages_done", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pages_failed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pages_skipped", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("shards_dispatched", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("shards_done", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("current_depth", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["persona_id"], ["personas.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["workflow_id"], ["automation_workflows.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_crawl_jobs_id", _TABLE, ["id"])
    op.create_index("ix_crawl_jobs_status", _TABLE, ["status"])
    op.create_index("ix_crawl_jobs_workflow_id", _TABLE, ["workflow_id"])
    op.create_index("ix_crawl_jobs_created_at", _TABLE, ["created_at"])


def downgrade() -> None:
    if not _has_table(_TABLE):
        return
    for idx in (
        "ix_crawl_jobs_created_at",
        "ix_crawl_jobs_workflow_id",
        "ix_crawl_jobs_status",
        "ix_crawl_jobs_id",
    ):
        try:
            op.drop_index(idx, table_name=_TABLE)
        except Exception:
            pass
    op.drop_table(_TABLE)
