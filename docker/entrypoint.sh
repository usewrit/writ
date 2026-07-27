#!/bin/sh
# Entrypoint for the self-hosted Writ coordinator (single container).
#
# 1. Ensure the data dir exists and is writable (VOLUME /data holds the SQLite
#    DB + tenant file bytes; a fresh named volume comes up empty/root-owned on
#    some engines, so create the subtree defensively).
# 2. Bring the schema up to date (alembic reads settings.database_url, which is
#    derived from WRIT_DB_PATH -> sqlite+aiosqlite:////data/writ.db).
# 3. Hand off to uvicorn (exec so it becomes PID 1 and receives SIGTERM/SIGINT
#    for graceful container stop).
set -eu

# Where the SQLite DB + files live. Defaults match the Dockerfile ENV but stay
# overridable so the same image works with a different mount layout.
DB_PATH="${WRIT_DB_PATH:-/data/writ.db}"
FILES_DIR="${WRIT_FILES_DIR:-/data/files}"
DB_DIR="$(dirname "$DB_PATH")"

mkdir -p "$DB_DIR" "$FILES_DIR"

echo "[entrypoint] applying database migrations (alembic upgrade head)..."
alembic upgrade head

echo "[entrypoint] starting coordinator on 0.0.0.0:8000 ..."
# Launch via serve.py (NOT `uvicorn main:app`) so WS protocol-level keepalive is
# disabled — the light writ-agent answers a WS Ping with a binary frame, which
# uvicorn's keepalive would otherwise misread as unresponsive and drop the healthy
# agent after ~1 min. See coordinator/serve.py for the full rationale.
export HOST="0.0.0.0"
export PORT="${PORT:-8000}"
exec python serve.py
