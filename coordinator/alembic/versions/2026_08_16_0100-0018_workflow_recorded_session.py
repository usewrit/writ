"""Pin the RECORDING browser's session to the workflow it produced.

Recording happens in a real browser: the user may clear a captcha, accept
cookie walls, or sign in while recording. That state (cookies + local/session
storage + fingerprint) previously died with the recording context, so the
first replay met every wall again. These columns hold that session — captured
at save time, Fernet-encrypted with the coordinator secret key (persona
framing: json -> gzip -> b64 -> Fernet) — and replays seed from it whenever no
persona is linked (a linked persona's warm session always wins).

Revision ID: 0018_workflow_recorded_sess
Revises: 0017_persona_login_workflow
Create Date: 2026-08-16
"""
import sqlalchemy as sa
from alembic import op

revision = "0018_workflow_recorded_sess"
down_revision = "0017_persona_login_workflow"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "automation_workflows",
        sa.Column(
            "recorded_session_encrypted",
            sa.Text(),
            nullable=True,
            comment=(
                "gzip+Fernet auth session captured from the RECORDING browser at "
                "save (cookies/storage/fingerprint — captcha clearance, logins). "
                "Replays seed from it when no persona is linked."
            ),
        ),
    )
    op.add_column(
        "automation_workflows",
        sa.Column(
            "recorded_session_captured_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When the pinned recorded session was captured or last refreshed",
        ),
    )


def downgrade() -> None:
    op.drop_column("automation_workflows", "recorded_session_captured_at")
    op.drop_column("automation_workflows", "recorded_session_encrypted")
