"""`ai_sessions.login_for_persona_id` — an AI session that RECORDS a persona's sign-in.

Revision ID: 0019_aiws_login_for_persona
Revises: 0018_workflow_recorded_sess
Create Date: 2026-08-17

Persona creation can now hand the sign-in to an AI session: the coordinator
dispatches `ai_session_start` to a fleet agent with the persona's credentials
sealed under the agent's channel key, the agent signs in and records the flow,
and its terminal frame returns the recorded RECIPE. This column marks which
persona that session is recording for, so the completion handler can materialize
the recipe as a coordinator-side workflow and point `personas.login_workflow_id`
at it.

Why the column rather than in-memory state: dispatch is fire-and-forget and the
reply lands on the agent's socket minutes later, so a coordinator restart in
between must not lose the link — the wiring is driven off this row, not a
pending-request map.

Note this is NOT the agent-side `workflow_id` already on the row: that id lives in
the agent's own namespace and can never satisfy the FK to `automation_workflows`.

SET NULL semantics: deleting the persona must never delete the session record.

SQLite-safe: batch_alter_table rebuilds the table to attach the FK (SQLite cannot
ALTER TABLE ADD CONSTRAINT); mirrors 0017_persona_login_workflow.
"""
import sqlalchemy as sa
from alembic import op

revision = "0019_aiws_login_for_persona"
down_revision = "0018_workflow_recorded_sess"
branch_labels = None
depends_on = None


def upgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("ai_sessions")}
    with op.batch_alter_table("ai_sessions", schema=None) as batch_op:
        if "login_for_persona_id" not in cols:
            batch_op.add_column(sa.Column("login_for_persona_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_ai_sessions_login_for_persona_id",
            "personas",
            ["login_for_persona_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index(
            "ix_ai_sessions_login_for_persona_id", ["login_for_persona_id"]
        )


def downgrade() -> None:
    with op.batch_alter_table("ai_sessions", schema=None) as batch_op:
        batch_op.drop_index("ix_ai_sessions_login_for_persona_id")
        batch_op.drop_constraint("fk_ai_sessions_login_for_persona_id", type_="foreignkey")
        batch_op.drop_column("login_for_persona_id")
