"""Native in-process streaming for the gateway-less coordinator.

The single-container coordinator holds the agent sockets directly
(``routers.user_recorder_ws._connections``) and runs everything in one process.
This module drives the whole streaming session lifecycle and the command/event
relay **in process**, using the coordinator's existing agent transport
(``push_to_recorder``/``push_fire_and_forget``) and plain asyncio primitives — no
Redis channels, no pub/sub-to-self.

Wire protocol (the standard agent build speaks it):

  coordinator → agent
    * ``start_streaming_session`` {task_id: "stream-<key>", session_key, config}
      — dispatched via ``push_to_recorder`` (which re-encrypts ``config
      .credentials_encrypted`` with the agent's channel key and awaits the agent's
      ``task_result`` start-ack).
    * ``streaming_command`` {session_key, action, data, request_id}
    * ``end_streaming_session`` {session_key}

  agent → coordinator  (routed in by ``user_recorder_ws._dispatch`` → ``on_agent_frame``)
    * ``command_response`` {session_key, request_id, data|error}  — terminal
    * ``stream_chunk``     {session_key, request_id, data}        — partial
    * ``streaming_event``  {session_key, event_name, data}        — autonomous
    * ``streaming_session_started`` {session_key, ...}            — informational
    * ``streaming_session_ended``   {session_key, reason, session_state}

In-process relay: each command gets a random ``request_id`` and an
``asyncio.Queue`` the WS loop feeds. A non-streaming ``invoke`` drains the same
queue to its terminal ``command_response``. Events fan out to per-session
subscriber queues (the SSE endpoint drains one).

Persistence: the coordinator does NOT pin sessions to a fleet agent. Warm auth
state is **identity-scoped to a Persona** — loaded into the start config and
saved back on end via ``PersonaService``.

Security: every agent→coordinator frame is handled with the AUTHENTICATED socket
``agent_id``. Relay queues and event subscribers are checked against the session's
owning agent so a connected agent cannot inject chunks/events into another agent's
session (request_ids and session_keys are unguessable random hex, and ownership is
verified regardless).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import AsyncIterator, Optional

logger = logging.getLogger(__name__)

# Serialized-command wait and idle-keepalive cadence (seconds).
_KEEPALIVE_AFTER = 5
_DEFAULT_INVOKE_TIMEOUT = 60


def resolve_advanced_script(steps, streaming_config) -> Optional[dict]:
    """Resolve a workflow's effective advanced script.

    An ``advanced_script`` STEP (``type == 'advanced_script'``) with code is
    authoritative; otherwise fall back to the legacy
    ``streaming_config.advanced_script``. Single resolution point so the injected
    script and the advertised callable surface never drift.
    """
    for s in (steps or []):
        if (s or {}).get("type") == "advanced_script":
            cfg = (s.get("config") or {})
            if cfg.get("code"):
                return {
                    "enabled": True,
                    "code": cfg.get("code"),
                    "persistent": cfg.get("persistent", True),
                    "functions": cfg.get("functions") or [],
                }
            break
    return (streaming_config or {}).get("advanced_script")


def _decrypt_consumer_blob(queued_config) -> Optional[dict]:
    """Decrypt the master-key Fernet ``_consumer_stream_enc`` blob a persona-linked
    start carries in ``queued_config`` (persona OTP context + persona auth
    session_state). Returns the dict, or None if absent/undecryptable. Never logs
    the plaintext."""
    if not isinstance(queued_config, dict):
        return None
    blob = queued_config.get("_consumer_stream_enc")
    if not blob:
        return None
    try:
        from routers.automation import get_fernet
        return json.loads(get_fernet().decrypt(blob.encode()).decode())
    except Exception:
        logger.exception("streaming: failed to decrypt consumer stream blob")
        return None


@dataclass
class _Session:
    session_key: str
    agent_id: str
    workflow_id: int
    started_at: float
    max_duration_seconds: int
    persona_id: Optional[str] = None
    session_persistence: bool = False


@dataclass
class _Waiter:
    """A per-request relay queue plus the agent allowed to feed it."""
    agent_id: str
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)


class StreamingManager:
    def __init__(self) -> None:
        self._sessions: dict[str, _Session] = {}
        self._waiters: dict[str, _Waiter] = {}          # request_id -> waiter
        self._events: dict[str, set[asyncio.Queue]] = {}  # session_key -> SSE subscriber queues
        self._locks: dict[str, asyncio.Lock] = {}        # session_key -> command serialization

    # ------------------------------------------------------------------ #
    # Lifecycle: coordinator → agent                                     #
    # ------------------------------------------------------------------ #
    async def start_on_agent(self, db, wf, session, agent_id: str) -> bool:
        """Assemble the start config and dispatch it to ``agent_id``. On the agent's
        start-ack, mark the session ``running`` and register it. Returns False (and
        leaves the row for the caller to re-queue) if the agent didn't ack."""
        config = await self._assemble_config(db, wf, session, agent_id)
        message = {
            "type": "start_streaming_session",
            "task_id": f"stream-{session.session_key}",
            "session_key": session.session_key,
            "config": config,
        }
        from routers.user_recorder_ws import push_to_recorder
        # push_to_recorder re-encrypts config.credentials_encrypted with the agent's
        # channel key AND (because start_streaming_session is a reply-awaited type)
        # blocks on the agent's task_result start-ack.
        result = await push_to_recorder(agent_id, message)
        if not result or result.get("error") or result.get("success") is False:
            logger.warning(
                "streaming: start for %s got no/err ack (%s)",
                session.session_key, (result or {}).get("error") if result else "no-agent",
            )
            return False

        from datetime import datetime, timezone
        session.status = "running"
        session.agent_id = agent_id
        session.started_at = datetime.now(timezone.utc)
        session.last_activity_at = session.started_at
        await db.flush()

        self._sessions[session.session_key] = _Session(
            session_key=session.session_key,
            agent_id=agent_id,
            workflow_id=session.workflow_id,
            started_at=time.monotonic(),
            max_duration_seconds=session.max_duration_seconds or 3600,
            persona_id=str(getattr(wf, "default_persona_id", "") or "") or None,
            session_persistence=bool(getattr(wf, "session_persistence", False)),
        )
        logger.info("streaming: session %s running on %s", session.session_key, agent_id)
        return True

    async def _assemble_config(self, db, wf, session, agent_id: str) -> dict:
        """Build the ``start_streaming_session`` config the agent injects. Warm
        state is persona-scoped (sessions are not pinned to a fleet agent)."""
        sc = (getattr(wf, "streaming_config", None) or {})
        steps = wf.steps or []
        setup_count = sc.get("setup_steps_count", len(steps))
        config = {
            "target_url": session.target_url,
            "all_steps": steps,
            "setup_steps": steps[:setup_count],
            "setup_steps_count": setup_count,
            "handlers": sc.get("handlers", []),
            "advanced_script": resolve_advanced_script(steps, sc),
            "openai_compat": sc.get("openai_compat"),
            "credentials_encrypted": wf.credentials_encrypted,
            "form_data": wf.form_data,
            "headless": bool(getattr(session, "_headless", True)),
            "fast_mode": getattr(wf, "fast_mode", False),
            "max_duration_seconds": session.max_duration_seconds,
            "workflow_id": wf.id,
            "login_url_patterns": getattr(wf, "login_url_patterns", None) or [],
            "streaming_config": {
                "multi_conversation": sc.get("multi_conversation", False),
                "context_mode": sc.get("context_mode", "shared"),
                "max_concurrent_threads": sc.get("max_concurrent_threads", 5),
            },
        }

        # Persona-scoped warm auth: a persona-linked start encrypts the persona's
        # OTP context + auth session_state into queued_config._consumer_stream_enc.
        # Decrypt in-process and inject (override ONLY the keys the blob carries so
        # the owner's workflow creds/form_data survive).
        consumer = _decrypt_consumer_blob(getattr(session, "queued_config", None))
        if consumer is not None:
            if "credentials_encrypted" in consumer:
                config["credentials_encrypted"] = consumer.get("credentials_encrypted")
            if "form_data" in consumer:
                config["form_data"] = consumer.get("form_data") or {}
            if consumer.get("persona") is not None:
                config["persona"] = consumer.get("persona")
            if consumer.get("session_state") is not None:
                config["session_state"] = consumer.get("session_state")
        return config

    async def end_session(self, db, session_key: str, reason: str = "user_ended") -> None:
        """Tell the agent to stop and mark the row terminal. Idempotent."""
        sess = self._sessions.get(session_key)
        agent_id = sess.agent_id if sess else await self._agent_for(db, session_key)
        if agent_id:
            from routers.user_recorder_ws import push_fire_and_forget
            await push_fire_and_forget(agent_id, {
                "type": "end_streaming_session", "session_key": session_key,
            })
        await self._mark_ended(db, session_key, reason)

    # ------------------------------------------------------------------ #
    # Relay: in-process command / stream / events                        #
    # ------------------------------------------------------------------ #
    async def invoke_streaming(
        self, session_key: str, handler_name: str, data: dict,
        timeout: float = _DEFAULT_INVOKE_TIMEOUT,
    ) -> AsyncIterator[dict]:
        """Send a command and yield ``{"type": "chunk"|"keepalive"|"done", ...}``
        as the agent streams back. Commands are serialized per session so a handler
        finishes before the next starts (the agent runs one browser tab per session)."""
        request_id = uuid.uuid4().hex
        sess = self._sessions.get(session_key)
        agent_id = sess.agent_id if sess else await self._agent_for(None, session_key)
        if not agent_id:
            yield {"type": "done", "data": {"error": "session not running"}}
            return

        waiter = _Waiter(agent_id=agent_id)
        self._waiters[request_id] = waiter
        lock = self._locks.setdefault(session_key, asyncio.Lock())
        try:
            async with lock:
                from routers.user_recorder_ws import push_fire_and_forget
                await push_fire_and_forget(agent_id, {
                    "type": "streaming_command",
                    "session_key": session_key,
                    "action": handler_name,
                    "data": data,
                    "request_id": request_id,
                })
                loop = asyncio.get_event_loop()
                deadline = loop.time() + timeout
                last = loop.time()
                while True:
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        yield {"type": "done", "data": {"error": "timeout"}}
                        return
                    try:
                        frame = await asyncio.wait_for(
                            waiter.queue.get(), timeout=min(remaining, 2),
                        )
                    except asyncio.TimeoutError:
                        if loop.time() - last > _KEEPALIVE_AFTER:
                            yield {"type": "keepalive"}
                            last = loop.time()
                        continue
                    last = loop.time()
                    ftype = frame.get("type")
                    if ftype == "command_response":
                        payload = frame.get("data")
                        if payload is None and frame.get("error") is not None:
                            payload = {"error": frame.get("error")}
                        yield {"type": "done", "data": payload if payload is not None else {}}
                        return
                    if ftype == "stream_chunk":
                        yield {"type": "chunk", "data": frame.get("data", {})}
        finally:
            self._waiters.pop(request_id, None)

    async def invoke(
        self, session_key: str, handler_name: str, data: dict,
        timeout: float = _DEFAULT_INVOKE_TIMEOUT,
    ) -> Optional[dict]:
        """Invoke a handler and return its terminal payload (drains the stream)."""
        final: Optional[dict] = None
        async for ev in self.invoke_streaming(session_key, handler_name, data, timeout=timeout):
            if ev.get("type") == "done":
                final = ev.get("data")
                break
        return final

    async def subscribe_events(self, session_key: str) -> AsyncIterator[dict]:
        """Yield autonomous ``streaming_event`` frames for a session (SSE)."""
        q: asyncio.Queue = asyncio.Queue()
        self._events.setdefault(session_key, set()).add(q)
        try:
            while True:
                yield await q.get()
        finally:
            subs = self._events.get(session_key)
            if subs is not None:
                subs.discard(q)
                if not subs:
                    self._events.pop(session_key, None)

    # ------------------------------------------------------------------ #
    # agent → coordinator frames (called from user_recorder_ws._dispatch) #
    # ------------------------------------------------------------------ #
    def on_agent_frame(self, agent_id: str, inner: dict) -> bool:
        """Route a streaming frame from the AUTHENTICATED ``agent_id`` into the
        in-process registries. Returns True if it was a streaming frame this manager
        owns (so the WS loop stops routing it to a browser socket)."""
        mtype = inner.get("type")
        if mtype in ("command_response", "stream_chunk"):
            waiter = self._waiters.get(inner.get("request_id"))
            # Ownership: only the agent the command was dispatched to may answer it.
            if waiter is not None and waiter.agent_id == agent_id:
                waiter.queue.put_nowait(inner)
            return True
        if mtype == "streaming_event":
            sk = inner.get("session_key")
            sess = self._sessions.get(sk)
            if sess is not None and sess.agent_id == agent_id:
                for q in self._events.get(sk, ()):  # fan out to SSE subscribers
                    q.put_nowait({"type": "event", **inner})
            return True
        if mtype == "streaming_session_started":
            return True  # informational — the session is already marked running
        if mtype == "streaming_session_ended":
            asyncio.create_task(self._on_session_ended(agent_id, inner))
            return True
        return False

    async def _on_session_ended(self, agent_id: str, inner: dict) -> None:
        """Agent reports a session ended on its own (max-duration / handler / error).
        Persist the harvested auth state to the persona (if any) and mark terminal."""
        session_key = inner.get("session_key")
        if not session_key:
            return
        sess = self._sessions.get(session_key)
        # Ownership: only the owning agent may end its session.
        if sess is not None and sess.agent_id != agent_id:
            logger.warning("streaming: %s ended by non-owner agent %s — ignoring", session_key, agent_id)
            return
        try:
            from database import AsyncSessionLocal
            async with AsyncSessionLocal() as db:
                await self._save_persona_state(db, sess, inner.get("session_state"))
                await self._mark_ended(db, session_key, inner.get("reason") or "session_ended")
        except Exception:
            logger.exception("streaming: failed to finalize ended session %s", session_key)

    async def on_agent_disconnect(self, agent_id: str) -> None:
        """A fleet agent dropped — its streaming sessions are dead. End their rows so
        the dispatcher's ``occupied`` set frees and the queue can redispatch."""
        keys = [k for k, s in self._sessions.items() if s.agent_id == agent_id]
        if not keys:
            return
        try:
            from database import AsyncSessionLocal
            async with AsyncSessionLocal() as db:
                for k in keys:
                    await self._mark_ended(db, k, "agent_disconnected")
        except Exception:
            logger.exception("streaming: failed to reap sessions for agent %s", agent_id)

    # ------------------------------------------------------------------ #
    # Janitor: max-duration / stale / orphaned                           #
    # ------------------------------------------------------------------ #
    async def janitor_tick(self, db) -> int:
        """End running rows past their max duration or whose agent is gone. Returns
        the number ended. Called periodically by the streaming queue processor."""
        from models.streaming_session import StreamingSession
        from sqlalchemy import select
        from datetime import datetime, timezone
        from routers.user_recorder_ws import _connections

        rows = (await db.execute(
            select(StreamingSession).where(StreamingSession.status.in_(["running", "ending"]))
        )).scalars().all()
        now = datetime.now(timezone.utc)
        ended = 0
        for r in rows:
            reason = None
            if r.agent_id and r.agent_id not in _connections:
                reason = "agent_offline"
            elif r.started_at and r.max_duration_seconds:
                started = r.started_at if r.started_at.tzinfo else r.started_at.replace(tzinfo=timezone.utc)
                if (now - started).total_seconds() > r.max_duration_seconds:
                    reason = "max_duration"
            if reason:
                await self._end_agent_and_mark(db, r, reason)
                ended += 1
        if ended:
            await db.commit()
        return ended

    async def _end_agent_and_mark(self, db, row, reason: str) -> None:
        if row.agent_id:
            from routers.user_recorder_ws import push_fire_and_forget
            try:
                await push_fire_and_forget(row.agent_id, {
                    "type": "end_streaming_session", "session_key": row.session_key,
                })
            except Exception:
                pass
        from datetime import datetime, timezone
        row.status = "ended"
        row.ended_at = datetime.now(timezone.utc)
        row.end_reason = reason
        self._sessions.pop(row.session_key, None)

    # ------------------------------------------------------------------ #
    # Helpers                                                            #
    # ------------------------------------------------------------------ #
    async def _agent_for(self, db, session_key: str) -> Optional[str]:
        sess = self._sessions.get(session_key)
        if sess:
            return sess.agent_id
        # Durable fallback: the row carries the assigned agent (e.g. after a restart
        # where the in-memory registry is empty).
        try:
            from database import AsyncSessionLocal
            from models.streaming_session import StreamingSession
            from sqlalchemy import select
            ctx = db if db is not None else AsyncSessionLocal()
            if db is not None:
                return await db.scalar(
                    select(StreamingSession.agent_id).where(StreamingSession.session_key == session_key)
                )
            async with ctx as _db:
                return await _db.scalar(
                    select(StreamingSession.agent_id).where(StreamingSession.session_key == session_key)
                )
        except Exception:
            logger.exception("streaming: agent lookup failed for %s", session_key)
            return None

    async def _mark_ended(self, db, session_key: str, reason: str) -> None:
        """Idempotently transition a row to ``ended`` and wake the queue."""
        from models.streaming_session import StreamingSession
        from sqlalchemy import update as _update
        from datetime import datetime, timezone
        res = await db.execute(
            _update(StreamingSession)
            .where(
                StreamingSession.session_key == session_key,
                StreamingSession.status.in_(["running", "starting", "ending", "queued"]),
            )
            .values(status="ended", ended_at=datetime.now(timezone.utc), end_reason=reason)
        )
        await db.commit()
        self._sessions.pop(session_key, None)
        if res.rowcount:
            try:
                from routers.streaming import wake_streaming_queue
                wake_streaming_queue()
            except Exception:
                pass

    async def _save_persona_state(self, db, sess: Optional[_Session], session_state) -> None:
        """Persist harvested browser auth state back to the linked persona (warm
        session for the next run). No-op without persistence or a persona."""
        if not sess or not sess.session_persistence or not sess.persona_id or not session_state:
            return
        try:
            from models.persona import Persona
            from services.persona_service import PersonaService
            persona = await db.get(Persona, sess.persona_id)
            if persona is not None:
                await PersonaService.save_session(db, persona, session_state)
                await db.commit()
        except Exception:
            logger.exception("streaming: failed to save persona session state for %s", sess.session_key)


# Process-wide singleton — the coordinator runs one event loop.
manager = StreamingManager()
