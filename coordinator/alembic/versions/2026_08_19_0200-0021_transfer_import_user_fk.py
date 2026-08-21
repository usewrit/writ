"""transfer_imports.created_by_user_id — make the type match users.id.

The column was declared String(36) while `users.id` is Uuid, and the model put a
ForeignKey across that mismatch. Postgres refuses such a constraint outright
("key columns are of incompatible types"), so `Base.metadata.create_all` — the
path a dev/test database and a fresh boot use — died on this table and took the
rest of the schema with it. The shipped 0015 migration sidestepped it by
creating the column with NO foreign key at all, which is why installs upgraded
through alembic never noticed.

This converts the column to the referenced type and attaches the FK the model
always claimed. Values that are not parseable as a UUID are set NULL first: the
column is nullable and advisory (it records who staged an import), so dropping an
unreadable attribution is strictly better than refusing the upgrade.

SQLite-safe: `Uuid` renders as CHAR(32) hex there, so dashed values are
de-dashed and the FK is attached with batch_alter_table (SQLite cannot
ALTER TABLE ADD CONSTRAINT); mirrors 0017_persona_login_workflow.
"""
import sqlalchemy as sa
from alembic import op

revision = "0021_transfer_import_user_fk"
down_revision = "0020_crawl_ai_executor"
branch_labels = None
depends_on = None

_TABLE = "transfer_imports"
_COL = "created_by_user_id"
_FK = "fk_transfer_imports_created_by_user_id_users"


def _column_type(bind) -> str:
    for col in sa.inspect(bind).get_columns(_TABLE):
        if col["name"] == _COL:
            return str(col["type"]).upper()
    return ""


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(_TABLE):
        return  # fresh DB: create_all/0015 builds it from the corrected model
    if "UUID" in _column_type(bind):
        return  # already converted

    if bind.dialect.name == "postgresql":
        # Unparseable attributions become NULL rather than failing the upgrade.
        op.execute(
            f"UPDATE {_TABLE} SET {_COL} = NULL WHERE {_COL} IS NOT NULL AND "
            f"{_COL} !~ '^[0-9a-fA-F]{{8}}-?[0-9a-fA-F]{{4}}-?[0-9a-fA-F]{{4}}-?"
            f"[0-9a-fA-F]{{4}}-?[0-9a-fA-F]{{12}}$'"
        )
        op.execute(
            f"ALTER TABLE {_TABLE} ALTER COLUMN {_COL} TYPE uuid USING {_COL}::uuid"
        )
        # The FK 0015 never created. Guarded so a re-run is a no-op (pg_constraint
        # check rather than sa.inspect: check-first inspection is unreliable
        # through pgbouncer).
        op.execute(
            f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = '{_FK}') "
            f"THEN ALTER TABLE {_TABLE} ADD CONSTRAINT {_FK} FOREIGN KEY ({_COL}) "
            f"REFERENCES users (id) ON DELETE SET NULL; END IF; END $$;"
        )
    else:
        # SQLite: Uuid is CHAR(32) hex, so shed the dashes before retyping.
        op.execute(f"UPDATE {_TABLE} SET {_COL} = REPLACE({_COL}, '-', '') "
                   f"WHERE {_COL} IS NOT NULL")
        op.execute(f"UPDATE {_TABLE} SET {_COL} = NULL "
                   f"WHERE {_COL} IS NOT NULL AND LENGTH({_COL}) <> 32")
        with op.batch_alter_table(_TABLE) as batch:
            batch.alter_column(_COL, type_=sa.Uuid(as_uuid=True), existing_nullable=True)
            batch.create_foreign_key(_FK, "users", [_COL], ["id"], ondelete="SET NULL")


def downgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(_TABLE):
        return
    if bind.dialect.name == "postgresql":
        op.execute(f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS {_FK}")
        op.execute(
            f"ALTER TABLE {_TABLE} ALTER COLUMN {_COL} TYPE varchar(36) USING {_COL}::text"
        )
    else:
        with op.batch_alter_table(_TABLE) as batch:
            batch.drop_constraint(_FK, type_="foreignkey")
            batch.alter_column(_COL, type_=sa.String(length=36), existing_nullable=True)
