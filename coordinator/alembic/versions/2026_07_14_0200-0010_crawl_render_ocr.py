"""crawl_render_ocr — render_mode + ocr_mode on crawl_jobs (self-host)

Single-tenant carve of the cloud 0091 migration. Adds the two orthogonal crawl
knobs the warm-browser render lane and the doc-extract (PDF/office/image + OCR)
lane are driven by:
  - crawl_jobs.render_mode : auto | http | browser.
  - crawl_jobs.ocr_mode    : auto | off | force.

Both default to "auto" (preserves the pre-existing behaviour). Idempotent:
add-column guarded by an inspector check.

Revision ID: 0010_crawl_render_ocr
Revises: 0009_target_last_checked
Create Date: 2026-07-14 02:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0010_crawl_render_ocr'
down_revision: Union[str, None] = '0009_target_last_checked'
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
    if "render_mode" not in cc:
        op.add_column(_TABLE, sa.Column(
            "render_mode", sa.String(length=20), nullable=False, server_default="auto"))
    if "ocr_mode" not in cc:
        op.add_column(_TABLE, sa.Column(
            "ocr_mode", sa.String(length=20), nullable=False, server_default="auto"))


def downgrade() -> None:
    cc = _cols()
    if "ocr_mode" in cc:
        op.drop_column(_TABLE, "ocr_mode")
    if "render_mode" in cc:
        op.drop_column(_TABLE, "render_mode")
