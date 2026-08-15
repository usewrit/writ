"""`personas.login_workflow_id` + `last_login_error` — how a persona SIGNS IN.

Revision ID: 0017_persona_login_workflow
Revises: 0016_trigger_next_scheduled
Create Date: 2026-08-15

A persona's warm session (`session_state_encrypted`) could previously only arrive
by CAPTURE from something that had already signed in — /personas/from-workflow,
/personas/from-task, /personas/from-ai-session, or the run-completion write-back.
Nothing could make a persona sign IN, so a persona created from credentials alone
never satisfied the authenticated-crawl precondition ("no live login session") and
that error had no route out of it.

`login_workflow_id` names the workflow that performs the login. Dispatching it with
`persona_id` folds the persona's credentials + 2FA into the run, and the existing
completion write-back (keyed on `trigger_context._persona_id`) persists the captured
session back onto the persona — re-runnable on demand and automatically when a crawl
finds the session stale. `last_login_error` records why the most recent attempt
failed (cleared on success) so the UI can explain without digging through runs.

SET NULL semantics: deleting the login workflow must never delete the identity.

SQLite-safe: batch_alter_table rebuilds the table to attach the FK (SQLite cannot
ALTER TABLE ADD CONSTRAINT); mirrors 0003_local_workflow_source.
"""
import sqlalchemy as sa
from alembic import op

revision = "0017_persona_login_workflow"
down_revision = "0016_trigger_next_scheduled"
branch_labels = None
depends_on = None


def upgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("personas")}
    with op.batch_alter_table("personas", schema=None) as batch_op:
        if "login_workflow_id" not in cols:
            batch_op.add_column(sa.Column("login_workflow_id", sa.Integer(), nullable=True))
        if "last_login_error" not in cols:
            batch_op.add_column(sa.Column("last_login_error", sa.Text(), nullable=True))
        batch_op.create_foreign_key(
            "fk_personas_login_workflow_id",
            "automation_workflows",
            ["login_workflow_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index("ix_personas_login_workflow_id", ["login_workflow_id"])


def downgrade() -> None:
    with op.batch_alter_table("personas", schema=None) as batch_op:
        batch_op.drop_index("ix_personas_login_workflow_id")
        batch_op.drop_constraint("fk_personas_login_workflow_id", type_="foreignkey")
        batch_op.drop_column("last_login_error")
        batch_op.drop_column("login_workflow_id")
