"""automation_workflows.mcp_tool_pinned — opt-in per-workflow MCP tool exposure.

The coordinator's MCP server (/mcp) used to mint one run_<name> tool for EVERY
saved workflow. MCP clients inject every advertised tool schema into model
context on every request, and several enforce hard tool caps, so an instance
with many workflows either burned thousands of tokens per turn or got its tool
list truncated arbitrarily. Exposure is now opt-in per workflow via this flag
(and capped server-side); every workflow — pinned or not — stays callable
through writ_run_workflow, and a stale run_<name> call from a client that
cached the old tool list still resolves via a conservative slug fallback.

No backfill on purpose: existing workflows become unpinned, which IS the new
default the flag exists to establish. Mirrors cloud migration 0140.

Check-first via the inspector (same shape as 0021): the shipped backend is
SQLite, which has no `ADD COLUMN IF NOT EXISTS`, and a fresh install's
create_all already builds the column from the model — so both paths must land
here as a no-op.

Revision ID: 0022_workflow_mcp_tool_pinned
Revises: 0021_transfer_import_user_fk
Create Date: 2026-08-20
"""
import sqlalchemy as sa
from alembic import op

revision = "0022_workflow_mcp_tool_pinned"
down_revision = "0021_transfer_import_user_fk"
branch_labels = None
depends_on = None

_TABLE = "automation_workflows"
_COL = "mcp_tool_pinned"


def _has_column(bind) -> bool:
    insp = sa.inspect(bind)
    if not insp.has_table(_TABLE):
        return True  # fresh DB: create_all builds the table from the model
    return any(c["name"] == _COL for c in insp.get_columns(_TABLE))


def upgrade() -> None:
    if _has_column(op.get_bind()):
        return
    op.add_column(
        _TABLE,
        sa.Column(_COL, sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if insp.has_table(_TABLE) and any(c["name"] == _COL for c in insp.get_columns(_TABLE)):
        op.drop_column(_TABLE, _COL)
