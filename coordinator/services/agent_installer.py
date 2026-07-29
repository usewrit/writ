"""The ONE implementation of "get the writ-agent-fleet binary onto this machine".

Two callers need that, and they used to answer it differently:

  * ``/agent.sh`` — the ``curl … | sh`` installer an operator pastes onto any
    machine. ``main.py`` serves the script this module holds.
  * ``services/local_agent.py`` — "Run one on this machine" in the Fleet connect
    modal, where the coordinator installs and supervises an agent on its OWN
    host (the ``run-local.sh`` case, i.e. most first installs).

The second one used to resolve, download and unpack the release itself, in
Python, and it had drifted from the script in every detail that decides whether
the result runs:

  * it matched RUST TARGET TRIPLES against assets that are named for their
    ``<os>-<arch>`` infix, so it matched nothing and failed naming a string that
    appears nowhere in the release;
  * it looked for an archive member called ``writ-agent``, while the fleet
    archive contains ``writ-agent-fleet``;
  * it extracted that binary ALONE, leaving behind the Playwright driver that
    ships beside it — so an agent that did start could not launch a browser.

Rather than repair a second copy, the local path now runs THIS script in
``--download-only`` mode. One asset-naming rule, one checksum verification, one
extraction, for every way an agent is installed.

The launch contract is part of the same story: ``writ-agent-fleet`` takes its
whole configuration from the ENVIRONMENT (``WRIT_SERVICE_TOKEN``,
``WRIT_COORDINATOR_URL``, ``WRIT_FLEET_ALLOW_INSECURE``) and is run bare. The
``config set`` / ``start --headless`` subcommands belong to the DESKTOP
``writ-agent`` binary and do not exist here.
"""
from __future__ import annotations

import asyncio
import logging
import os
import platform
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


AGENT_BOOTSTRAP = r"""#!/bin/sh
# Writ agent installer.
#   curl -fsSL @@BASE@@/agent.sh | sh -s -- WRIT-XXXX-XXX
#   curl -fsSL @@BASE@@/agent.sh | sh -s -- --download-only
#
# Installs the writ-agent binary for this platform, exchanges your pairing code
# for its credentials, and starts it. Nothing else to configure — the code is
# single-use and expires, and every setting comes from the coordinator.
set -eu

COORDINATOR="@@BASE@@"
REPO="@@REPO@@"
CODE="${1:-}"

# --- Download-only mode ------------------------------------------------------
# The manual/long-lived-token path (Fleet → Connect agent → Other ways to
# connect) needs the binary WITHOUT enrolling it: the operator supplies the
# token themselves. That step used to be a fifteen-line shell blob pasted into
# the modal — a `uname` case statement, a GitHub releases API call piped through
# grep/cut, and an untar — which is both unreadable and a second copy of the
# resolution logic below that could drift from it. Same script, one flag.
DOWNLOAD_ONLY=0
case "$CODE" in
  --download-only|--install-only) DOWNLOAD_ONLY=1; CODE="" ;;
esac

die() { printf '\033[1;31merror\033[0m %s\n' "$*" >&2; exit 1; }
say() { printf '\033[1;34m::\033[0m %s\n' "$*"; }

[ -n "$CODE" ] || [ "$DOWNLOAD_ONLY" = 1 ] \
  || die "Usage: curl -fsSL $COORDINATOR/agent.sh | sh -s -- <pairing-code>"
command -v curl >/dev/null 2>&1 || die "curl is required."

# --- 1. Redeem the pairing code ---------------------------------------------
# Done FIRST: a bad or expired code should fail in a second, before downloading
# tens of megabytes. Skipped in download-only mode — there is no code to redeem.
TOKEN=""
DOC_URL=""
DOC_SECRET=""
if [ "$DOWNLOAD_ONLY" = 0 ]; then
  say "Redeeming pairing code..."
  RESP="$(curl -fsS -X POST "$COORDINATOR/api/fleet/pair-code/exchange" \
    -H 'Content-Type: application/json' \
    -d "{\"code\":\"$CODE\"}" 2>/dev/null)" \
    || die "That pairing code is invalid, already used, or expired. Generate a new one in Fleet."

  # Extract one JSON string field without needing jq (which is not everywhere).
  jsonstr() {
    printf '%s' "$RESP" | sed -n "s/.*\"$1\"[[:space:]]*:[[:space:]]*\"\([^\"]*\)\".*/\1/p"
  }
  TOKEN="$(jsonstr token)"
  [ -n "$TOKEN" ] || die "The coordinator did not return a token. Check its logs."
  DOC_URL="$(jsonstr DOC_EXTRACT_URL)"
  DOC_SECRET="$(jsonstr DOC_EXTRACT_SECRET)"
fi

# --- 2. Resolve this platform's release asset -------------------------------
# These are the RELEASE ASSET INFIXES (os-arch), not Rust target triples. The
# assets are named `writ-agent-fleet-<os>-<arch>.tar.gz`; matching on a triple
# like `aarch64-apple-darwin` finds nothing and fails naming a string that
# appears nowhere in the release.
case "$(uname -s)-$(uname -m)" in
  Darwin-arm64)              TARGET=macos-arm64 ;;
  Darwin-x86_64)             TARGET=macos-x86_64 ;;
  Linux-x86_64)              TARGET=linux-x86_64 ;;
  Linux-aarch64|Linux-arm64) TARGET=linux-aarch64 ;;
  *) die "No prebuilt agent for $(uname -sm). Build from source: https://github.com/$REPO" ;;
esac

WRIT_DIR="${WRIT_HOME:-$HOME/.writ}"
mkdir -p "$WRIT_DIR"
BIN="$WRIT_DIR/writ-agent-fleet"

if [ -x "$BIN" ] && [ "${WRIT_FORCE_DOWNLOAD:-0}" != "1" ]; then
  say "Using the agent already at $BIN"
else
  say "Finding the $TARGET build..."
  URLS="$(curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" \
    | grep -o "\"browser_download_url\"[[:space:]]*:[[:space:]]*\"[^\"]*\"" \
    | cut -d'"' -f4)"
  # Every archive has a `.sha256` sibling whose name also contains the infix, so
  # filter those out before picking — otherwise the checksum file can win.
  ASSET="$(printf '%s\n' "$URLS" | grep -- "-${TARGET}\." | grep -v '\.sha256$' | head -1)"
  [ -n "$ASSET" ] || die "No published release asset for $TARGET yet. Build from source: https://github.com/$REPO"
  SUMS="$(printf '%s\n' "$URLS" | grep -- "-${TARGET}\." | grep '\.sha256$' | head -1)"

  # The URL was read out of a remote API response and we are about to run what it
  # points at, so refuse anything off GitHub's own release hosts. Keep in step
  # with ALLOWED_ASSET_HOSTS in this module.
  case "$ASSET" in
    https://github.com/*|https://objects.githubusercontent.com/*|https://release-assets.githubusercontent.com/*) ;;
    *) die "Refusing to download the agent from an unexpected host: $ASSET" ;;
  esac

  say "Downloading $(basename "$ASSET")..."
  TMP="$(mktemp -d)"
  trap 'rm -rf "$TMP"' EXIT
  curl -fL# "$ASSET" -o "$TMP/asset" || die "Download failed."

  # Verify the checksum when the release publishes one. This is a
  # `curl | sh` installer fetching a 60 MB binary it is about to run, so a
  # corrupted or substituted download should stop here rather than later.
  if [ -n "$SUMS" ]; then
    EXPECTED="$(curl -fsSL "$SUMS" 2>/dev/null | awk '{print $1}' | head -1)"
    if [ -n "$EXPECTED" ]; then
      if command -v sha256sum >/dev/null 2>&1; then
        ACTUAL="$(sha256sum "$TMP/asset" | awk '{print $1}')"
      elif command -v shasum >/dev/null 2>&1; then
        ACTUAL="$(shasum -a 256 "$TMP/asset" | awk '{print $1}')"
      fi
      if [ -n "${ACTUAL:-}" ] && [ "$ACTUAL" != "$EXPECTED" ]; then
        die "Checksum mismatch for $(basename "$ASSET").
     expected $EXPECTED
     got      $ACTUAL
   Refusing to run it. Try again, or build from source."
      fi
      [ -n "${ACTUAL:-}" ] && say "Checksum verified."
    fi
  fi

  # Releases ship archives (the binary needs its Playwright driver alongside it),
  # but tolerate a bare binary so an older or hand-cut release still installs.
  case "$ASSET" in
    *.tar.gz|*.tgz) tar -xzf "$TMP/asset" -C "$TMP" ;;
    *.zip)          command -v unzip >/dev/null 2>&1 || die "unzip is required for this asset."
                    unzip -q "$TMP/asset" -d "$TMP" ;;
    *)              mv "$TMP/asset" "$TMP/writ-agent-fleet" ;;
  esac

  FOUND="$(find "$TMP" -type f -name 'writ-agent-fleet*' ! -name '*.d' | head -1)"
  [ -n "$FOUND" ] || die "The release archive contained no writ-agent-fleet binary."
  # Copy the whole payload directory: the Playwright driver ships beside the
  # binary and the agent cannot launch a browser without it.
  PAYLOAD="$(dirname "$FOUND")"
  [ "$PAYLOAD" = "$TMP" ] || cp -R "$PAYLOAD"/. "$WRIT_DIR"/
  [ -f "$BIN" ] || cp "$FOUND" "$BIN"
  chmod +x "$BIN"
fi

# --- 3. Start it -------------------------------------------------------------
# writ-agent-fleet is configured ENTIRELY BY ENVIRONMENT — it has no `config`
# subcommand (that belongs to the desktop `writ-agent` binary). So the
# coordinator URL travels as WRIT_COORDINATOR_URL; there is no config file to
# write and nothing to go stale.
if [ "$DOWNLOAD_ONLY" = 1 ]; then
  printf '\033[1;32mok\033[0m  Agent installed at %s\n' "$BIN"
  printf '    Start it with the run command shown in Fleet, which carries your token:\n'
  printf '    WRIT_SERVICE_TOKEN=<token> WRIT_COORDINATOR_URL=%s %s\n' "$COORDINATOR" "$BIN"
  exit 0
fi

say "Starting the agent..."
ALLOW_INSECURE=""
case "$COORDINATOR" in
  https://*) ;;
  http://localhost*|http://127.0.0.1*|"http://[::1]"*) ;;
  # The agent refuses to send its bearer over plaintext to a non-loopback host
  # unless this is set. Only reached when the operator chose a plaintext URL.
  http://*) ALLOW_INSECURE=1 ;;
esac

WRIT_SERVICE_TOKEN="$TOKEN" \
WRIT_COORDINATOR_URL="$COORDINATOR" \
WRIT_FLEET_ALLOW_INSECURE="$ALLOW_INSECURE" \
DOC_EXTRACT_URL="$DOC_URL" \
DOC_EXTRACT_SECRET="$DOC_SECRET" \
WRIT_HOME="$WRIT_DIR" \
nohup "$BIN" >"$WRIT_DIR/agent.log" 2>&1 &

sleep 3
if kill -0 $! 2>/dev/null; then
  printf '\033[1;32mok\033[0m  Agent running (pid %s). Logs: %s\n' "$!" "$WRIT_DIR/agent.log"
  printf '    It should appear in Fleet at %s within a few seconds.\n' "$COORDINATOR"
else
  die "The agent exited immediately. Last lines of $WRIT_DIR/agent.log:
$(tail -20 "$WRIT_DIR/agent.log" 2>/dev/null)"
fi
"""

# Hosts a release asset may be fetched from. The download URL is read out of a
# remote API response and we are about to EXECUTE what it points at, so anything
# off GitHub's own release hosts is refused. Enforced inside the script (see its
# "unexpected host" guard) so both callers get the check from one place; kept
# here as the reviewable list, pinned against the script by the tests.
ALLOWED_ASSET_HOSTS = (
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
)

# What the script installs the binary as, inside ``$WRIT_HOME``. The fleet agent
# is NOT called ``writ-agent`` — that is the desktop binary, and looking for that
# name inside a fleet archive finds nothing.
INSTALLED_BASENAME = "writ-agent-fleet"

# RELEASE ASSET INFIXES, keyed by ``(platform.system(), platform.machine())``.
#
# These are the same ``<os>-<arch>`` strings the script's own ``uname`` case
# block resolves — deliberately NOT Rust target triples, which appear nowhere in
# the release. The map exists so Python can answer "does this host have a
# published build at all?" BEFORE anything is downloaded (see
# ``local_agent.preflight``); the actual resolution stays in the script. Its only
# job is to agree with that case block, which the tests pin branch by branch in
# both directions.
#
# Windows is deliberately absent. The release does publish a Windows asset, but
# this script is POSIX sh and the local path runs it, so one-click install cannot
# happen there at all. The connect modal's PowerShell snippet is that platform's
# route, and ``preflight`` says so instead of failing halfway through.
PLATFORM_TARGETS = {
    ("Darwin", "arm64"): "macos-arm64",
    ("Darwin", "x86_64"): "macos-x86_64",
    ("Linux", "x86_64"): "linux-x86_64",
    ("Linux", "aarch64"): "linux-aarch64",
    ("Linux", "arm64"): "linux-aarch64",
}

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def host_target() -> Optional[str]:
    """This machine's release-asset infix, or ``None`` if we publish no build."""
    return PLATFORM_TARGETS.get((platform.system(), platform.machine()))


def render(base: str, repo: str) -> str:
    """The script with its per-deployment placeholders filled in.

    ``@@BASE@@`` / ``@@REPO@@`` are substituted per request rather than baked in,
    so a rotated coordinator URL or a forked agent repo takes effect on the very
    next fetch.
    """
    return AGENT_BOOTSTRAP.replace("@@BASE@@", base).replace("@@REPO@@", repo)


class InstallerError(RuntimeError):
    """The installer failed, carrying its own message for the operator."""


def _plain(output: str) -> str:
    """The script's output with its ANSI colouring removed."""
    return _ANSI.sub("", output or "").strip()


def _failure_message(output: str) -> str:
    """The reason the installer gave, preferred over an exit code.

    ``die()`` prints ``error <message>``, which is a sentence written for the
    operator ("No published release asset for macos-arm64 yet…"). Surfacing that
    is the difference between a modal that says what to do next and one that
    says a number.
    """
    text = _plain(output)
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if stripped.lower().startswith("error"):
            return stripped[len("error"):].strip(" :") or stripped
    return text[-400:] or "the installer produced no output"


async def download_only(
    dest: Path,
    base: str,
    repo: str,
    timeout: float = 900.0,
) -> dict:
    """Install the fleet agent into ``dest``, without enrolling or starting it.

    Runs the very script served at ``/agent.sh``, fed on stdin, with the flag the
    connect modal's own one-liner uses — so the coordinator's local install and
    an operator's copy-paste install are the same code path, down to the checksum
    verification and the whole-payload extraction.

    ``dest`` becomes the script's ``$WRIT_HOME``, which is both where it installs
    and where the agent later keeps its state; pointing it at the local-agent
    directory keeps everything this feature creates inside one removable folder.

    Returns ``{"asset", "reused", "binary"}`` plus ``"checksum_verified"`` ONLY
    when something was actually downloaded. A reused binary was verified when it
    was fetched, not now, and reporting ``False`` for it would cry wolf on every
    restart — while reporting ``True`` would be a claim about a check that did
    not run.
    """
    if not dest.exists():
        dest.mkdir(parents=True, exist_ok=True)

    script = render(base, repo)
    try:
        proc = await asyncio.create_subprocess_exec(
            "sh", "-s", "--", "--download-only",
            cwd=str(dest),
            # WRIT_HOME decides where the script installs; everything else the
            # script needs it discovers itself.
            env={**os.environ, "WRIT_HOME": str(dest)},
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except (FileNotFoundError, PermissionError) as e:
        raise InstallerError(f"could not run a POSIX shell to install the agent: {e}")

    try:
        out_bytes, _ = await asyncio.wait_for(proc.communicate(script.encode()), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()  # reap it; a killed child left unwaited becomes a zombie
        raise InstallerError(
            f"the agent download did not finish within {int(timeout)}s — "
            "check this host's connectivity to GitHub"
        )

    output = (out_bytes or b"").decode("utf-8", "replace")
    if proc.returncode != 0:
        raise InstallerError(_failure_message(output))

    binary = dest / INSTALLED_BASENAME
    if not binary.exists():
        raise InstallerError(
            f"the installer reported success but left no {INSTALLED_BASENAME} in {dest}"
        )

    plain = _plain(output)
    downloaded = re.search(r"Downloading (\S+?)\.\.\.", plain)
    info: dict = {
        "asset": downloaded.group(1) if downloaded else None,
        "reused": downloaded is None,
        "binary": str(binary),
    }
    if downloaded:
        info["checksum_verified"] = "Checksum verified." in plain
    logger.info(
        "fleet agent %s at %s", "reused" if info["reused"] else f"installed ({info['asset']})", binary
    )
    return info
