"""
The Connect page's MCP snippets must never put the API key on a command line.

THE INVARIANT under test: every snippet ``GET /api/mcp/connect-info`` hands a
user carries the key in an ENVIRONMENT variable (``WRIT_API_KEY``), never as a
``--api-key`` argv element.

Why this is a security test and not a style preference: a process's arguments
are world-readable on every platform the coordinator runs on — any unprivileged
local process can read the key out of ``ps``/``/proc/<pid>/cmdline`` for as long
as the MCP server lives, which is the whole session. The terminal one-liner is
worse still: the user's shell writes it verbatim into ``~/.zsh_history``, where
it outlives the key rotation that would otherwise contain a leak.

The ``writ-mcp`` connector already knows this — it prints a startup warning
whenever a key arrives via ``--api-key``, and its README and SECURITY.md both
tell users to prefer ``WRIT_API_KEY``. These snippets used to teach the exact
thing the connector warns about, which is the failure this test locks out: the
product's own UI was the loudest source of the bad pattern.

``claude mcp add`` takes ``-e KEY=value`` before the ``--`` separator; the
JSON-config clients (Claude Desktop, Cursor) take an ``env`` block beside
``command``/``args``. Both are stored in a config file the client reads, so the
key never reaches the process table.
"""
import os
import sys

import pytest

COORDINATOR_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if COORDINATOR_DIR not in sys.path:
    sys.path.insert(0, COORDINATOR_DIR)

os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("API_SECRET_KEY", "test-api-secret-0123456789abcdefABCDEF0123456789")
os.environ.setdefault("HMAC_SECRET_KEY", "test-hmac-secret-0123456789abcdefABCDEF0123456789")
os.environ.setdefault("RECORDER_AUTH_SECRET", "test-recorder-secret-0123456789abcdefABCDEF")

from routers import mcp_server  # noqa: E402

KEY_ENV = "WRIT_API_KEY"
# The literal the snippets tell the user to swap for their real key.
PLACEHOLDER = "<YOUR_API_KEY>"


class _StubURL:
    scheme = "http"


class _StubRequest:
    """Only what ``connect_info`` reads: the scheme and the Host header."""

    url = _StubURL()
    headers = {"host": "localhost:8011"}


@pytest.fixture
def info(monkeypatch):
    import asyncio

    # No WRIT_PUBLIC_URL/PUBLIC_URL set: exercise the Host-header fallback, which
    # is what a stock `docker compose up` self-host actually hits.
    monkeypatch.delenv("WRIT_PUBLIC_URL", raising=False)
    monkeypatch.delenv("PUBLIC_URL", raising=False)
    return asyncio.run(mcp_server.connect_info(_StubRequest(), auth=None))


def _argv_strings(info):
    """Every string a shell or client would turn into process arguments."""
    connector = info["node_connector"]
    out = [connector["claude_code"]]
    for client in ("claude_desktop", "cursor"):
        block = connector[client]["mcpServers"][info["server_name"]]
        out.append(block["command"])
        out.extend(block["args"])
    return out


# ---------------------------------------------------------------------------
# The invariant
# ---------------------------------------------------------------------------

def test_no_snippet_passes_the_key_as_a_flag(info):
    for s in _argv_strings(info):
        assert "--api-key" not in s, f"key passed as a flag in: {s}"
        assert "--key" not in s, f"key passed as a flag in: {s}"


def test_config_client_args_carry_no_placeholder(info):
    """The placeholder in a config block belongs in `env`, never in `args`."""
    for client in ("claude_desktop", "cursor"):
        block = info["node_connector"][client]["mcpServers"][info["server_name"]]
        assert PLACEHOLDER not in block["args"]
        assert block["env"] == {KEY_ENV: PLACEHOLDER}


def test_claude_code_one_liner_uses_the_env_flag(info):
    cmd = info["node_connector"]["claude_code"]
    # `-e KEY=value` has to land BEFORE the `--`, or it is parsed as an argument
    # to the spawned connector instead of to `claude mcp add`.
    flag = f"-e {KEY_ENV}={PLACEHOLDER}"
    assert flag in cmd
    assert cmd.index(flag) < cmd.index(" -- "), cmd


def test_snippets_still_point_at_this_coordinator(info):
    """Env-ifying the key must not drop `--url`; without it the connector
    silently targets Writ Cloud, and a self-host user gets someone else's
    server (or a 401) with no hint why."""
    base = info["endpoint"].rsplit("/mcp", 1)[0]
    assert f"--url {base}" in info["node_connector"]["claude_code"]
    for client in ("claude_desktop", "cursor"):
        args = info["node_connector"][client]["mcpServers"][info["server_name"]]["args"]
        assert args[args.index("--url") + 1] == base


def test_http_fallback_keeps_the_key_in_a_header(info):
    """The no-Node path has no process to leak into — a header is correct there."""
    block = info["streamable_http"]["mcpServers"][info["server_name"]]
    assert block["headers"]["Authorization"] == f"Bearer {PLACEHOLDER}"
