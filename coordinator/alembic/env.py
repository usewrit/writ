"""
Alembic environment configuration for async database migrations.
"""
import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# Import Base and all models to ensure they're registered with metadata
from database import Base
from config import settings

# Import the models package so EVERY model registers on Base.metadata.
# (models/__init__.py imports all model modules; importing a hand-picked
# subset here made autogenerate silently miss tables.)
import models  # noqa: F401

# this is the Alembic Config object
config = context.config

# Set database URL from settings. The self-host coordinator runs on a single
# SQLite file via the async aiosqlite driver (Postgres/asyncpg was carved out),
# e.g. "sqlite+aiosqlite:////data/writ.db".
db_url = str(settings.database_url)
config.set_main_option("sqlalchemy.url", db_url)

# Interpret the config file for Python logging
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # SQLite cannot ALTER TABLE in most ways; batch mode emulates it by
        # rebuild-and-copy so future migrations that add/drop/alter columns work.
        render_as_batch=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # See run_migrations_offline: SQLite needs batch (rebuild) ALTER support.
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations in 'online' mode with async engine."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
