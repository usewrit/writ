"""Regression: the registry-read half of the direct-socket migration.

The external ws-gateway process is gone, so the old ``ws-gateway:recorder:*``
Redis registry (``services/gateway_registry.py``) is always empty. Every consumer
that used to read it to decide "which recorders are live" must instead read the
in-process directly-connected fleet (``user_recorder_ws._connections`` /
``_agent_meta``), the same source the dispatcher and ``_pick_recorder`` use.

These tests prove that path end-to-end:

  * A stub agent connects a real WebSocket to ``/ws/ai-gateway`` (via TestClient
    on a minimal app — no ``main`` lifespan / Postgres / Redis required: the WS
    handler falls back to an in-memory registration when the DB is unreachable and
    to no-channel-key when Redis is absent). We assert the reworked consumers see
    it: ``get_connected_recorders`` / ``get_connected_recorder_meta`` (used by
    ``routers.agents`` enrichment and ``routers.automation`` role resolution), and
    that the ``ai_keys_configured`` connect param is captured.

  * BYO-AI candidate selection (``byo_ai_router._list_candidates``) and the
    capacity manager's fleet count read the same helper — we drive those with
    directly-seeded registry entries (a user-hosted, key-bearing agent can't be
    minted over an infra JWT without the OAuth DB) and assert role/keys filtering.
"""
import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from routers import user_recorder_ws
from routers.user_recorder_ws import (
    _agent_meta,
    _connections,
    get_connected_recorder_meta,
    get_connected_recorders,
)
from utils import recorder_auth
from utils.recorder_auth import generate_service_token

_TEST_SECRET = "test-recorder-auth-secret-for-fleet-reads"


@pytest.fixture()
def ws_client(monkeypatch):
    """Minimal app exposing ONLY the recorder-WS router (no main lifespan)."""
    # Validation reads the module-global secret; pin it so our JWT verifies
    # regardless of env-var timing.
    monkeypatch.setattr(recorder_auth, "RECORDER_AUTH_SECRET", _TEST_SECRET)
    app = FastAPI()
    app.include_router(user_recorder_ws.router)
    # Also mount the app-root alias exactly like main.py.
    app.add_api_websocket_route("/ws/ai-gateway", user_recorder_ws._recorder_ws_handler)
    return TestClient(app)


def _connect_stub(client, agent_id, *, ai_keys=False):
    """Open a stub-agent WS (dialect A: first-frame auth) and drain the two
    handshake frames. Returns the assigned agent_id."""
    token = generate_service_token("local", max_sessions=5, secret=_TEST_SECRET, agent_id=agent_id)
    qs = "?ai_keys_configured=1" if ai_keys else ""
    ws = client.websocket_connect(f"/ws/ai-gateway{qs}")
    ws.__enter__()
    ws.send_json({"type": "auth", "token": token, "agent_id": agent_id})
    welcome = ws.receive_json()
    auth_ok = ws.receive_json()
    assert welcome["type"] == "welcome"
    assert auth_ok["type"] == "auth_ok"
    return ws, auth_ok["agent_id"]


def test_connected_agent_visible_to_consumers(ws_client):
    """A real WS connect makes the agent visible to the presence-based consumers
    (agents enrichment role + automation executor-role resolution) and captures
    the ai_keys_configured connect param."""
    ws, agent_id = _connect_stub(ws_client, "stub-infra-1", ai_keys=True)
    try:
        recs = {r["agent_id"]: r for r in get_connected_recorders()}
        assert agent_id in recs, "connected agent must appear in the in-process fleet"

        # get_connected_recorder_meta is what routers.automation now reads to
        # resolve an executor's venue role (was gateway_registry.get_recorder_meta).
        meta = get_connected_recorder_meta(agent_id)
        assert meta is not None
        assert meta["role"] == "infrastructure"  # JWT service token → infra
        # ai_keys_configured captured from the connect query param.
        assert meta["ai_keys_configured"] is True
        assert recs[agent_id]["ai_keys_configured"] is True
    finally:
        ws.__exit__(None, None, None)

    # After disconnect the agent drops out of the fleet.
    assert get_connected_recorder_meta(agent_id) is None


def test_agent_still_served_when_registration_fails(ws_client, monkeypatch):
    """A DB fault during registration must degrade to an in-memory agent rather
    than kill the socket.

    ``_register_agent`` returns ``(agent_id, operator_max_sessions)`` in a single
    unpacking, so a raise leaves NEITHER name bound. The recovery branch re-bound
    only the first, and the meta dict built right after reads the second — so the
    handler died on UnboundLocalError before sending a frame, and the connect
    failed with a bare ``anyio.EndOfStream``. That is what "database is locked"
    under write contention produces on the SQLite this coordinator ships with,
    and the degrade path the ``except`` exists for never once ran.

    Every other test here connects against a migrated database, so none of them
    exercise the branch; this one forces it.
    """
    async def _fail(*args, **kwargs):
        raise OperationalError("SELECT agents.id FROM agents", {}, Exception("database is locked"))

    monkeypatch.setattr(user_recorder_ws, "_register_agent", _fail)

    ws, agent_id = _connect_stub(ws_client, "stub-db-locked")
    try:
        assert agent_id == "stub-db-locked", "the requested id is kept when no row can be written"
        meta = get_connected_recorder_meta(agent_id)
        assert meta is not None, "an unregistered agent is still a connected one"
        # No row means no operator override, so the effective cap falls back to
        # the ceiling the token carries — not to the 2-slot default, which would
        # quietly halve what the operator provisioned.
        assert meta["max_sessions"] == 5
        # The override itself is not in the public projection (safe fields only),
        # so read the raw entry: it must be an explicit None, which is precisely
        # the value that used to be unbound.
        assert _agent_meta[agent_id]["operator_max_sessions"] is None
    finally:
        ws.__exit__(None, None, None)


def test_byo_candidates_filter_role_and_keys():
    """byo_ai_router._list_candidates reads the in-process fleet and keeps only
    user-hosted, key-bearing agents."""
    from services import byo_ai_router

    seeded = {
        # user-hosted + keys → candidate
        "byo-user-keys": {"role": "user-hosted", "ai_keys_configured": True, "max_sessions": 2, "active_sessions": 0},
        # user-hosted, no keys → excluded
        "byo-user-nokeys": {"role": "user-hosted", "ai_keys_configured": False, "max_sessions": 2, "active_sessions": 0},
        # infra + keys → excluded (infra never holds the owner's keys)
        "byo-infra-keys": {"role": "infrastructure", "ai_keys_configured": True, "max_sessions": 5, "active_sessions": 0},
    }
    sentinel = object()
    for aid, m in seeded.items():
        _connections[aid] = sentinel
        _agent_meta[aid] = dict(m)
    try:
        cands = {c["agent_id"] for c in byo_ai_router._list_candidates()}
        assert cands == {"byo-user-keys"}
    finally:
        for aid in seeded:
            _connections.pop(aid, None)
            _agent_meta.pop(aid, None)


def test_capacity_counts_connected_fleet():
    """The capacity manager's fleet block iterates get_connected_recorders() and
    counts each connected agent's slots (deduped against DB-counted agents)."""
    seeded = {
        "cap-a": {"role": "infrastructure", "max_sessions": 5, "active_sessions": 1, "ai_keys_configured": False},
        "cap-b": {"role": "user-hosted", "max_sessions": 2, "active_sessions": 0, "ai_keys_configured": False},
    }
    sentinel = object()
    for aid, m in seeded.items():
        _connections[aid] = sentinel
        _agent_meta[aid] = dict(m)
    try:
        recs = {r["agent_id"]: r for r in get_connected_recorders()}
        # Both connected agents are visible with their advertised slot counts —
        # exactly the fields the capacity block sums (max_sessions/active_sessions).
        assert recs["cap-a"]["max_sessions"] == 5
        assert recs["cap-b"]["max_sessions"] == 2
        total = sum(r["max_sessions"] for aid, r in recs.items() if aid in seeded)
        assert total == 7
    finally:
        for aid in seeded:
            _connections.pop(aid, None)
            _agent_meta.pop(aid, None)


if __name__ == "__main__":
    # Allow running without pytest: `python tests/test_connected_fleet_registry_reads.py`
    import sys
    from unittest.mock import patch

    class _MP:
        def setattr(self, obj, name, val):
            setattr(obj, name, val)

    monkeypatch = _MP()
    monkeypatch.setattr(recorder_auth, "RECORDER_AUTH_SECRET", _TEST_SECRET)
    app = FastAPI()
    app.include_router(user_recorder_ws.router)
    app.add_api_websocket_route("/ws/ai-gateway", user_recorder_ws._recorder_ws_handler)
    client = TestClient(app)
    test_connected_agent_visible_to_consumers(client)
    test_byo_candidates_filter_role_and_keys()
    test_capacity_counts_connected_fleet()
    print("ALL_FLEET_READ_TESTS_OK")
