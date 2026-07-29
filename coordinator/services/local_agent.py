"""Run a writ-agent ON THE COORDINATOR'S OWN HOST.

Connecting the first agent is the hard step of self-host onboarding: mint a
token, find the right binary for your platform, put it somewhere, point it at
the coordinator, keep it running. Every one of those is a place to stall. When
the operator is sitting at the same machine the coordinator runs on — the
`run-local.sh` case, which is most first-run installs — none of it needs to be
manual: the coordinator can install the release for its own platform, hand the
agent its credentials, and supervise the process itself.

The copy-paste path stays for every other machine; this is the "and one right
here" option beside it.

INSTALLING IS NOT THIS MODULE'S JOB. It used to be, in Python, and that copy had
drifted from the installer every other path uses until nothing about it could
work: it resolved assets by Rust target triple against a release named
`<os>-<arch>`, hunted for a `writ-agent` member inside an archive containing
`writ-agent-fleet`, and deliberately extracted that binary alone — leaving the
Playwright driver that ships beside it behind, so an agent that did come up could
not launch a browser. Acquisition now delegates to `services/agent_installer.py`,
which holds the one script `/agent.sh` also serves.

What is left here is supervision, and the launch contract: `writ-agent-fleet` is
configured ENTIRELY BY ENVIRONMENT and run bare. `config set` / `start
--headless` belong to the DESKTOP `writ-agent` binary; issuing them against this
one is how the local path used to fail even when a download had succeeded.

SCOPE AND LIMITS — deliberate:
  * Platform-admin only, and it ends up executing a downloaded binary, so the
    installer verifies the release-published checksum when there is one and
    refuses any download URL off GitHub's own hosts. A MISSING checksum is
    reported to the caller, never silently treated as a pass.
  * One local agent per coordinator. Re-running adopts the live one instead of
    racing a second copy onto the same directory.
  * State is a pid + metadata file on disk, so a coordinator restart re-adopts a
    still-running agent rather than orphaning it.
  * A containerised coordinator usually should NOT run the agent inside itself
    (no browser deps in the slim image). `preflight()` reports that so the UI can
    steer to the copy-paste path instead of failing halfway through a download.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import shutil
import signal
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from services import agent_installer

logger = logging.getLogger(__name__)

# How long to watch a freshly launched agent before calling it started. Long
# enough to catch the failures that are instant (bad token, missing browser
# runtime, a half-installed payload), short enough that the request returns.
_SETTLE_SECONDS = 2.0

# Commands the installer script needs on the host. Checked in `preflight()`
# rather than discovered by a shell dying mid-download, because the point of
# preflight is that the UI never offers a path this host cannot take.
_REQUIRED_TOOLS = ("sh", "curl", "tar")


def host_target() -> Optional[str]:
    """This machine's release-asset infix, or None if we don't ship for it."""
    return agent_installer.host_target()


def _repo() -> str:
    return (os.getenv("WRIT_AGENT_REPO") or "usewrit/writ-agent").strip().strip("/")


def _agent_dir(create: bool = False) -> Path:
    """Where the installed payload, agent state and pid file live.

    Doubles as the agent's ``WRIT_HOME``, so everything this feature creates —
    binary, Playwright driver, logs, agent state — sits inside one folder that
    ``uninstall()`` can remove whole.

    Does NOT create the directory unless asked: `preflight()` is a read-only
    status probe and must not leave a folder behind on a host that turns out to
    be unsupported, or that the operator never opts in on.
    """
    base = os.getenv("WRIT_FILES_DIR") or str(Path.home() / ".writ")
    d = Path(base).expanduser() / "local-agent"
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def _binary_path() -> Path:
    """The installed fleet agent. NOT ``writ-agent`` — that is the desktop binary."""
    return _agent_dir() / agent_installer.INSTALLED_BASENAME


def _state_path() -> Path:
    return _agent_dir() / "state.json"


def _log_path() -> Path:
    return _agent_dir() / "agent.log"


def _read_state() -> dict:
    try:
        return json.loads(_state_path().read_text())
    except Exception:
        return {}


def _write_state(state: dict) -> None:
    try:
        _agent_dir(create=True)
        _state_path().write_text(json.dumps(state, indent=2))
    except Exception as e:
        logger.warning("could not persist local-agent state: %s", e)


def _pid_alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)  # signal 0 = existence check only
        return True
    except (OSError, ValueError):
        return False


def _in_container() -> bool:
    """Best-effort: are we inside a container image without browser deps?"""
    if os.path.exists("/.dockerenv"):
        return True
    try:
        return "docker" in Path("/proc/1/cgroup").read_text()
    except Exception:
        return False


def _needs_insecure_optin(url: str) -> bool:
    """Does the agent need ``WRIT_FLEET_ALLOW_INSECURE`` to dial this URL?

    It refuses to send its bearer token over plaintext ``http://`` to a
    non-loopback host without the opt-in. ``https://`` and loopback need none —
    the same rule the printed connect command follows, so the two paths behave
    identically.
    """
    parsed = urlparse(url)
    if parsed.scheme != "http":
        return False
    return (parsed.hostname or "").lower() not in ("localhost", "127.0.0.1", "::1")


def preflight() -> dict:
    """Can this host run a local agent, and is one already running?

    Answered BEFORE any download so the UI can offer the choice honestly instead
    of discovering halfway through that this host was never a candidate.
    """
    target = host_target()
    state = _read_state()
    running = _pid_alive(state.get("pid"))
    blockers = []
    if target is None:
        if platform.system() == "Windows":
            # A Windows agent IS published; what is missing is a way to install it
            # from here — the installer is a POSIX shell script. Say that, rather
            # than implying no build exists.
            blockers.append(
                "One-click install needs a POSIX shell, so it is not available on "
                "Windows. Use the PowerShell download command under \"Other ways "
                "to connect\", then the run command beside it."
            )
        else:
            blockers.append(
                f"No agent build for this platform ({platform.system()} {platform.machine()})."
            )
    else:
        missing = [t for t in _REQUIRED_TOOLS if not shutil.which(t)]
        if missing:
            blockers.append(
                f"The installer needs {', '.join(missing)} on this host. "
                "Install them, or connect an agent from another machine."
            )
    if _in_container():
        blockers.append(
            "The coordinator is running in a container, which has no browser runtime. "
            "Run the agent on your host (or another machine) with the command instead."
        )
    return {
        "supported": not blockers,
        "blockers": blockers,
        "target": target,
        "platform": f"{platform.system()} {platform.machine()}",
        "running": running,
        "pid": state.get("pid") if running else None,
        "agent_name": state.get("agent_name"),
        "binary_installed": _binary_path().exists(),
        "installed_version": state.get("version"),
        "log_path": str(_log_path()),
    }


class LocalAgentError(RuntimeError):
    """A step of the local install/launch failed, with a caller-safe message."""


async def install_and_start(saas_url: str, token: str, agent_name: str) -> dict:
    """Install (if needed) and launch the fleet agent on this host.

    Returns the resulting status dict. Raises LocalAgentError with a message
    meant for the operator.
    """
    pre = preflight()
    if pre["running"]:
        return {**pre, "adopted": True}
    if not pre["supported"]:
        raise LocalAgentError(" ".join(pre["blockers"]))
    if not saas_url:
        raise LocalAgentError(
            "Set WRIT_PUBLIC_URL on the coordinator first — the agent needs a URL to dial back."
        )

    agent_dir = _agent_dir(create=True)
    try:
        # The very script /agent.sh serves, in the mode the connect modal's own
        # download one-liner uses. It resolves the asset for this platform,
        # verifies the published checksum, and unpacks the WHOLE payload — the
        # Playwright driver included, without which no browser ever launches.
        install_info = await agent_installer.download_only(agent_dir, saas_url, _repo())
    except agent_installer.InstallerError as e:
        raise LocalAgentError(f"Could not install the agent: {e}")

    binary = _binary_path()

    # Hand the agent the document/OCR extraction settings alongside its token.
    # The agent treats an unset DOC_EXTRACT_URL as "silently skip non-HTML", so
    # without this a one-click agent would come up looking perfectly healthy
    # while dropping every PDF a crawl reaches.
    from routers.fleet import _doc_extract_env

    # writ-agent-fleet takes its ENTIRE configuration from the environment and is
    # run with no subcommand. There is no config file to write and nothing to go
    # stale — see agent_installer's launch contract, which /agent.sh follows too.
    env = {
        **os.environ,
        "WRIT_SERVICE_TOKEN": token,
        "WRIT_COORDINATOR_URL": saas_url,
        "WRIT_HOME": str(agent_dir),
        **_doc_extract_env(),
    }
    if _needs_insecure_optin(saas_url):
        env["WRIT_FLEET_ALLOW_INSECURE"] = "1"

    log_path = _log_path()
    with open(log_path, "ab") as log:
        # The child dups the fd, so closing ours right after spawning is correct
        # — and leaving it open would leak one per start.
        proc = await asyncio.create_subprocess_exec(
            str(binary),
            cwd=str(agent_dir),
            env=env,
            stdout=log,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,  # survive a coordinator reload; we supervise by pid
        )

    # Give it a moment to fail fast (bad token, missing browser, bad payload).
    await asyncio.sleep(_SETTLE_SECONDS)
    if proc.returncode is not None:
        tail = ""
        try:
            tail = log_path.read_text(errors="replace")[-600:]
        except Exception:
            pass
        raise LocalAgentError(
            f"The agent exited immediately (code {proc.returncode}). Log tail: {tail.strip()[-400:]}"
        )

    state = {
        "pid": proc.pid,
        "agent_name": agent_name,
        "saas_url": saas_url,
        "started_at": time.time(),
        # On a reused install the asset name lives in the state we already wrote.
        "version": install_info.get("asset") or _read_state().get("version"),
        "checksum_verified": install_info.get("checksum_verified"),
    }
    _write_state(state)
    logger.info("local writ-agent-fleet started (pid %s) for %s", proc.pid, saas_url)

    # `checksum_verified` is reported only when something was downloaded now, so
    # the UI's "unverified download" warning cannot fire for a binary that was
    # verified when it was fetched.
    reported = {k: v for k, v in install_info.items() if k in ("asset", "checksum_verified")}
    return {**preflight(), **reported, "adopted": False}


async def stop() -> dict:
    """Stop the coordinator-hosted agent, if one is running."""
    state = _read_state()
    pid = state.get("pid")
    if not _pid_alive(pid):
        _write_state({})
        return {"stopped": False, "reason": "not running"}
    try:
        os.kill(int(pid), signal.SIGTERM)
    except OSError as e:
        raise LocalAgentError(f"Could not stop the agent (pid {pid}): {e}")
    for _ in range(20):  # up to ~2s for a clean exit
        if not _pid_alive(pid):
            break
        # `await`, not time.sleep: this runs inside the request loop, and blocking
        # it for two seconds stalls every other request on a single-worker
        # coordinator.
        await asyncio.sleep(0.1)
    if _pid_alive(pid):
        try:
            os.kill(int(pid), signal.SIGKILL)
        except OSError:
            pass
    _write_state({})
    return {"stopped": True, "pid": pid}


async def uninstall() -> dict:
    """Stop the agent and remove its installed payload, state and logs."""
    await stop()
    d = _agent_dir()
    if not d.exists():
        return {"removed": False, "reason": "nothing installed"}
    try:
        shutil.rmtree(d)
    except Exception as e:
        raise LocalAgentError(f"Could not remove {d}: {e}")
    return {"removed": True}
