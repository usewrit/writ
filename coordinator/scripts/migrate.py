"""
Startup migration runner.

    python scripts/migrate.py

OSS self-host coordinator: a clean-install-only migration path (no in-place
upgrade from any prior fork). Runs ``alembic upgrade head`` against the single
fresh baseline:

  * fresh/empty DB  -> baseline tables created, stamped at head
  * existing DB     -> any pending revisions beyond the baseline applied

Uses DATABASE_URL from the environment (same as the app).
"""
import asyncio
import logging
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("migrate")


def main() -> None:
    # The coordinator ships a single fresh alembic baseline for a clean install.
    # Applying the baseline (and any later revisions) via alembic is the whole
    # migration story.
    from database import engine

    async def _dispose():
        await engine.dispose()

    from alembic import command
    from alembic.config import Config as AlembicConfig

    cfg = AlembicConfig(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    command.upgrade(cfg, "head")

    asyncio.run(_dispose())
    logger.info("migrations complete")


if __name__ == "__main__":
    main()
