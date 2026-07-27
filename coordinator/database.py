"""
Database configuration and session management.

Uses SQLAlchemy 2.0 async patterns over a local SQLite file (aiosqlite driver).

The self-hosted coordinator dispatches every browser workload to fleet agents,
so it has no need for a networked Postgres — it ships a single-file SQLite
database instead (WRIT_DB_PATH, resolved into ``settings.database_url``). The
async session maker + ``get_db`` dependency keep the exact same shape they had
under Postgres so every caller is unchanged; only the engine construction and
the connection pragmas differ.
"""
import logging
from typing import AsyncGenerator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import declarative_base

from config import settings

logger = logging.getLogger(__name__)

# The async SQLite URL derived from WRIT_DB_PATH (see config.Settings.database_url),
# e.g. "sqlite+aiosqlite:////data/writ.db".
database_url = str(settings.database_url)

# Create async engine.
#
# SQLite has no server-side connection pool to size, so the Postgres pool math
# (DB_POOL_SIZE/DB_MAX_OVERFLOW/PgBouncer) is gone. We keep pool_pre_ping off —
# a local file connection can't go "stale" the way a networked one can — and
# rely on the busy_timeout pragma (below) plus WAL to survive concurrent
# writers within a single process. SQLAlchemy's async SQLite uses an
# AsyncAdaptedQueuePool by default; the defaults are fine for a single-node
# coordinator.
engine = create_async_engine(
    database_url,
    echo=settings.log_level == "debug",
    # aiosqlite ignores unknown args, but keep the connect timeout modest so a
    # locked DB fails fast rather than hanging a request indefinitely. The
    # per-statement lock wait is governed by the busy_timeout pragma below.
    connect_args={"timeout": 10},
)


@event.listens_for(engine.sync_engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, connection_record) -> None:
    """
    Apply the durable, concurrency-friendly SQLite pragmas on every new
    connection.

    - journal_mode=WAL       : readers never block the single writer (and vice
                               versa), which is what makes SQLite viable under
                               an async web server.
    - foreign_keys=ON        : SQLite disables FK enforcement per-connection by
                               default; the schema relies on it.
    - busy_timeout=5000      : wait up to 5s for a competing writer's lock to
                               clear before raising "database is locked".
    - synchronous=NORMAL     : the safe/fast pairing with WAL (no data loss on
                               app crash; only a power loss between checkpoints
                               risks the last transactions — acceptable here).

    The listener is registered on the SYNC engine that backs the async engine;
    it fires once per physical DBAPI connection as aiosqlite opens it.
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA synchronous=NORMAL")
    finally:
        cursor.close()


# Create async session factory
AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)

# Alias for background task usage (without request dependency)
async_session_maker = AsyncSessionLocal

# Base class for declarative models
Base = declarative_base()


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    Dependency for getting async database sessions.

    Yields:
        AsyncSession: Database session

    Example:
        @app.get("/users")
        async def get_users(db: AsyncSession = Depends(get_db)):
            result = await db.execute(select(User))
            return result.scalars().all()
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception as e:
            await session.rollback()
            logger.error(f"Database session error: {e}", exc_info=True)
            raise
        finally:
            await session.close()


async def init_db() -> None:
    """
    Bring the database schema up to date with the models.

    Schema creation is handled by ``alembic upgrade head`` on boot, so this is
    a no-op kept for the lifespan call site.
    """
    return None


async def close_db() -> None:
    """Close database connections on shutdown."""
    await engine.dispose()
    logger.info("Database connections closed")
