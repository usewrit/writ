"""AI-session dispatch proxy: the coordinator FIRES ONE ``ai_session_start`` frame at
a connected fleet agent (non-blocking) and later records the result when the agent's
terminal frame lands over the WS layer (contract SELFHOST_AISESSION_CONTRACT).

Two tiers, matching the repo's offline-safe + DB-gated split (mirrors
test_fleet_deploy):

  Tier A (OFFLINE, always runs): the ack routing + sealing + agent-pick paths against
  a minimal in-loop harness. No Postgres required.
    - ai_session_complete resolves the send_and_await future (correlate_by session_id)
      — the future path is retained even though /start no longer awaits it.
    - a secret credentials map seals under the channel key.
    - _pick_agent gates: explicit-offline 409, none-online 409, picks the online one.

  Tier B (DB-gated, skips cleanly without Postgres): full POST /start via an ASGI
  client against a real schema. Now ASYNC:
    - /start persists a 'running' row and FIRES the correct frame WITHOUT awaiting the
      terminal reply (stub `push_fire_and_forget`, assert the frame + the running row).
    - feeding an `ai_session_complete` frame through the WS dispatch handler updates
      the row to its terminal state.
    - the no-online-agent 409 path.
"""
import asyncio
import json

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException

from routers import user_recorder_ws
from routers.user_recorder_ws import _agent_meta, _connections


@pytest.fixture(autouse=True)
def _master_key():
    """Pin a valid Fernet master key — the sealing path routes plaintext through the
    master key before re-sealing under the channel key."""
    from config import settings
    from security.encryption import SecretEncryption

    prev_key = settings.secret_encryption_key
    prev_cipher = SecretEncryption._cipher
    settings.secret_encryption_key = Fernet.generate_key().decode()
    SecretEncryption._cipher = None
    try:
        yield
    finally:
        settings.secret_encryption_key = prev_key
        SecretEncryption._cipher = prev_cipher


def _seed_agent(agent_id, channel_key, *, local_capable=True, active=0, max_sessions=5):
    _connections[agent_id] = object()
    _agent_meta[agent_id] = {
        "role": "infrastructure",
        "is_trusted": True,
        "channel_key": channel_key,
        "local_workflows_capable": local_capable,
        "max_sessions": max_sessions,
        "active_sessions": active,
        "ai_keys_configured": True,
    }


def _drop_agent(agent_id):
    _connections.pop(agent_id, None)
    _agent_meta.pop(agent_id, None)


# ---------------------------------------------------------------------------
# Tier A — offline (no Postgres)
# ---------------------------------------------------------------------------
class _CompletingWS:
    """Fake agent socket that, on an ai_session_start frame, schedules an out-of-band
    ai_session_complete ack so send_and_await(reply_type="ai_session_complete")
    resolves — exactly as a real inbound frame would via the ws dispatch arm."""

    def __init__(self, reply_extra=None, reply_type="ai_session_complete"):
        self.sent = []
        self.reply_extra = reply_extra or {}
        self.reply_type = reply_type

    async def send_json(self, frame):
        self.sent.append(frame)
        if frame.get("type") == "ai_session_start":
            ack = {"type": self.reply_type, "session_id": frame.get("session_id")}
            ack.update(self.reply_extra)
            asyncio.get_event_loop().call_soon(
                user_recorder_ws._handle_agent_reply,
                "stub-ai-a",
                ack,
                "session_id",
            )


def test_ai_session_complete_ack_resolves_future():
    """An inbound ai_session_complete correlated by session_id resolves the pending
    send_and_await future (the mechanism the /start endpoint blocks on)."""
    channel_key = Fernet.generate_key().decode()
    ws = _CompletingWS(reply_extra={
        "status": "complete", "workflow_id": 123,
        "workflow_name": "AI: buy widget", "steps": 7, "message": "done",
    })
    _seed_agent("stub-ai-a", channel_key)
    _connections["stub-ai-a"] = ws

    async def _run():
        frame = {
            "type": "ai_session_start",
            "name": "AI: buy widget",
            "goal": "buy widget",
            "entry_url": "https://x",
            "available_data": {},
            "credentials_encrypted": None,
            "persona_id": None,
            "max_steps": 20,
            "generate_workflow": True,
        }
        reply = await user_recorder_ws.send_and_await(
            "stub-ai-a", frame,
            reply_type="ai_session_complete",
            correlate_by="session_id",
            timeout=5,
        )
        return frame, reply

    try:
        frame, reply = asyncio.run(_run())
    finally:
        _drop_agent("stub-ai-a")

    # A session_id was injected onto the frame (correlate_by default-gen) and echoed back.
    assert frame.get("session_id")
    assert reply is not None and not reply.get("error")
    assert reply["session_id"] == frame["session_id"]
    assert reply["status"] == "complete"
    assert reply["workflow_id"] == 123
    assert reply["steps"] == 7


def test_ai_session_failed_ack_also_resolves_future():
    """The failed terminal frame resolves the SAME future (both arms route by
    session_id in the ws dispatch)."""
    channel_key = Fernet.generate_key().decode()
    ws = _CompletingWS(
        reply_type="ai_session_failed",
        reply_extra={"status": "error", "error": "ai_unavailable", "message": "no local AI"},
    )
    _seed_agent("stub-ai-a", channel_key)
    _connections["stub-ai-a"] = ws

    async def _run():
        frame = {"type": "ai_session_start", "goal": "x"}
        return await user_recorder_ws.send_and_await(
            "stub-ai-a", frame,
            reply_type="ai_session_complete",
            correlate_by="session_id",
            timeout=5,
        )

    try:
        reply = asyncio.run(_run())
    finally:
        _drop_agent("stub-ai-a")

    assert reply is not None
    assert reply.get("error") == "ai_unavailable"
    assert reply.get("status") == "error"


def test_seal_credentials_under_channel_key():
    """Secret credentials seal under the agent channel key (same path as deploy)."""
    from routers.fleet import _seal_plaintext_for_agent

    channel_key = Fernet.generate_key().decode()
    creds = {"password": "s3cr3t", "otp_seed": "ABCD"}
    sealed = _seal_plaintext_for_agent(json.dumps(creds), channel_key)
    plaintext = Fernet(channel_key.encode()).decrypt(sealed.encode()).decode()
    assert json.loads(plaintext) == creds


def test_pick_agent_gates():
    """_pick_agent: explicit-offline 409, none-online 409, picks the online agent,
    honors an explicit online agent."""
    from routers.ai_sessions import _pick_agent

    # Ensure a clean registry for the none-online assertion.
    for aid in list(_connections):
        _drop_agent(aid)

    # Explicit but offline → 409
    with pytest.raises(HTTPException) as e_off:
        _pick_agent("ghost")
    assert e_off.value.status_code == 409

    # None online (no agents) → 409
    with pytest.raises(HTTPException) as e_none:
        _pick_agent(None)
    assert e_none.value.status_code == 409

    channel_key = Fernet.generate_key().decode()
    _seed_agent("ai-pick-1", channel_key, active=0, max_sessions=5)
    try:
        # Auto-pick returns the online agent.
        assert _pick_agent(None) == "ai-pick-1"
        # Explicit online agent honored.
        assert _pick_agent("ai-pick-1") == "ai-pick-1"
    finally:
        _drop_agent("ai-pick-1")


# ---------------------------------------------------------------------------
# Tier B — full POST /start via ASGI client (Postgres-gated; skips without a DB)
# ---------------------------------------------------------------------------
@pytest.fixture()
def ai_app(db_engine):
    """A FastAPI app wiring the ai_sessions router against the throwaway schema, with
    require_platform_admin bypassed and get_db bound to the test engine."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from database import get_db
    from security.dependencies import require_platform_admin, AuthContext
    from routers.ai_sessions import router as ai_sessions_router

    maker = async_sessionmaker(bind=db_engine, expire_on_commit=False, class_=AsyncSession)

    async def _override_get_db():
        async with maker() as s:
            yield s

    async def _override_admin():
        return AuthContext(user_id=1, auth_method="jwt", is_platform_admin=True)

    app = FastAPI()
    app.include_router(ai_sessions_router, prefix="/api")
    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[require_platform_admin] = _override_admin
    return app, maker


def _client(app):
    import httpx
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _stub_fire_and_forget(monkeypatch, *, ok=True):
    """Patch push_fire_and_forget (the fire-and-forget send the /start endpoint now
    uses instead of send_and_await) to record the dispatched frame/agent and report
    whether the send 'succeeded' (ok) without touching a real socket."""
    async def _fake(agent_id, frame):
        _fake.last_frame = frame
        _fake.last_agent = agent_id
        return ok

    _fake.last_frame = None
    _fake.last_agent = None
    monkeypatch.setattr(user_recorder_ws, "push_fire_and_forget", _fake)
    return _fake


async def test_start_persists_running_row_and_fires_frame(ai_app, monkeypatch):
    """/start persists a 'running' row and FIRES the correct frame without awaiting
    the terminal reply — the response returns immediately with status 'running'."""
    app, maker = ai_app
    channel_key = Fernet.generate_key().decode()
    _seed_agent("ai-b1", channel_key)
    fire = _stub_fire_and_forget(monkeypatch)
    try:
        async with _client(app) as client:
            r = await client.post("/api/ai-sessions/start", json={
                "goal": "log in and buy",
                "entry_url": "https://shop.example.com",
                "available_data": {"email": "me@x.com"},
                "credentials": {"password": "pw", "card": "4111"},
                "max_steps": 15,
                "generate_workflow": True,
            })
        assert r.status_code == 200, r.text
        out = r.json()
        # Returned immediately as a 'running' row — NOT waited on to completion.
        assert out["status"] == "running"
        assert out["session_id"]
        assert out["agent_id"] == "ai-b1"
        assert out["completed_at"] is None
        assert out["workflow_id"] is None

        # The fired frame carries the frozen fields + sealed credentials.
        frame = fire.last_frame
        assert frame["type"] == "ai_session_start"
        assert frame["goal"] == "log in and buy"
        assert frame["entry_url"] == "https://shop.example.com"
        assert frame["available_data"] == {"email": "me@x.com"}
        assert frame["max_steps"] == 15
        assert frame["generate_workflow"] is True
        assert frame["session_id"] == out["session_id"]
        assert "request_id" in frame
        creds = json.loads(
            Fernet(channel_key.encode()).decrypt(frame["credentials_encrypted"].encode()).decode()
        )
        assert creds == {"password": "pw", "card": "4111"}
        assert fire.last_agent == "ai-b1"
    finally:
        _drop_agent("ai-b1")

    # Row exists and is 'running' (terminal state lands later via the WS handler).
    from models.ai_session import AiSession
    from sqlalchemy import select
    async with maker() as s:
        row = (
            await s.execute(select(AiSession).where(AiSession.session_id == out["session_id"]))
        ).scalar_one_or_none()
        assert row is not None
        assert row.status == "running"
        assert row.agent_id == "ai-b1"


async def test_ws_terminal_frame_updates_row(ai_app, monkeypatch):
    """Feeding an ai_session_complete frame through the WS terminal handler moves the
    persisted 'running' row to its terminal state (the fire-and-forget completion path)."""
    app, maker = ai_app
    channel_key = Fernet.generate_key().decode()
    _seed_agent("ai-b2", channel_key)
    _stub_fire_and_forget(monkeypatch)

    # Bind AsyncSessionLocal (used inside the WS handler) to the test schema so the
    # handler writes to the SAME throwaway schema the /start row was persisted in.
    import database
    monkeypatch.setattr(database, "AsyncSessionLocal", maker)
    monkeypatch.setattr(user_recorder_ws, "AsyncSessionLocal", maker)

    try:
        async with _client(app) as client:
            r = await client.post("/api/ai-sessions/start", json={
                "goal": "record then complete", "generate_workflow": True,
            })
        assert r.status_code == 200, r.text
        out = r.json()
        session_id = out["session_id"]
        assert out["status"] == "running"

        # The agent reports the terminal frame over its socket (scoped to ai-b2).
        await user_recorder_ws._apply_ai_session_terminal("ai-b2", {
            "type": "ai_session_complete",
            "session_id": session_id,
            "status": "complete",
            "workflow_id": 55,
            "workflow_name": "AI: record then complete",
            "steps": 9,
            "message": "recorded",
            "error": None,
        })
    finally:
        _drop_agent("ai-b2")

    from models.ai_session import AiSession
    from sqlalchemy import select
    async with maker() as s:
        row = (
            await s.execute(select(AiSession).where(AiSession.session_id == session_id))
        ).scalar_one_or_none()
        assert row is not None
        assert row.status == "complete"
        assert row.workflow_id == 55
        assert row.workflow_name == "AI: record then complete"
        assert row.steps == 9
        assert row.message == "recorded"
        assert row.completed_at is not None


async def test_ws_terminal_frame_scoped_to_dispatching_agent(ai_app, monkeypatch):
    """A terminal frame from a DIFFERENT agent must not touch another agent's row —
    identity in the payload is never trusted; the row is scoped to its agent_id."""
    app, maker = ai_app
    channel_key = Fernet.generate_key().decode()
    _seed_agent("ai-owner", channel_key)
    _stub_fire_and_forget(monkeypatch)

    import database
    monkeypatch.setattr(database, "AsyncSessionLocal", maker)
    monkeypatch.setattr(user_recorder_ws, "AsyncSessionLocal", maker)

    try:
        async with _client(app) as client:
            r = await client.post("/api/ai-sessions/start", json={"goal": "owned session"})
        assert r.status_code == 200, r.text
        session_id = r.json()["session_id"]

        # A foreign agent claims completion of this session → must be ignored.
        await user_recorder_ws._apply_ai_session_terminal("ai-foreign", {
            "type": "ai_session_complete",
            "session_id": session_id,
            "status": "complete",
            "workflow_id": 999,
        })
    finally:
        _drop_agent("ai-owner")

    from models.ai_session import AiSession
    from sqlalchemy import select
    async with maker() as s:
        row = (
            await s.execute(select(AiSession).where(AiSession.session_id == session_id))
        ).scalar_one_or_none()
        assert row is not None
        # Untouched by the foreign agent's frame.
        assert row.status == "running"
        assert row.workflow_id is None


async def test_start_no_online_agent_returns_4xx(ai_app):
    app, maker = ai_app
    # No agents seeded → auto-pick has nothing online.
    for aid in list(_connections):
        _drop_agent(aid)
    async with _client(app) as client:
        r = await client.post("/api/ai-sessions/start", json={"goal": "do a thing"})
    assert r.status_code == 409, r.text
    assert "no online agent" in r.text.lower()


async def test_start_marks_error_when_send_fails(ai_app, monkeypatch):
    """If the agent vanished between the pick and the fire (push_fire_and_forget →
    False), /start marks the persisted row 'error' and returns a 409."""
    app, maker = ai_app
    channel_key = Fernet.generate_key().decode()
    _seed_agent("ai-b3", channel_key)
    _stub_fire_and_forget(monkeypatch, ok=False)
    try:
        async with _client(app) as client:
            r = await client.post("/api/ai-sessions/start", json={"goal": "x", "generate_workflow": False})
        assert r.status_code == 409, r.text
    finally:
        _drop_agent("ai-b3")

    # The row was persisted and flipped to 'error'.
    from models.ai_session import AiSession
    from sqlalchemy import select
    async with maker() as s:
        rows = (await s.execute(select(AiSession).where(AiSession.goal == "x"))).scalars().all()
        assert rows, "row should be persisted even when the fire fails"
        row = rows[-1]
        assert row.status == "error"
        assert row.error == "agent_disconnected"
        assert row.completed_at is not None


async def test_list_and_get(ai_app, monkeypatch):
    app, maker = ai_app
    channel_key = Fernet.generate_key().decode()
    _seed_agent("ai-b4", channel_key)
    _stub_fire_and_forget(monkeypatch)
    try:
        async with _client(app) as client:
            r = await client.post("/api/ai-sessions/start", json={"goal": "listable goal"})
            assert r.status_code == 200, r.text
            pk = r.json()["id"]

            lst = await client.get("/api/ai-sessions")
            assert lst.status_code == 200
            sessions = lst.json()["sessions"]
            assert any(s["id"] == pk for s in sessions)

            got = await client.get(f"/api/ai-sessions/{pk}")
            assert got.status_code == 200
            assert got.json()["id"] == pk

            missing = await client.get("/api/ai-sessions/99999999")
            assert missing.status_code == 404
    finally:
        _drop_agent("ai-b4")
