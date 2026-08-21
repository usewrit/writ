"""crawl_ai_executor — executor + extract_prompt on crawl_jobs (self-host)

Single-operator carve of the cloud's AI crawl executor. Splits "who reads the
page" out of the output-shape enum, exactly as the cloud model does:

  - crawl_jobs.executor       : regular | ai  (default "regular" — every
                                pre-existing crawl keeps its behaviour).
  - crawl_jobs.extract_prompt : the per-page extraction instruction the ai
                                executor applies.

There is no billing column and no metered gateway here: a self-hosted AI crawl
runs on the owner's OWN provider (Settings → AI, or a BYO-key agent), so the
port is these two columns and nothing else.

Idempotent: add-column guarded by an inspector check, matching 0010.

Revision ID: 0020_crawl_ai_executor
Revises: 0019_aiws_login_for_persona
Create Date: 2026-08-19 01:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0020_crawl_ai_executor'
down_revision: Union[str, None] = '0019_aiws_login_for_persona'
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
        return
    if "executor" not in cc:
        op.add_column(_TABLE, sa.Column(
            "executor", sa.String(length=20), nullable=False, server_default="regular"))
    if "extract_prompt" not in cc:
        op.add_column(_TABLE, sa.Column("extract_prompt", sa.Text(), nullable=True))


def downgrade() -> None:
    cc = _cols()
    if "extract_prompt" in cc:
        op.drop_column(_TABLE, "extract_prompt")
    if "executor" in cc:
        op.drop_column(_TABLE, "executor")
