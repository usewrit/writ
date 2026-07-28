"""One-line agent enrolment: pairing codes and the installer script.

The exchange endpoint is UNAUTHENTICATED by necessity — the machine being
enrolled holds no credential yet, so the code itself is the credential. That
makes its properties load-bearing rather than cosmetic, and each one below is a
way the feature could quietly become an open enrolment endpoint:

  * single use  — a code lifted from a shell history or a proxy log is spent
  * short TTL   — a leaked code stops working on its own
  * unguessable — 32^7 ≈ 34 billion, over a rate-limited endpoint
  * indistinguishable failures — wrong, spent and expired all answer the same,
    so the endpoint cannot be used to confirm that a code ever existed

The script served at /agent.sh is checked for the two mistakes that would make
it dangerous or dead: shipping a secret, and being shadowed by the SPA
catch-all so that `curl … | sh` pipes an HTML page into a shell.
"""
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from routers.fleet import (  # noqa: E402
    _PAIR_ALPHABET,
    _PAIR_TTL_SECONDS,
    _format_pair_code,
    _normalise_pair_code,
)


# --- code shape -------------------------------------------------------------

def test_alphabet_excludes_confusable_characters():
    """A code is read off a screen and often dictated, so I/L/O/U are out."""
    for ch in "ILOU":
        assert ch not in _PAIR_ALPHABET, f"{ch} is easily misread or misheard"
    assert len(_PAIR_ALPHABET) == 32


def test_keyspace_is_large_enough_to_be_unguessable():
    # 7 chars over a 32-char alphabet, behind a 10/minute per-IP limit.
    assert len(_PAIR_ALPHABET) ** 7 > 30_000_000_000


def test_format_is_grouped_and_prefixed():
    assert _format_pair_code("ABCD123") == "WRIT-ABCD-123"


def test_ttl_is_short():
    assert 0 < _PAIR_TTL_SECONDS <= 30 * 60


# --- what a human actually types --------------------------------------------

@pytest.mark.parametrize("typed", [
    "WRIT-ABCD-123",
    "writ-abcd-123",
    "WRITABCD123",
    "abcd123",
    "ABCD-123",
    "  WRIT-abcd-123  ",
    "WRIT abcd 123",
])
def test_normalisation_accepts_realistic_input(typed):
    """Case, dashes, spaces and the WRIT prefix are all optional."""
    assert _normalise_pair_code(typed) == "ABCD123"


@pytest.mark.parametrize("junk", ["", None, "-", "WRIT-", "   "])
def test_normalisation_rejects_empty(junk):
    assert _normalise_pair_code(junk) == ""


def test_normalisation_strips_only_a_leading_prefix():
    """A code whose body happens to start with these letters is not mangled."""
    # "WRITE" -> prefix "WRIT" consumed, "E" remains, plus the rest.
    assert _normalise_pair_code("WRIT-WRIT-123") == "WRIT123"


# --- the served script ------------------------------------------------------

def _script() -> str:
    import main

    return main._AGENT_BOOTSTRAP


def test_script_carries_no_secret():
    """It is served unauthenticated, so it must contain nothing worth having."""
    src = _script()
    for marker in ("SECRET_ENCRYPTION_KEY", "JWT_SECRET", "RECORDER_AUTH_SECRET",
                   "API_SECRET_KEY", "eyJ"):
        assert marker not in src, f"{marker} must never appear in a public script"
    # The doc-extract secret arrives from the exchange at run time; the script
    # may only reference the shell variable, never a literal.
    assert 'DOC_SECRET="$(jsonstr DOC_EXTRACT_SECRET)"' in src


def test_script_is_reachable_and_not_shadowed_by_the_spa():
    """`/agent.sh` must be declared before the SPA history fallback.

    If the catch-all wins, curl receives the HTML shell with a 200 and pipes a
    web page into sh — which fails in a baffling way rather than an obvious one.
    """
    import main

    assert "/agent.sh" in main._UI_RESERVED_EXACT
    paths = [getattr(r, "path", "") for r in main.app.routes]
    assert "/agent.sh" in paths
    if "/{full_path:path}" in paths:
        assert paths.index("/agent.sh") < paths.index("/{full_path:path}")


def test_script_configures_the_agent_by_environment():
    """`writ-agent-fleet` has no `config` subcommand — it is env-only.

    An earlier revision wrote `saas.url` with `config set` (which belongs to the
    desktop binary), swallowed the failure with `|| true`, and started an agent
    with no coordinator URL. It died on boot every time.
    """
    src = _script()
    assert 'WRIT_COORDINATOR_URL="$COORDINATOR"' in src
    assert "config set" not in src
    # Verified against writ-agent-fleet.rs — the name is fleet-specific.
    assert "WRIT_FLEET_ALLOW_INSECURE" in src


def test_script_redeems_before_downloading():
    """A bad code should fail in a second, not after tens of megabytes."""
    src = _script()
    assert src.index("pair-code/exchange") < src.index("releases/latest")


def test_script_is_posix_sh():
    """It runs under dash on Debian images, so no bashisms.

    `[[` is matched only as the bash conditional — `[[:space:]]` and friends are
    POSIX character classes inside sed/grep expressions and are perfectly fine.
    """
    import re

    src = _script()
    assert src.startswith("#!/bin/sh")
    assert not re.search(r"\[\[(?!:)", src), "bash [[ ]] conditional"
    for bashism in ("function ", "declare ", "local ", "&>"):
        assert bashism not in src, bashism
    # ANSI-C quoting is `$'...'` at the START of a word. A `$` immediately
    # before a closing quote is a regex anchor — `grep -v '\.sha256$'` — and is
    # perfectly POSIX.
    assert not re.search(r"(^|[\s=(])\$'", src), "bash ANSI-C quoting $'...'"


def test_script_placeholders_are_substituted_not_literal():
    """The template markers must never reach a user's shell."""
    import os

    import main

    base = "https://writ.example.com"
    rendered = main._AGENT_BOOTSTRAP.replace("@@BASE@@", base).replace(
        "@@REPO@@", os.getenv("WRIT_AGENT_REPO") or "usewrit/writ-agent"
    )
    assert "@@" not in rendered
    assert base in rendered


# --- release-asset naming ---------------------------------------------------
#
# These pin the ONE fact that has already broken this feature once: the
# installer must match the release's own `<os>-<arch>` infix, not a Rust target
# triple. `writ-agent-fleet-macos-arm64.tar.gz` contains no
# `aarch64-apple-darwin`, so a triple matches nothing and the installer dies
# naming a string that appears nowhere in the release.
#
# The names below are the assets published by writ-agent v1.0.0.

REAL_ASSETS = [
    "writ-agent-fleet-linux-aarch64.tar.gz",
    "writ-agent-fleet-linux-aarch64.tar.gz.sha256",
    "writ-agent-fleet-linux-x86_64.tar.gz",
    "writ-agent-fleet-linux-x86_64.tar.gz.sha256",
    "writ-agent-fleet-macos-arm64.tar.gz",
    "writ-agent-fleet-macos-arm64.tar.gz.sha256",
    "writ-agent-fleet-macos-x86_64.tar.gz",
    "writ-agent-fleet-macos-x86_64.tar.gz.sha256",
    "writ-agent-fleet-windows-x86_64.zip",
    "writ-agent-fleet-windows-x86_64.zip.sha256",
]


@pytest.mark.parametrize("uname_s,uname_m,expected", [
    ("Darwin", "arm64", "writ-agent-fleet-macos-arm64.tar.gz"),
    ("Darwin", "x86_64", "writ-agent-fleet-macos-x86_64.tar.gz"),
    ("Linux", "x86_64", "writ-agent-fleet-linux-x86_64.tar.gz"),
    ("Linux", "aarch64", "writ-agent-fleet-linux-aarch64.tar.gz"),
    ("Linux", "arm64", "writ-agent-fleet-linux-aarch64.tar.gz"),
])
def test_installer_selects_the_right_asset(uname_s, uname_m, expected):
    """Replay the script's own selection logic against the real asset list."""
    import re

    src = _script()
    case = re.search(r'case "\$\(uname -s\)-\$\(uname -m\)" in(.+?)esac', src, re.S)
    assert case, "platform case block not found"

    target = None
    for line in case.group(1).splitlines():
        m = re.match(r"\s*([^)]+)\)\s*TARGET=(\S+)\s*;;", line.strip())
        if not m:
            continue
        if f"{uname_s}-{uname_m}" in [p.strip() for p in m.group(1).split("|")]:
            target = m.group(2)
            break
    assert target, f"no TARGET branch for {uname_s}-{uname_m}"

    # The script's own filter: infix match, minus the checksum siblings.
    picked = [a for a in REAL_ASSETS if f"-{target}." in a and not a.endswith(".sha256")]
    assert picked == [expected], f"target {target!r} selected {picked}"


def test_installer_does_not_use_rust_target_triples():
    """The regression itself: triples appear nowhere in the release.

    Comment lines are stripped first — the script explains this trap by naming a
    triple, which is the opposite of committing it.
    """
    code = "\n".join(
        l for l in _script().splitlines() if not l.lstrip().startswith("#")
    )
    for triple in ("aarch64-apple-darwin", "x86_64-apple-darwin",
                   "x86_64-unknown-linux-gnu", "aarch64-unknown-linux-gnu",
                   "x86_64-pc-windows-msvc"):
        assert triple not in code, (
            f"{triple} is a Rust target triple; release assets are named "
            "<os>-<arch> and it will match nothing"
        )


def test_installer_excludes_checksum_siblings():
    """A `.sha256` carries the same infix and must never be chosen as the asset."""
    src = _script()
    assert "grep -v '\\.sha256$'" in src or 'grep -v "\\.sha256$"' in src


def test_installer_verifies_the_checksum():
    """It is curl-pipe-sh fetching 60 MB it is about to execute."""
    src = _script()
    assert "sha256sum" in src and "shasum -a 256" in src
    assert "Checksum mismatch" in src


def test_fleet_snippets_use_the_same_naming():
    """The Fleet page's manual commands had the identical triple bug."""
    from routers.fleet import _build_install_commands

    cmds = _build_install_commands()
    for name, body in cmds.items():
        for triple in ("aarch64-apple-darwin", "x86_64-unknown-linux-gnu",
                       "x86_64-pc-windows-msvc"):
            assert triple not in body, f"{name} snippet still uses {triple}"
    assert "macos-arm64" in cmds["unix"]
    assert "windows-x86_64" in cmds["windows"]
