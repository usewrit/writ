#!/bin/sh
# deploy.sh — put this coordinator on a public domain, with TLS, in one command.
#
#   ./scripts/deploy.sh writ.example.com you@example.com
#
# What it does, in order:
#   1. checks Docker, the ports, and that your domain already points here;
#   2. creates .env with fresh secrets if you have not run gen-env.sh yet;
#   3. writes every domain-derived setting into .env consistently — the public
#      URL, the ACME domain and contact, CORS, and the forwarded-IP trust range;
#   4. brings up the `tls` profile, which starts Caddy alongside the coordinator;
#   5. waits for the Let's Encrypt certificate and verifies the live https URL.
#
# It is idempotent: re-run it to change domain, to repair a half-finished
# deploy, or after `docker compose down`. Existing secrets are never rotated.
#
# Flags:
#   --skip-dns-check   proceed even if the domain does not resolve here
#   --staging          use Let's Encrypt's STAGING CA (untrusted certs, but
#                      effectively unlimited retries — use while debugging)
#   --yes              never prompt; assume yes
set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$ROOT/.env"

DOMAIN=""
ACME_EMAIL=""
SKIP_DNS=0
STAGING=0
ASSUME_YES=0

bold=""; red=""; grn=""; ylw=""; dim=""; rst=""
if [ -t 1 ]; then
  bold="$(printf '\033[1m')"; red="$(printf '\033[1;31m')"; grn="$(printf '\033[1;32m')"
  ylw="$(printf '\033[1;33m')"; dim="$(printf '\033[2m')"; rst="$(printf '\033[0m')"
fi
say()  { printf '%s::%s %s\n' "$bold" "$rst" "$*"; }
ok()   { printf '%s ok %s %s\n' "$grn" "$rst" "$*"; }
warn() { printf '%swarn%s %s\n' "$ylw" "$rst" "$*" >&2; }
die()  { printf '%serr %s %s\n' "$red" "$rst" "$*" >&2; exit 1; }

usage() {
  cat >&2 <<EOF
usage: $0 <domain> [acme-email] [--skip-dns-check] [--staging] [--yes]

  domain       the hostname users will open, e.g. writ.example.com
               It must already have an A (or AAAA) record pointing at this server.
  acme-email   contact address for Let's Encrypt expiry/failure notices.
               Strongly recommended; without it you get no warning when renewal breaks.
EOF
  exit 2
}

for arg in "$@"; do
  case "$arg" in
    --skip-dns-check) SKIP_DNS=1 ;;
    --staging)        STAGING=1 ;;
    --yes|-y)         ASSUME_YES=1 ;;
    -h|--help)        usage ;;
    -*)               die "unknown flag: $arg" ;;
    *)
      if [ -z "$DOMAIN" ]; then DOMAIN="$arg"
      elif [ -z "$ACME_EMAIL" ]; then ACME_EMAIL="$arg"
      else die "unexpected argument: $arg"
      fi
      ;;
  esac
done

[ -n "$DOMAIN" ] || usage

# A scheme or path here is the single most likely typo, and it would end up in
# the Caddy site address (where it is illegal) and in WRIT_PUBLIC_URL (where it
# would double the scheme). Reject it with the corrected value rather than
# silently repairing, so the operator's own notes stay right.
case "$DOMAIN" in
  http://*|https://*) die "pass the bare hostname, not a URL: $(printf '%s' "$DOMAIN" | sed -e 's|^https\{0,1\}://||' -e 's|/.*$||')" ;;
  */*)                die "pass the bare hostname, no path: $(printf '%s' "$DOMAIN" | sed 's|/.*$||')" ;;
  *:*)                die "pass the bare hostname, no port: $(printf '%s' "$DOMAIN" | sed 's|:.*$||')" ;;
esac
case "$DOMAIN" in
  *.*) : ;;
  *)   die "'$DOMAIN' is not a fully-qualified domain. Let's Encrypt cannot issue for a bare label." ;;
esac
if [ -z "$ACME_EMAIL" ]; then
  warn "no ACME contact email given. Let's Encrypt will not be able to warn you"
  warn "when renewal starts failing. Re-run with: $0 $DOMAIN you@example.com"
fi

confirm() {
  # confirm <question> — returns 0 for yes. Non-interactive runs (no tty) and
  # --yes both auto-accept, so this is safe in a provisioning script.
  [ "$ASSUME_YES" -eq 1 ] && return 0
  [ -t 0 ] || return 0
  printf '%s [y/N] ' "$1"
  read -r _reply || return 1
  case "$_reply" in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}

# --- 1. Preflight -------------------------------------------------------------
say "Checking prerequisites…"
command -v docker >/dev/null 2>&1 || die "docker is not installed. See https://docs.docker.com/engine/install/"
if docker compose version >/dev/null 2>&1; then
  COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE="docker-compose"
else
  die "docker compose is not available. Install the Compose v2 plugin."
fi
docker info >/dev/null 2>&1 || die "cannot talk to the Docker daemon. Is it running, and is your user in the 'docker' group?"
ok "docker + compose available"

# Ports 80 and 443 must be free for Caddy AND reachable from the internet: ACME
# HTTP-01 dials back on 80. A busy port here is the most common cause of a
# deploy that hangs at "obtaining certificate".
for _port in 80 443; do
  if command -v ss >/dev/null 2>&1; then
    _busy="$(ss -ltn "sport = :$_port" 2>/dev/null | tail -n +2)"
  elif command -v lsof >/dev/null 2>&1; then
    _busy="$(lsof -nP -iTCP:"$_port" -sTCP:LISTEN 2>/dev/null || true)"
  else
    _busy=""
  fi
  # Our own Caddy from a previous run holding the port is expected, not a clash.
  _ours="$( (cd "$ROOT" && $COMPOSE ps 2>/dev/null) | grep -c caddy || true)"
  if [ -n "$_busy" ] && [ "$_ours" = "0" ]; then
    warn "something is already listening on port $_port:"
    printf '%s%s%s\n' "$dim" "$_busy" "$rst" >&2
    warn "Caddy needs both 80 and 443. Stop the other service (a system nginx/apache is the usual culprit) first."
    confirm "Continue anyway?" || exit 1
  fi
done
ok "ports 80/443 checked"

# DNS. Getting this wrong wastes a Let's Encrypt failure, and repeated failures
# are rate-limited, so it is worth one lookup up front.
if [ "$SKIP_DNS" -eq 1 ]; then
  warn "skipping the DNS check (--skip-dns-check)"
else
  _resolved=""
  if command -v dig >/dev/null 2>&1; then
    _resolved="$(dig +short A "$DOMAIN" 2>/dev/null | grep -E '^[0-9.]+$' | head -1)"
    [ -n "$_resolved" ] || _resolved="$(dig +short AAAA "$DOMAIN" 2>/dev/null | head -1)"
  elif command -v host >/dev/null 2>&1; then
    _resolved="$(host -t A "$DOMAIN" 2>/dev/null | awk '/has address/ {print $NF; exit}')"
  elif command -v getent >/dev/null 2>&1; then
    _resolved="$(getent hosts "$DOMAIN" 2>/dev/null | awk '{print $1; exit}')"
  fi

  if [ -z "$_resolved" ]; then
    warn "$DOMAIN does not resolve yet."
    warn "Add an A record pointing at this server's public IP and wait for it to propagate."
    warn "Bringing TLS up before that will fail ACME validation."
    confirm "Continue anyway?" || exit 1
  else
    ok "$DOMAIN resolves to $_resolved"
    # Best-effort comparison against our own public IP. Purely advisory: this
    # machine may legitimately be behind a NAT, a load balancer or a CDN.
    _public_ip=""
    for _svc in https://api.ipify.org https://ifconfig.me/ip; do
      _public_ip="$(curl -fsS --max-time 5 "$_svc" 2>/dev/null | tr -d '[:space:]' || true)"
      [ -n "$_public_ip" ] && break
    done
    if [ -n "$_public_ip" ] && [ "$_public_ip" != "$_resolved" ]; then
      warn "$DOMAIN resolves to $_resolved but this server's public IP looks like $_public_ip."
      warn "If you are behind a NAT, load balancer or CDN this is fine. Otherwise ACME will fail."
      confirm "Continue anyway?" || exit 1
    fi
  fi
fi

# --- 2. .env ------------------------------------------------------------------
if [ ! -f "$ENV_FILE" ]; then
  say "No .env yet — generating one with fresh secrets…"
  sh "$SCRIPT_DIR/gen-env.sh" >/dev/null
  ok "created $ENV_FILE"
else
  ok "using existing $ENV_FILE (secrets left untouched)"
fi

# set_var NAME VALUE — replace the NAME= line, or APPEND it when absent.
#
# The append branch is not optional. gen-env.sh's equivalent is a bare `sed`
# substitution, which silently does nothing when the key is missing — and an
# .env created by an older release has none of the domain keys below, so a
# substitute-only version of this function would report success while writing
# nothing at all.
#
# The anchor is `^NAME=` with NO tolerance for a leading `#`. That is deliberate:
# .env.example documents several of these keys with an indented example line
# inside a comment block (`#   CORS_ORIGINS=https://writ.example.com`). A regex
# that allowed leading `#` would rewrite the DOCUMENTATION as well as the real
# setting, leaving two live assignments and a mangled comment.
set_var() {
  _name="$1"; _value="$2"
  if grep -qE "^${_name}=" "$ENV_FILE"; then
    sed -i.bak "s|^${_name}=.*|${_name}=${_value}|" "$ENV_FILE"
    rm -f "$ENV_FILE.bak"
  else
    printf '%s=%s\n' "$_name" "$_value" >> "$ENV_FILE"
  fi
}

say "Writing domain settings into .env…"
set_var ENVIRONMENT           production
set_var WRIT_DOMAIN           "$DOMAIN"
set_var WRIT_ACME_EMAIL       "$ACME_EMAIL"
# The public URL is https because Caddy terminates TLS for this hostname. The
# coordinator derives its Host allowlist from this, so no ALLOWED_HOSTS needed.
set_var WRIT_PUBLIC_URL       "https://$DOMAIN"
set_var CORS_ORIGINS          "https://$DOMAIN"
# Caddy sits on the compose network, NOT on loopback, so the default
# FORWARDED_ALLOW_IPS=127.0.0.1 would not trust it — every client would then
# share one rate-limit bucket and every audit log would record Caddy's IP.
# 172.16.0.0/12 covers Docker's default bridge pool. Safe because the
# coordinator itself is published only on 127.0.0.1:8000.
set_var FORWARDED_ALLOW_IPS   "127.0.0.1,172.16.0.0/12"
chmod 600 "$ENV_FILE"
ok "public URL https://$DOMAIN"

# WRIT_ACME_CA is always written as a CONCRETE directory URL, never left blank.
# Caddy's `{$VAR:default}` substitution only falls back when the variable is
# unset, and compose passes a blank .env value through as set-but-empty — which
# would leave `acme_ca` with no argument and fail the Caddyfile parse.
CURL_INSECURE=""
if [ "$STAGING" -eq 1 ]; then
  set_var WRIT_ACME_CA "https://acme-staging-v02.api.letsencrypt.org/directory"
  # Staging certs chain to an untrusted root, so the verification curl below
  # must skip validation or it would report a working deploy as broken.
  CURL_INSECURE="-k"
  warn "using the Let's Encrypt STAGING CA — browsers will show an untrusted certificate."
  warn "Re-run without --staging once the deploy works to get a real one."
else
  # Overwrite any staging pin left behind by an earlier debugging run, otherwise
  # the "real" deploy keeps quietly issuing untrusted certificates.
  set_var WRIT_ACME_CA "https://acme-v02.api.letsencrypt.org/directory"
fi

# --- 3. Bring it up -----------------------------------------------------------
say "Starting the coordinator and Caddy…"
# --profile tls adds the caddy service; without it compose brings up exactly what
# the local quickstart does.
( cd "$ROOT" && $COMPOSE --profile tls up -d --build )

# --- 4. Verify ----------------------------------------------------------------
say "Waiting for the coordinator to become healthy…"
_healthy=0
_i=0
while [ "$_i" -lt 60 ]; do
  if curl -fsS --max-time 3 http://127.0.0.1:8000/health >/dev/null 2>&1; then
    _healthy=1; break
  fi
  _i=$((_i + 1)); sleep 2
done
if [ "$_healthy" -ne 1 ]; then
  printf '\n'
  warn "the coordinator did not report healthy within 2 minutes. Recent logs:"
  ( cd "$ROOT" && $COMPOSE logs --tail 40 coordinator ) >&2 || true
  die "deploy incomplete — fix the error above and re-run this script."
fi
ok "coordinator healthy"

say "Waiting for the TLS certificate (Let's Encrypt usually takes 10-30s)…"
_served=0
_i=0
while [ "$_i" -lt 60 ]; do
  # Unquoted on purpose: CURL_INSECURE is either empty (no argument) or "-k".
  # shellcheck disable=SC2086
  if curl -fsS --max-time 5 $CURL_INSECURE "https://$DOMAIN/health" >/dev/null 2>&1; then
    _served=1; break
  fi
  _i=$((_i + 1)); sleep 3
done

printf '\n'
if [ "$_served" -eq 1 ]; then
  ok "https://$DOMAIN is live"
  printf '\n'
  printf '%sOpen https://%s and create your account.%s\n' "$bold" "$DOMAIN" "$rst"
  printf '\n'
  printf 'Then, in the app:\n'
  printf '  * Fleet → Connect a new agent — the install one-liner now carries your\n'
  printf '    real https URL, so it works on any machine.\n'
  printf '  * Settings → Network — confirm the public URL and trusted hosts.\n'
  printf '\n'
  printf '%sBack up SECRET_ENCRYPTION_KEY from .env, somewhere other than this server.%s\n' "$bold" "$rst"
  printf '%sA database backup without it cannot decrypt your stored credentials.%s\n' "$dim" "$rst"
else
  warn "https://$DOMAIN is not serving yet."
  warn "The containers are up, so this is almost always DNS or a blocked port 80."
  warn "Check with:"
  printf '  %sdocker compose --profile tls logs -f caddy%s\n' "$dim" "$rst" >&2
  warn "Common causes:"
  warn "  * $DOMAIN does not point at this server yet (DNS still propagating)"
  warn "  * a firewall or security group is blocking inbound 80/443"
  warn "  * port 80 is held by another web server on this host"
  warn "Re-run this script once fixed; nothing here needs undoing first."
  exit 1
fi
