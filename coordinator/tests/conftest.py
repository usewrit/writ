"""
Shared pytest fixtures for the backend test suite.

Most existing suites are dependency-light (they stub orgs / monkeypatch
credit_service and drive their own asyncio loop) and need nothing here. This
file adds the pieces the *DB-backed* tests want: an ephemeral `db_engine` /
`db_session` pair built from the SQLAlchemy model metadata.

**The default backend is SQLite, because that is what this coordinator ships.**
A bare `pytest` on a fresh clone — no services, no env vars — runs every
DB-backed test against a throwaway SQLite file in pytest's tmp dir, which is the
same engine (and the same aiosqlite driver) the product uses in production. That
matters: a fixture that skipped unless Postgres was reachable meant the shipped
database had zero DB-backed coverage anywhere, including CI.

Postgres is still supported for anyone running the coordinator against one: set
`DATABASE_URL` to a `postgresql://` URL and the fixtures switch to it. In that
mode the run is REQUIRED to reach it (an unreachable explicit URL skips loudly
rather than silently falling back to SQLite and pretending it tested Postgres).

Isolation, per backend:
  * SQLite   — a fresh database file per session under `tmp_path_factory`, never
               the operator's real `writ.db`, deleted on teardown.
  * Postgres — a throwaway SCHEMA pinned via `search_path`, dropped CASCADE on
               teardown, so the connected database's `public` schema is untouched.

Either way the tables come from `Base.metadata`, which keeps the fixture
independent of the Alembic chain (export.sh proves `alembic upgrade head`
separately, as its own real-boot step). Loop scoping for the session-scoped
async fixtures is set in pytest.ini via asyncio_default_*_loop_scope, not a
custom event_loop fixture.
"""
import asyncio
import os
import sys
import uuid
from pathlib import Path

import pytest
import pytest_asyncio

# Make `import config`, `import database`, `import models` resolve the same way
# the app does (sys.path includes the coordinator directory), regardless of pytest's rootdir.
BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


def _explicit_postgres_url() -> str | None:
    """
    The Postgres URL the operator explicitly asked for, normalised to asyncpg —
    or None, which means "use the shipped SQLite backend".

    Only an explicit `DATABASE_URL` counts. `settings.database_url` is NOT
    consulted: on a self-host install it points at the operator's real
    `writ.db`, and a test run must never open that file.
    """
    url = (os.getenv("DATABASE_URL") or "").strip()
    if not url:
        return None
    if url.startswith("postgres://"):  # heroku-style scheme
        url = url.replace("postgres://", "postgresql+asyncpg://", 1)
    elif url.startswith("postgresql://") and "+asyncpg" not in url:
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if not url.startswith("postgresql"):
        # A sqlite DATABASE_URL is honoured as "just use SQLite" — the throwaway
        # file below is used regardless, so the operator's path is never opened.
        return None
    return url


def _is_reachable(url: str) -> bool:
    """
    Best-effort connectivity probe; never raises. Runs on its OWN short-lived
    event loop via asyncio.run() so it works before pytest-asyncio installs one
    and stays correct on Python 3.12+/3.14 (where asyncio.get_event_loop() with
    no running loop is deprecated/raises).
    """

    async def _probe() -> bool:
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        engine = create_async_engine(url, pool_pre_ping=True)
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        finally:
            await engine.dispose()

    try:
        return asyncio.run(_probe())
    except Exception:
        return False


@pytest.fixture(scope="session")
def db_url(tmp_path_factory) -> str:
    """
    The async database URL the DB-backed fixtures bind to.

    Default: a throwaway SQLite file — the engine this coordinator actually
    ships — so a bare `pytest` on a fresh clone exercises real DB code.

    Override: `DATABASE_URL=postgresql://…` switches the session to Postgres.
    That is an explicit request, so an unreachable server SKIPS loudly instead
    of silently falling back to SQLite and reporting a green Postgres run that
    never happened.
    """
    pg = _explicit_postgres_url()
    if pg is None:
        db_file = tmp_path_factory.mktemp("writ-db") / f"test_{uuid.uuid4().hex[:12]}.db"
        return f"sqlite+aiosqlite:///{db_file}"
    if not _is_reachable(pg):
        pytest.skip(
            f"DATABASE_URL points at Postgres ({pg!r}) but nothing is reachable there. "
            "Start it, or unset DATABASE_URL to run these against SQLite.",
            allow_module_level=False,
        )
    return pg


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def db_engine(db_url):
    """
    Session-scoped async engine over an isolated, throwaway database built from
    the model metadata: a temp file on SQLite, a throwaway SCHEMA on Postgres.
    Either way nothing outside the fixture is touched, and teardown removes it.
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    is_sqlite = db_url.startswith("sqlite")
    schema = f"test_{uuid.uuid4().hex[:12]}"

    if is_sqlite:
        engine = create_async_engine(db_url)
    else:
        # Pin every connection to the throwaway schema via search_path so model
        # tables (which declare no explicit schema) land there.
        engine = create_async_engine(
            db_url,
            pool_pre_ping=True,
            connect_args={"server_settings": {"search_path": schema}},
        )

    # Import models for their side effect: registering every table on Base.metadata.
    import models  # noqa: F401
    from database import Base

    async with engine.begin() as conn:
        if is_sqlite:
            # Model code relies on ON DELETE cascades, which SQLite ignores
            # unless foreign keys are switched on per connection — the same
            # PRAGMA database.py applies at runtime.
            await conn.execute(text("PRAGMA foreign_keys=ON"))
        else:
            # search_path can't be used for CREATE SCHEMA target, so name it explicitly.
            await conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
        await conn.run_sync(Base.metadata.create_all)

    try:
        yield engine
    finally:
        if not is_sqlite:
            async with engine.begin() as conn:
                await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()
        if is_sqlite:
            # tmp_path_factory keeps the last few runs around; drop the file now
            # so a long CI session does not accumulate databases.
            Path(db_url.replace("sqlite+aiosqlite:///", "", 1)).unlink(missing_ok=True)


@pytest_asyncio.fixture(loop_scope="session")
async def db_session(db_engine):
    """
    Function-scoped AsyncSession wrapped in a transaction that is ROLLED BACK on
    teardown, so each test sees a clean slate without re-creating the schema.
    """
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    connection = await db_engine.connect()
    trans = await connection.begin()
    maker = async_sessionmaker(bind=connection, expire_on_commit=False, class_=AsyncSession)
    session = maker()
    try:
        yield session
    finally:
        await session.close()
        await trans.rollback()
        await connection.close()
