#!/usr/bin/env bash
# =============================================================================
# release-e2e.sh — the release gate: prove the WHOLE promise, across repos
# =============================================================================
#
# Every component here has unit tests. None of them prove the sentence the
# README opens with, which is what a new user actually buys:
#
#   docker compose up  →  create the owner  →  connect a RELEASED agent  →
#   record a workflow  →  replay it  →  call it over REST and over MCP
#
# That chain crosses three repositories (usewrit/writ, usewrit/writ-agent, and
# the published release assets), four processes and two protocols. Every part of
# it has shipped broken at least once while its own test suite was green:
#
#   * the installer resolved release assets by Rust target triple against a
#     release named <os>-<arch>, so it downloaded nothing and named a file that
#     does not exist;
#   * relocated agent binaries shipped without the Playwright driver beside
#     them, so the agent started and could never open a browser;
#   * the coordinator handed agents a DOC_EXTRACT_URL nothing served, and the
#     agent's response to an unreachable extractor is to silently skip content.
#
# None of those is visible to a unit test, because each one lives in the seam
# BETWEEN two components. This script is the only thing that walks the seam.
#
# It is deliberately a plain shell script rather than CI-only YAML: a
# contributor can run it on a laptop, and the failure they report is the failure
# CI saw. `.github/workflows/release-e2e.yml` just calls it.
#
# Usage:
#   scripts/release-e2e.sh                    # full run, tears down after
#   scripts/release-e2e.sh --keep             # leave the stack up to poke at
#   scripts/release-e2e.sh --agent-repo o/r   # test against a fork's release
#
# Exit code is the gate: 0 = the promise holds.
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." >/dev/null 2>&1 && pwd)"

# Ports are overridable because 8000 is the most contended port on a developer
# machine, and a gate that can only run when nothing else is up is a gate people
# stop running. CI always has them free, so the defaults are the documented ones.
HOST_PORT="${WRIT_HOST_PORT:-8000}"
DOC_PORT="${WRIT_DOC_EXTRACT_HOST_PORT:-8092}"
BASE="http://localhost:${HOST_PORT}"

# The page the recorded workflow works against MUST be public.
#
# The obvious design — serve a fixture on 127.0.0.1 and point the agent at it —
# cannot work, and finding that out is worth writing down: the agent refuses to
# browse loopback and RFC1918 addresses unconditionally, with no environment
# opt-out, and normalises IPv4-mapped IPv6 first so the block cannot be smuggled
# past. That is deliberate and correct for something that drives a browser on
# your machine, and it is not going to be relaxed for a test:
#
#   WARN writ_agent::security::url_guard: SSRF blocked: hostname is blocked
#
# So the default is example.com — IANA-reserved, unchanged for well over a
# decade, and about the most stable structured markup on the public web. Override
# it to test against your own page; assert on something that page actually
# contains by setting FIXTURE_MATCH too.
FIXTURE_URL="${FIXTURE_URL:-https://example.com/}"
FIXTURE_MATCH="${FIXTURE_MATCH:-Example Domain}"
AGENT_REPO="${WRIT_AGENT_REPO:-usewrit/writ-agent}"
# The agent gets its OWN data home, for two independent reasons.
#
# 1. A single WRIT_HOME is exclusively locked by one live process. On a machine
#    where the operator already runs the desktop daemon or a fleet worker, the
#    gate's agent would refuse to start with "another Writ daemon is already
#    running" — a collision with the developer's real installation, reported as
#    if the release were broken.
# 2. The installer reuses a binary that is already there. Pointed at a shared
#    home it would print "using the agent already at …" and skip the download
#    entirely — so the stage whose entire purpose is to exercise the published
#    release assets and their checksums would silently test a cached file.
AGENT_HOME="${AGENT_HOME:-${TMPDIR:-/tmp}/writ-release-gate-home}"
OWNER_EMAIL="release-gate@localhost"
OWNER_PASS="ReleaseGate!2026x"
KEEP=0

while [ $# -gt 0 ]; do
  case "$1" in
    --keep)        KEEP=1; shift;;
    --agent-repo)  AGENT_REPO="${2:?}"; shift 2;;
    -h|--help)     sed -n '2,40p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0;;
    *) echo "unknown argument: $1" >&2; exit 2;;
  esac
done

STAGE=0
stage() { STAGE=$((STAGE + 1)); printf '\n\033[1;34m[%d/9]\033[0m \033[1m%s\033[0m\n' "$STAGE" "$*"; }
ok()    { printf '  \033[1;32m✓\033[0m %s\n' "$*"; }
info()  { printf '  \033[2m%s\033[0m\n' "$*"; }
die()   { printf '\n\033[1;31m[FAIL]\033[0m %s\n' "$*" >&2; exit 1; }

# --- JSON without a jq dependency -------------------------------------------
# python3 is already a hard requirement (it is what the coordinator runs on), so
# leaning on it costs nothing, while jq is absent from a stock macOS. `jget`
# returns empty rather than raising, so callers test for emptiness themselves
# and report a domain error instead of a stack trace.
jget() {
  python3 -c '
import json,sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for k in sys.argv[1].split("."):
    if isinstance(d, list):
        try: d = d[int(k)]
        except Exception: sys.exit(0)
    elif isinstance(d, dict):
        d = d.get(k)
    else:
        sys.exit(0)
    if d is None: sys.exit(0)
print(d if not isinstance(d, (dict, list)) else json.dumps(d))
' "$1"
}

# --- teardown ----------------------------------------------------------------
# A trap, not a tidy exit path: the whole point is that a FAILING run also stops
# the agent and frees port 8000, otherwise the next run fails for the wrong
# reason and the real defect gets misdiagnosed as flakiness.
cleanup() {
  local rc=$?
  # Put the operator's .env back. This script has to rewrite the URL and ports to
  # configure the stack, but that file is theirs — leaving it pointing at a
  # throwaway port would break the next plain `docker compose up` they run, and
  # they would have no reason to connect that to having run a test.
  #
  # Every step here is `|| true`. `set -e` applies inside a trap handler too, so
  # one failing step used to abandon the rest — a failed .env restore left the
  # stack, the agent and the fixture all running, and the next run then failed in
  # preflight for a reason unrelated to whatever it was testing.
  if [ -n "${ENV_BACKUP:-}" ] && [ -f "$ENV_BACKUP" ]; then
    mv -f "$ENV_BACKUP" "$ROOT/.env" 2>/dev/null \
      || printf '\033[1;33m[warn]\033[0m could not restore .env — your copy is at %s\n' "$ENV_BACKUP"
  fi
  [ -n "${FIXTURE_PID:-}" ] && kill "$FIXTURE_PID" 2>/dev/null || true
  # Match on the gate's OWN home, never on the binary name: `pkill -f
  # writ-agent` would also kill the operator's desktop daemon, a fleet worker
  # they are running deliberately, and this script's own command line.
  pkill -f "$AGENT_HOME/writ-agent-fleet" 2>/dev/null || true
  [ "$KEEP" -eq 1 ] || rm -rf "$AGENT_HOME"
  if [ "$KEEP" -eq 1 ]; then
    printf '\n\033[1;33m[keep]\033[0m stack left running at %s (docker compose down to stop)\n' "$BASE"
  else
    ( cd "$ROOT" && docker compose down -v >/dev/null 2>&1 ) || true
  fi
  exit $rc
}
trap cleanup EXIT

# =============================================================================
stage "Preflight"
# =============================================================================
for tool in docker curl python3; do
  command -v "$tool" >/dev/null || die "$tool is required and not on PATH."
done
docker info >/dev/null 2>&1 || die "Docker is not running."
docker compose version >/dev/null 2>&1 || die "This needs Docker Compose v2 (docker compose, not docker-compose)."
ok "docker, curl, python3"

# The gate is worthless if it silently tests a stale image or a previous run's
# database — a workflow left over from last time would make stage 8 pass with no
# agent involved at all.
( cd "$ROOT" && docker compose down -v >/dev/null 2>&1 ) || true
pkill -f "$AGENT_HOME/writ-agent-fleet" 2>/dev/null || true
rm -rf "$AGENT_HOME"
ok "no leftover stack or agent from a previous run"

# Check the ports BEFORE the build. Compose reports a clash only after several
# minutes of image work, as "failed programming external connectivity", which
# reads like a Docker networking fault rather than "something else is on 8000".
port_busy() {
  if command -v lsof >/dev/null; then
    lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1
  else
    # No lsof (most CI images): a successful connect means someone is listening.
    python3 -c '
import socket,sys
s = socket.socket(); s.settimeout(0.4)
sys.exit(0 if s.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)' "$1"
  fi
}
for p in "$HOST_PORT:WRIT_HOST_PORT" "$DOC_PORT:WRIT_DOC_EXTRACT_HOST_PORT"; do
  pnum="${p%%:*}"; pvar="${p##*:}"
  port_busy "$pnum" && die "port $pnum is already in use, so this run cannot bind it.
       Free it, or re-run with $pvar=<other port>."
done
ok "ports $HOST_PORT and $DOC_PORT are free"

# =============================================================================
stage "Bring the stack up (docker compose up --build)"
# =============================================================================
cd "$ROOT"
[ -f .env ] || { ./scripts/gen-env.sh >/dev/null 2>&1 || die "gen-env.sh failed"; }
ENV_BACKUP="$(mktemp)"
cp .env "$ENV_BACKUP"

# The agent runs on the HOST and dials this URL back. It must be the published
# port, not the container-internal one — an agent handed http://coordinator:8000
# resolves nothing outside the compose network and reports no error the operator
# can see, it simply never appears in the fleet.
python3 - "$BASE" "$HOST_PORT" "$DOC_PORT" <<'PY'
import pathlib, re, sys
base, host_port, doc_port = sys.argv[1:4]
p = pathlib.Path(".env"); s = p.read_text()
for key, val in (("WRIT_PUBLIC_URL", base),
                 ("WRIT_HOST_PORT", host_port),
                 ("WRIT_DOC_EXTRACT_HOST_PORT", doc_port)):
    line = f"{key}={val}"
    s, n = re.subn(rf"^{key}=.*$", line, s, flags=re.M)
    if not n:
        s = s.rstrip("\n") + "\n" + line + "\n"
p.write_text(s)
PY
ok "WRIT_PUBLIC_URL=$BASE, host ports $HOST_PORT/$DOC_PORT"

info "building images (first run pulls the Playwright base — several minutes)"
docker compose up -d --build >/dev/null 2>&1 || die "docker compose up failed. Run it by hand to see the build output."

for i in $(seq 1 90); do
  if curl -fsS --max-time 3 "$BASE/health" >/dev/null 2>&1; then break; fi
  [ "$i" = 90 ] && { docker compose logs --tail 40 coordinator; die "coordinator never became healthy."; }
  sleep 2
done
HEALTH="$(curl -fsS "$BASE/health")"
ok "coordinator healthy ($(printf '%s' "$HEALTH" | jget status))"

# doc-extract is part of the bundle and starts with everything else. Asserting it
# here is the difference between "PDFs work" and "PDFs are silently skipped",
# which is exactly how this lane shipped dark once already.
curl -fsS --max-time 5 "http://localhost:${DOC_PORT}/health" >/dev/null 2>&1 \
  && ok "doc-extract reachable on :$DOC_PORT" \
  || die "doc-extract is not answering — the crawl document lane would silently skip every PDF."

# =============================================================================
stage "Claim the instance (create the owner)"
# =============================================================================
NEEDS="$(curl -fsS "$BASE/api/auth/setup-status" | jget needs_setup)"
[ "$NEEDS" = "True" ] || [ "$NEEDS" = "true" ] \
  || die "setup-status says needs_setup=$NEEDS on a fresh volume — first-run onboarding would not appear."
ok "fresh instance reports needs_setup=true"

REG="$(curl -fsS -X POST "$BASE/api/auth/register" -H 'content-type: application/json' \
  -d "{\"email\":\"$OWNER_EMAIL\",\"password\":\"$OWNER_PASS\",\"name\":\"Release Gate\"}")"
TOKEN="$(printf '%s' "$REG" | jget access_token)"
[ -n "$TOKEN" ] || die "register returned no access_token: $REG"
ok "owner created and logged in"

# Registration is a one-time bootstrap. If this ever stops being true, a public
# coordinator is one POST away from a second platform admin.
SECOND="$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/api/auth/register" \
  -H 'content-type: application/json' \
  -d '{"email":"intruder@localhost","password":"AlsoStrong!2026x","name":"No"}')"
[ "$SECOND" = "403" ] || die "a SECOND registration returned $SECOND, expected 403 — registration is not closed."
ok "second registration refused (403)"

# =============================================================================
stage "Mint an API key (the credential a real caller uses)"
# =============================================================================
KEYJSON="$(curl -fsS -X POST "$BASE/api/auth/api-keys" \
  -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"label":"release-gate","preset":"full"}')"
APIKEY="$(printf '%s' "$KEYJSON" | jget key)"
[ -n "$APIKEY" ] || APIKEY="$(printf '%s' "$KEYJSON" | jget api_key)"
[ -n "$APIKEY" ] || die "no API key in the create response: $KEYJSON"
ok "API key minted (${APIKEY:0:6}…)"

# =============================================================================
stage "Check the target page is reachable"
# =============================================================================
# Checked from the runner FIRST, so that a later extraction failure means the
# agent's browser, not the network. Without this the two are indistinguishable
# and a blocked egress reads as a product defect.
curl -fsS --max-time 10 "$FIXTURE_URL" >/dev/null 2>&1 \
  || die "cannot reach $FIXTURE_URL from this machine. The recorded workflow needs a
       public page (the agent blocks loopback and private addresses by design).
       Set FIXTURE_URL and FIXTURE_MATCH to a page you can reach."
ok "target page reachable: $FIXTURE_URL"

# =============================================================================
stage "Install and connect the RELEASED agent (cross-repo)"
# =============================================================================
# THE point of this gate. Everything above runs from the working tree; this
# stage reaches out to the published release of a DIFFERENT repository and runs
# the one-liner the UI prints, byte for byte. It is the only check that the
# asset names, the checksums, the archive layout and the token contract still
# agree with each other after either repo changes.
PAIR="$(curl -fsS -X POST "$BASE/api/fleet/pair-code" \
  -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"name":"release-gate-agent"}')"
CODE="$(printf '%s' "$PAIR" | jget code)"
[ -n "$CODE" ] || die "no pairing code minted: $PAIR"
ok "pairing code $CODE"

info "running the printed installer against $AGENT_REPO's published release"
AGENT_LOG="$(mktemp)"
if ! env WRIT_AGENT_REPO="$AGENT_REPO" WRIT_HOME="$AGENT_HOME" \
      sh -c "curl -fsSL '$BASE/agent.sh' | sh -s -- '$CODE'" >"$AGENT_LOG" 2>&1; then
  sed 's/^/    /' "$AGENT_LOG"
  die "the published install one-liner failed. This is what a new user runs first."
fi
# A cached binary would make this stage a no-op. The home is thrown away before
# every run precisely so this cannot pass without a real download.
grep -qi 'already at' "$AGENT_LOG" \
  && die "the installer reused an existing binary instead of downloading — this stage tested nothing.
       AGENT_HOME=$AGENT_HOME was not clean."
grep -qi 'checksum verified' "$AGENT_LOG" \
  && ok "release asset downloaded and its published SHA-256 verified" \
  || die "the installer did not verify a checksum — an unverified binary was about to run."

# `online_count` is reported by the endpoint itself, from the live WS registry.
# Deriving it here from per-agent fields instead means guessing at their names —
# which is exactly how the first version of this check failed against a fleet
# that was working perfectly (it looked for `status`/`connected`; the rows carry
# `id` and `online`).
for i in $(seq 1 60); do
  AGENTS="$(curl -fsS "$BASE/api/fleet/agents" -H "authorization: Bearer $TOKEN" || echo '{}')"
  COUNT="$(printf '%s' "$AGENTS" | jget online_count)"
  [ "${COUNT:-0}" -ge 1 ] 2>/dev/null && break
  [ "$i" = 60 ] && { sed 's/^/    /' "$AGENT_LOG"
    tail -30 "$AGENT_HOME/agent.log" 2>/dev/null | sed 's/^/    agent| /'
    die "the agent never appeared as connected in the fleet (online_count=$COUNT)."; }
  sleep 2
done
ok "agent connected and visible in the fleet (online_count=$COUNT)"

# =============================================================================
stage "Record a workflow over MCP"
# =============================================================================
mcp() {
  local name="$1" args="$2"
  curl -fsS -X POST "$BASE/mcp" \
    -H "authorization: Bearer $APIKEY" -H 'content-type: application/json' \
    --max-time "${MCP_TIMEOUT:-180}" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/call\",\"params\":{\"name\":\"$name\",\"arguments\":$args}}"
}

TOOLS="$(curl -fsS -X POST "$BASE/mcp" -H "authorization: Bearer $APIKEY" \
  -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}')"
NTOOLS="$(printf '%s' "$TOOLS" | python3 -c 'import json,sys
try: print(len(json.load(sys.stdin)["result"]["tools"]))
except Exception: print(0)')"
[ "${NTOOLS:-0}" -gt 0 ] || die "tools/list returned nothing — the MCP surface is dark: $TOOLS"
ok "MCP handshake: $NTOOLS tools advertised"

REC="$(mcp writ_record_start "{\"url\":\"$FIXTURE_URL\"}")"
SESSION="$(printf '%s' "$REC" | python3 -c 'import json,sys,re
try: d=json.load(sys.stdin)
except Exception: raise SystemExit
t="".join(c.get("text","") for c in d.get("result",{}).get("content",[]))
m=re.search(r"[\"'"'"']?session_id[\"'"'"']?\s*[:=]\s*[\"'"'"']?([A-Za-z0-9_.:-]+)", t)
print(m.group(1) if m else "")')"
[ -n "$SESSION" ] || die "writ_record_start opened no session (is a browser reachable on the agent?): $REC"
ok "record session $SESSION opened on the agent's real browser"

# Two actions, doing two different jobs.
#
# `extract_data` is an INSPECTION action: it proves a real page really loaded,
# and records NOTHING. `click` is an INTERACTION: it is what becomes a workflow
# step. A session that only inspected refuses to save — and so does one that only
# re-navigates to the URL it already opened, because the start URL is the
# workflow's entry point rather than a step. So the recorded workflow needs at
# least one genuine interaction, and a click on the page's own link is the
# smallest honest one.
#
# Extraction runs FIRST, while the original page is still loaded — the click
# navigates away.
ACT="$(mcp writ_record_act "{\"session_id\":\"$SESSION\",\"actions\":[{\"action\":\"extract_data\",\"selector\":\"h1\"},{\"action\":\"click\",\"selector\":\"a\"}]}")"
printf '%s' "$ACT" | grep -qF "$FIXTURE_MATCH" \
  || die "extraction did not return \"$FIXTURE_MATCH\" — the agent reached no page: $ACT"
ok "agent extracted \"$FIXTURE_MATCH\" through a live browser"

# Assert the interaction actually became a step BEFORE saving. Without this, a
# recorder that silently records nothing surfaces as "writ_record_save returned
# no workflow_id", which points at the save rather than at the recording.
CTX="$(mcp writ_record_context "{\"session_id\":\"$SESSION\"}")"
NSTEPS="$(printf '%s' "$CTX" | python3 -c 'import json,sys,re
try: d=json.load(sys.stdin)
except Exception: raise SystemExit
t="".join(c.get("text","") for c in d.get("result",{}).get("content",[]))
m=re.search(r"recorded_steps[\"'"'"']?\s*[:=]\s*(\d+)", t)
print(m.group(1) if m else "")')"
[ "${NSTEPS:-0}" -ge 1 ] 2>/dev/null \
  || die "the click recorded no step (recorded_steps=${NSTEPS:-?}); there is nothing to save.
       Session context: $CTX"
ok "interaction recorded as a workflow step (recorded_steps=$NSTEPS)"

SAVE="$(mcp writ_record_save "{\"session_id\":\"$SESSION\",\"name\":\"release-gate-probe\"}")"
WFID="$(printf '%s' "$SAVE" | python3 -c 'import json,sys,re
try: d=json.load(sys.stdin)
except Exception: raise SystemExit
t="".join(c.get("text","") for c in d.get("result",{}).get("content",[]))
m=re.search(r"[\"'"'"']?workflow_id[\"'"'"']?\s*[:=]\s*[\"'"'"']?(\d+)", t)
print(m.group(1) if m else "")')"
[ -n "$WFID" ] || die "writ_record_save returned no workflow_id: $SAVE"
ok "saved as workflow #$WFID"

# =============================================================================
stage "Replay it over REST"
# =============================================================================
# Replay is the product's actual claim: recorded once, then runs with no model
# in the loop. A save that cannot be replayed is a recording, not a workflow.
# `?wait=true` blocks until the run reaches a TERMINAL state and returns it
# inline, which is the difference between "the coordinator accepted the request"
# and "the agent actually replayed it". A failed run answers 200 with
# status:"failed" by design, so the status is what has to be asserted — checking
# only the HTTP code would pass on every failure.
RUN="$(curl -fsS --max-time 200 -X POST "$BASE/api/automation/workflows/$WFID/run?wait=true&timeout=150" \
  -H "authorization: Bearer $APIKEY" -H 'content-type: application/json' -d '{}')"
RSTATUS="$(printf '%s' "$RUN" | jget status)"
case "$RSTATUS" in
  success|succeeded|completed|complete|ok) ok "REST replay ran to completion (status=$RSTATUS)";;
  *) die "REST replay did not succeed (status=${RSTATUS:-<none>}): $RUN";;
esac

# =============================================================================
stage "Call it over MCP"
# =============================================================================
# Same workflow, second protocol. These are separate code paths to the same
# executor, and they have drifted before — the MCP side once saved workflows
# with is_active=False, which made them invisible to exactly this call.
MRUN="$(mcp writ_run_workflow "{\"workflow_id\":$WFID,\"wait\":true}")"
# An MCP tool failure comes back as a 200 with `isError: true` and the message in
# the SAME content block, so matching on hopeful words like "running" would pass
# on "Error: workflow not running". Check the flag.
[ "$(printf '%s' "$MRUN" | jget result.isError)" = "True" ] \
  && die "MCP run of the same workflow returned an error: $MRUN"
printf '%s' "$MRUN" | grep -qiE "success|complete|finished|$FIXTURE_MATCH" \
  || die "MCP run did not report a completed run: $MRUN"
ok "MCP replay of workflow #$WFID ran to completion"

LIST="$(mcp writ_list_workflows '{}')"
printf '%s' "$LIST" | grep -q 'release-gate-probe' \
  || die "the recorded workflow is not listed over MCP — saved inactive or wrongly scoped: $LIST"
ok "workflow is visible and callable over MCP"

# =============================================================================
printf '\n\033[1;32m═══ RELEASE GATE PASSED ═══\033[0m\n'
printf 'compose up → owner → released agent from %s → record → replay → REST → MCP\n\n' "$AGENT_REPO"
