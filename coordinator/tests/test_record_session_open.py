"""
`session_open` record-contract unit tests (no DB, no network, no socket).

THE INVARIANT under test: the frame the coordinator sends to open a recording
session on an agent must carry ``purpose:"record"``.

An agent multiplexes THREE kinds of session over its single persistent WS —
recording, streaming, and backend-orchestrated concierge/browse — and
``session_open`` is the open frame for all of them. ``purpose`` is the ONLY
discriminator the agent routes on (writ-agent: `local::record::bridge::open` is
reached only when `is_record_open(msg)`, i.e. `purpose == "record"` at the top
level or under `config`). Without it the open falls through to the browse
handler, which opens a page, acks `session_opened{success:true}`, and then
silently drops every recorder frame — the browser recorder spins on "connecting"
forever while the agent visibly opens a browser. The removed Node ws-gateway
always sent `purpose:"record"`; the in-process replacement must too.

The second invariant: a NAKed open (`session_opened{success:false}` — a
browserless agent build, or a session-id collision) must mark the session closed
and record the reason, so the opener fails loudly instead of relaying frames into
a session that has no driver behind it.

Runnable with plain ``python3 coordinator/tests/test_record_session_open.py``.
"""
import asyncio
import os
import sys

COORDINATOR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if COORDINATOR not in sys.path:
    sys.path.insert(0, COORDINATOR)

# Settings refuses to construct with shipped-default secrets; give it throwaway
# ones so importing the router is possible without a configured install.
os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("ALLOW_INSECURE_DEV", "true")

import routers.user_recorder_ws as urw  # noqa: E402


# --- the open frame ---------------------------------------------------------

def test_open_frame_is_a_session_open_for_the_given_id():
    frame = urw._record_session_open("abc123")
    assert frame["type"] == "session_open"
    assert frame["session_id"] == "abc123"


def test_open_frame_declares_the_record_purpose():
    # THE regression this file exists for. Mirrors the agent's `is_record_open`.
    frame = urw._record_session_open("abc123")
    assert frame.get("purpose") == "record"


def test_open_frame_carries_a_config_object():
    # Parity with the agent contract, which also accepts `config.purpose`.
    assert isinstance(urw._record_session_open("abc123").get("config"), dict)


# --- the ack ----------------------------------------------------------------

def _dispatch(msg: dict):
    """Apply one inbound `session_opened` ack the way the agent loop does."""
    urw._handle_session_opened_ack("agent-test", msg)


def test_successful_ack_opens_the_session():
    opened, closed = asyncio.Event(), asyncio.Event()
    urw._record_sessions["s-ok"] = {"opened": opened, "closed": closed}
    try:
        _dispatch({"type": "session_opened", "session_id": "s-ok", "success": True})
        assert opened.is_set() is True
        assert closed.is_set() is False
    finally:
        urw._record_sessions.pop("s-ok", None)


def test_naked_ack_closes_the_session_and_keeps_the_reason():
    opened, closed = asyncio.Event(), asyncio.Event()
    urw._record_sessions["s-nak"] = {"opened": opened, "closed": closed}
    try:
        _dispatch({
            "type": "session_opened",
            "session_id": "s-nak",
            "success": False,
            "error": "no recorder available on this agent (browserless build)",
        })
        # Unblock the waiter (never hang it) …
        assert opened.is_set() is True
        # … but as a FAILED open, with the agent's reason preserved for the close.
        assert closed.is_set() is True
        assert "browserless" in urw._record_sessions["s-nak"]["open_error"]
    finally:
        urw._record_sessions.pop("s-nak", None)


def test_ack_without_a_success_field_is_treated_as_open():
    # Older/leaner agents ack with no `success` key at all — that is not a NAK.
    opened, closed = asyncio.Event(), asyncio.Event()
    urw._record_sessions["s-bare"] = {"opened": opened, "closed": closed}
    try:
        _dispatch({"type": "session_opened", "session_id": "s-bare"})
        assert opened.is_set() is True
        assert closed.is_set() is False
    finally:
        urw._record_sessions.pop("s-bare", None)


def test_ack_for_an_unknown_session_is_ignored():
    _dispatch({"type": "session_opened", "session_id": "does-not-exist"})  # must not raise


if __name__ == "__main__":  # pragma: no cover - script-style run
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError:
                failures += 1
                print(f"FAIL {name}")
    sys.exit(1 if failures else 0)
