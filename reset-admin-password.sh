#!/usr/bin/env bash
# Reset the admin password for a LOCAL (run-local.sh) install — no email needed.
#
#   bash reset-admin-password.sh                  # generate a new password (printed)
#   bash reset-admin-password.sh --password 'X'   # set a specific one
#   bash reset-admin-password.sh --list           # list accounts
#
# For a Docker install use instead:
#   docker compose -f docker/docker-compose.yml exec coordinator python reset_password.py [args]
set -euo pipefail
cd "$(cd "$(dirname "$0")" && pwd)"            # -> selfhost/
VENV="$PWD/coordinator/.venv-p3"
ENVF="$PWD/.local/env.sh"

if [ ! -x "$VENV/bin/python" ]; then
  echo "The local environment isn't set up yet — run 'bash run-local.sh' once first." >&2
  exit 1
fi
# Load the same env the app runs with, so WRIT_DB_PATH points at the local database.
# shellcheck disable=SC1090
[ -f "$ENVF" ] && source "$ENVF"

cd coordinator
exec "$VENV/bin/python" reset_password.py "$@"
