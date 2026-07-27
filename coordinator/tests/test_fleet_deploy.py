"""Fleet-local deploy: send a coordinator workflow/secret/persona to a fleet
agent with Mirror/Move disposition (plan §4.2 + §4.4).

Two tiers, matching the repo's offline-safe + DB-gated split:

  Tier A (OFFLINE, always runs): the sealing path, the send_and_await/*_saved
  reply routing, and the FROZEN-contract catalog field-name fix
  (`workflows`, not `catalog`). No Postgres required — drives a minimal FastAPI
  app + TestClient exactly like test_connected_fleet_registry_reads.

  Tier B (DB-gated, skips cleanly without Postgres): full deploy(mirror)/
  deploy(move)/deploy(secret)/deploy(persona)/offline-404 via TestClient against
  a real app, stubbing the agent socket so send_and_await returns a canned ack.
"""
import asyncio
import json

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers import user_recorder_ws
from routers.user_recorder_ws import _agent_meta, _connections


@pytest.fixture(autouse=True)
def _master_key():
    """Pin a valid Fernet master key for the whole module — the deploy sealing
    path (_seal_plaintext_for_agent) routes plaintext through the master key
    before re-sealing under the channel key, and the DB fixtures store master-
    sealed blobs. Reset the cached cipher so the pinned key takes effect."""
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


# ---------------------------------------------------------------------------
# Tier A — offline (no Postgres)
# ---------------------------------------------------------------------------
class _CapturingWS:
    """Fake agent socket that records every frame the coordinator sends and, on a
    save frame, schedules an out-of-band *_saved ack so send_and_await resolves."""

    def __init__(self, ack_extra=None):
        self.sent = []
        self.ack_extra = ack_extra or {}

    async def send_json(self, frame):
        self.sent.append(frame)
        mt = frame.get("type")
        reply_type = {
            "save_local_workflow": "local_workflow_saved",
            "save_local_secret": "local_secret_saved",
            "save_local_persona": "local_persona_saved",
        }.get(mt)
        if reply_type:
            ack = {"type": reply_type, "request_id": frame.get("request_id")}
            ack.update(self.ack_extra)
            # Resolve on the loop after send_json returns (mirror a real inbound frame).
            asyncio.get_event_loop().call_soon(
                user_recorder_ws._handle_agent_reply, "stub-fleet-a", ack
            )


def _seed_agent(agent_id, channel_key, *, local_capable=True):
    _connections[agent_id] = object()
    _agent_meta[agent_id] = {
        "role": "infrastructure",
        "is_trusted": True,
        "channel_key": channel_key,
        "local_workflows_capable": local_capable,
        "max_sessions": 5,
        "active_sessions": 0,
        "ai_keys_configured": False,
    }


def _drop_agent(agent_id):
    _connections.pop(agent_id, None)
    _agent_meta.pop(agent_id, None)


def test_seal_and_send_workflow_frame_offline():
    """A save_local_workflow frame carries the frozen field names and a
    credentials_encrypted that decrypts under the channel key back to the map."""
    from routers.fleet import _seal_plaintext_for_agent

    channel_key = Fernet.generate_key().decode()
    ws = _CapturingWS(ack_extra={"local_id": "wf_local_abc", "recipe_hash": "deadbeef"})
    _seed_agent("stub-fleet-a", channel_key)
    _connections["stub-fleet-a"] = ws  # replace the placeholder with the capturing ws

    async def _run():
        creds_map = {"API_KEY": "s3cr3t", "password": "pw"}
        sealed = _seal_plaintext_for_agent(json.dumps(creds_map), channel_key)
        frame = {
            "type": "save_local_workflow",
            "name": "Nightly scrape",
            "description": "",
            "steps": [{"type": "navigate", "config": {"url": "https://x"}}],
            "form_data": {},
            "declared_inputs": [],
            "credentials_encrypted": sealed,
            "persona": None,
            "execution_target": "local",
            "cloud_callable": True,
            "source_workflow_id": 42,
        }
        reply = await user_recorder_ws.send_and_await(
            "stub-fleet-a", frame,
            reply_type="local_workflow_saved",
            correlate_by="request_id",
        )
        return frame, sealed, reply

    try:
        frame, sealed, reply = asyncio.run(_run())
    finally:
        _drop_agent("stub-fleet-a")

    # Frozen-contract field names present.
    for field in (
        "type", "request_id", "name", "steps", "form_data", "declared_inputs",
        "credentials_encrypted", "persona", "execution_target", "cloud_callable",
        "source_workflow_id",
    ):
        assert field in frame, f"missing frozen field {field}"
    assert frame["execution_target"] == "local"
    assert frame["cloud_callable"] is True

    # credentials_encrypted decrypts under the channel key back to the map.
    plaintext = Fernet(channel_key.encode()).decrypt(sealed.encode()).decode()
    assert json.loads(plaintext) == {"API_KEY": "s3cr3t", "password": "pw"}

    # The out-of-band ack resolved send_and_await with the local_id.
    assert reply is not None and reply.get("local_id") == "wf_local_abc"
    assert reply.get("recipe_hash") == "deadbeef"


def test_send_and_await_resolves_all_saved_ack_types():
    """local_secret_saved / local_persona_saved acks resolve their futures via the
    new _dispatch routing (EDIT 4)."""
    channel_key = Fernet.generate_key().decode()

    async def _one(save_type, reply_type, ack_extra):
        ws = _CapturingWS(ack_extra=ack_extra)
        _connections["stub-fleet-a"] = ws
        _agent_meta["stub-fleet-a"] = {"channel_key": channel_key, "local_workflows_capable": True}
        try:
            frame = {"type": save_type, "key": "K"}
            return await user_recorder_ws.send_and_await(
                "stub-fleet-a", frame, reply_type=reply_type, correlate_by="request_id"
            )
        finally:
            _drop_agent("stub-fleet-a")

    r_sec = asyncio.run(_one("save_local_secret", "local_secret_saved", {"key": "API_KEY"}))
    assert r_sec.get("key") == "API_KEY"
    r_per = asyncio.run(
        _one("save_local_persona", "local_persona_saved", {"persona_local_id": "p_local_1"})
    )
    assert r_per.get("persona_local_id") == "p_local_1"


def test_resolve_deploy_target_gates():
    """_resolve_deploy_target: 404 offline, 409 not-capable, 409 no-key, key otherwise."""
    from fastapi import HTTPException
    from routers.fleet import _resolve_deploy_target

    # Offline → 404
    with pytest.raises(HTTPException) as e404:
        _resolve_deploy_target("nope")
    assert e404.value.status_code == 404

    channel_key = Fernet.generate_key().decode()
    # Connected but not local-capable → 409
    _connections["gate-a"] = object()
    _agent_meta["gate-a"] = {"channel_key": channel_key, "local_workflows_capable": False}
    try:
        with pytest.raises(HTTPException) as e409a:
            _resolve_deploy_target("gate-a")
        assert e409a.value.status_code == 409
    finally:
        _drop_agent("gate-a")

    # Capable but no channel key → 409
    _connections["gate-b"] = object()
    _agent_meta["gate-b"] = {"channel_key": None, "local_workflows_capable": True}
    try:
        with pytest.raises(HTTPException) as e409b:
            _resolve_deploy_target("gate-b")
        assert e409b.value.status_code == 409
    finally:
        _drop_agent("gate-b")

    # Capable + key → returns the key
    _seed_agent("gate-c", channel_key)
    try:
        assert _resolve_deploy_target("gate-c") == channel_key
    finally:
        _drop_agent("gate-c")


def test_handle_local_catalog_reads_workflows_field():
    """The FROZEN contract sends the entry list under `workflows`. The coordinator
    must ingest that field (the pre-existing bug read `catalog`, which would
    withdraw everything). Verified through the sanitizer used by the handler."""
    from services import local_workflow_catalog

    # The field-name selection lives in _handle_local_catalog; assert the exact
    # precedence: prefer `workflows`, fall back to `catalog`.
    msg_new = {"type": "local_catalog", "workflows": [{"local_id": "a"}]}
    msg_legacy = {"type": "local_catalog", "catalog": [{"local_id": "b"}]}

    def _pick(msg):
        entries = msg.get("workflows")
        if entries is None:
            entries = msg.get("catalog")
        return entries

    assert _pick(msg_new) == [{"local_id": "a"}]
    assert _pick(msg_legacy) == [{"local_id": "b"}]
    # And the handler-facing sanitizer accepts that list shape.
    assert local_workflow_catalog._sanitize_input_schema({"inputs": []}) == {"inputs": []}


# ---------------------------------------------------------------------------
# Tier B — full deploy via ASGI AsyncClient (Postgres-gated; skips without a DB)
#
# These are `async def` tests so they run on pytest-asyncio's SESSION loop (see
# pytest.ini) — the SAME loop the session-scoped db_engine/asyncpg connections
# are bound to. We drive the app via httpx.ASGITransport (in-loop) rather than the
# sync TestClient (which spins its own loop and would collide with asyncpg).
# ---------------------------------------------------------------------------
@pytest.fixture()
def deploy_app(db_engine, monkeypatch):
    """A FastAPI app wiring the fleet + automation routers against the throwaway
    schema, with require_platform_admin bypassed and get_db bound to the test
    engine. Skips (via db_engine → postgres_url) when no Postgres is reachable."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from database import get_db
    from security.dependencies import require_platform_admin, AuthContext
    from routers.fleet import router as fleet_router
    from routers.automation import router as automation_router

    maker = async_sessionmaker(bind=db_engine, expire_on_commit=False, class_=AsyncSession)

    async def _override_get_db():
        async with maker() as s:
            yield s

    async def _override_admin():
        return AuthContext(user_id=1, auth_method="jwt", is_platform_admin=True)

    app = FastAPI()
    app.include_router(fleet_router)
    app.include_router(automation_router, prefix="/api")
    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[require_platform_admin] = _override_admin

    return app, maker


def _client(app):
    """In-loop ASGI client (no separate event loop, unlike the sync TestClient)."""
    import httpx
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _seed_workflow_fixtures(maker, channel_key):
    """Create a workflow that references an exclusive secret + a shared secret + a
    persona, plus a SECOND workflow that also references the shared secret.

    Every key/name is UNIQUE per invocation (uuid suffix) so cross-test rows in
    the shared session schema (these tests commit; they aren't rolled back) never
    collide in the by-KEY ref-count scan. Returns a dict of ids + key strings."""
    import uuid as _uuid
    from models.automation_workflow import AutomationWorkflow
    from models.vault_secret import VaultSecret
    from models.persona import Persona
    from security.encryption import SecretEncryption

    tag = _uuid.uuid4().hex[:8]
    excl_key = f"EXCLUSIVE_KEY_{tag}"
    shared_key = f"SHARED_KEY_{tag}"

    async with maker() as s:
        excl = VaultSecret(key=excl_key, value_encrypted=SecretEncryption.encrypt_secret("excl-val"))
        shared = VaultSecret(key=shared_key, value_encrypted=SecretEncryption.encrypt_secret("shared-val"))
        persona = Persona(
            name=f"deploy-persona-{tag}",
            login_username="user@example.com",
            credentials_encrypted=SecretEncryption.encrypt_secret(json.dumps({"password": "pw"})),
            twofa_method="none",
            fingerprint={"user_agent": "UA"},
        )
        s.add_all([excl, shared, persona])
        await s.flush()

        wf = AutomationWorkflow(
            name=f"Deployable WF {tag}",
            workflow_type="recorded",
            steps=[
                {"type": "fill", "config": {"selector": "#k", "value": f"{{{{secret:{excl_key}}}}}"}},
                {"type": "fill", "config": {"selector": "#s", "value": f"{{{{vault:{shared_key}}}}}"}},
            ],
            form_data={},
            default_persona_id=persona.id,
        )
        other = AutomationWorkflow(
            name=f"Other WF {tag}",
            workflow_type="recorded",
            steps=[{"type": "fill", "config": {"selector": "#s", "value": f"{{{{vault:{shared_key}}}}}"}}],
            form_data={},
        )
        s.add_all([wf, other])
        await s.commit()
        return {
            "wf_id": wf.id, "other_id": other.id, "persona_id": persona.id,
            "excl_id": excl.id, "shared_id": shared.id,
            "excl_key": excl_key, "shared_key": shared_key,
        }


def _stub_send_and_await(monkeypatch, reply):
    """Patch user_recorder_ws.send_and_await (imported lazily inside the endpoint)
    to return a canned ack and record the dispatched frame on the patched
    function's ``last_frame`` attribute (read via user_recorder_ws.send_and_await)."""
    async def _fake(agent_id, frame, reply_type, correlate_by="request_id", timeout=120):
        out = dict(reply)
        out["request_id"] = frame.get("request_id")
        _fake.last_frame = frame
        return out

    _fake.last_frame = None
    monkeypatch.setattr(user_recorder_ws, "send_and_await", _fake)
    return _fake


async def test_deploy_workflow_mirror(deploy_app, monkeypatch):
    app, maker = deploy_app
    channel_key = Fernet.generate_key().decode()
    fx = await _seed_workflow_fixtures(maker, channel_key)
    wf_id = fx["wf_id"]
    _seed_agent("fleet-b1", channel_key)
    _stub_send_and_await(monkeypatch, {"type": "local_workflow_saved", "local_id": "wf_local_mir", "recipe_hash": "h1"})
    try:
        async with _client(app) as client:
            r = await client.post(
                "/api/fleet/agents/fleet-b1/deploy",
                json={"kind": "workflow", "id": wf_id, "include_deps": True, "mode": "mirror"},
            )
        assert r.status_code == 200, r.text
        assert r.json()["local_id"] == "wf_local_mir"
        assert r.json()["mode"] == "mirror"

        # The dispatched frame sealed the creds under the channel key.
        frame = user_recorder_ws.send_and_await.last_frame
        assert frame["type"] == "save_local_workflow"
        assert frame["source_workflow_id"] == wf_id
        creds = json.loads(Fernet(channel_key.encode()).decrypt(frame["credentials_encrypted"].encode()).decode())
        assert creds[fx["excl_key"]] == "excl-val"
        assert creds[fx["shared_key"]] == "shared-val"
        assert frame["persona"] is not None
    finally:
        _drop_agent("fleet-b1")

    # Mirror keeps everything on the coordinator + a LocalWorkflow row exists with source_workflow_id.
    from models.automation_workflow import AutomationWorkflow
    from models.vault_secret import VaultSecret
    from models.local_workflow import LocalWorkflow
    from sqlalchemy import select
    async with maker() as s:
        assert (await s.execute(select(AutomationWorkflow).where(AutomationWorkflow.id == wf_id))).scalar_one_or_none() is not None
        assert (await s.execute(select(VaultSecret).where(VaultSecret.id == fx["excl_id"]))).scalar_one_or_none() is not None
        assert (await s.execute(select(VaultSecret).where(VaultSecret.id == fx["shared_id"]))).scalar_one_or_none() is not None
        row = (await s.execute(select(LocalWorkflow).where(LocalWorkflow.agent_id == "fleet-b1", LocalWorkflow.local_id == "wf_local_mir"))).scalar_one_or_none()
        assert row is not None
        assert row.source_workflow_id == wf_id


async def test_deploy_workflow_move_refcounts_deps(deploy_app, monkeypatch):
    app, maker = deploy_app
    channel_key = Fernet.generate_key().decode()
    fx = await _seed_workflow_fixtures(maker, channel_key)
    wf_id, other_id, persona_id = fx["wf_id"], fx["other_id"], fx["persona_id"]
    excl_id, shared_id = fx["excl_id"], fx["shared_id"]
    _seed_agent("fleet-b2", channel_key)
    _stub_send_and_await(monkeypatch, {"type": "local_workflow_saved", "local_id": "wf_local_mov", "recipe_hash": "h2"})
    try:
        async with _client(app) as client:
            r = await client.post(
                "/api/fleet/agents/fleet-b2/deploy",
                json={"kind": "workflow", "id": wf_id, "include_deps": True, "mode": "move"},
            )
        assert r.status_code == 200, r.text
        assert r.json()["mode"] == "move"
    finally:
        _drop_agent("fleet-b2")

    from models.automation_workflow import AutomationWorkflow
    from models.vault_secret import VaultSecret
    from models.persona import Persona
    from sqlalchemy import select
    async with maker() as s:
        # Moved workflow deleted.
        assert (await s.execute(select(AutomationWorkflow).where(AutomationWorkflow.id == wf_id))).scalar_one_or_none() is None
        # Exclusive secret deleted; shared secret KEPT (still used by other_id).
        assert (await s.execute(select(VaultSecret).where(VaultSecret.id == excl_id))).scalar_one_or_none() is None
        assert (await s.execute(select(VaultSecret).where(VaultSecret.id == shared_id))).scalar_one_or_none() is not None
        # Persona deleted (no other workflow defaults to it).
        assert (await s.execute(select(Persona).where(Persona.id == persona_id))).scalar_one_or_none() is None
        # The other workflow survives.
        assert (await s.execute(select(AutomationWorkflow).where(AutomationWorkflow.id == other_id))).scalar_one_or_none() is not None


async def test_deploy_workflow_move_keeps_deps_shared_with_monitor(deploy_app, monkeypatch):
    """Move must NOT delete a persona/secret that a live monitor (Target) still
    uses — Target.persona_id (SET NULL) + Target.setup_steps {{secret:/vault:}}
    are ref-count consumers just like a second AutomationWorkflow (Decision D1)."""
    app, maker = deploy_app
    channel_key = Fernet.generate_key().decode()
    fx = await _seed_workflow_fixtures(maker, channel_key)
    wf_id, persona_id = fx["wf_id"], fx["persona_id"]
    excl_id, excl_key = fx["excl_id"], fx["excl_key"]

    # Delete the SECOND workflow so the shared secret is no longer workflow-shared;
    # instead, a monitor Target references both the workflow's persona AND its
    # otherwise-exclusive secret (via setup_steps). The move must keep both.
    from models.automation_workflow import AutomationWorkflow
    from models.target import Target
    from sqlalchemy import select
    async with maker() as s:
        other = (
            await s.execute(select(AutomationWorkflow).where(AutomationWorkflow.id == fx["other_id"]))
        ).scalar_one()
        await s.delete(other)
        tgt = Target(
            url="https://monitor.example.com",
            check_type="content",
            persona_id=persona_id,
            setup_steps=json.dumps({
                "steps": [
                    {"type": "fill", "config": {"selector": "#k", "value": f"{{{{secret:{excl_key}}}}}"}}
                ],
                "credentials": {},
            }),
        )
        s.add(tgt)
        await s.commit()

    _seed_agent("fleet-b5", channel_key)
    _stub_send_and_await(monkeypatch, {"type": "local_workflow_saved", "local_id": "wf_local_mon", "recipe_hash": "h5"})
    try:
        async with _client(app) as client:
            r = await client.post(
                "/api/fleet/agents/fleet-b5/deploy",
                json={"kind": "workflow", "id": wf_id, "include_deps": True, "mode": "move"},
            )
        assert r.status_code == 200, r.text
    finally:
        _drop_agent("fleet-b5")

    from models.vault_secret import VaultSecret
    from models.persona import Persona
    async with maker() as s:
        # Moved workflow deleted.
        assert (await s.execute(select(AutomationWorkflow).where(AutomationWorkflow.id == wf_id))).scalar_one_or_none() is None
        # Secret KEPT — the monitor's setup_steps still references it.
        assert (await s.execute(select(VaultSecret).where(VaultSecret.id == excl_id))).scalar_one_or_none() is not None
        # Persona KEPT — the monitor's persona_id still references it (SET NULL avoided).
        assert (await s.execute(select(Persona).where(Persona.id == persona_id))).scalar_one_or_none() is not None


async def test_deploy_persona_move_kept_when_monitor_uses_it(deploy_app, monkeypatch):
    """Standalone persona Move must be blocked when a Target references it."""
    app, maker = deploy_app
    channel_key = Fernet.generate_key().decode()

    from models.persona import Persona
    from models.target import Target
    from security.encryption import SecretEncryption
    from sqlalchemy import select
    async with maker() as s:
        p = Persona(
            name="monitor-persona",
            login_username="mon@example.com",
            credentials_encrypted=SecretEncryption.encrypt_secret(json.dumps({"password": "pw"})),
            twofa_method="none",
            fingerprint={"user_agent": "UA3"},
        )
        s.add(p)
        await s.flush()
        pid = p.id
        s.add(Target(url="https://mon2.example.com", check_type="content", persona_id=pid))
        await s.commit()

    _seed_agent("fleet-b6", channel_key)
    _stub_send_and_await(monkeypatch, {"type": "local_persona_saved", "persona_local_id": "p_local_mon"})
    try:
        async with _client(app) as client:
            r = await client.post(
                "/api/fleet/agents/fleet-b6/deploy",
                json={"kind": "persona", "id": pid, "mode": "move"},
            )
        assert r.status_code == 200, r.text
    finally:
        _drop_agent("fleet-b6")

    async with maker() as s:
        # Persona KEPT — a Target still references it.
        assert (await s.execute(select(Persona).where(Persona.id == pid))).scalar_one_or_none() is not None


async def test_deploy_secret_frame(deploy_app, monkeypatch):
    app, maker = deploy_app
    channel_key = Fernet.generate_key().decode()

    from models.vault_secret import VaultSecret
    from security.encryption import SecretEncryption
    async with maker() as s:
        sec = VaultSecret(key="STANDALONE", value_encrypted=SecretEncryption.encrypt_secret("standalone-val"))
        s.add(sec)
        await s.commit()
        sec_id = sec.id

    _seed_agent("fleet-b3", channel_key)
    _stub_send_and_await(monkeypatch, {"type": "local_secret_saved", "key": "STANDALONE"})
    try:
        async with _client(app) as client:
            r = await client.post(
                "/api/fleet/agents/fleet-b3/deploy",
                json={"kind": "secret", "id": sec_id, "mode": "mirror"},
            )
        assert r.status_code == 200, r.text
        frame = user_recorder_ws.send_and_await.last_frame
        assert frame["type"] == "save_local_secret"
        assert frame["key"] == "STANDALONE"
        val = Fernet(channel_key.encode()).decrypt(frame["value_encrypted"].encode()).decode()
        assert val == "standalone-val"
    finally:
        _drop_agent("fleet-b3")


async def test_deploy_persona_frame(deploy_app, monkeypatch):
    app, maker = deploy_app
    channel_key = Fernet.generate_key().decode()

    from models.persona import Persona
    from security.encryption import SecretEncryption
    async with maker() as s:
        p = Persona(
            name="standalone-persona",
            login_username="p@example.com",
            credentials_encrypted=SecretEncryption.encrypt_secret(json.dumps({"password": "pw"})),
            twofa_method="none",
            fingerprint={"user_agent": "UA2"},
        )
        s.add(p)
        await s.commit()
        persona_id = p.id

    _seed_agent("fleet-b4", channel_key)
    _stub_send_and_await(monkeypatch, {"type": "local_persona_saved", "persona_local_id": "p_local_x"})
    try:
        async with _client(app) as client:
            r = await client.post(
                "/api/fleet/agents/fleet-b4/deploy",
                json={"kind": "persona", "id": persona_id, "mode": "mirror"},
            )
        assert r.status_code == 200, r.text
        assert r.json()["persona_local_id"] == "p_local_x"
        frame = user_recorder_ws.send_and_await.last_frame
        assert frame["type"] == "save_local_persona"
        assert frame["name"] == "standalone-persona"
        assert frame["fingerprint"] == {"user_agent": "UA2"}
        creds = json.loads(Fernet(channel_key.encode()).decrypt(frame["creds_encrypted"].encode()).decode())
        assert creds["username"] == "p@example.com"
        assert creds["password"] == "pw"
    finally:
        _drop_agent("fleet-b4")


async def test_deploy_404_when_offline(deploy_app):
    app, maker = deploy_app

    from models.vault_secret import VaultSecret
    from security.encryption import SecretEncryption
    async with maker() as s:
        sec = VaultSecret(key="OFFLINE_SECRET", value_encrypted=SecretEncryption.encrypt_secret("v"))
        s.add(sec)
        await s.commit()
        sec_id = sec.id

    async with _client(app) as client:
        r = await client.post(
            "/api/fleet/agents/ghost-agent/deploy",
            json={"kind": "secret", "id": sec_id, "mode": "mirror"},
        )
    assert r.status_code == 404, r.text
