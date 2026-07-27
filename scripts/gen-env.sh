#!/bin/sh
# gen-env.sh — create a filled-in .env from .env.example with fresh secrets.
#
#   ./scripts/gen-env.sh            # refuses to overwrite an existing .env
#   ./scripts/gen-env.sh --force    # overwrite an existing .env
#
# Generates WRIT_JWT_SECRET, API_SECRET_KEY, HMAC_SECRET_KEY,
# RECORDER_AUTH_SECRET, INTERNAL_API_SECRET, GATEWAY_SECRET, DOC_EXTRACT_SECRET
# (openssl rand -hex 32) and SECRET_ENCRYPTION_KEY (a Fernet key), and writes
# them into .env in place.
#
# This list must stay in sync with the startup validator in coordinator/config.py
# (_validate_production_secrets). Every secret it requires in production is
# generated here — otherwise the documented quickstart produces a .env that
# cannot boot. The self-check at the end of this script enforces that.
set -eu

# Resolve the selfhost root (this script's parent directory).
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_EXAMPLE="$ROOT/.env.example"
ENV_FILE="$ROOT/.env"

FORCE=0
if [ "${1:-}" = "--force" ]; then
  FORCE=1
elif [ "${1:-}" != "" ]; then
  echo "usage: $0 [--force]" >&2
  exit 2
fi

[ -f "$ENV_EXAMPLE" ] || { echo "ERROR: $ENV_EXAMPLE not found." >&2; exit 1; }
command -v openssl >/dev/null 2>&1 || { echo "ERROR: openssl is required." >&2; exit 1; }

if [ -f "$ENV_FILE" ] && [ "$FORCE" -ne 1 ]; then
  echo "ERROR: $ENV_FILE already exists — refusing to overwrite it." >&2
  echo "       Re-run with --force to replace it (your current secrets will be lost)." >&2
  exit 1
fi

# --- Generate the secrets -----------------------------------------------------
JWT_SECRET="$(openssl rand -hex 32)"
API_SECRET="$(openssl rand -hex 32)"
HMAC_SECRET="$(openssl rand -hex 32)"
RECORDER_SECRET="$(openssl rand -hex 32)"
INTERNAL_SECRET="$(openssl rand -hex 32)"
GATEWAY_SECRET_VALUE="$(openssl rand -hex 32)"
# The document/OCR extraction service and every agent that calls it share this
# one. Generated here so PDF and scanned-page coverage is simply on after the
# quickstart, rather than a lane the operator has to discover and wire up.
DOC_EXTRACT_SECRET_VALUE="$(openssl rand -hex 32)"

# SECRET_ENCRYPTION_KEY must be a Fernet key: 32 random bytes, urlsafe-base64.
# Prefer the cryptography package (validates itself); fall back to openssl,
# which produces an equally valid urlsafe-base64 32-byte key.
FERNET_KEY=""
if command -v python3 >/dev/null 2>&1; then
  FERNET_KEY="$(python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())' 2>/dev/null || true)"
fi
if [ -z "$FERNET_KEY" ]; then
  echo "note: python3 'cryptography' package not importable — generating the Fernet key with openssl instead."
  FERNET_KEY="$(openssl rand -base64 32 | tr '+/' '-_')"
fi
[ -n "$FERNET_KEY" ] || { echo "ERROR: could not generate SECRET_ENCRYPTION_KEY." >&2; exit 1; }

# --- Write .env ---------------------------------------------------------------
cp "$ENV_EXAMPLE" "$ENV_FILE"

# Portable in-place substitution (BSD + GNU sed both accept -i.bak).
set_var() {
  # set_var NAME VALUE — replaces the `NAME=` line in .env.
  sed -i.bak "s|^$1=.*|$1=$2|" "$ENV_FILE" && rm -f "$ENV_FILE.bak"
}
set_var WRIT_JWT_SECRET "$JWT_SECRET"
set_var API_SECRET_KEY "$API_SECRET"
set_var HMAC_SECRET_KEY "$HMAC_SECRET"
set_var RECORDER_AUTH_SECRET "$RECORDER_SECRET"
set_var INTERNAL_API_SECRET "$INTERNAL_SECRET"
set_var GATEWAY_SECRET "$GATEWAY_SECRET_VALUE"
set_var DOC_EXTRACT_SECRET "$DOC_EXTRACT_SECRET_VALUE"
set_var SECRET_ENCRYPTION_KEY "$FERNET_KEY"

# --- Self-check: every required secret actually landed --------------------------
# `set_var` is a `sed` substitution on an existing `NAME=` line — if the template
# ever loses one of these keys, the substitution silently does nothing and the
# operator gets a .env that cannot boot. Fail here instead, with a clear cause.
MISSING=""
for _required in WRIT_JWT_SECRET API_SECRET_KEY HMAC_SECRET_KEY \
                 RECORDER_AUTH_SECRET INTERNAL_API_SECRET GATEWAY_SECRET \
                 DOC_EXTRACT_SECRET SECRET_ENCRYPTION_KEY; do
  if ! grep -qE "^${_required}=.+" "$ENV_FILE"; then
    MISSING="$MISSING $_required"
  fi
done
if [ -n "$MISSING" ]; then
  echo "ERROR: these required secrets are missing from $ENV_EXAMPLE, so they could" >&2
  echo "       not be generated:$MISSING" >&2
  echo "       Add them to .env.example (as bare 'NAME=' lines) and re-run." >&2
  rm -f "$ENV_FILE"
  exit 1
fi

# The file holds live signing keys — keep it owner-readable only.
chmod 600 "$ENV_FILE"

echo "Wrote $ENV_FILE with freshly generated secrets."
echo
echo "IMPORTANT:"
echo "  * BACK UP SECRET_ENCRYPTION_KEY somewhere separate from your data volume."
echo "    It encrypts stored credentials at rest; without it a database backup"
echo "    cannot decrypt them. See SECURITY.md."
echo "  * For production, edit .env and set WRIT_PUBLIC_URL to your public"
echo "    https:// URL and ALLOWED_HOSTS to the hostname(s) you serve on."
echo
echo "Next: docker compose -f docker/docker-compose.yml --env-file .env up -d --build"
