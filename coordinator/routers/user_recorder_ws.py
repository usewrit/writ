"""
Unified WebSocket endpoint for all recorder types.

Both user-hosted recorders (wto_ OAuth tokens) and infrastructure
direct-fleet recorders (JWT service tokens) connect here via persistent
WSS to receive workflow and AI task dispatches.

Security model:
- Auth via first-message token (not query params — avoids server log leakage)
- Token type auto-detected: JWT (infrastructure) or wto_ (user-hosted)
- Shared validation via utils.recorder_auth.validate_recorder_token()
- Per-agent channel key for credential encryption in transit
- All task dispatches scoped to the owning agent (enforced at DB level)
- Incoming results validated: task_id must be assigned to this agent
- Heartbeat throttled, message sizes limited
- Connections audited
"""
import asyncio
import hmac
import json
import logging
import os
import secrets
import time
import uuid
from datetime import datetime, timezone
from typing import Dict, Optional, Set
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from database import AsyncSessionLocal
from security.dependencies import get_auth_context, AuthContext
from utils.recorder_auth import validate_recorder_token, RecorderAuthContext

logger = logging.getLogger(__name__)


def _plaintext_channel_key_allowed() -> bool:
    """Whether a plaintext (unencrypted) channel-key fallback may be used.

    Plaintext channel keys only exist when no master SECRET_ENCRYPTION_KEY is
    configured (pure local-dev). The fallback is gated on BOTH conditions:
      1. environment is explicitly 'development', AND
      2. no master key is configured.

    When a master key IS configured but decryption fails, we MUST fail closed —
    returning plaintext (or a raw blob) in that case would silently downgrade
    transit encryption for credentials and mask key-rotation/corruption bugs.
    """
    return (
        settings.environment.lower() == "development"
        and not settings.secret_encryption_key
    )

router = APIRouter(tags=["Recorder WS"])

# In-memory registries
_connections: Dict[str, WebSocket] = {}       # agent_id -> ws
_agent_meta: Dict[str, dict] = {}             # agent_id -> metadata
_pending_results: Dict[str, asyncio.Future] = {}  # task_id -> Future
_dispatched_tasks: Dict[str, Set[str]] = {}   # agent_id -> set of task_ids assigned

# Live-recording session relay (browser /ws/record <-> agent persistent WS).
# Replaces the removed Node ws-gateway's multiplexing: every interactive
# recording/preview session gets a session_id and is tunneled over the chosen
# agent's single persistent WS as {channel:"session", session_id, msg} JSON
# frames + [0x01]-prefixed binary screencast. The coordinator IS the gateway now.
#   session_id -> {"browser_ws": WebSocket, "agent_id": str,
#                  "opened": asyncio.Event, "closed": asyncio.Event}
_record_sessions: Dict[str, dict] = {}

# --- Per-identity /ws/record abuse controls (security findings #4 + #13) ------
# The browser /ws/record socket picks a fleet agent and opens a live recording
# session on its persistent WS. Without a cap, a single (now-authenticated, per
# #4) caller could open unbounded sessions and exhaust every agent's recording
# slots, starving legitimate dispatch. We bound concurrent record sessions per
# identity AND rate-limit new opens per identity. Identity is the authenticated
# JWT subject resolved from the ticket/token (never client-asserted).
MAX_RECORD_SESSIONS_PER_IDENTITY = 4       # concurrent live record sessions
RECORD_CONN_RATE_MAX = 20                  # new opens allowed per window
RECORD_CONN_RATE_WINDOW = 60.0             # seconds
_record_sessions_by_identity: Dict[str, int] = {}   # identity_key -> live count
_record_conn_times: Dict[str, list] = {}            # identity_key -> [ts, ...]


def _browser_ws_identity_key(identity: dict) -> str:
    """Stable per-caller key from a resolved /ws/record JWT payload.

    Uses the token subject / user id — the authenticated identity, never a
    client-supplied value. Falls back to a constant bucket so a payload without
    the expected claims still shares one (conservative) cap bucket rather than
    escaping the limiter."""
    if not isinstance(identity, dict):
        return "anonymous"
    return str(
        identity.get("sub")
        or identity.get("user_id")
        or identity.get("uid")
        or "authenticated"
    )


def _record_rate_limited(identity_key: str) -> bool:
    """Sliding-window connection rate limit for /ws/record opens by identity.

    Returns True when this identity has opened more than RECORD_CONN_RATE_MAX
    sockets within the trailing RECORD_CONN_RATE_WINDOW. Prunes stale timestamps
    as a side effect and records the current attempt when it is allowed."""
    now = time.time()
    times = [t for t in _record_conn_times.get(identity_key, []) if now - t < RECORD_CONN_RATE_WINDOW]
    if len(times) >= RECORD_CONN_RATE_MAX:
        _record_conn_times[identity_key] = times
        return True
    times.append(now)
    _record_conn_times[identity_key] = times
    return False

# Correlation registry for the backend-orchestrated AI session loop. Keyed by a
# correlation id (request_id for per-turn agent_action, session_id for
# ai_session_open/ai_session_close acks) — strictly separate from
# _pending_results (task_id-keyed, also consumed by the DB-completion fallback).
# Correlation ids are UUIDs so they never collide with numeric task_ids.
_pending_replies: Dict[str, asyncio.Future] = {}  # correlation_id -> Future
# correlation_id -> agent_id, so a mid-loop disconnect can fail in-flight turns
# fast instead of stalling the orchestrator to the per-turn timeout.
_pending_reply_agent: Dict[str, str] = {}

MAX_MESSAGE_SIZE = 10 * 1024 * 1024  # 10MB
MIN_HEARTBEAT_INTERVAL = 10  # seconds
# App-level dead-peer detection. WS protocol keepalive is disabled at the server
# (serve.py) because the light writ-agent pongs a Ping with a binary frame the
# uvicorn keepalive rejects. A live agent heartbeats every ~25s and answers our
# 45s ping, both advancing last_heartbeat; if a socket goes silent past this
# window it is presumed dead and closed by _ping_loop.
AGENT_SILENCE_TIMEOUT = 120  # seconds with no heartbeat/pong before force-close


class AgentRevokedError(Exception):
    """Raised when a connecting agent's DB row has been revoked by the user.

    A revoked agent must NOT be auto-reactivated on reconnect — the user
    explicitly disconnected it. The WS handler turns this into an auth_error.
    """


class AgentOwnershipError(Exception):
    """Raised when a connecting caller tries to reuse/reactivate an agent row
    that is already owned by a DIFFERENT user.

    Security finding #31 (agent-id identity takeover): an existing agent row was
    previously reused purely by client-supplied ``requested_agent_id`` and
    ``_stamp_agent_identity`` overwrote ``meta['user_id']`` with the new caller's
    with no ownership check, letting any authenticated user seize another user's
    agent (and its channel key / dispatch stream). We now refuse the reuse when
    the row's stored owner does not match the connecting user; the WS handler
    turns this into an auth_error/conflict close. The normal same-owner reconnect
    path (matching or absent stored owner) is unaffected.
    """


# ---------------------------------------------------------------------------
# Public query helpers
# ---------------------------------------------------------------------------
def get_connected_recorders(
    role: Optional[str] = None,
) -> list[dict]:
    """Get all WS-connected recorders (single global fleet), optionally by role.

    Args:
        role: Optional "infrastructure" or "user-hosted" filter.
    """
    results = []
    for agent_id, meta in _agent_meta.items():
        if agent_id in _connections:
            if role and meta.get("role") != role:
                continue
            results.append({
                "agent_id": agent_id,
                "max_sessions": meta.get("max_sessions", 2),
                "active_sessions": meta.get("active_sessions", 0),
                "platform": meta.get("platform", "unknown"),
                "version": meta.get("version"),
                "ai_provider": meta.get("ai_provider"),
                "ai_keys_configured": bool(meta.get("ai_keys_configured", False)),
                "connected_at": meta.get("connected_at"),
                "role": meta.get("role", "user-hosted"),
                # Stamped owner (set by _stamp_agent_identity); lets BYO routing
                # scope candidates to the requesting owner in a shared fleet (#32).
                "user_id": meta.get("user_id"),
            })
    return results


def get_connected_recorder_meta(agent_id: str) -> Optional[dict]:
    """Live in-process projection for ONE connected agent, or ``None``.

    O(1) lookup into the directly-connected fleet — the same
    ``_connections`` / ``_agent_meta`` the dispatcher reads. Returns only the
    safe display/routing fields (never ``channel_key`` / ``user_id`` /
    ``token_prefix``), so callers can read an agent's live role, session load, or
    advertised capabilities without a registry round-trip. Replaces the old
    ``gateway_registry.get_recorder_meta`` Redis read, which is always empty now
    that the external ws-gateway no longer exists.
    """
    if agent_id not in _connections:
        return None
    meta = _agent_meta.get(agent_id)
    if meta is None:
        return None
    return {
        "agent_id": agent_id,
        "role": meta.get("role", "user-hosted"),
        "max_sessions": meta.get("max_sessions", 2),
        "active_sessions": meta.get("active_sessions", 0),
        "platform": meta.get("platform", "unknown"),
        "version": meta.get("version"),
        "ai_provider": meta.get("ai_provider"),
        "ai_keys_configured": bool(meta.get("ai_keys_configured", False)),
        "local_workflows_capable": bool(meta.get("local_workflows_capable", False)),
        "connected_at": meta.get("connected_at"),
        "is_trusted": bool(meta.get("is_trusted", False)),
    }


def get_connected_recorder(
    role: Optional[str] = None,
) -> Optional[dict]:
    """Get the best available WS-connected recorder (single global fleet).

    Args:
        role: Optional "infrastructure" or "user-hosted" filter.
    """
    recorders = get_connected_recorders(role=role)
    if not recorders:
        return None
    recorders.sort(key=lambda r: r["max_sessions"] - r["active_sessions"], reverse=True)
    best = recorders[0]
    if best["max_sessions"] - best["active_sessions"] <= 0:
        return None
    return best


def get_all_connected_recorders() -> list[dict]:
    """Get all WS-connected recorders."""
    return [{"agent_id": aid, **meta} for aid, meta in _agent_meta.items() if aid in _connections]


def get_agent_live_identity(agent_id: str) -> dict:
    """Return the live connection's {user_id, token_prefix} for an agent, if it
    is currently connected. Lets the disconnect endpoint revoke the exact token
    even for agents registered before identity was persisted on the DB row."""
    meta = _agent_meta.get(agent_id) or {}
    return {
        "user_id": meta.get("user_id"),
        "token_prefix": meta.get("token_prefix"),
    }


# ---------------------------------------------------------------------------
# Dispatch (server → recorder)
# ---------------------------------------------------------------------------
# Message types that carry a task_id and expect a correlated `task_result` reply
# from the agent. Every writ-agent connects DIRECTLY to this process (/ws/ai-gateway),
# so the reply lands on the same socket and is resolved via the in-process
# _pending_results Future registry — there is no external ws-gateway round-trip.
_REPLY_AWAITED_TYPES = (
    "execute_workflow", "execute_ai_task", "run_local_workflow",
    # Streaming start: the agent acks with a task_result{task_id:"stream-<key>"};
    # StreamingManager.start_on_agent blocks on it via push_to_recorder.
    "start_streaming_session",
)


# Coordinator-authoritative run-slot reservations, keyed by task_id → agent_id.
# We NEVER trust an OSS agent's self-reported active_sessions for scheduling (an
# untrusted agent could report 0 to hog dispatch or dodge the queue). Instead the
# coordinator owns active_sessions: reserved AT PICK TIME (atomically, under the
# dispatcher's pick lock) so concurrent picks see each other's load and either
# spread across the fleet or — once every agent is full — get None and QUEUE;
# released when the task reaches a terminal state. task_id keying makes reserve/
# release idempotent and symmetric across the direct-run and queue-drain paths.
_reserved_slots: Dict[str, str] = {}  # task_id -> agent_id


def reserve_agent_slot(agent_id: str, task_id) -> None:
    """Reserve one capacity slot on ``agent_id`` for ``task_id`` (idempotent).

    Increments the agent's coordinator-owned ``active_sessions`` so the next pick
    sees the load. Call synchronously while holding the dispatcher pick lock.
    """
    tid = str(task_id)
    if not tid or tid in _reserved_slots:
        return
    m = _agent_meta.get(agent_id)
    if m is not None:
        m["active_sessions"] = int(m.get("active_sessions", 0)) + 1
        _reserved_slots[tid] = agent_id


def release_agent_slot(task_id) -> None:
    """Release the slot reserved for ``task_id`` (idempotent, floored at 0).

    No-op if the task never reserved (HTTP-pool runs, or a task whose agent already
    disconnected — its meta, and thus its count, was dropped)."""
    tid = str(task_id)
    agent_id = _reserved_slots.pop(tid, None)
    if agent_id is None:
        return
    m = _agent_meta.get(agent_id)
    if m is not None and m.get("active_sessions"):
        m["active_sessions"] = max(0, int(m["active_sessions"]) - 1)


def purge_agent_reservations(agent_id: str) -> None:
    """Drop all slot reservations for a departed agent. Its ``active_sessions``
    count vanishes with its meta on disconnect; this keeps ``_reserved_slots`` from
    leaking stale task ids (whose in-flight runs are lost with the socket)."""
    for tid in [t for t, a in _reserved_slots.items() if a == agent_id]:
        _reserved_slots.pop(tid, None)


async def push_to_recorder(agent_id: str, message: dict) -> Optional[dict]:
    """
    Push a task to a DIRECTLY-connected recorder (user-hosted or infrastructure fleet).

    The agent holds a persistent socket in `_connections` (it connected to
    /ws/ai-gateway on THIS process). We write the frame straight to that socket;
    for reply-awaited types (execute_workflow / execute_ai_task / run_local_workflow)
    we register an in-process asyncio.Future keyed by task_id and await the agent's
    `task_result` — correlated in `_handle_task_result` on the inbound message loop.
    No Redis relay / external ws-gateway indirection is involved.

    Credentials in the message config are re-encrypted with the agent's channel key
    before sending. Returns None when the agent is not connected.
    """
    ws = _connections.get(agent_id)
    if not ws:
        # Not connected to this coordinator. There is no separate ws-gateway to
        # relay through (this process IS the gateway), so the agent is simply
        # unreachable — fail fast rather than publish into a dead Redis channel.
        logger.warning(f"push_to_recorder: agent {agent_id} is not connected")
        return None

    meta = _agent_meta.get(agent_id, {})
    task_id = str(message.get("task_id", ""))

    # Re-encrypt credentials with the agent's channel key.
    # The server decrypts with its Fernet master key, re-encrypts with the
    # per-agent channel key so the agent can decrypt on its end.
    config = message.get("config", {})
    if config.get("credentials_encrypted"):
        channel_key = meta.get("channel_key")
        if not channel_key:
            logger.error(f"No channel key for agent {agent_id} — cannot dispatch credentials securely")
            return {"error": "no_channel_key", "success": False}
        try:
            config["credentials_encrypted"] = _reencrypt_for_agent(
                config["credentials_encrypted"], channel_key
            )
        except Exception as e:
            logger.error(f"Failed to re-encrypt credentials for agent {agent_id}: {e}")
            return {"error": "credential_encryption_failed", "success": False}

    # Track which tasks belong to this agent (for result validation)
    if task_id:
        _dispatched_tasks.setdefault(agent_id, set()).add(task_id)

    try:
        await ws.send_json(message)
    except Exception as e:
        logger.error(f"Failed to send to user recorder {agent_id}: {e}")
        if task_id:
            _dispatched_tasks.get(agent_id, set()).discard(task_id)
        return None

    if message.get("type") in _REPLY_AWAITED_TYPES and task_id:
        # NOTE: the capacity slot for this run is reserved at PICK time (see
        # reserve_agent_slot, called under the dispatcher pick lock) and released
        # when the task reaches a terminal state — NOT here. Reserving here would be
        # too late: concurrent picks would all read load=0 and pile onto one agent.
        future = asyncio.get_event_loop().create_future()
        _pending_results[task_id] = future
        try:
            result = await asyncio.wait_for(future, timeout=300)
            return result
        except asyncio.TimeoutError:
            logger.warning(f"Timeout waiting for result from {agent_id} task {task_id}")
            return {"error": "timeout", "success": False}
        finally:
            _pending_results.pop(task_id, None)
            _dispatched_tasks.get(agent_id, set()).discard(task_id)

    return {"status": "sent"}


async def send_and_await(
    agent_id: str,
    message: dict,
    reply_type: str,
    correlate_by: str = "request_id",
    timeout: float = 120,
) -> Optional[dict]:
    """Send a message to an agent and await a correlated reply.

    Generic request/reply primitive for the backend-orchestrated AI session
    loop. Unlike push_to_recorder (which only awaits task_result by task_id for
    execute_workflow/execute_ai_task), this correlates by request_id (per-turn
    agent_action) or session_id (ai_session_open/ai_session_close acks).

    - If message[correlate_by] is absent, a UUID is generated and injected.
    - Resolves when an inbound reply of `reply_type` carries the same
      correlate_by value (routed by _handle_agent_reply).
    - On timeout returns {'error':'timeout','success':False}.
    - On agent disconnect mid-flight returns {'error':'agent_disconnected',...}.

    NOTE: no credential re-encryption (agent_action/session frames carry no
    credentials) and no _dispatched_tasks tracking (these are not DB tasks).
    """
    corr_id = str(message.get(correlate_by) or uuid.uuid4())
    message[correlate_by] = corr_id

    ws = _connections.get(agent_id)
    if not ws:
        # The agent connects directly to this process, so an absent socket means it
        # is simply not connected. There is no external ws-gateway to delegate to.
        logger.warning(
            f"send_and_await: agent {agent_id} is not connected "
            f"(correlate_by={correlate_by})"
        )
        return {"error": "agent_disconnected", "success": False}

    future = asyncio.get_event_loop().create_future()
    _pending_replies[corr_id] = future
    _pending_reply_agent[corr_id] = agent_id

    try:
        await ws.send_json(message)
    except Exception as e:
        logger.error(f"send_and_await: failed to send to agent {agent_id}: {e}")
        _pending_replies.pop(corr_id, None)
        _pending_reply_agent.pop(corr_id, None)
        return {"error": "send_failed", "success": False}

    try:
        result = await asyncio.wait_for(future, timeout=timeout)
        return result
    except asyncio.TimeoutError:
        logger.warning(
            f"send_and_await: timeout waiting for {reply_type} ({correlate_by}={corr_id}) "
            f"from {agent_id}"
        )
        return {"error": "timeout", "success": False}
    finally:
        _pending_replies.pop(corr_id, None)
        _pending_reply_agent.pop(corr_id, None)


async def push_fire_and_forget(agent_id: str, message: dict) -> bool:
    ws = _connections.get(agent_id)
    if not ws:
        return False
    try:
        await ws.send_json(message)
        return True
    except Exception:
        return False


# Backward-compat alias (old name used by automation.py imports)
push_to_user_recorder = push_to_recorder


async def disconnect_agent(agent_id: str, reason: str = "disconnected"):
    ws = _connections.get(agent_id)
    if ws:
        # The agent holds a direct socket on this process — close it in-band.
        try:
            await ws.send_json({"type": "disconnect", "reason": reason})
            await ws.close(code=4003, reason=reason)
        except Exception:
            pass
    _connections.pop(agent_id, None)
    _agent_meta.pop(agent_id, None)
    _dispatched_tasks.pop(agent_id, None)
    purge_agent_reservations(agent_id)
    _fail_pending_replies_for_agent(agent_id)


def _handle_agent_reply(agent_id: str, msg: dict, correlate_by: str = "request_id"):
    """Resolve a _pending_replies future for a correlated agent reply.

    Mirror of the _handle_task_result resolution branch but against the
    request_id/session_id-keyed map. No DB fallback — unmatched replies (for a
    closed/timed-out turn) are dropped.
    """
    corr = str(msg.get(correlate_by, "") or "")
    if not corr:
        return
    future = _pending_replies.get(corr)
    if future and not future.done():
        future.set_result(msg)
    _pending_replies.pop(corr, None)
    _pending_reply_agent.pop(corr, None)


def _fail_pending_replies_for_agent(agent_id: str):
    """Fail any in-flight orchestrator turns targeting a disconnected agent so
    the backend loop fails fast instead of stalling to the per-turn timeout."""
    stale = [cid for cid, aid in _pending_reply_agent.items() if aid == agent_id]
    for cid in stale:
        future = _pending_replies.pop(cid, None)
        _pending_reply_agent.pop(cid, None)
        if future and not future.done():
            future.set_result({"error": "agent_disconnected", "success": False})


# ---------------------------------------------------------------------------
# Credential re-encryption
# ---------------------------------------------------------------------------
def _reencrypt_for_agent(credentials_encrypted: str, channel_key: str) -> str:
    """
    Decrypt credentials with the server's Fernet master key,
    re-encrypt with the per-agent channel key.
    Never exposes plaintext outside this function.
    """
    from cryptography.fernet import Fernet
    from security.encryption import SecretEncryption

    plaintext = SecretEncryption.decrypt_secret(credentials_encrypted)
    agent_fernet = Fernet(channel_key.encode())
    return agent_fernet.encrypt(plaintext.encode()).decode()


_app_redis = None

def set_redis_client(redis_client):
    """Called at startup to share the app's Redis connection."""
    global _app_redis
    _app_redis = redis_client


async def _get_channel_key_for_token(token_prefix: str) -> Optional[str]:
    """Retrieve the per-agent channel key from Redis, decrypt it."""
    r = _app_redis
    if not r:
        # Fallback: shared in-process client (no external Redis).
        try:
            from utils.redis_client import get_redis
            r = get_redis()
        except Exception as e:
            logger.error(f"Failed to obtain in-process Redis for channel key: {e}")
            return None

    try:
        stored = await r.get(f"agent_channel_key:{token_prefix}")
        if not stored:
            logger.warning(f"No channel key found in Redis for token prefix {token_prefix}")
            return None

        # Try to decrypt (key was encrypted with master Fernet key)
        try:
            from security.encryption import SecretEncryption
            return SecretEncryption.decrypt_secret(stored)
        except Exception:
            # Fallback: key was stored as plaintext. Only legitimate in pure
            # local-dev where no master key is configured. If a master key IS
            # configured, decryption failure is a real error (rotation/corruption)
            # — fail closed rather than trusting an undecryptable blob.
            if _plaintext_channel_key_allowed() and len(stored) == 44 and stored.endswith('='):
                logger.warning(
                    "DEV ONLY: using PLAINTEXT channel key for token prefix %s "
                    "(no SECRET_ENCRYPTION_KEY configured)", token_prefix
                )
                return stored
            logger.error(
                f"Channel key for {token_prefix} could not be decrypted "
                f"(master key configured: {bool(settings.secret_encryption_key)}); failing closed"
            )
            return None
    except Exception as e:
        logger.error(f"Failed to retrieve channel key from Redis: {e}")
        return None


# ---------------------------------------------------------------------------
# Channel key helpers
# ---------------------------------------------------------------------------
async def _get_channel_key_for_agent_readonly(agent_id: str) -> Optional[str]:
    """Read-only lookup of an agent's channel key by agent_id. Unlike
    _get_or_create_*, this NEVER generates a new key — minting one the agent
    doesn't have would make its _decrypt_with_channel_key fail. Returns None if
    absent so callers can fail loudly instead of shipping undecryptable creds."""
    r = _app_redis
    if not r:
        try:
            from utils.redis_client import get_redis
            r = get_redis()
        except Exception as e:
            logger.error(f"Read channel key: no in-process Redis ({e})")
            return None
    try:
        stored = await r.get(f"agent_channel_key:{agent_id}")
        if not stored:
            return None
        try:
            from security.encryption import SecretEncryption
            return SecretEncryption.decrypt_secret(stored)
        except Exception:
            # Plaintext fallback only when no master key is configured (dev).
            # With a master key present, a decrypt failure means fail closed.
            if _plaintext_channel_key_allowed() and len(stored) == 44 and stored.endswith('='):
                logger.warning(
                    "DEV ONLY: using PLAINTEXT channel key for agent %s "
                    "(no SECRET_ENCRYPTION_KEY configured)", agent_id
                )
                return stored
            logger.error(
                f"Read channel key for {agent_id}: undecryptable "
                f"(master key configured: {bool(settings.secret_encryption_key)}); failing closed"
            )
            return None
    except Exception as e:
        logger.error(f"Read channel key for {agent_id} failed: {e}")
        return None


async def mirror_channel_key_to_agent_id(token_prefix: str, agent_id: str) -> bool:
    """Copy the device-flow channel key (stored under token_prefix) into the
    agent_id namespace.

    The ws-gateway dispatch path knows only agent_id, but the agent's REAL channel
    key was issued during the OAuth device flow and stored under token_prefix. This
    bridges the two so the gateway path re-encrypts with the SAME key the agent
    holds — otherwise it mints a fresh, mismatched key and the agent can't decrypt
    credentials ({{secret:...}} resolves empty). Called at agent_connect, where
    both agent_id (baked into the gateway JWT) and token_prefix are known."""
    if not token_prefix or not agent_id:
        return False
    r = _app_redis
    if not r:
        try:
            from utils.redis_client import get_redis
            r = get_redis()
        except Exception as e:
            logger.error(f"Mirror channel key: no in-process Redis ({e})")
            return False
    key = await _get_channel_key_for_token(token_prefix)
    if not key:
        return False
    try:
        from security.encryption import SecretEncryption
        try:
            stored = SecretEncryption.encrypt_secret(key)
        except Exception as enc_err:
            # Plaintext mirror only in pure dev (no master key). With a master key
            # configured, fail closed rather than mirroring an unencrypted key.
            if not _plaintext_channel_key_allowed():
                logger.error(
                    f"Could not encrypt mirrored channel key for {agent_id} "
                    f"(master key configured: {bool(settings.secret_encryption_key)}); "
                    f"failing closed: {enc_err}"
                )
                return False
            logger.warning(
                "DEV ONLY: mirroring PLAINTEXT channel key for agent %s "
                "(no SECRET_ENCRYPTION_KEY configured)", agent_id
            )
            stored = key
        await r.set(f"agent_channel_key:{agent_id}", stored)
        logger.info(f"Mirrored channel key to agent_id namespace for {agent_id}")
        return True
    except Exception as e:
        logger.warning(f"mirror_channel_key_to_agent_id failed for {agent_id}: {e}")
        return False


async def _get_or_create_channel_key_for_agent(agent_id: str) -> Optional[str]:
    """
    Get or create a per-agent Fernet channel key.

    For JWT (infrastructure) agents that don't go through device flow,
    we generate a channel key on first WS connection and store it in Redis.
    On reconnect, we reuse the existing key.
    """
    r = _app_redis
    if not r:
        try:
            from utils.redis_client import get_redis
            r = get_redis()
        except Exception as e:
            logger.error(f"Failed to obtain in-process Redis for channel key: {e}")
            return None

    redis_key = f"agent_channel_key:{agent_id}"

    try:
        # Check for existing key
        stored = await r.get(redis_key)
        if stored:
            try:
                from security.encryption import SecretEncryption
                return SecretEncryption.decrypt_secret(stored)
            except Exception:
                # Fallback: stored as plaintext. Only legitimate in pure dev with
                # no master key. With a master key configured, a decrypt failure is
                # a real error — fail closed instead of silently re-keying, which
                # would orphan the agent's existing key and could mask corruption.
                if _plaintext_channel_key_allowed() and len(stored) == 44 and stored.endswith('='):
                    logger.warning(
                        "DEV ONLY: using PLAINTEXT channel key for agent %s "
                        "(no SECRET_ENCRYPTION_KEY configured)", agent_id
                    )
                    return stored
                if settings.secret_encryption_key:
                    logger.error(
                        f"Channel key for {agent_id} is undecryptable while a master "
                        f"key is configured; failing closed (not re-keying)"
                    )
                    return None
                logger.error(f"Channel key for {agent_id} is not valid")
                # Fall through to generate a new one (dev only, no master key)

        # Generate new channel key
        from cryptography.fernet import Fernet as _Fernet
        channel_key_raw = _Fernet.generate_key()
        channel_key_plaintext = channel_key_raw.decode()

        # Store encrypted
        try:
            from security.encryption import SecretEncryption
            channel_key_encrypted = SecretEncryption.encrypt_secret(channel_key_plaintext)
        except Exception as enc_err:
            # Plaintext-at-rest fallback only in pure dev (no master key). If a
            # master key is configured but encryption failed, fail closed rather
            # than persisting an unencrypted channel key in Redis.
            if not _plaintext_channel_key_allowed():
                logger.error(
                    f"Could not encrypt new channel key for {agent_id} "
                    f"(master key configured: {bool(settings.secret_encryption_key)}); "
                    f"failing closed: {enc_err}"
                )
                return None
            logger.warning(
                "DEV ONLY: storing PLAINTEXT channel key for agent %s "
                "(no SECRET_ENCRYPTION_KEY configured)", agent_id
            )
            channel_key_encrypted = channel_key_plaintext

        await r.set(redis_key, channel_key_encrypted)
        logger.info(f"Generated new channel key for agent {agent_id}")
        return channel_key_plaintext

    except Exception as e:
        logger.error(f"Failed to get/create channel key for {agent_id}: {e}")
        return None


# ---------------------------------------------------------------------------
# WebSocket endpoint (unified: user-hosted + infrastructure direct fleet)
# ---------------------------------------------------------------------------
async def _recorder_ws_handler(websocket: WebSocket):
    """
    Unified persistent WebSocket for all recorder types.

    Auth: first-message token (not query param — avoids log leakage).
    Supports both JWT service tokens (infrastructure) and wto_ OAuth
    tokens (user-hosted). Token type is auto-detected by prefix.
    """
    await websocket.accept()

    # --- Phase 1: Authentication (dual-dialect) ---
    #
    # Two agent dialects connect here:
    #  A) Coordinator dialect (P4/P5 device-flow + stub smokes): the FIRST WS frame
    #     is {"type":"auth","token":…}. We reply {"type":"auth_ok",…}.
    #  B) Stock cloud writ-agent dialect (saas_bridge.rs): the gateway JWT rides in
    #     the WS `Authorization: Bearer` header, the agent then WAITS to *receive* a
    #     {"type":"welcome","agent_id":…} frame before it sends anything, and its
    #     first agent→server frame is `monitor_register` (never an `auth` frame).
    #
    # We auto-detect: read the first frame; if it is `type:"auth"`, use its token
    # (dialect A). Otherwise fall back to the `Authorization: Bearer` header token
    # (dialect B) — validated by the SAME validate_recorder_token path — and REPLAY
    # that first frame (e.g. monitor_register) as the first in-loop message instead
    # of rejecting it. In BOTH cases we send `welcome` after auth so a stock agent
    # adopts the coordinator-assigned id; dialect A additionally gets `auth_ok`.
    auth_msg: dict = {}
    buffered_msg: Optional[dict] = None   # dialect-B first frame, replayed in-loop
    used_header_auth = False

    try:
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=15)
        first_frame = json.loads(raw)
    except asyncio.TimeoutError:
        await websocket.send_json({"type": "auth_error", "reason": "Auth timeout"})
        await websocket.close(code=4001, reason="Auth failed")
        return
    except json.JSONDecodeError:
        # A non-JSON first frame can't be dialect A; still allow header auth so a
        # stock agent whose first frame is malformed isn't hard-rejected on parse.
        first_frame = None

    if isinstance(first_frame, dict) and first_frame.get("type") == "auth":
        # Dialect A: first-frame auth.
        auth_msg = first_frame
        token = auth_msg.get("token", "")
        auth = await validate_recorder_token(token)
        if not auth:
            await websocket.send_json({"type": "auth_error", "reason": "Invalid or expired token"})
            await websocket.close(code=4001, reason="Invalid token")
            return
    else:
        # Dialect B: authenticate via the Authorization: Bearer header, and keep the
        # received frame (if any) as the first in-loop message.
        used_header_auth = True
        buffered_msg = first_frame if isinstance(first_frame, dict) else None
        header = websocket.headers.get("authorization", "") or websocket.headers.get("Authorization", "")
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        auth = await validate_recorder_token(token) if token else None
        if not auth:
            await websocket.send_json({"type": "auth_error", "reason": "Invalid or expired token"})
            await websocket.close(code=4001, reason="Invalid token")
            return
        # The header-dialect agent supplies its stable id in the ?agent_id= query
        # param (or the signed JWT's agent_id claim). Seed auth_msg so the shared
        # registration path below picks it up exactly like the first-frame agent_id.
        agent_id_hint = websocket.query_params.get("agent_id") or auth.agent_id or ""
        if agent_id_hint:
            auth_msg = {"agent_id": agent_id_hint}

    user_id = auth.user_id
    is_trusted = auth.is_trusted
    role = auth.role

    # --- Phase 3: Retrieve or generate channel key ---
    channel_key = None
    if auth.token_prefix:
        # wto_ token: channel key was generated during device flow, stored by token_prefix
        channel_key = await _get_channel_key_for_token(auth.token_prefix)
    if not channel_key:
        # JWT token or missing key: generate/retrieve by agent_id
        requested_agent_id = auth_msg.get("agent_id") or f"writ-{uuid.uuid4().hex[:12]}"
        channel_key = await _get_or_create_channel_key_for_agent(requested_agent_id)
    if not channel_key:
        logger.warning(f"No channel key available for {role} recorder — credentials will not be encrypted")

    # --- Phase 4: Register agent ---
    requested_agent_id = auth_msg.get("agent_id") or f"writ-{uuid.uuid4().hex[:12]}"
    try:
        async with AsyncSessionLocal() as db:
            agent_id = await _register_agent(
                db, requested_agent_id, user_id,
                is_trusted=is_trusted,
                token_prefix=auth.token_prefix,
            )
            await db.commit()
    except AgentRevokedError:
        logger.info(f"Refusing reconnect of revoked agent {requested_agent_id}")
        try:
            await websocket.send_json({
                "type": "auth_error",
                "reason": "This agent has been disconnected. Re-run 'writ-agent login' to reconnect.",
            })
            await websocket.close(code=4003, reason="Agent revoked")
        except Exception:
            pass
        return
    except AgentOwnershipError:
        # Finding #31: the claimed agent_id belongs to a different user. Refuse
        # the takeover instead of re-stamping identity onto someone else's row.
        logger.warning(
            f"Refusing agent {requested_agent_id}: owned by a different user "
            f"(connecting user_id={user_id})"
        )
        try:
            await websocket.send_json({
                "type": "auth_error",
                "reason": "This agent id is registered to a different account.",
            })
            await websocket.close(code=4003, reason="Agent owned by another account")
        except Exception:
            pass
        return
    except Exception as e:
        logger.error(f"Failed to register recorder agent: {e}")
        agent_id = requested_agent_id

    # The agent advertises whether it has its own AI provider keys via the
    # `ai_keys_configured=1` connect query param (same source the old ws-gateway
    # read). Capture it here so BYO-AI routing can see the live capability off the
    # direct socket instead of the now-empty gateway registry.
    ai_keys_configured = websocket.query_params.get("ai_keys_configured") == "1"

    # A fleet-local-capable agent (standalone writ-agent built with the `fleet`
    # feature) advertises that it can HOST local workflows via the
    # `local_workflows=1` connect query param. Unlike a legacy user-hosted daemon
    # (which connects untrusted), a fleet-token agent connects TRUSTED, so the
    # `not is_trusted` catalog gate below never fires for it — this flag opts it in.
    local_workflows_capable = websocket.query_params.get("local_workflows") == "1"

    _connections[agent_id] = websocket
    _agent_meta[agent_id] = {
        "user_id": user_id,
        "channel_key": channel_key,
        "token_prefix": auth.token_prefix,
        "role": role,
        "is_trusted": is_trusted,
        # `token_max_sessions` is the immutable server-issued capacity CEILING (from
        # the agent's token). `max_sessions` is the effective cap actually used for
        # scheduling; a heartbeat may lower it (agent self-limiting) but never raise
        # it above the ceiling. `active_sessions` is coordinator-owned (never taken
        # from the agent) — see _acquire_run_slot / _release_run_slot.
        "token_max_sessions": auth.max_sessions,
        "max_sessions": auth.max_sessions,
        "active_sessions": 0,
        "ai_keys_configured": ai_keys_configured,
        "local_workflows_capable": local_workflows_capable,
        "connected_at": datetime.now(timezone.utc).isoformat(),
        "last_heartbeat": time.time(),
    }
    _dispatched_tasks[agent_id] = set()

    # Send `welcome` FIRST so the stock cloud writ-agent (dialect B) — which blocks
    # up to 5s waiting to *receive* a {"type":"welcome", agent_id} frame before it
    # emits anything — adopts the coordinator-assigned id instead of falling back to
    # a random `agent-xxxx`. Harmless to the coordinator-dialect agent, which
    # ignores unknown frames and keys off the auth_ok below. Sent for BOTH dialects
    # so a coordinator agent that later grows the welcome-wait still works.
    await websocket.send_json({"type": "welcome", "agent_id": agent_id})

    # Send auth_ok with channel key (so the coordinator-dialect bridge can cache it).
    # The stock header-dialect agent has no auth_ok step; it ignores this frame.
    auth_ok_msg = {
        "type": "auth_ok",
        "agent_id": agent_id,
    }
    if channel_key:
        auth_ok_msg["channel_key"] = channel_key

    await websocket.send_json(auth_ok_msg)

    logger.info(
        f"Recorder connected: {agent_id} (role: {role}, trusted: {is_trusted}, "
        f"auth: {'header' if used_header_auth else 'first-frame'})"
    )

    # Cloud-callable LOCAL workflows: now that the user-hosted daemon row exists /
    # is reactivated and its WS is mapped, ask it to advertise its own local
    # workflows. The daemon replies with a `local_catalog` frame, ingested below
    # (scoped to this trusted agent — never an identity from the payload).
    # A legacy user-hosted daemon (untrusted) hosts its own local workflows; a
    # fleet-local-capable agent (trusted fleet token + `local_workflows=1`) also
    # hosts a coordinator-deployable local catalog. Ask EITHER to advertise. Plain
    # infra/trusted agents that don't opt in host no user workflows, so we skip them.
    if (not is_trusted) or local_workflows_capable:
        try:
            await websocket.send_json({"type": "request_local_catalog"})
        except Exception:
            pass  # best-effort; the daemon also advertises on its own next connect

    # --- Phase 5: Message loop ---
    ping_task = asyncio.create_task(_ping_loop(agent_id, websocket))

    async def _dispatch(msg: dict):
        """Route a single inbound agent frame. Shared by the in-loop reader and the
        dialect-B buffered-first-frame replay so both go through identical handling."""
        # Session-relay frames (from the agent's AgentSessionRelay) are wrapped
        # {channel:"session", session_id, msg}. Streaming responses (command_response
        # / stream_chunk / streaming_event / streaming_session_started) arrive this
        # way — route them into the in-process StreamingManager. Anything else is a
        # live-recording frame destined for the owning browser /ws/record socket.
        if msg.get("channel") == "session":
            inner = msg.get("msg") or {}
            from services.streaming_manager import manager as _stream_mgr
            if _stream_mgr.on_agent_frame(agent_id, inner):
                return
            await _route_session_json_to_browser(msg.get("session_id"), inner)
            return

        msg_type = msg.get("type")

        if msg_type == "heartbeat":
            _handle_heartbeat(agent_id, msg)
            # Advance the durable last_seen_at so the fleet view (which reads the DB
            # row) reflects a live, heartbeating agent — not just the in-memory
            # last_heartbeat. Best-effort; never break the loop on a DB blip.
            await _touch_agent_last_seen(agent_id)
        elif msg_type == "pong":
            if agent_id in _agent_meta:
                _agent_meta[agent_id]["last_heartbeat"] = time.time()
        elif msg_type == "task_result":
            await _handle_task_result(agent_id, msg)
        elif msg_type == "task_progress":
            await _handle_task_progress(agent_id, msg)
        elif msg_type == "finding":
            await _handle_finding(agent_id, msg)
        elif msg_type == "agent_action_result":
            _handle_agent_reply(agent_id, msg, correlate_by="request_id")
        elif msg_type == "session_opened":
            # The agent created its relay for a live-recording session; unblock the
            # browser /ws/record handler so it can start relaying frames.
            sess = _record_sessions.get(msg.get("session_id"))
            if sess:
                sess["opened"].set()
        elif msg_type == "session_closed":
            # The agent tore the session down (idle timeout / its own close). Close
            # the browser side so the UI stops waiting for frames — or, for a
            # server-/MCP-driven session, push a sentinel onto its queue.
            sess = _record_sessions.get(msg.get("session_id"))
            if sess:
                sess["opened"].set()
                sess["closed"].set()
                q = sess.get("server_queue")
                if q is not None:
                    try:
                        q.put_nowait({"type": "session_closed"})
                    except Exception:
                        pass
                bw = sess.get("browser_ws")
                if bw is not None:
                    try:
                        await bw.close(code=1000)
                    except Exception:
                        pass
        elif msg_type in (
            "ai_session_opened",
            "ai_session_open_result",
            "ai_session_closed",
            "ai_session_close_result",
            "ai_session_complete",
            "ai_session_failed",
        ):
            # AI session lifecycle acks. Correlated by session_id (kept identical
            # across Python + Rust runtimes). The `_handle_agent_reply` call resolves
            # any pending future (kept so a future synchronous awaiter still works),
            # but the AI-session dispatch is now FIRE-AND-FORGET — routers.ai_sessions
            # returns the 'running' row immediately, so the terminal complete/failed
            # frames are what durably move the AiSession row to its terminal state.
            # Do BOTH here.
            _handle_agent_reply(agent_id, msg, correlate_by="session_id")
            if msg_type in ("ai_session_complete", "ai_session_failed"):
                await _apply_ai_session_terminal(agent_id, msg)
        elif msg_type == "streaming_session_ended":
            # A fleet agent reporting a streaming session ended on its own
            # (max-duration / agent-side end / error). The StreamingManager saves the
            # harvested persona auth state and marks the row terminal so the agent
            # frees from the dispatcher's `occupied` set. Scoped to THIS authenticated
            # socket's agent_id inside on_agent_frame — never an agent-claimed one.
            from services.streaming_manager import manager as _stream_mgr
            _stream_mgr.on_agent_frame(agent_id, msg)
        elif msg_type in (
            "local_workflow_saved",
            "local_secret_saved",
            "local_persona_saved",
        ):
            # Fleet-local deploy acks (routers.fleet.deploy_to_agent → send_and_await).
            # Correlated by request_id — the deploy endpoint blocks on the matching
            # future before it upserts the LocalWorkflow row / runs move-cleanup.
            _handle_agent_reply(agent_id, msg, correlate_by="request_id")
        elif msg_type == "optimize_workflow_live_result":
            # Live-optimize diff reply (routers.ai_assist.ai_optimize_workflow_live →
            # send_and_await). The agent replayed its own deployed workflow with network
            # capture on and returns the finished diff envelope; the endpoint blocks on the
            # matching request_id future. Without this branch the future never resolves.
            _handle_agent_reply(agent_id, msg, correlate_by="request_id")
        elif msg_type == "local_catalog":
            # The daemon advertising its cloud-callable LOCAL workflows over a
            # DIRECT WS (not via the gateway). Upsert scoped to this socket's
            # AUTHENTICATED owner from _agent_meta — NEVER an owner claimed in the
            # payload (never trust identity asserted by the agent).
            await _handle_local_catalog(agent_id, msg)
        elif msg_type in (
            "monitor_register",
            "target_check_batch",
            "precheck_complete",
            "scheduled_workflow_complete",
        ):
            # Autonomous scheduled-monitoring frames sent by the agent over its
            # DIRECT socket. These carry no request/task correlation id (they are
            # not request/reply), so they are ingested straight into the
            # monitoring pipeline. The AUTHENTICATED agent_id bound to this socket
            # is the reporting identity — never an id in the payload
            # (feedback_never_trust_byo_agents). Previously these were routed only
            # by the ws-gateway Redis subscriber (which never fires in the
            # direct-socket model), so they were silently dropped.
            await _handle_monitoring_frame(agent_id, msg)
        # Unknown types silently ignored

    try:
        # Dialect B (stock writ-agent): its first WS frame was consumed during
        # header auth. Replay it as the first in-loop message so a leading
        # `monitor_register` isn't dropped (it registers monitoring capacity).
        if buffered_msg is not None:
            await _dispatch(buffered_msg)

        # Raw receive() rather than iter_text(): the stock cloud writ-agent replies
        # to a WS ping with an application-level BINARY frame (saas_bridge.rs sends
        # Message::Binary as its pong), and iter_text() raises KeyError('text') on a
        # binary frame — which would crash this loop and drop an otherwise-healthy
        # agent after ~15s. We accept the "text" payload (all agent JSON frames are
        # text), silently ignore "bytes" frames, and break on disconnect.
        while True:
            event = await websocket.receive()
            etype = event.get("type")
            if etype == "websocket.disconnect":
                break
            raw = event.get("text")
            if raw is None:
                # Binary frame: either the agent's binary WS pong (ignored) or a
                # live-recording screencast envelope destined for a browser socket
                # ([0x01][sid_len][sid][payload]). Route the latter; drop the rest.
                b = event.get("bytes")
                if b:
                    await _route_session_binary_to_browser(b)
                continue

            if len(raw) > MAX_MESSAGE_SIZE:
                logger.warning(f"Message too large from {agent_id}: {len(raw)} bytes")
                continue

            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            await _dispatch(msg)

    except WebSocketDisconnect:
        logger.info(f"Recorder disconnected: {agent_id} (role: {role})")
    except Exception as e:
        logger.error(f"Recorder WS error ({agent_id}): {e}")
    finally:
        ping_task.cancel()
        _connections.pop(agent_id, None)
        _agent_meta.pop(agent_id, None)
        _dispatched_tasks.pop(agent_id, None)
        _fail_pending_replies_for_agent(agent_id)

        # Tear down any live-recording browser sessions bound to this agent so the
        # UI doesn't hang waiting for screencast frames from a gone agent.
        for _sid, _sess in list(_record_sessions.items()):
            if _sess.get("agent_id") == agent_id:
                _sess["opened"].set()
                _sess["closed"].set()
                try:
                    await _sess["browser_ws"].close(code=1012)  # service restart
                except Exception:
                    pass
                _record_sessions.pop(_sid, None)

        # AUTHORITATIVE liveness: streaming sessions on this agent are dead now that
        # its socket dropped. The StreamingManager ends their rows so the dispatcher's
        # `occupied` set frees and the queue can redispatch (this is the FAST path;
        # the manager's janitor + the agent's own streaming_session_ended frame are
        # the other two ends of the same guarantee).
        try:
            from services.streaming_manager import manager as _stream_mgr
            await _stream_mgr.on_agent_disconnect(agent_id)
        except Exception:
            logger.exception(f"Failed to reap streaming sessions for agent {agent_id}")

        try:
            async with AsyncSessionLocal() as db:
                from models.agent import Agent as AgentModel, AgentStatus
                result = await db.execute(
                    select(AgentModel).where(AgentModel.agent_id == agent_id)
                )
                agent = result.scalar_one_or_none()
                # Mark offline, but never downgrade a user-revoked row back to
                # INACTIVE — REVOKED must stick so the agent can't reconnect.
                if agent and agent.status != AgentStatus.REVOKED:
                    agent.status = AgentStatus.INACTIVE
                    await db.commit()
        except Exception:
            pass

        logger.info(f"Recorder cleaned up: {agent_id}")


# Route: new unified endpoint
@router.websocket("/ws/recorder")
async def recorder_ws(websocket: WebSocket):
    await _recorder_ws_handler(websocket)


# Route: backward-compat alias (old endpoint name)
@router.websocket("/ws/user-recorder")
async def user_recorder_ws(websocket: WebSocket):
    await _recorder_ws_handler(websocket)


# Route: agent gateway alias. The self-host coordinator IS its own gateway — there
# is no separate ws-gateway process. The light writ-agent (saas_bridge) is handed
# a gateway_ws_url of "<coordinator>/ws/ai-gateway" by /api/recorder/connect and
# opens the persistent agent WS here. Aliased to the same unified handler as
# /ws/recorder so infra + user-hosted agents share one ingress. This mirror is
# also registered at the APP ROOT ("/ws/ai-gateway", no /api prefix) in main.py so
# the URL recorder_proxy advertises resolves on this same coordinator.
@router.websocket("/ws/ai-gateway")
async def ai_gateway_ws(websocket: WebSocket):
    await _recorder_ws_handler(websocket)


# ---------------------------------------------------------------------------
# Live-recording session relay: browser /ws/record <-> agent persistent WS.
# ---------------------------------------------------------------------------
# This is the in-process replacement for the removed Node ws-gateway. The
# frontend (BrowserRecorder / RecorderPreview / ApiRecorder) opens
# `ws://<host>/ws/record?ticket=…&agent_id=…`; the coordinator picks a connected
# fleet agent, opens a multiplexed session on that agent's single persistent WS,
# and relays frames both ways:
#   browser -> agent : {channel:"session", session_id, msg:<frame>}
#   agent  -> browser: {channel:"session", session_id, msg:<event>} (JSON)  and
#                      [0x01][4B sid_len BE][sid][payload] (binary screencast).
# The inner browser payload/screencast bytes are byte-identical to what the old
# ws-gateway forwarded, so the frontend sees the exact same wire it always has.

async def _route_session_json_to_browser(session_id, inner):
    """Forward one unwrapped agent session-event to the owning consumer.

    A record session is owned either by a browser WS (the frontend recorder) or,
    for a backend-/MCP-driven session, by an in-process ``server_queue`` (see
    ``open_server_record_session``). Route to whichever this session has.
    """
    if not session_id or inner is None:
        return
    sess = _record_sessions.get(session_id)
    if not sess:
        return
    q = sess.get("server_queue")
    if q is not None:
        try:
            q.put_nowait(inner)
        except Exception:
            pass
        return
    bw = sess.get("browser_ws")
    if bw is None:
        return
    try:
        await bw.send_text(json.dumps(inner))
    except Exception:
        pass


async def _route_session_binary_to_browser(b: bytes):
    """Route a binary agent frame to a browser socket.

    Screencast frames carry the session envelope [0x01][4B sid_len BE][sid][payload].
    The stripped `payload` is exactly what the frontend canvas decoder expects
    ([4B urlLen BE][url][jpeg]). Any other binary frame (e.g. the agent's WS pong)
    has no 0x01 marker and is ignored.
    """
    if len(b) < 5 or b[0] != 0x01:
        return
    sid_len = int.from_bytes(b[1:5], "big")
    if 5 + sid_len > len(b):
        return
    session_id = b[5:5 + sid_len].decode("utf-8", "ignore")
    payload = b[5 + sid_len:]
    sess = _record_sessions.get(session_id)
    if not sess:
        return
    bw = sess.get("browser_ws")
    if bw is None:
        # Server-/MCP-driven session: no browser socket wants the screencast.
        return
    try:
        await bw.send_bytes(payload)
    except Exception:
        pass


# ── Server-/MCP-driven record sessions ───────────────────────────────────────
# The frontend recorder opens a session by connecting a browser WS to /ws/record.
# A backend caller (the MCP record-build tools) needs the SAME live session on a
# fleet agent but with no browser socket — it drives the agent programmatically
# and consumes the agent's session frames from an in-process queue. These four
# primitives provide exactly that, reusing the existing `_record_sessions` relay
# and agent socket registry.

async def open_server_record_session(
    preferred_agent: Optional[str] = None, timeout: float = 15.0
) -> Optional[tuple]:
    """Open a live-record session on a fleet agent for backend driving.

    Sends ``session_open`` and waits for the agent's ``session_opened`` ack, then
    returns ``(session_id, agent_id)``. Agent session frames are queued on the
    session's ``server_queue`` (see ``_route_session_json_to_browser``). Returns
    None when no agent is connected or the open times out.
    """
    agent_id = _pick_record_agent(preferred_agent or "auto")
    if not agent_id:
        return None
    agent_ws = _connections.get(agent_id)
    if agent_ws is None:
        return None
    session_id = uuid.uuid4().hex
    opened = asyncio.Event()
    closed = asyncio.Event()
    _record_sessions[session_id] = {
        "agent_id": agent_id,
        "opened": opened,
        "closed": closed,
        "server_queue": asyncio.Queue(),
        "browser_ws": None,
    }
    try:
        await agent_ws.send_json({"type": "session_open", "session_id": session_id})
        await asyncio.wait_for(opened.wait(), timeout=timeout)
    except Exception:
        _record_sessions.pop(session_id, None)
        return None
    if closed.is_set():
        _record_sessions.pop(session_id, None)
        return None
    meta = _agent_meta.get(agent_id)
    if meta is not None:
        meta["active_sessions"] = (meta.get("active_sessions") or 0) + 1
    return session_id, agent_id


async def server_record_send(session_id: str, frame: dict) -> bool:
    """Send one session-channel frame to the agent for a server-driven session."""
    sess = _record_sessions.get(session_id)
    if not sess:
        return False
    agent_ws = _connections.get(sess.get("agent_id"))
    if agent_ws is None:
        return False
    try:
        await agent_ws.send_json(
            {"channel": "session", "session_id": session_id, "msg": frame}
        )
        return True
    except Exception:
        return False


async def server_record_recv(session_id: str, timeout: float = 90.0) -> Optional[dict]:
    """Await the next agent session frame for a server-driven session (or None)."""
    sess = _record_sessions.get(session_id)
    if not sess:
        return None
    q = sess.get("server_queue")
    if q is None:
        return None
    try:
        return await asyncio.wait_for(q.get(), timeout=timeout)
    except asyncio.TimeoutError:
        return None


def server_record_is_open(session_id: str) -> bool:
    sess = _record_sessions.get(session_id)
    return bool(sess) and not sess["closed"].is_set()


async def close_server_record_session(session_id: str) -> None:
    """Tear down a server-driven record session (best-effort agent close)."""
    sess = _record_sessions.pop(session_id, None)
    if not sess:
        return
    m = _agent_meta.get(sess.get("agent_id"))
    if m and m.get("active_sessions"):
        m["active_sessions"] = max(0, m["active_sessions"] - 1)
    agent_ws = _connections.get(sess.get("agent_id"))
    if agent_ws is not None:
        try:
            await agent_ws.send_json(
                {"type": "session_close", "session_id": session_id}
            )
        except Exception:
            pass


def _pick_record_agent(preferred: Optional[str]) -> Optional[str]:
    """Choose a connected fleet agent for a live-recording session.

    Honors an explicit `agent_id` when that agent is connected; otherwise picks
    the connected agent with the most free session slots. Any connected agent can
    serve an interactive recording session in the single-owner self-host model.
    """
    if preferred and preferred != "auto" and preferred in _connections:
        return preferred
    best = None
    best_free = -1
    for aid, meta in _agent_meta.items():
        if aid not in _connections:
            continue
        free = (meta.get("max_sessions") or 1) - (meta.get("active_sessions") or 0)
        if free > best_free:
            best_free = free
            best = aid
    return best


async def _resolve_browser_ws_credential(ticket: Optional[str], token: Optional[str]) -> Optional[dict]:
    """Best-effort verify a browser /ws/record credential.

    Resolves a single-use `?ticket=` (minted via POST /api/ws-ticket, consumed
    here) back to the caller's JWT, or verifies a `?token=` JWT directly. Returns
    the decoded JWT payload, or None when the credential is missing/invalid. The
    caller allows a MISSING credential (self-host is single-admin, same-origin,
    behind the app login, and ApiRecorder opens without one) but rejects a
    PRESENT-but-invalid one.
    """
    from security.jwt import decode_token

    resolved_token = token
    if ticket:
        redis = _ticket_redis()
        if redis is not None:
            try:
                raw = await redis.get(f"{_WS_TICKET_PREFIX}{ticket}")
                # Single-use: delete on first read.
                try:
                    await redis.delete(f"{_WS_TICKET_PREFIX}{ticket}")
                except Exception:
                    pass
                if raw:
                    if isinstance(raw, (bytes, bytearray)):
                        raw = raw.decode("utf-8", "ignore")
                    data = json.loads(raw)
                    resolved_token = data.get("token") or resolved_token
            except Exception:
                pass
    if resolved_token:
        try:
            return decode_token(resolved_token)
        except Exception:
            return None
    return None


def _self_origins(websocket: WebSocket) -> set[str]:
    """The coordinator's OWN origin(s) as the browser would spell them.

    Self-host serves the SPA from the coordinator process itself (see the
    StaticFiles mount + SPA fallback in main.py), so the recorder page is
    SAME-ORIGIN with ``/ws/record``. Same-origin is never CSWSH — the attack
    requires an attacker-controlled page on a DIFFERENT origin — and the
    operator has no reason to list their own URL in ``CORS_ORIGINS`` (same-origin
    HTTP calls never hit CORS at all). Derived from the request's ``Host`` header
    plus the effective scheme, honouring ``X-Forwarded-Proto`` for a TLS
    terminator in front.
    """
    origins: set[str] = set()
    host = websocket.headers.get("host")
    if host:
        scheme = "https" if websocket.url.scheme == "wss" else "http"
        fwd_proto = (websocket.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
        if fwd_proto in ("http", "https"):
            scheme = fwd_proto
        origins.add(f"{scheme}://{host}")
    # The configured app URL is by definition an app origin, even when the
    # operator never mirrored it into CORS_ORIGINS.
    try:
        frontend = (settings.frontend_url or "").strip().rstrip("/")
        if frontend:
            parsed = urlparse(frontend)
            if parsed.scheme and parsed.netloc:
                origins.add(f"{parsed.scheme}://{parsed.netloc}")
    except Exception:
        pass
    return origins


def _browser_ws_origin_allowed(websocket: WebSocket) -> bool:
    """Validate the WebSocket ``Origin`` against the coordinator's allowed origins.

    Cross-site WebSocket hijacking (CSWSH) defense (security finding #4): a
    WebSocket handshake is NOT subject to CORS, so a page on any origin can open
    ``/ws/record`` and — riding the victim's cookies/ticket — drive a fleet
    agent's browser. We therefore check the browser-supplied ``Origin`` header
    BEFORE accepting the socket, against ``settings.cors_origins_list`` PLUS the
    coordinator's own origin (see ``_self_origins``).

    - A wildcard (``*``) in the configured origins disables the check (explicit
      operator opt-out, matching the HTTP CORS behavior).
    - A missing or non-matching ``Origin`` is rejected (fail closed): a legitimate
      browser recorder is always served from an allowed app origin and sends it.
    """
    try:
        allowed = set(settings.cors_origins_list)
    except Exception:
        allowed = set()
    if "*" in allowed:
        return True
    origin = websocket.headers.get("origin") or websocket.headers.get("Origin")
    if not origin:
        return False
    return origin in allowed or origin in _self_origins(websocket)


async def browser_record_ws_handler(websocket: WebSocket):
    """Serve the browser side of an interactive recording/preview session.

    Registered at the app root as `/ws/record` (see main.py). Bridges the browser
    to a connected fleet agent's persistent WS via the session-multiplexing
    contract the agent (writ-agent cloud bridge) speaks.
    """
    # CSWSH defense (finding #4): reject a cross-site Origin BEFORE accept(). A
    # pre-accept close sends an HTTP 403 handshake response — the hijacking page
    # never gets a WebSocket.
    if not _browser_ws_origin_allowed(websocket):
        logger.warning(
            "/ws/record rejected: disallowed Origin %r",
            websocket.headers.get("origin") or websocket.headers.get("Origin"),
        )
        try:
            await websocket.close(code=4403, reason="Origin not allowed")
        except Exception:
            pass
        return

    await websocket.accept()
    qp = websocket.query_params
    ticket = qp.get("ticket")
    token = qp.get("token")

    # Fail closed (finding #4): a live-recording session grants full control of a
    # fleet agent's browser (navigation, SSRF, data exfiltration). Require a VALID
    # authenticated identity — reject when the credential is missing OR invalid,
    # not only when a present credential fails to verify.
    identity = await _resolve_browser_ws_credential(ticket, token)
    if identity is None:
        # 4001 = auth failed (missing or invalid credential).
        try:
            await websocket.close(code=4001, reason="Authentication required")
        except Exception:
            pass
        return

    # Per-identity abuse controls (finding #13). Enforced BEFORE we allocate an
    # agent slot so an abusive caller can't consume recording capacity.
    identity_key = _browser_ws_identity_key(identity)
    if _record_rate_limited(identity_key):
        logger.warning("/ws/record rate-limited for identity %s", identity_key)
        try:
            await websocket.close(code=4429, reason="Too many recording sessions")
        except Exception:
            pass
        return
    if _record_sessions_by_identity.get(identity_key, 0) >= MAX_RECORD_SESSIONS_PER_IDENTITY:
        logger.warning(
            "/ws/record concurrency cap (%d) reached for identity %s",
            MAX_RECORD_SESSIONS_PER_IDENTITY, identity_key,
        )
        try:
            await websocket.close(code=4429, reason="Too many concurrent recording sessions")
        except Exception:
            pass
        return

    agent_id = _pick_record_agent(qp.get("agent_id") or "auto")
    if not agent_id:
        # 4003 = no recorder available; the frontend maps this to a friendly toast.
        try:
            await websocket.close(code=4003, reason="No agent available")
        except Exception:
            pass
        return

    session_id = uuid.uuid4().hex
    opened = asyncio.Event()
    closed = asyncio.Event()
    _record_sessions[session_id] = {
        "browser_ws": websocket,
        "agent_id": agent_id,
        "opened": opened,
        "closed": closed,
    }
    # Count this live session against the identity cap; released in the finally.
    _record_sessions_by_identity[identity_key] = _record_sessions_by_identity.get(identity_key, 0) + 1
    meta = _agent_meta.get(agent_id)
    if meta is not None:
        meta["active_sessions"] = (meta.get("active_sessions") or 0) + 1

    try:
        agent_ws = _connections.get(agent_id)
        if agent_ws is None:
            await websocket.close(code=4003, reason="Agent disconnected")
            return

        # Open the multiplexed session on the agent's persistent WS.
        await agent_ws.send_json({"type": "session_open", "session_id": session_id})

        # Wait for the agent to create its relay before relaying any browser frame —
        # the agent drops channel:session frames for an unknown session_id, so an
        # early `start` would be lost without this barrier.
        try:
            await asyncio.wait_for(opened.wait(), timeout=15)
        except asyncio.TimeoutError:
            await websocket.close(code=4003, reason="Agent did not open session")
            return
        if closed.is_set():
            return

        # Relay browser -> agent until either side closes.
        while True:
            event = await websocket.receive()
            if event.get("type") == "websocket.disconnect":
                break
            text = event.get("text")
            if text is None:
                continue  # browser binary is unused by the recorder
            try:
                inner = json.loads(text)
            except json.JSONDecodeError:
                continue
            a = _connections.get(agent_id)
            if a is None:
                break
            try:
                await a.send_json({"channel": "session", "session_id": session_id, "msg": inner})
            except Exception:
                break
    except WebSocketDisconnect:
        pass
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(f"/ws/record relay error (session {session_id[:8]}): {e}")
    finally:
        _record_sessions.pop(session_id, None)
        # Release this identity's concurrent-session slot (floored at 0, key pruned
        # when it reaches 0 so the map doesn't grow unbounded).
        _cnt = _record_sessions_by_identity.get(identity_key, 0) - 1
        if _cnt > 0:
            _record_sessions_by_identity[identity_key] = _cnt
        else:
            _record_sessions_by_identity.pop(identity_key, None)
        m = _agent_meta.get(agent_id)
        if m and m.get("active_sessions"):
            m["active_sessions"] = max(0, m["active_sessions"] - 1)
        # Best-effort: tell the agent to tear its session down.
        a = _connections.get(agent_id)
        if a is not None:
            try:
                await a.send_json({"type": "session_close", "session_id": session_id})
            except Exception:
                pass


# ---------------------------------------------------------------------------
# WebSocket auth ticket (single-use, short-lived)
# ---------------------------------------------------------------------------
# The browser cannot set an Authorization header on a WebSocket, so the live
# recording socket (/ws/record, served by the ws-gateway) is authenticated via a
# query-string credential. Passing the long-lived access JWT there leaks it into
# proxy/access logs and browser history. To avoid that, the frontend first calls
# /ws-ticket (a normal authenticated HTTP request, Bearer in the header — never
# logged in the URL) to mint a SINGLE-USE, SHORT-LIVED ticket bound to the
# caller's identity. The ticket is then placed in the WS URL instead of the
# JWT: it is useless after first use and expires in ~60s, so a log leak is inert.
#
# The ticket maps (in Redis) to the caller's real access token + identity. A
# consumer (the ws-gateway, once taught to accept "?ticket=") resolves it via the
# internal /internal/ws-ticket/consume endpoint, which DELETES the ticket on the
# first read (single-use) and returns the bound token so the gateway can run its
# normal JWT verification. This is purely additive: the existing "?token=" path
# is untouched and remains valid for backward-compat.

WS_TICKET_TTL = 60  # seconds — the agent/frontend must open the WS within this window
_WS_TICKET_PREFIX = "ws_ticket:"

_INTERNAL_API_SECRET = os.getenv("INTERNAL_API_SECRET", "")


def _ticket_redis():
    """Return a usable async Redis client, preferring the shared app client.

    Falls back to the lazily-shared client from utils.redis_client (the same one
    other routers use) so the ticket store works even if set_redis_client() was
    not called at startup. Returns None only if Redis is entirely unavailable.
    """
    if _app_redis is not None:
        return _app_redis
    try:
        from utils.redis_client import get_redis
        return get_redis()
    except Exception as e:  # pragma: no cover - defensive
        logger.error(f"ws-ticket: no Redis available ({e})")
        return None


class WsTicketResponse(BaseModel):
    ticket: str
    expires_in: int


@router.post("/ws-ticket", response_model=WsTicketResponse)
async def mint_ws_ticket(
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
):
    """Mint a single-use, short-lived WebSocket auth ticket for the caller.

    Authenticated like any other API call (Bearer in the Authorization header,
    which is NOT logged in URLs). The returned ticket is bound to this caller's
    identity + their access token in Redis, and is consumed (deleted) on first
    use by the WS handshake. Lets the frontend open the recording WS without
    putting the long-lived JWT in the query string.
    """
    # Only first-party web-session (JWT) callers should funnel a token through the
    # recording WS. API keys / OAuth tokens use other surfaces and have no JWT to
    # bind; reject them so we never store a non-JWT credential as a WS ticket.
    if auth.auth_method != "jwt":
        raise HTTPException(status_code=403, detail="WS tickets require a web session")

    # Recover the caller's raw Bearer access token from the request — this is the
    # exact JWT the ws-gateway already knows how to verify. We never log it.
    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authentication required")
    access_token = auth_header[7:].strip()
    if not access_token:
        raise HTTPException(status_code=401, detail="Authentication required")

    redis = _ticket_redis()
    if redis is None:
        raise HTTPException(status_code=503, detail="Ticket store unavailable")

    ticket = "wst_" + secrets.token_urlsafe(32)
    payload = json.dumps({
        "token": access_token,
        "user_id": str(auth.user_id) if auth.user_id else None,
        "minted_at": datetime.now(timezone.utc).isoformat(),
    })
    try:
        # NX + EX: write only if absent (random key, effectively always) with TTL.
        await redis.set(f"{_WS_TICKET_PREFIX}{ticket}", payload, ex=WS_TICKET_TTL, nx=True)
    except Exception as e:
        logger.error(f"ws-ticket: failed to store ticket: {e}")
        raise HTTPException(status_code=503, detail="Ticket store unavailable")

    return WsTicketResponse(ticket=ticket, expires_in=WS_TICKET_TTL)


# NOTE: the single-use ticket *consume* endpoint (POST /api/internal/ws-ticket/consume,
# called by the ws-gateway) lives in routers/internal.py alongside the other
# X-Internal-Secret-guarded service endpoints. This router only MINTS tickets (above);
# the consume half was deduplicated to internal.py to avoid two routes on the same path.


async def _ping_loop(agent_id: str, websocket: WebSocket):
    while agent_id in _connections:
        try:
            await asyncio.sleep(45)
            if agent_id not in _connections:
                break
            # Dead-peer detection (replaces the disabled WS protocol keepalive):
            # if the agent hasn't heartbeated or ponged within the silence window,
            # presume the socket is dead and close it so the read loop unblocks and
            # the registry is cleaned up (otherwise a half-open TCP peer lingers and
            # record/dispatch would pick a zombie agent).
            meta = _agent_meta.get(agent_id)
            last = (meta or {}).get("last_heartbeat", 0)
            if meta is not None and (time.time() - last) > AGENT_SILENCE_TIMEOUT:
                logger.warning(
                    f"Agent {agent_id} silent for >{AGENT_SILENCE_TIMEOUT}s — closing stale socket"
                )
                try:
                    await websocket.close(code=1001, reason="Idle timeout")
                except Exception:
                    pass
                break
            await websocket.send_json({"type": "ping"})
        except Exception:
            break


# ---------------------------------------------------------------------------
# Message handlers (with security checks)
# ---------------------------------------------------------------------------
def _handle_heartbeat(agent_id: str, msg: dict):
    if agent_id not in _agent_meta:
        return

    meta = _agent_meta[agent_id]

    # Throttle: ignore if too frequent
    now = time.time()
    if now - meta.get("last_heartbeat", 0) < MIN_HEARTBEAT_INTERVAL:
        return

    meta["last_heartbeat"] = now
    # NEVER trust an OSS agent's self-reported load. `active_sessions` is
    # coordinator-authoritative (see _acquire_run_slot / _release_run_slot and the
    # record-session accounting) — the heartbeat must not clobber it, or an agent
    # could report 0 to hog dispatch / evade the queue. `max_sessions` is a capacity
    # CEILING issued in the agent's token at connect (auth.max_sessions); honor an
    # agent asking for FEWER slots, but never let it claim MORE than the token grants.
    _token_ceiling = int(meta.get("token_max_sessions", meta.get("max_sessions", 2)) or 2)
    _requested = msg.get("max_sessions")
    if isinstance(_requested, (int, float)) and _requested >= 1:
        meta["max_sessions"] = max(1, min(int(_requested), _token_ceiling))
    meta["platform"] = msg.get("platform", meta.get("platform"))
    meta["version"] = msg.get("version")
    meta["ai_provider"] = msg.get("ai_provider")


async def _touch_agent_last_seen(agent_id: str):
    """Bump the agent's durable ``last_seen_at`` to now (best-effort).

    ``/api/fleet/agents`` reports ``last_seen`` from the DB row, so heartbeats must
    advance it for the fleet view to show a live agent's clock moving. Only touches
    a still-connected agent; never raises (a DB blip must not drop the WS loop)."""
    if agent_id not in _connections:
        return
    try:
        async with AsyncSessionLocal() as db:
            from models.agent import Agent as AgentModel
            result = await db.execute(
                select(AgentModel).where(AgentModel.agent_id == agent_id)
            )
            agent = result.scalar_one_or_none()
            if agent:
                agent.last_seen_at = datetime.now(timezone.utc)
                await db.commit()
    except Exception:
        pass


async def _handle_task_result(agent_id: str, msg: dict):
    task_id = str(msg.get("task_id", ""))
    if not task_id:
        return

    logger.info(f"[TaskResult] Received result for task {task_id} from {agent_id}: success={msg.get('success')}")

    # Log tracking state (dispatched_tasks is in-memory, cleared on restart)
    dispatched = _dispatched_tasks.get(agent_id, set())
    if task_id not in dispatched:
        logger.info(f"[TaskResult] Task {task_id} not in dispatched set (server may have restarted) — processing anyway")

    # Resolve pending future if exists. The future and the dispatched set live in
    # the same process, so if a future exists this agent must also have it in its
    # dispatched set — require that, otherwise a malicious agent could forge
    # another agent's in-flight result by guessing the (sequential) task_id.
    future = _pending_results.get(task_id)
    if future and not future.done():
        if task_id not in dispatched:
            logger.warning(
                f"[TaskResult] Agent {agent_id} attempted to resolve task {task_id} "
                f"it was not dispatched — rejecting"
            )
            return
        logger.info(f"[TaskResult] Resolving future for task {task_id}")
        future.set_result(msg)
        return

    # Update task in DB using the shared completion logic
    try:
        async with AsyncSessionLocal() as db:
            from models.automation_task import AutomationTask
            result = await db.execute(
                select(AutomationTask).where(AutomationTask.id == int(task_id))
            )
            task = result.scalar_one_or_none()
            if not task:
                logger.warning(f"[TaskResult] Task {task_id} not found in DB")
                return

            # Authorization (fail closed). The reporting agent must be the one this
            # task was DISPATCHED to — a durable executor match. The agent can only
            # authenticate as its own agent_id, so the executor match is secure.
            is_executor = bool(task.executor_agent_id) and str(task.executor_agent_id) == str(agent_id)
            if not is_executor:
                logger.warning(f"[TaskResult] Unauthorized result: agent {agent_id} vs task {task_id}")
                return

            # Idempotency: a duplicate/retried result frame for an ALREADY-terminal
            # task must not re-run the success path (which would double-count the
            # creator usage counter, re-fire notifications, etc.).
            if task.status in ("success", "failed", "timeout", "cancelled"):
                logger.info(
                    f"[TaskResult] Task {task_id} already terminal ({task.status}); "
                    f"ignoring duplicate result"
                )
                return

            # CRAWL SHARD → crawl-native THIN completion (atomic claim + crawl
            # advance) — see crawl_orchestrator.complete_shard_task. Same routing as
            # the WS dispatch path, so a shard completes identically however its
            # result frame arrives.
            _cid = (task.trigger_context or {}).get("_crawl_id")
            if _cid:
                from services.crawl_orchestrator import complete_shard_task
                await complete_shard_task(
                    int(task_id), int(_cid),
                    success=msg.get("success", False),
                    result_data=msg.get("result_data"),
                    error=msg.get("error"),
                    reporter_agent=agent_id,
                )
                logger.info(f"[TaskResult] Crawl shard task {task_id} processed via thin path")
                return

            from routers.automation import _process_task_completion
            await _process_task_completion(
                db=db, task=task,
                success=msg.get("success", False),
                result_data=msg.get("result_data"),
                error=msg.get("error"),
            )
            logger.info(f"[TaskResult] Task {task_id} processed via _process_task_completion")
    except Exception as e:
        logger.error(f"[TaskResult] Failed to update task {task_id}: {e}", exc_info=True)
    finally:
        dispatched.discard(task_id)


async def _handle_task_progress(agent_id: str, msg: dict):
    task_id = msg.get("task_id")
    if not task_id:
        return

    # Only accept progress for dispatched tasks
    if str(task_id) not in _dispatched_tasks.get(agent_id, set()):
        return

    try:
        async with AsyncSessionLocal() as db:
            from models.automation_task import AutomationTask
            result = await db.execute(
                select(AutomationTask).where(AutomationTask.id == int(task_id))
            )
            task = result.scalar_one_or_none()
            if task:
                # Same authorization as _handle_task_result: the assigned executor.
                is_executor = bool(task.executor_agent_id) and str(task.executor_agent_id) == str(agent_id)
                if not is_executor:
                    return
                task.status = "running"
                if msg.get("message"):
                    task.progress_message = str(msg["message"])[:500]
                await db.commit()
    except Exception:
        pass


async def _handle_finding(agent_id: str, msg: dict):
    """No-op: the coordinator has no AI-session finding store. The `finding`
    frame is accepted for wire compatibility and dropped."""
    return


async def _handle_local_catalog(agent_id: str, msg: dict):
    """Ingest a daemon's `local_catalog` frame received over a DIRECT WS.

    The owning identity is the AUTHENTICATED ``agent_id`` bound to this socket —
    never an identity carried in the payload. We hand the trusted ``agent_id`` to
    the catalog service, which sanitizes the entries (metadata only — no
    steps/recipe/creds) and upserts/withdraws scoped to it.
    """
    if agent_id not in _agent_meta:
        logger.warning(f"local_catalog from {agent_id} with no bound connection; skipping")
        return
    try:
        from services.local_workflow_catalog import handle_local_catalog
        # The FROZEN wire contract sends the entry list under `workflows`
        # ({"type":"local_catalog","workflows":[..]}). A legacy desktop-daemon
        # sender used `catalog`; accept it as a fallback so old agents still
        # ingest, but prefer the contract field. Reading the wrong key would
        # ingest an empty list and WITHDRAW every advertised workflow.
        entries = msg.get("workflows")
        if entries is None:
            entries = msg.get("catalog")
        async with AsyncSessionLocal() as db:
            await handle_local_catalog(
                db,
                agent_id=agent_id,
                catalog=entries,
            )
    except Exception as e:
        logger.error(f"Failed to ingest local_catalog from {agent_id}: {e}")


async def _apply_ai_session_terminal(agent_id: str, msg: dict):
    """Durably move an ``AiSession`` row to its terminal state from a terminal frame.

    The AI-session dispatch (``routers.ai_sessions``) is fire-and-forget: it returns
    the ``running`` row immediately and relies on this handler to finish the row when
    the agent reports ``ai_session_complete`` / ``ai_session_failed`` over its socket.

    The row is looked up by the frame's ``session_id`` and scoped to the AUTHENTICATED
    ``agent_id`` bound to this socket — an agent can only complete a session that was
    dispatched to it, never one belonging to another agent (identity in the payload is
    never trusted). Failures are logged and swallowed so a DB blip or a stray frame
    for an unknown session never crashes the WS receive loop.
    """
    session_id = str(msg.get("session_id") or "")
    if not session_id:
        return
    try:
        from models.ai_session import AiSession

        async with AsyncSessionLocal() as db:
            row = (
                await db.execute(
                    select(AiSession).where(
                        AiSession.session_id == session_id,
                        AiSession.agent_id == agent_id,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                # Unknown/foreign session — nothing to update. (A row created by the
                # fire-and-forget /start is always scoped to this agent_id, so this
                # only fires for a stray frame or a session dispatched elsewhere.)
                return
            # Terminal frames both carry the same shape; never trust an agent id.
            row.status = str(msg.get("status") or ("error" if msg.get("error") else "complete"))
            wf_id = msg.get("workflow_id")
            row.workflow_id = int(wf_id) if isinstance(wf_id, int) else None
            row.workflow_name = msg.get("workflow_name")
            steps = msg.get("steps")
            row.steps = int(steps) if isinstance(steps, int) else None
            row.message = msg.get("message")
            row.error = msg.get("error")
            row.completed_at = datetime.now(timezone.utc)
            await db.commit()
    except Exception as e:
        logger.error(
            f"Failed to apply AI-session terminal frame from {agent_id} "
            f"(session {session_id[:8]}): {e}"
        )


async def _handle_monitoring_frame(agent_id: str, msg: dict):
    """Ingest an autonomous scheduled-monitoring frame from a DIRECTLY-connected
    agent.

    Routes each monitoring frame type to its ingest function, passing the
    AUTHENTICATED ``agent_id`` bound to this socket as the reporting identity
    (never an id carried in the payload — the agent's asserted identity is not
    trusted).
    """
    mt = msg.get("type")
    try:
        if mt == "monitor_register":
            from services.monitoring_ingest import ingest_monitor_register
            await ingest_monitor_register(msg, agent_id)
        elif mt == "target_check_batch":
            from services.monitoring_ingest import ingest_target_check_batch
            await ingest_target_check_batch(msg, agent_id)
        elif mt == "precheck_complete":
            from services.monitoring_ingest import ingest_precheck_complete
            await ingest_precheck_complete(msg, agent_id)
        elif mt == "scheduled_workflow_complete":
            from services.monitoring_ingest import ingest_scheduled_complete
            await ingest_scheduled_complete(msg, agent_id)
    except Exception as e:
        logger.error(f"Failed to ingest monitoring frame {mt!r} from {agent_id}: {e}")


def _agent_owner_conflict(agent, user_id: Optional[str]) -> bool:
    """Whether reusing ``agent`` would take it over from a DIFFERENT owner.

    Security finding #31: an existing row must not be reused/re-stamped for a
    caller who does not own it. Returns True when the row already carries a
    ``meta['user_id']`` that does NOT match the connecting ``user_id``. Returns
    False (same-owner reconnect OR an unowned/legacy row) when the stored owner is
    absent or equal — preserving the normal reconnect path and trusted-infra rows
    (which carry no ``user_id``)."""
    existing_owner = str((getattr(agent, "meta", None) or {}).get("user_id") or "")
    if not existing_owner:
        return False
    return existing_owner != str(user_id or "")


def _stamp_agent_identity(agent, user_id: Optional[str], token_prefix: Optional[str]):
    """Persist the owning user + OAuth token prefix onto the agent's meta so a
    later user-initiated disconnect can revoke exactly this agent's token.

    Callers MUST have already asserted ownership (``_agent_owner_conflict``) before
    reusing an existing row — this function overwrites ``meta['user_id']`` and is
    the exact takeover primitive finding #31 flagged when called without a check."""
    meta = dict(agent.meta or {})
    if user_id:
        meta["user_id"] = str(user_id)
    if token_prefix:
        meta["oauth_token_prefix"] = token_prefix
    agent.meta = meta


async def _register_agent(
    db: AsyncSession,
    requested_agent_id: str,
    user_id: Optional[str],
    is_trusted: bool = False,
    token_prefix: Optional[str] = None,
) -> str:
    """Reuse an existing agent row, or create one (single global fleet).

    The client sends its stored agent_id from a previous connection.
    If it matches an existing row, reuse it (even if inactive).
    This ensures the same machine always maps to the same agent across
    token refreshes and reconnects.

    A REVOKED row is never reactivated — the user explicitly disconnected it,
    so we raise AgentRevokedError to refuse the reconnection.

    Infrastructure (trusted) agents are stored with user_hosted=False and
    is_trusted=True so the dispatcher can distinguish them from user-hosted.
    """
    from models.agent import Agent as AgentModel, AgentStatus, Platform

    is_user_hosted = not is_trusted

    # 1. Try to find the exact agent_id the client claims
    if requested_agent_id:
        result = await db.execute(
            select(AgentModel).where(
                AgentModel.agent_id == requested_agent_id,
            )
        )
        agent = result.scalar_one_or_none()
        if agent and agent.status == AgentStatus.REVOKED:
            # Explicitly disconnected by the user — refuse to bring it back.
            raise AgentRevokedError(requested_agent_id)
        if agent and agent.agent_id not in _connections:
            # Ownership check (finding #31): never re-stamp a row owned by another
            # user onto this caller's identity. Same-owner reconnect (or an
            # unowned/legacy row) passes through unchanged.
            if _agent_owner_conflict(agent, user_id):
                raise AgentOwnershipError(requested_agent_id)
            agent.status = AgentStatus.ACTIVE
            agent.last_seen_at = datetime.now(timezone.utc)
            agent.is_trusted = is_trusted
            agent.user_hosted = is_user_hosted
            _stamp_agent_identity(agent, user_id, token_prefix)
            return agent.agent_id

    # 2. Fallback: reuse any disconnected, non-revoked agent of the same type
    result = await db.execute(
        select(AgentModel).where(
            AgentModel.user_hosted == is_user_hosted,
            AgentModel.status != AgentStatus.REVOKED,
        ).order_by(AgentModel.last_seen_at.desc().nulls_last())
    )
    for agent in result.scalars().all():
        if agent.agent_id not in _connections:
            # Never opportunistically reuse a row owned by a DIFFERENT user
            # (finding #31). Skip it and fall through to minting a fresh row
            # rather than silently taking over another owner's agent.
            if _agent_owner_conflict(agent, user_id):
                continue
            agent.status = AgentStatus.ACTIVE
            agent.last_seen_at = datetime.now(timezone.utc)
            agent.is_trusted = is_trusted
            _stamp_agent_identity(agent, user_id, token_prefix)
            return agent.agent_id

    # 3. No reusable agent — create one
    agent = AgentModel(
        agent_id=requested_agent_id,
        platform=Platform.RECORDER,
        status=AgentStatus.ACTIVE,
        secret_hash="jwt-service-token" if is_trusted else "device-flow-auth",
        last_seen_at=datetime.now(timezone.utc),
        meta={
            "max_sessions": 5 if is_trusted else 2,
            "active_sessions": 0,
            "user_hosted": is_user_hosted,
            **({"user_id": str(user_id)} if user_id else {}),
            **({"oauth_token_prefix": token_prefix} if token_prefix else {}),
        },
        is_trusted=is_trusted,
        user_hosted=is_user_hosted,
    )
    db.add(agent)
    await db.flush()
    # Surface a one-time "new machine linked" notification — only for user-hosted
    # agents (infra agents are auto-provisioned and shouldn't ping an owner).
    # Best-effort: never block agent registration on a notification failure.
    if is_user_hosted:
        try:
            from uuid import UUID as _UUID
            from services import platform_notifier
            await platform_notifier.notify(
                db,
                user_id=_UUID(str(user_id)) if user_id else None,
                event="agent_connected",
                title="New agent connected",
                body=f"“{requested_agent_id}” is now linked to your account and can run workflows on your machine.",
                link="/my-agents",
            )
        except Exception:
            pass
    return requested_agent_id
