"""workflow_auth_config — browserless HTTP lane: auth_config + http_capable (self-host)

Adds two columns to ``automation_workflows`` mirroring the cloud 0087 migration:

  * auth_config (JSON, nullable) — the declarative AuthRecipe used to authenticate an
    api_call/login_post workflow over HTTP without launching a browser.
  * http_capable (Boolean, nullable) — runtime hint stamped on completion (True=HTTP-proven,
    False=browser-only, Null=unknown/probe).

Both additive and nullable. DDL is check-first so re-running is a no-op even where schema_sync
created the column at startup.

Apply forward with:  alembic upgrade 0007_workflow_auth_config

Revision ID: 0007_workflow_auth_config
Revises: 0006_ai_sessions
Create Date: 2026-07-10 07:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0007_workflow_auth_config'
down_revision: Union[str, None] = '0006_ai_sessions'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "automation_workflows"


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
    if not _has_table(_TABLE):
        # Fresh DB created from models — the model already declares these columns.
        return
    if not _has_column(_TABLE, "auth_config"):
        op.add_column(_TABLE, sa.Column("auth_config", sa.JSON(), nullable=True))
    if not _has_column(_TABLE, "http_capable"):
        op.add_column(_TABLE, sa.Column("http_capable", sa.Boolean(), nullable=True))


def downgrade() -> None:
    if not _has_table(_TABLE):
        return
    for col in ("http_capable", "auth_config"):
        if _has_column(_TABLE, col):
            op.drop_column(_TABLE, col)
