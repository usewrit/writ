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


def test_installer_extracts_the_whole_payload():
    """The binary alone is not a working agent.

    The Playwright driver ships beside it in the archive, and without the driver
    the agent starts, registers, and then cannot launch a browser for anything —
    a fleet member that looks healthy and runs nothing.
    """
    src = _script()
    assert "cp -R" in src, "the payload directory must be copied, not just the binary"


def test_installer_refuses_an_unexpected_download_host():
    """The asset URL is read from a remote API response and then EXECUTED."""
    from services import agent_installer

    src = _script()
    assert "unexpected host" in src
    for host in agent_installer.ALLOWED_ASSET_HOSTS:
        assert f"https://{host}/" in src, f"{host} is allowed in Python but not in the script"


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
    assert "windows-x86_64" in cmds["windows"]


def test_unix_install_snippet_is_one_line_and_reuses_the_installer():
    """The macOS/Linux download step delegates to /agent.sh.

    It used to be a fifteen-line blob — a `uname` case statement, a releases-API
    call piped through grep/cut, an untar — pasted into the connect modal above
    a one-line input. That is unreadable at a glance AND a second copy of the
    resolution logic in the script above, free to drift from it (and from the
    checksum verification it does not have).
    """
    from routers.fleet import _build_install_commands

    unix = _build_install_commands()["unix"]
    assert "\n" not in unix, "the download step must be a single line"
    assert "/agent.sh" in unix
    assert "--download-only" in unix
    assert "api.github.com" not in unix, "asset resolution belongs to the installer"


def test_installer_supports_download_only():
    """…and the flag that one-liner passes must actually exist in the script.

    Download-only skips the redemption (there is no code) and stops before the
    start (the operator supplies the token from the modal), so both guards are
    pinned here — a script that ignored the flag would try to redeem
    `--download-only` as a pairing code.
    """
    src = _script()
    assert "--download-only" in src
    assert 'if [ "$DOWNLOAD_ONLY" = 0 ]; then' in src, "redemption must be skipped"
    assert 'if [ "$DOWNLOAD_ONLY" = 1 ]; then' in src, "it must stop before starting"


# --- "Run one on this machine" ----------------------------------------------
#
# The Fleet connect modal's local path (POST /api/fleet/local-agent) used to
# resolve, download, unpack and launch the agent itself, in Python — and every
# one of those steps had drifted from the installer above, completely:
#
#   * it matched Rust target triples, which appear in no asset name;
#   * it hunted for a member called `writ-agent` inside an archive that contains
#     `writ-agent-fleet`;
#   * it extracted that binary ALONE, dropping the Playwright driver beside it,
#     so an agent that started could never launch a browser;
#   * it launched `writ-agent config set …` then `writ-agent start --headless`,
#     which is the DESKTOP binary's CLI. The fleet binary is env-only.
#
# Acquisition now runs the script above, so what is left to pin is that the local
# path's own platform map agrees with that script's `uname` case block, that no
# second resolver has grown back, and that the launch contract is the fleet
# binary's.

def _local_src() -> str:
    from services import local_agent

    return Path(local_agent.__file__).read_text()


def _local_code() -> str:
    """`local_agent.py` with its comments and docstrings removed.

    The module explains the desktop-CLI trap by naming `config set`, which is the
    opposite of committing it — so the launch-contract assertions have to read
    what runs, not what it says about itself.
    """
    import re

    body = re.sub(r'"""(?:.|\n)*?"""', '""', _local_src())
    return "\n".join(l for l in body.splitlines() if not l.lstrip().startswith("#"))


def _case_targets() -> dict:
    """``(uname -s, uname -m) -> TARGET``, read out of the script's case block."""
    import re

    case = re.search(
        r'case "\$\(uname -s\)-\$\(uname -m\)" in(.+?)esac', _script(), re.S
    )
    assert case, "platform case block not found"
    targets = {}
    for line in case.group(1).splitlines():
        # `*)` is the die branch, not a platform.
        m = re.match(r"([^)*]+)\)\s*TARGET=(\S+)\s*;;", line.strip())
        if not m:
            continue
        for pattern in m.group(1).split("|"):
            system, _, machine = pattern.strip().partition("-")
            targets[(system, machine)] = m.group(2)
    return targets


@pytest.mark.parametrize("uname_s,uname_m,expected", [
    ("Darwin", "arm64", "writ-agent-fleet-macos-arm64.tar.gz"),
    ("Darwin", "x86_64", "writ-agent-fleet-macos-x86_64.tar.gz"),
    ("Linux", "x86_64", "writ-agent-fleet-linux-x86_64.tar.gz"),
    ("Linux", "aarch64", "writ-agent-fleet-linux-aarch64.tar.gz"),
    ("Linux", "arm64", "writ-agent-fleet-linux-aarch64.tar.gz"),
])
def test_local_path_targets_a_real_published_asset(uname_s, uname_m, expected, monkeypatch):
    """The same pin as the installer's, against the local path's own map.

    `aarch64-apple-darwin` selected NOTHING from this list, and the failure named
    that triple — a string the operator could not find in the release either.
    """
    from services import agent_installer

    monkeypatch.setattr(agent_installer.platform, "system", lambda: uname_s)
    monkeypatch.setattr(agent_installer.platform, "machine", lambda: uname_m)

    target = agent_installer.host_target()
    assert target, f"no build claimed for {uname_s} {uname_m}"
    picked = [a for a in REAL_ASSETS if f"-{target}." in a and not a.endswith(".sha256")]
    assert picked == [expected], f"target {target!r} selected {picked}"


def test_local_platform_map_mirrors_the_installers_case_block():
    """One authority. The map only exists to answer preflight before a download.

    Both directions: an entry the script does not have would offer a one-click
    install that dies in the shell, and a missing entry would refuse a platform
    the installer handles fine.
    """
    from services import agent_installer

    assert agent_installer.PLATFORM_TARGETS == _case_targets()


def test_local_path_has_no_second_asset_resolver():
    """Resolution, download and extraction belong to the installer, wholly."""
    src = _local_src()
    for triple in ("aarch64-apple-darwin", "x86_64-apple-darwin",
                   "x86_64-unknown-linux-gnu", "aarch64-unknown-linux-gnu",
                   "x86_64-pc-windows-msvc"):
        assert triple not in src, f"{triple} matches no published asset"
    for owned_elsewhere in ("api.github.com", "releases/latest", "browser_download_url",
                            "tarfile", "zipfile", "hashlib", "httpx"):
        assert owned_elsewhere not in src, (
            f"{owned_elsewhere} is a second copy of what /agent.sh already does"
        )
    assert "agent_installer.download_only" in src


def test_local_path_installs_the_fleet_binary_not_the_desktop_one():
    """`writ-agent` is the DESKTOP binary; the release archive has neither."""
    from services import agent_installer, local_agent

    assert agent_installer.INSTALLED_BASENAME == "writ-agent-fleet"
    assert local_agent._binary_path().name == "writ-agent-fleet"
    for asset in (a for a in REAL_ASSETS if not a.endswith(".sha256")):
        assert asset.startswith("writ-agent-fleet-")
    assert "writ-agent.exe" not in _local_src()


def test_local_path_launches_by_environment():
    """No `config set`, no `start`, no `--headless` — those are the desktop CLI."""
    code = _local_code()
    assert "config set" not in code
    assert '"config"' not in code and "'config'" not in code
    assert "--headless" not in code
    for var in ("WRIT_SERVICE_TOKEN", "WRIT_COORDINATOR_URL", "WRIT_FLEET_ALLOW_INSECURE"):
        assert var in code, f"{var} is how the fleet agent is configured"


@pytest.mark.parametrize("url,needs_optin", [
    ("https://writ.example.com", False),
    ("http://localhost:8000", False),
    ("http://127.0.0.1:8000", False),
    ("http://writ.example.com", True),
])
def test_plaintext_optin_matches_the_printed_command(url, needs_optin):
    """The agent refuses its bearer over plaintext to a non-loopback host.

    Same rule as `_build_connect_commands`, so the one-click and copy-paste paths
    cannot disagree about whether a given coordinator URL needs the opt-in.
    """
    from services import local_agent

    assert local_agent._needs_insecure_optin(url) is needs_optin


def test_local_start_runs_the_bare_binary_with_its_environment(tmp_path, monkeypatch):
    """The launch contract, executed: argv is the binary and nothing else."""
    import asyncio

    from config import settings
    from services import agent_installer, local_agent

    monkeypatch.setenv("WRIT_FILES_DIR", str(tmp_path))
    monkeypatch.setattr(agent_installer.platform, "system", lambda: "Linux")
    monkeypatch.setattr(agent_installer.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(local_agent, "_in_container", lambda: False)
    monkeypatch.setattr(local_agent, "_SETTLE_SECONDS", 0)
    monkeypatch.setattr(settings, "doc_extract_url", "", raising=False)

    agent_dir = tmp_path / "local-agent"
    binary = agent_dir / agent_installer.INSTALLED_BASENAME

    async def fake_install(dest, base, repo, **kw):
        assert dest == agent_dir, "the payload belongs in the local-agent directory"
        dest.mkdir(parents=True, exist_ok=True)
        binary.write_text("#!/bin/sh\n")
        return {
            "asset": "writ-agent-fleet-linux-x86_64.tar.gz",
            "reused": False,
            "checksum_verified": True,
            "binary": str(binary),
        }

    monkeypatch.setattr(agent_installer, "download_only", fake_install)

    seen = {}

    class _Proc:
        pid = 424242
        returncode = None

    async def fake_exec(*args, **kwargs):
        seen["argv"] = args
        seen["env"] = kwargs.get("env") or {}
        seen["cwd"] = kwargs.get("cwd")
        return _Proc()

    monkeypatch.setattr(local_agent.asyncio, "create_subprocess_exec", fake_exec)

    result = asyncio.run(local_agent.install_and_start(
        saas_url="http://writ.example.com",
        token="tok-abc",
        agent_name="local-agent",
    ))

    assert seen["argv"] == (str(binary),), "writ-agent-fleet takes no subcommand"
    assert seen["cwd"] == str(agent_dir)
    env = seen["env"]
    assert env["WRIT_SERVICE_TOKEN"] == "tok-abc"
    assert env["WRIT_COORDINATOR_URL"] == "http://writ.example.com"
    # Plaintext to a non-loopback host: without this the agent refuses to send
    # its bearer and the connection dies on the first request.
    assert env["WRIT_FLEET_ALLOW_INSECURE"] == "1"
    assert env["WRIT_HOME"] == str(agent_dir)
    assert result["checksum_verified"] is True
    assert result["installed_version"] == "writ-agent-fleet-linux-x86_64.tar.gz"


def test_local_start_reports_the_installers_reason(tmp_path, monkeypatch):
    """A failed install must say what the script said, not just that it failed."""
    import asyncio

    from services import agent_installer, local_agent

    monkeypatch.setenv("WRIT_FILES_DIR", str(tmp_path))
    monkeypatch.setattr(agent_installer.platform, "system", lambda: "Linux")
    monkeypatch.setattr(agent_installer.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(local_agent, "_in_container", lambda: False)

    async def boom(dest, base, repo, **kw):
        raise agent_installer.InstallerError("No published release asset for linux-x86_64 yet.")

    monkeypatch.setattr(agent_installer, "download_only", boom)

    with pytest.raises(local_agent.LocalAgentError) as e:
        asyncio.run(local_agent.install_and_start(
            saas_url="https://writ.example.com", token="t", agent_name="n",
        ))
    assert "No published release asset for linux-x86_64 yet." in str(e.value)


def test_preflight_blocks_windows_with_the_route_that_works(monkeypatch):
    """A Windows asset EXISTS; what is missing is a POSIX shell to install it.

    Reporting "no build for this platform" would send the operator looking for
    something that is right there in the release.
    """
    from services import agent_installer, local_agent

    monkeypatch.setattr(agent_installer.platform, "system", lambda: "Windows")
    monkeypatch.setattr(agent_installer.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(local_agent, "_in_container", lambda: False)

    pre = local_agent.preflight()
    assert pre["supported"] is False
    assert "PowerShell" in " ".join(pre["blockers"])
    assert any(a.startswith("writ-agent-fleet-windows-") for a in REAL_ASSETS)


def test_preflight_names_a_missing_installer_dependency(monkeypatch):
    """Better a blocker up front than a shell dying mid-download."""
    from services import agent_installer, local_agent

    monkeypatch.setattr(agent_installer.platform, "system", lambda: "Linux")
    monkeypatch.setattr(agent_installer.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(local_agent, "_in_container", lambda: False)
    monkeypatch.setattr(local_agent.shutil, "which", lambda name: None if name == "curl" else "/bin/" + name)

    pre = local_agent.preflight()
    assert pre["supported"] is False
    assert "curl" in " ".join(pre["blockers"])


def test_download_only_invokes_the_shared_installer(tmp_path, monkeypatch):
    """The local install IS `/agent.sh --download-only`, fed on stdin."""
    import asyncio

    from services import agent_installer

    seen = {}

    class _Proc:
        returncode = 0

        async def communicate(self, data=None):
            seen["stdin"] = (data or b"").decode()
            (tmp_path / agent_installer.INSTALLED_BASENAME).write_text("#!/bin/sh\n")
            return (
                b":: Downloading writ-agent-fleet-linux-x86_64.tar.gz...\n"
                b":: Checksum verified.\n",
                b"",
            )

    async def fake_exec(*args, **kwargs):
        seen["argv"] = args
        seen["env"] = kwargs.get("env") or {}
        return _Proc()

    monkeypatch.setattr(agent_installer.asyncio, "create_subprocess_exec", fake_exec)

    info = asyncio.run(agent_installer.download_only(
        tmp_path, "https://writ.example.com", "usewrit/writ-agent",
    ))

    assert seen["argv"] == ("sh", "-s", "--", "--download-only")
    assert seen["env"]["WRIT_HOME"] == str(tmp_path)
    assert seen["stdin"].startswith("#!/bin/sh")
    assert "@@" not in seen["stdin"], "placeholders must be substituted"
    assert info["asset"] == "writ-agent-fleet-linux-x86_64.tar.gz"
    assert info["reused"] is False
    assert info["checksum_verified"] is True


def test_download_only_installs_the_whole_payload_for_real(tmp_path, monkeypatch):
    """Run the ACTUAL script against a stubbed release, on this machine.

    The one thing no source assertion can prove: that what lands on disk is a
    working agent. The Python installer this replaced took the binary and nothing
    else, so the Playwright driver beside it in the archive was left behind and
    the agent could not launch a browser — with no error at install time to say
    so. Here the archive carries a driver, and the driver has to arrive.
    """
    import asyncio
    import hashlib
    import json
    import os
    import platform
    import shutil as shutil_mod
    import tarfile

    if not shutil_mod.which("sh") or not shutil_mod.which("tar"):
        pytest.skip("needs a POSIX shell and tar")

    from services import agent_installer

    target = agent_installer.host_target()
    if not target:
        pytest.skip(f"no published build for {platform.system()} {platform.machine()}")
    asset_name = f"writ-agent-fleet-{target}.tar.gz"

    # A release archive shaped like the real one: a payload directory holding the
    # binary AND the driver that has to travel with it.
    fixture = tmp_path / "fixture"
    payload = fixture / "payload" / f"writ-agent-fleet-{target}"
    (payload / "driver").mkdir(parents=True)
    (payload / "writ-agent-fleet").write_text("#!/bin/sh\nexit 0\n")
    (payload / "driver" / "node").write_text("stub playwright driver\n")

    archive = fixture / asset_name
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(payload, arcname=payload.name)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (fixture / f"{asset_name}.sha256").write_text(f"{digest}  {asset_name}\n")

    base = "https://github.com/usewrit/writ-agent/releases/download/v1.0.0"
    (fixture / "release.json").write_text(json.dumps({"assets": [
        {"name": asset_name, "browser_download_url": f"{base}/{asset_name}"},
        {"name": f"{asset_name}.sha256", "browser_download_url": f"{base}/{asset_name}.sha256"},
    ]}))

    # Stub curl: the releases API, the asset, and its checksum sibling. Nothing
    # else about the script is stubbed — selection, the host allowlist, checksum
    # verification and extraction all run for real.
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    curl = stub_bin / "curl"
    curl.write_text(
        "#!/bin/sh\n"
        'URL=""; OUT=""; prev=""\n'
        'for a in "$@"; do\n'
        '  case "$a" in https://*) URL="$a" ;; esac\n'
        '  [ "$prev" = "-o" ] && OUT="$a"\n'
        '  prev="$a"\n'
        "done\n"
        'case "$URL" in\n'
        '  *api.github.com*) cat "$FIXTURE/release.json" ;;\n'
        '  *.sha256) cat "$FIXTURE/$(basename "$URL")" ;;\n'
        '  *) cp "$FIXTURE/$(basename "$URL")" "$OUT" ;;\n'
        "esac\n"
    )
    curl.chmod(0o755)
    monkeypatch.setenv("FIXTURE", str(fixture))
    monkeypatch.setenv("PATH", f"{stub_bin}:{os.environ.get('PATH', '')}")

    dest = tmp_path / "local-agent"
    info = asyncio.run(agent_installer.download_only(
        dest, "https://writ.example.com", "usewrit/writ-agent",
    ))

    assert info["asset"] == asset_name
    assert info["checksum_verified"] is True, "the published checksum must be checked"
    binary = dest / agent_installer.INSTALLED_BASENAME
    assert binary.exists() and os.access(binary, os.X_OK)
    assert (dest / "driver" / "node").is_file(), (
        "the Playwright driver ships beside the binary and the agent cannot launch "
        "a browser without it"
    )


def test_download_only_does_not_claim_a_checksum_it_never_checked(tmp_path, monkeypatch):
    """A reused binary was verified when it was FETCHED, not now.

    Reporting False would warn about every restart; reporting True would be a
    claim about a check that did not run. So the key is simply absent.
    """
    import asyncio

    from services import agent_installer

    class _Proc:
        returncode = 0

        async def communicate(self, data=None):
            (tmp_path / agent_installer.INSTALLED_BASENAME).write_text("#!/bin/sh\n")
            return (b":: Using the agent already at /x/writ-agent-fleet\n", b"")

    async def fake_exec(*args, **kwargs):
        return _Proc()

    monkeypatch.setattr(agent_installer.asyncio, "create_subprocess_exec", fake_exec)

    info = asyncio.run(agent_installer.download_only(
        tmp_path, "https://writ.example.com", "usewrit/writ-agent",
    ))
    assert info["reused"] is True
    assert "checksum_verified" not in info


def test_download_only_failure_carries_the_scripts_sentence(tmp_path, monkeypatch):
    """`die()` writes for the operator; an exit code does not."""
    import asyncio

    from services import agent_installer

    class _Proc:
        returncode = 1

        async def communicate(self, data=None):
            return (
                b"\x1b[1;34m::\x1b[0m Finding the linux-x86_64 build...\n"
                b"\x1b[1;31merror\x1b[0m No published release asset for linux-x86_64 yet.\n",
                b"",
            )

    async def fake_exec(*args, **kwargs):
        return _Proc()

    monkeypatch.setattr(agent_installer.asyncio, "create_subprocess_exec", fake_exec)

    with pytest.raises(agent_installer.InstallerError) as e:
        asyncio.run(agent_installer.download_only(
            tmp_path, "https://writ.example.com", "usewrit/writ-agent",
        ))
    assert str(e.value) == "No published release asset for linux-x86_64 yet."


def test_rendered_script_is_valid_posix_shell():
    """Parsed by a real `sh -n`, so a broken branch cannot ship as a string."""
    import shutil as shutil_mod
    import subprocess

    sh = shutil_mod.which("sh")
    if not sh:
        pytest.skip("no POSIX shell on this host")

    from services import agent_installer

    script = agent_installer.render("https://writ.example.com", "usewrit/writ-agent")
    done = subprocess.run([sh, "-n"], input=script, text=True, capture_output=True)
    assert done.returncode == 0, done.stderr


# --- concurrent minting -----------------------------------------------------

def test_registry_write_is_serialised():
    """Two overlapping mints must not race on the single `config` KV row.

    The registry is a read-modify-write over one JSON row with awaits in
    between. Without serialisation two overlapping mints interleave: on a fresh
    install both find no row and both INSERT, which SQLite rejects with
    `UNIQUE constraint failed: config.key`; and once the row exists, both UPDATE
    and one token vanishes from the list.

    The setup wizard hit exactly this by requesting a pairing code and a raw
    token at the same moment.
    """
    import inspect

    from routers import fleet

    assert isinstance(fleet._TOKEN_REGISTRY_LOCK, __import__("asyncio").Lock)
    src = inspect.getsource(fleet._mint_fleet_token)
    assert "_TOKEN_REGISTRY_LOCK" in src, "the mint must hold the lock"
    lock_at = src.index("_TOKEN_REGISTRY_LOCK")
    assert lock_at < src.index("_load_token_registry"), "lock must cover the READ too"
    assert lock_at < src.index("_save_token_registry")


def test_registry_save_tolerates_a_concurrent_creator():
    """Defence in depth: adopt a row someone else created, never 500."""
    import inspect

    from routers import fleet

    src = inspect.getsource(fleet._save_token_registry)
    assert "IntegrityError" in src
    assert "begin_nested" in src, "the failed INSERT must not poison the outer transaction"


def test_pair_code_response_carries_the_manual_fallbacks():
    """One mint fills all three tabs.

    The wizard used to fetch a pairing code AND a raw token to populate its
    quick/binary/docker tabs, minting two fleet tokens — two agent identities —
    for a single connection.
    """
    fields = set(fleet_mod().PairCodeResponse.model_fields)
    for f in ("code", "install_command", "token", "agent_id",
              "connect_command", "docker_command", "install_commands"):
        assert f in fields, f"PairCodeResponse is missing {f}"


def fleet_mod():
    from routers import fleet

    return fleet
