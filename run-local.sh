#!/usr/bin/env bash
# Run the OSS self-host coordinator locally (no Docker) for testing.
#   bash selfhost/run-local.sh
# Then open http://localhost:8000 and log in with the printed admin creds.
# Secrets + the SQLite DB are persisted under selfhost/.local so restarts keep working.
set -euo pipefail
cd "$(cd "$(dirname "$0")" && pwd)"            # -> selfhost/
COORD="coordinator"; FE="frontend"
VENV="$PWD/$COORD/.venv-p3"
LOCAL="$PWD/.local"
ENVF="$LOCAL/env.sh"
PORT="${PORT:-8000}"

mkdir -p "$LOCAL/data/files"

# 1) Python venv (reuse the one the build already created; else make it).
if [ ! -x "$VENV/bin/python" ]; then
  echo "==> creating venv + installing coordinator requirements (one-time)…"
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install -q --upgrade pip
  "$VENV/bin/pip" install -q -r "$COORD/requirements.txt"
fi

# 2) Dev secrets — generated ONCE and persisted (so the encrypted DB stays readable).
if [ ! -f "$ENVF" ]; then
  echo "==> generating dev secrets (persisted to $ENVF)…"
  # This file holds the Fernet key and all four signing secrets. Create it
  # owner-readable only — the default umask would leave it world-readable.
  umask 077
  FERNET="$("$VENV/bin/python" -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
  cat > "$ENVF" <<EOF
export ENVIRONMENT=development
export WRIT_DB_PATH="$LOCAL/data/writ.db"
export WRIT_FILES_DIR="$LOCAL/data/files"
export SECRET_ENCRYPTION_KEY="$FERNET"
export JWT_SECRET_KEY="$(openssl rand -hex 32)"
export API_SECRET_KEY="$(openssl rand -hex 32)"
export HMAC_SECRET_KEY="$(openssl rand -hex 32)"
export RECORDER_AUTH_SECRET="$(openssl rand -hex 32)"
# Suggested admin email prefilled in first-run onboarding. WRIT_ADMIN_PASSWORD is
# intentionally left UNSET so the first visit shows the in-app onboarding page
# (create your own password). Set WRIT_ADMIN_PASSWORD here to skip onboarding and
# auto-provision the admin on boot instead.
export WRIT_ADMIN_EMAIL="admin@local"
export WRIT_ADMIN_NAME="Owner"
EOF
fi
# shellcheck disable=SC1090
source "$ENVF"

# WRIT_PUBLIC_URL is PORT-derived, NOT a secret, so it is NOT persisted in env.sh
# (older env.sh files may still carry a stale line — this override wins). Deriving
# it fresh from the live PORT every launch guarantees the URL handed to agents
# (saas.url in the connect command) always matches the port the server binds. A
# drift here silently points every fleet agent at a dead port.
export WRIT_PUBLIC_URL="http://localhost:$PORT"

# 3) Resolve the web UI. A released checkout already ships a built SPA at ui/, so
#    prefer that — it needs no Node toolchain and no network. Only fall back to
#    building from source (a working copy, or after you have edited frontend/).
if [ -f "ui/index.html" ]; then
  export WRIT_UI_DIR="$PWD/ui"
elif [ -f "$FE/dist/index.html" ]; then
  export WRIT_UI_DIR="$PWD/$FE/dist"
else
  echo "==> building the web UI (one-time)…"
  command -v npm >/dev/null 2>&1 || {
    echo "ERROR: no prebuilt UI at ui/ and npm is not installed — cannot build the SPA." >&2
    exit 1
  }
  ( cd "$FE" && (npm ci || npm install) && npm run build )
  export WRIT_UI_DIR="$PWD/$FE/dist"
fi

# 4) Migrate the SQLite schema, then launch.
echo "==> applying database migrations…"
( cd "$COORD" && "$VENV/bin/alembic" upgrade head )

cat <<EOF

==========================================================
  Writ self-host coordinator is starting.
  Open:     http://localhost:$PORT
  First run: complete the on-screen setup to create your
             admin account (suggested email: $WRIT_ADMIN_EMAIL).
  Connect an agent: Fleet -> "Connect a new agent"
  DB + secrets live under: $LOCAL  (delete it for a clean slate)
==========================================================

EOF
cd "$COORD"
# Launch via serve.py (NOT `uvicorn main:app`) so WS protocol-level keepalive is
# disabled — the light writ-agent answers a WS Ping with a binary frame, which
# uvicorn's keepalive would misread as unresponsive and kill the healthy agent
# after ~1 min. See coordinator/serve.py for the full rationale.
# Loopback by default. This script runs with ENVIRONMENT=development, which is
# the RELAXED posture — the non-development secret gates are skipped and
# TrustedHost is off — so binding it to every interface would put a deliberately
# unhardened instance on whatever network the machine is attached to. Override
# with HOST=0.0.0.0 only on a network you trust, and prefer the Docker setup
# (which binds 127.0.0.1 and runs ENVIRONMENT=production) for anything real.
export HOST="${HOST:-127.0.0.1}"
export PORT
if [ "$HOST" != "127.0.0.1" ] && [ "$HOST" != "localhost" ]; then
  echo "WARNING: binding to $HOST with ENVIRONMENT=development — the relaxed"
  echo "         security posture is now reachable from your network."
fi
exec "$VENV/bin/python" serve.py
