"""ai_sessions — coordinator record of agent-run autonomous AI sessions (self-host)

Create ``ai_sessions``: one row per autonomous AI session the coordinator
dispatches to a connected fleet agent. The coordinator is a dispatch PROXY (no
coordinator-side brain) — this row stores the request metadata (goal / entry_url /
generate_workflow) plus the outcome the agent reports back (status / workflow_id /
workflow_name / steps / message / error). Secret fill values are re-sealed onto
the wire frame under the agent channel key and are NEVER persisted here.

Not tenant-scoped (single-owner coordinator). ``session_id`` is the uuid4
correlation handle used to route the ``ai_session_complete`` reply.

DDL is check-first so re-running is a no-op even where schema_sync created the
table at startup.

Apply forward with:  alembic upgrade 0006_ai_sessions

Revision ID: 0006_ai_sessions
Revises: 0005_schedule_recurrence
Create Date: 2026-07-06 06:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0006_ai_sessions'
down_revision: Union[str, None] = '0005_schedule_recurrence'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "ai_sessions"


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
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("agent_id", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=500), nullable=True),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column("entry_url", sa.String(length=2048), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="running"),
        sa.Column("workflow_id", sa.Integer(), nullable=True),
        sa.Column("workflow_name", sa.String(length=500), nullable=True),
        sa.Column("generate_workflow", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("steps", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("session_id", name="uq_ai_sessions_session_id"),
    )
    op.create_index("ix_ai_sessions_session_id", _TABLE, ["session_id"], unique=False)
    op.create_index("ix_ai_sessions_agent_id", _TABLE, ["agent_id"], unique=False)
    op.create_index("ix_ai_sessions_status", _TABLE, ["status"], unique=False)


def downgrade() -> None:
    if not _has_table(_TABLE):
        return
    op.drop_index("ix_ai_sessions_status", table_name=_TABLE)
    op.drop_index("ix_ai_sessions_agent_id", table_name=_TABLE)
    op.drop_index("ix_ai_sessions_session_id", table_name=_TABLE)
    op.drop_table(_TABLE)
