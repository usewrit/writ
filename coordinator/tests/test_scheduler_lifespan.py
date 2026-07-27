"""Runtime test: the APScheduler is started in the app lifespan with the 4 jobs.

Uses a fresh SQLite DB (WRIT_DB_PATH) migrated with alembic upgrade head, then
enters the FastAPI lifespan via TestClient(app) as a context manager and
introspects app.state.scheduler.

Env is set at import time (before `config`/`database`/`main` import) because
`database.database_url` is resolved from WRIT_DB_PATH at module import.
"""
import os
import pathlib
import tempfile

# --- Isolated environment (set BEFORE importing the app) --------------------
_TMPDIR = tempfile.mkdtemp(prefix="writ-sched-test-")
_DB_PATH = str(pathlib.Path(_TMPDIR) / "writ.db")
os.environ["WRIT_DB_PATH"] = _DB_PATH
# development keeps the config's secret gates permissive (this is a lifespan-wiring
# test, not a secrets test) and auto-creates tables on startup.
os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("RECORDER_AUTH_SECRET", "test-recorder-secret-0123456789abcdef")
os.environ.setdefault("SECRET_ENCRYPTION_KEY", "test-encryption-key-0123456789abcdefABCDEF")
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-0123456789abcdefABCDEF")
os.environ.setdefault("SECRET_KEY", "test-jwt-secret-0123456789abcdefABCDEF")
os.environ.setdefault("HMAC_SECRET_KEY", "test-hmac-secret-0123456789abcdefABCDEF0123456789")
os.environ.setdefault("API_SECRET_KEY", "test-api-secret-0123456789abcdefABCDEF0123456789")
os.environ.setdefault("WRIT_PUBLIC_URL", "http://localhost:8000")

import pytest  # noqa: E402


def _migrate() -> None:
    """alembic upgrade head against the temp DB (same WRIT_DB_PATH the app uses)."""
    from alembic import command
    from alembic.config import Config as AlembicConfig

    root = pathlib.Path(__file__).resolve().parent.parent
    cfg = AlembicConfig(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "alembic"))
    command.upgrade(cfg, "head")


def test_scheduler_started_with_four_jobs():
    _migrate()

    from fastapi.testclient import TestClient
    from main import app
    from services.scheduler import (
        JOB_MONITOR_DISPATCH,
        JOB_STALENESS_SWEEP,
        JOB_SCHEDULED_WORKFLOWS,
        JOB_HOUSEKEEPING,
    )

    expected = {
        JOB_MONITOR_DISPATCH,
        JOB_STALENESS_SWEEP,
        JOB_SCHEDULED_WORKFLOWS,
        JOB_HOUSEKEEPING,
    }

    # Entering the context manager runs the lifespan startup (starts the scheduler);
    # leaving it runs shutdown (stops the scheduler).
    with TestClient(app) as client:
        # Sanity: the app is up.
        assert client.get("/").status_code == 200

        scheduler = getattr(app.state, "scheduler", None)
        assert scheduler is not None, "app.state.scheduler was not set in lifespan"
        assert scheduler.running is True, "scheduler is not running"

        job_ids = {j.id for j in scheduler.get_jobs()}
        assert expected.issubset(job_ids), (
            f"missing scheduler jobs: expected {expected}, got {job_ids}"
        )
        assert len(expected & job_ids) == 4

        # Every job must be coalesced + single-instance (single-process contract).
        for job in scheduler.get_jobs():
            if job.id in expected:
                assert job.coalesce is True, f"{job.id} not coalesced"
                assert job.max_instances == 1, f"{job.id} max_instances != 1"

    # After lifespan shutdown the scheduler must be stopped.
    assert scheduler.running is False, "scheduler still running after shutdown"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
