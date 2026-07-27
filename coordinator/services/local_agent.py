"""Run a writ-agent ON THE COORDINATOR'S OWN HOST.

Connecting the first agent is the hard step of self-host onboarding: mint a
token, find the right binary for your platform, put it somewhere, point it at
the coordinator, keep it running. Every one of those is a place to stall. When
the operator is sitting at the same machine the coordinator runs on — the
`run-local.sh` case, which is most first-run installs — none of it needs to be
manual: the coordinator can fetch the release asset for its own platform, write
the config, and supervise the process itself.

The copy-paste path stays for every other machine; this is the "and one right
here" option beside it.

SCOPE AND LIMITS — deliberate:
  * Platform-admin only, and it executes a downloaded binary, so the download is
    HTTPS-only from the configured repo and the resolved asset URL must live on
    the expected host. A release-published checksum is verified when present; its
    ABSENCE is reported, never silently treated as a pass.
  * One local agent per coordinator. Re-running adopts the live one instead of
    racing a second copy onto the same config.
  * State is a pid + metadata file on disk, so a coordinator restart re-adopts a
    still-running agent rather than orphaning it.
  * A containerised coordinator usually should NOT run the agent inside itself
    (no browser deps in the slim image). `preflight()` reports that so the UI can
    steer to the copy-paste path instead of failing halfway through a download.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import platform
import shutil
import signal
import stat
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

# Rust target triples, keyed by (platform.system(), platform.machine()).
_TARGETS = {
    ("Darwin", "arm64"): "aarch64-apple-darwin",
    ("Darwin", "aarch64"): "aarch64-apple-darwin",
    ("Darwin", "x86_64"): "x86_64-apple-darwin",
    ("Linux", "x86_64"): "x86_64-unknown-linux-gnu",
    ("Linux", "amd64"): "x86_64-unknown-linux-gnu",
    ("Linux", "aarch64"): "aarch64-unknown-linux-gnu",
    ("Linux", "arm64"): "aarch64-unknown-linux-gnu",
    ("Windows", "AMD64"): "x86_64-pc-windows-msvc",
    ("Windows", "ARM64"): "aarch64-pc-windows-msvc",
}

_ALLOWED_ASSET_HOSTS = {"github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com"}


def host_target() -> Optional[str]:
    """This machine's Rust target triple, or None if we don't ship for it."""
    return _TARGETS.get((platform.system(), platform.machine()))


def _repo() -> str:
    return (os.getenv("WRIT_AGENT_REPO") or "usewrit/writ-agent").strip().strip("/")


def _agent_dir(create: bool = False) -> Path:
    """Where the downloaded binary, config and pid file live.

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
    return _agent_dir() / ("writ-agent.exe" if platform.system() == "Windows" else "writ-agent")


def _state_path() -> Path:
    return _agent_dir() / "state.json"


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
        blockers.append(
            f"No agent build for this platform ({platform.system()} {platform.machine()})."
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
        "log_path": str(_agent_dir() / "agent.log"),
    }


class LocalAgentError(RuntimeError):
    """A step of the local install/launch failed, with a caller-safe message."""


async def _resolve_asset(target: str) -> tuple[str, str, Optional[str]]:
    """(download_url, asset_name, sha256_from_release_or_None) for this target.

    Matches by target triple rather than a hardcoded filename — asset naming
    changes between releases and a wrong literal 404s with no explanation.
    """
    repo = _repo()
    api = f"https://api.github.com/repos/{repo}/releases/latest"
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as c:
        r = await c.get(api, headers={"Accept": "application/vnd.github+json"})
        if r.status_code == 404:
            raise LocalAgentError(
                f"No published release found for {repo}. If the repository is private "
                "or has no release yet, use the copy-paste command or build from source."
            )
        if r.status_code >= 400:
            raise LocalAgentError(f"GitHub returned {r.status_code} resolving the latest release.")
        rel = r.json()

    assets = rel.get("assets") or []
    match = next((a for a in assets if target in (a.get("name") or "")), None)
    if not match:
        names = ", ".join((a.get("name") or "?") for a in assets[:8]) or "none"
        raise LocalAgentError(
            f"The latest release has no asset for {target} (assets: {names})."
        )

    url = match.get("browser_download_url") or ""
    host = urlparse(url).hostname or ""
    if not url.startswith("https://") or host not in _ALLOWED_ASSET_HOSTS:
        # The URL comes from a remote API response and we are about to execute
        # what it points at — refuse anything off the expected hosts.
        raise LocalAgentError(f"Refusing to download from an unexpected host: {host or url!r}")

    # A checksum is used when the release publishes one; a missing checksum is
    # surfaced to the caller rather than quietly accepted.
    digest = None
    for a in assets:
        name = (a.get("name") or "").lower()
        if name.endswith((".sha256", ".sha256sum")) and target in name:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as c:
                d = await c.get(a.get("browser_download_url") or "")
                if d.status_code < 400:
                    digest = d.text.strip().split()[0]
            break
    return url, match.get("name") or "writ-agent", digest


_BINARY_NAMES = {"writ-agent", "writ-agent.exe"}


def _extract_binary(archive: Path, asset_name: str, dest: Path) -> None:
    """Pull just the agent binary out of a release archive, into ``dest``.

    Never extracts the archive wholesale: a remote tar/zip can carry absolute or
    ``..`` member paths that would write outside the target directory, and it can
    carry symlinks/hardlinks pointing anywhere. Only a regular file whose BASE
    name is the agent binary is copied out, by stream — so a hostile member name
    has nothing to land on.
    """
    import tarfile
    import zipfile

    if asset_name.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            member = next(
                (m for m in z.infolist()
                 if not m.is_dir() and Path(m.filename).name in _BINARY_NAMES),
                None,
            )
            if member is None:
                raise LocalAgentError(f"No writ-agent binary inside {asset_name}.")
            with z.open(member) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)
        return

    with tarfile.open(archive, "r:gz") as tf:
        member = next(
            (m for m in tf.getmembers()
             if m.isfile() and Path(m.name).name in _BINARY_NAMES),
            None,
        )
        if member is None:
            raise LocalAgentError(f"No writ-agent binary inside {asset_name}.")
        src = tf.extractfile(member)
        if src is None:
            raise LocalAgentError(f"Could not read the binary inside {asset_name}.")
        with src, open(dest, "wb") as out:
            shutil.copyfileobj(src, out)


async def _download_binary(target: str) -> dict:
    url, name, expected_sha = await _resolve_asset(target)
    _agent_dir(create=True)
    dest = _binary_path()
    tmp = dest.with_suffix(".part")

    async with httpx.AsyncClient(timeout=300.0, follow_redirects=True) as c:
        async with c.stream("GET", url) as resp:
            if resp.status_code >= 400:
                raise LocalAgentError(f"Download failed ({resp.status_code}) for {name}.")
            h = hashlib.sha256()
            with open(tmp, "wb") as f:
                async for chunk in resp.aiter_bytes(65536):
                    h.update(chunk)
                    f.write(chunk)
    actual_sha = h.hexdigest()

    if expected_sha and actual_sha.lower() != expected_sha.lower():
        tmp.unlink(missing_ok=True)
        raise LocalAgentError("Checksum mismatch on the downloaded agent — refusing to run it.")

    if name.endswith((".tar.gz", ".tgz", ".zip")):
        # Release assets are usually archives, so unpack rather than refuse —
        # but extraction of a remote archive is a path-traversal sink, so member
        # paths are validated and only the agent binary itself is taken out.
        try:
            _extract_binary(tmp, name, dest)
        finally:
            tmp.unlink(missing_ok=True)
    else:
        tmp.replace(dest)
    dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
    return {"asset": name, "sha256": actual_sha, "checksum_verified": bool(expected_sha)}


async def _run(*args: str, env: Optional[dict] = None, timeout: float = 30.0) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(_agent_dir()),
        env={**os.environ, **(env or {})},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise LocalAgentError(f"`{args[0]} {args[1] if len(args) > 1 else ''}` timed out.")
    return proc.returncode or 0, (out or b"").decode("utf-8", "replace")


async def install_and_start(saas_url: str, token: str, agent_name: str) -> dict:
    """Download (if needed), configure, and launch the agent on this host.

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

    target = pre["target"]
    download_info = {}
    if not _binary_path().exists():
        download_info = await _download_binary(target)

    binary = str(_binary_path())
    code, out = await _run(binary, "config", "set", "saas.url", saas_url)
    if code != 0:
        raise LocalAgentError(f"`writ-agent config set saas.url` failed: {out.strip()[:300]}")

    # The agent refuses to send its bearer over plaintext http:// to a non-loopback
    # host unless this is set — the same opt-in the printed command carries.
    parsed = urlparse(saas_url)
    if parsed.scheme == "http" and (parsed.hostname or "") not in ("localhost", "127.0.0.1", "::1"):
        await _run(binary, "config", "set", "saas.allow_insecure", "true")

    log_path = _agent_dir(create=True) / "agent.log"
    log = open(log_path, "ab")
    # Hand the agent the document/OCR extraction settings alongside its token.
    # The agent treats an unset DOC_EXTRACT_URL as "silently skip non-HTML", so
    # without this a one-click agent would come up looking perfectly healthy
    # while dropping every PDF a crawl reaches.
    from routers.fleet import _doc_extract_env

    proc = await asyncio.create_subprocess_exec(
        binary, "start", "--headless",
        cwd=str(_agent_dir()),
        env={**os.environ, "WRIT_SERVICE_TOKEN": token, **_doc_extract_env()},
        stdout=log,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,  # survive a coordinator reload; we supervise by pid
    )

    # Give it a moment to fail fast (bad token, port in use, missing browser).
    await asyncio.sleep(2.0)
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
        "version": download_info.get("asset"),
        "sha256": download_info.get("sha256"),
        "checksum_verified": download_info.get("checksum_verified"),
    }
    _write_state(state)
    logger.info("local writ-agent started (pid %s) for %s", proc.pid, saas_url)
    return {**preflight(), **download_info, "adopted": False}


def stop() -> dict:
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
        time.sleep(0.1)
    if _pid_alive(pid):
        try:
            os.kill(int(pid), signal.SIGKILL)
        except OSError:
            pass
    _write_state({})
    return {"stopped": True, "pid": pid}


def uninstall() -> dict:
    """Stop and remove the downloaded binary + config."""
    stop()
    d = _agent_dir()
    try:
        shutil.rmtree(d)
    except Exception as e:
        raise LocalAgentError(f"Could not remove {d}: {e}")
    return {"removed": True}
