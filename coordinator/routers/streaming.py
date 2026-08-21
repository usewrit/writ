"""
Streaming router — serves live streaming sessions NATIVELY, in-process.

The coordinator holds the fleet-agent sockets directly, so this router does NOT
proxy to a separate streaming-service. It drives the whole session lifecycle and
the command/event/OpenAI relay in one process:

1. Validates user auth (JWT, API key, OAuth) and per-plan feature gate.
2. Resolves a recorder agent for session start (IP-distribution aware).
3. Dispatches ``start_streaming_session`` to the agent over the coordinator's
   direct WS and relays commands/events/chat via ``services.streaming_manager``.
4. Serves the OpenAI-compatible surface (chat/responses/models) via
   ``services.streaming_openai``, with conversation affinity across sessions.

Single-owner coordinator: a session's random ``session_key`` is the capability.
"""
import hashlib
import json
import logging
import unicodedata
from typing import Optional, List, Any

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, Request, Query, WebSocket
from pydantic import BaseModel, Field
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from dependencies import get_redis_client
from security.dependencies import get_auth_context, AuthContext
from security.feature_gate import require_feature, feature_killed_for_background

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/streaming", tags=["Streaming"])

# The coordinator runs streaming NATIVELY in-process (StreamingManager +
# streaming_openai): it dispatches start_streaming_session to a fleet agent over
# its direct WS and relays commands/events/chat with plain asyncio — no
# streaming-service sidecar and no ws-gateway. Per-plan enable/disable is enforced
# by require_feature("streaming") on the routes.


async def _verify_session_owner(
    session_key: str,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
) -> AuthContext:
    """Dependency: 404 unless the streaming session exists (single-owner self-host —
    existence is authorization; the session_key is unguessable random hex)."""
    from models.streaming_session import StreamingSession
    owned = await db.execute(
        select(StreamingSession.id).where(
            StreamingSession.session_key == session_key,
        )
    )
    if owned.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return auth


# ── Pydantic Schemas ───────────────────────────────────────────────────────

class SessionStartRequest(BaseModel):
    workflow_id: int
    target_url: str
    max_duration_seconds: int = Field(default=3600, ge=60, le=86400)
    headless: bool = True
    execution_target: str = "auto"
    # Optional form inputs for a streaming session.
    form_data: Optional[dict] = None


class InvokeRequest(BaseModel):
    data: dict = Field(default_factory=dict)
    timeout: float = Field(default=30, ge=1, le=120)


class HandlerDefinition(BaseModel):
    name: str = Field(..., max_length=100)
    type: str = "steps"
    step_range: Optional[list] = None  # [start, end] — handler action steps; prerequisites auto-derived
    input_variables: list = Field(default_factory=list)
    extract_fields: list = Field(default_factory=list)
    code: Optional[str] = Field(None, max_length=50000)
    trigger_config: Optional[dict] = None


class OpenAIChatRequest(BaseModel):
    model: str = "streaming"
    messages: list = Field(...)
    stream: bool = False
    max_tokens: Optional[int] = None
    temperature: Optional[float] = None
    tools: Optional[List[dict]] = None
    tool_choice: Optional[str] = None
    functions: Optional[List[dict]] = None
    function_call: Optional[str] = None
    user: Optional[str] = None


class OpenAIResponsesRequest(BaseModel):
    """OpenAI Responses API request format."""
    model: str = "streaming"
    input: Any = Field(...)
    instructions: Optional[str] = None  # top-level system prompt (Responses API)
    stream: bool = False
    max_output_tokens: Optional[int] = None
    temperature: Optional[float] = None
    tools: Optional[List[dict]] = None
    tool_choice: Optional[Any] = None
    user: Optional[str] = None


# ── Streaming queue (wait for a free agent, start ASAP) ─────────────────────
#
# When a streaming session is requested but no recorder is free, we DON'T fail —
# the user clicked run and wants it to start as soon as an agent is available.
# We create the session as status="queued" and a background processor promotes
# it the instant a recorder frees up (event-driven on agent-connect + a short
# poll fallback). No artificial max-wait timeout.
import asyncio as _asyncio

_streaming_queue_event = _asyncio.Event()

# A claimed ("starting") session that hasn't advanced to running/queued within this
# window is treated as a dead dispatch and reclaimed to "queued". Must exceed the
# streaming-service dispatch timeout (30s) by a wide margin so a legitimately slow
# start is never reclaimed mid-flight.
STREAMING_STALE_CLAIM_SECONDS = 120


def wake_streaming_queue() -> None:
    """Nudge the streaming queue processor to dispatch queued sessions now
    (called when an agent connects, for ASAP start)."""
    try:
        _streaming_queue_event.set()
    except Exception:
        pass


async def _resolve_streaming_recorder(db, wf, *, distribute_ips, occupied_agents, preferred_agent_id):
    """Pick a recorder for a streaming session, or None if none is free.
    Mirrors the distribute-IPs / pack / first-session logic of /sessions/start."""
    from routers.automation import _pick_recorder
    # Streaming is a live, watched session → always route to the fastest box.
    if distribute_ips and occupied_agents:
        for _ in range(5):
            candidate = await _pick_recorder(db, wf, traffic_type="interactive")
            if not candidate:
                return None
            if candidate["agent_id"] not in occupied_agents:
                return candidate
        return None
    if not distribute_ips and occupied_agents:
        pack_preferred = preferred_agent_id if preferred_agent_id in occupied_agents else list(occupied_agents)[0]
        return await _pick_recorder(db, wf, preferred_agent_id=pack_preferred, traffic_type="interactive")
    return await _pick_recorder(db, wf, preferred_agent_id=preferred_agent_id, traffic_type="interactive")


async def _enqueue_streaming_session(db, auth, wf, req, distribute_ips) -> dict:
    """Create a status='queued' streaming session and return a SessionResponse-shaped
    dict. The processor promotes it once an agent is free."""
    from models.streaming_session import StreamingSession
    import uuid as _uuid

    qcount = await db.scalar(
        select(func.count(StreamingSession.id))
        .where(StreamingSession.status == "queued")
    ) or 0
    if qcount >= 20:
        raise HTTPException(429, "Too many queued streaming sessions; try again shortly.")

    session_key = _uuid.uuid4().hex
    sess = StreamingSession(
        workflow_id=wf.id,
        session_key=session_key,
        status="queued",
        target_url=req.target_url,
        max_duration_seconds=req.max_duration_seconds,
        queued_config={
            "target_url": req.target_url,
            "headless": req.headless,
            "execution_target": req.execution_target,
            "max_duration_seconds": req.max_duration_seconds,
            "distribute_ips": distribute_ips,
        },
    )
    db.add(sess)
    await db.commit()
    wake_streaming_queue()
    logger.info(f"[start_session] queued streaming session {session_key} (wf {wf.id}) — waiting for agent")
    return _session_response(sess)


def _session_response(sess) -> dict:
    """Serialize a StreamingSession row to the shape the frontend/OpenAI clients
    expect (matches the old streaming-service response)."""
    def _iso(v):
        return v.isoformat() if v else None
    return {
        "session_key": sess.session_key, "status": sess.status,
        "target_url": sess.target_url, "current_url": sess.current_url,
        "agent_id": sess.agent_id, "workflow_id": sess.workflow_id, "handlers": [],
        "events_emitted": sess.events_emitted or 0,
        "commands_received": sess.commands_received or 0,
        "started_at": _iso(sess.started_at), "last_activity_at": _iso(sess.last_activity_at),
        "ended_at": _iso(sess.ended_at), "end_reason": sess.end_reason,
        "max_duration_seconds": sess.max_duration_seconds, "error_message": sess.error_message,
    }


async def _native_start(db, wf, req, recorder, *, distribute_ips, queued_config_extra=None):
    """Create a StreamingSession row and dispatch it to the picked agent IN-PROCESS
    via the StreamingManager. On the agent's start-ack the row is "running"; if the
    agent doesn't ack, the row falls back to "queued" for the processor to retry on
    another healthy agent (so a single dead/slow agent never fails the user's start)."""
    from models.streaming_session import StreamingSession
    import uuid as _uuid
    from services.streaming_manager import manager

    session_key = _uuid.uuid4().hex
    qc = {
        "target_url": req.target_url, "headless": req.headless,
        "execution_target": req.execution_target,
        "max_duration_seconds": req.max_duration_seconds, "distribute_ips": distribute_ips,
    }
    if queued_config_extra:
        qc.update(queued_config_extra)
    sess = StreamingSession(
        workflow_id=wf.id, session_key=session_key, status="starting",
        target_url=req.target_url, agent_id=recorder["agent_id"],
        max_duration_seconds=req.max_duration_seconds, queued_config=qc,
    )
    db.add(sess)
    await db.flush()
    sess._headless = req.headless
    ok = await manager.start_on_agent(db, wf, sess, recorder["agent_id"])
    if ok:
        await db.commit()
        return _session_response(sess)
    sess.status = "queued"
    sess.agent_id = None
    await db.commit()
    wake_streaming_queue()
    logger.info(f"[start_session] no ack from {recorder['agent_id']} — queued {session_key} for retry")
    return _session_response(sess)


async def streaming_queue_processor():
    """Background loop: promote queued streaming sessions onto recorders as they
    free up. Event-driven (agent-connect wakes it) with a 3s poll fallback. No
    expiry — a queued session waits until an agent is available."""
    from database import AsyncSessionLocal
    from models.streaming_session import StreamingSession
    from models.automation_workflow import AutomationWorkflow

    import time as _time
    _loop_started = _time.monotonic()

    logger.info("Streaming queue processor started")
    while True:
        try:
            try:
                await _asyncio.wait_for(_streaming_queue_event.wait(), timeout=3.0)
            except _asyncio.TimeoutError:
                pass
            _streaming_queue_event.clear()

            async with AsyncSessionLocal() as db:
                from sqlalchemy import update as _update
                from datetime import datetime as _dt, timedelta as _td

                # RECLAIM stale claims first. A session wedged in "starting" past the
                # dispatch window (agent died mid-handshake, or the streaming-service
                # dropped the start request without re-queueing) would otherwise hang
                # forever — the poll below only looks at "queued". Flip it back so it's
                # retried. The threshold sits well above the 30s dispatch timeout so a
                # legitimately-starting session is never yanked out from under itself;
                # coalesce(last_activity_at, created_at) covers rows claimed by older
                # paths that didn't stamp an activity time.
                await db.execute(
                    _update(StreamingSession)
                    .where(
                        StreamingSession.status == "starting",
                        func.coalesce(
                            StreamingSession.last_activity_at,
                            StreamingSession.created_at,
                        ) < _dt.utcnow() - _td(seconds=STREAMING_STALE_CLAIM_SECONDS),
                    )
                    .values(status="queued")
                )
                await db.commit()

                # END orphaned RUNNING sessions. This is the gateway-less OSS
                # coordinator's substitute for the ws-gateway → gateway:results →
                # streaming-service chain that normally publishes a terminal
                # streaming_session_ended: here that chain does not exist, so a
                # session that ends on its own (max-duration / agent-side end) or
                # whose agent is gone would otherwise linger in "running" forever,
                # keeping the agent permanently in the dispatcher's `occupied` set.
                # The in-process fleet map `_connections` is AUTHORITATIVE liveness
                # in this topology (there is no gateway registry), so a running/
                # ending row whose agent holds no live socket is a dead session —
                # end it and let its slot free. ("starting" is intentionally left to
                # the reclaim above, which re-queues rather than kills.)
                #
                # Startup grace: skip this sweep for the first STALE window after the
                # loop starts, so a coordinator restart doesn't reap live sessions in
                # the gap before their agents' sockets reconnect into `_connections`.
                # End running rows past max-duration or whose agent is gone. The
                # StreamingManager owns this sweep (single source of lifecycle truth);
                # the startup grace avoids reaping live sessions in the gap before
                # agents reconnect into _connections after a coordinator restart.
                if (_time.monotonic() - _loop_started) >= STREAMING_STALE_CLAIM_SECONDS:
                    try:
                        from services.streaming_manager import manager as _stream_mgr
                        await _stream_mgr.janitor_tick(db)
                    except Exception:
                        logger.exception("Streaming janitor tick failed")

                queued = (await db.execute(
                    select(StreamingSession)
                    .where(StreamingSession.status == "queued")
                    .order_by(StreamingSession.created_at)
                    .limit(20)
                )).scalars().all()
                if not queued:
                    continue

                for sess in queued:
                    wf = await db.get(AutomationWorkflow, sess.workflow_id)
                    if not wf or wf.workflow_type != "streaming" or not wf.is_active:
                        sess.status = "failed"
                        sess.error_message = "Workflow unavailable"
                        await db.commit()
                        continue

                    # FEATURE GATE: skip promotion while the feature is disabled AND
                    # "halt background runs" is set. Leave the session queued (no
                    # expiry) so it resumes once the gate is re-enabled.
                    _gate_feature = "marketplace" if getattr(wf, "is_installed", False) else "streaming"
                    if await feature_killed_for_background(_gate_feature, db):
                        continue

                    qc = sess.queued_config or {}
                    wf._runtime_execution_target = qc.get("execution_target", "auto")

                    existing = (await db.execute(
                        select(StreamingSession.agent_id)
                        .where(StreamingSession.workflow_id == sess.workflow_id)
                        .where(StreamingSession.status.in_(["running", "starting"]))
                    )).scalars().all()
                    occupied = set(a for a in existing if a)

                    preferred = None
                    if wf.session_persistence:
                        try:
                            from services.session_state_service import SessionStateService
                            pref = await SessionStateService.get_preferred_affinity(db, wf.id)
                            if pref and not SessionStateService.is_expired(pref, wf.session_ttl_seconds):
                                preferred = pref.agent_id
                        except Exception:
                            pass

                    recorder = await _resolve_streaming_recorder(
                        db, wf,
                        distribute_ips=qc.get("distribute_ips", False),
                        occupied_agents=occupied,
                        preferred_agent_id=preferred,
                    )
                    if not recorder:
                        continue  # still no free agent — keep waiting

                    # ATOMIC CLAIM: compare-and-swap queued→starting and COMMIT *before*
                    # dispatching. This is the keystone that eliminates the whole class
                    # of races. The poll above only selects status=="queued", so the
                    # instant a session is claimed it leaves the queue — no matter how
                    # long the agent takes to ack (the old code left it "queued" for the
                    # entire 30s dispatch await, so this 3s loop re-grabbed it). The
                    # `WHERE status=='queued'` guard makes this a single-winner CAS
                    # across every backend worker, so exactly ONE dispatch is ever in
                    # flight per session: no duplicate browser, and the streaming
                    # service's per-session result future can't be clobbered. agent_id +
                    # last_activity_at are stamped so the stale-claim sweep can recover a
                    # dispatch that dies silently.
                    claimed = (await db.execute(
                        _update(StreamingSession)
                        .where(
                            StreamingSession.id == sess.id,
                            StreamingSession.status == "queued",
                        )
                        .values(
                            status="starting",
                            agent_id=recorder["agent_id"],
                            last_activity_at=_dt.utcnow(),
                        )
                        .returning(StreamingSession.id)
                    )).scalar_one_or_none()
                    await db.commit()
                    if claimed is None:
                        continue  # lost the race — another worker/cycle already claimed it

                    # Promote NATIVELY: dispatch start_streaming_session to the agent
                    # in-process (no streaming-service). start_on_agent flips the row to
                    # "running" on the agent's start-ack; on no/err ack it returns False
                    # and we release the claim back to "queued" for retry on the next
                    # healthy agent (instead of wedging in "starting" until the sweep).
                    async def _release_claim():
                        try:
                            await db.execute(
                                _update(StreamingSession)
                                .where(
                                    StreamingSession.id == sess.id,
                                    StreamingSession.status == "starting",
                                )
                                .values(status="queued")
                            )
                            await db.commit()
                        except Exception:
                            await db.rollback()

                    try:
                        sess._headless = qc.get("headless", True)
                        from services.streaming_manager import manager as _stream_mgr
                        ok = await _stream_mgr.start_on_agent(db, wf, sess, recorder["agent_id"])
                        if ok:
                            await db.commit()
                            logger.info(f"[streaming_queue] promoted {sess.session_key} → {recorder['agent_id']}")
                        else:
                            await _release_claim()
                    except Exception as e:
                        logger.warning(f"[streaming_queue] promote {sess.session_key} failed: {e}")
                        await _release_claim()
                # All session-state writes above are committed per-session (atomic claim,
                # reclaim, failure-release); nothing should be pending. Defensive rollback
                # discards any uncommitted read state before the session closes.
                await db.rollback()

        except _asyncio.CancelledError:
            logger.info("Streaming queue processor stopped")
            break
        except Exception as e:
            logger.error(f"Streaming queue processor error: {e}", exc_info=True)


# ── Streaming workflow + own-persona resolution ─────────────────────────────

def _encrypt_consumer_stream_blob(consumer_data: dict) -> str:
    """Master-key Fernet-encrypt the buyer's resolved streaming data so it can ride
    through queued_config WITHOUT plaintext creds/session at rest.

    The session_state carries the caller's auth cookies/localStorage and the
    persona_cfg carries an otp_token — both sensitive — so the WHOLE blob is
    encrypted with the SAME master key the run path uses (credentials_encrypted is
    already a master-key Fernet blob; we wrap it again here so a single decrypt
    yields everything). Decrypted only in-process by the StreamingManager
    (_decrypt_consumer_blob) right before it builds the agent start config; never
    logged.

    Serializes with default=str so a UUID persona_id (in persona_cfg) is JSON-safe —
    the agent reads persona_id as a string, identical to the run-path dispatch."""
    import json as _json
    from routers.automation import get_fernet
    payload = _json.dumps(consumer_data or {}, default=str).encode()
    return get_fernet().encrypt(payload).decode()


async def _resolve_own_streaming_persona(db, auth, wf):
    """Build the OWN session's persona OTP context (+ persona auth session_state),
    or None if the workflow has no default persona / 2FA is not configured.

    Mirrors the own-RUN persona resolution (routers/automation.py 5568): the returned
    dict is the same consumer_data shape the hosted build used, so the
    existing encrypt → StreamingManager decrypt → config["persona"] / session_state
    plumbing carries it unchanged. The otp_token is a short-lived mint token (the
    TOTP seed / mailbox creds never leave the backend); it is encrypted at rest by
    the caller. The persona is the WORKFLOW OWNER's own persona only.
    """
    persona_id = getattr(wf, "default_persona_id", None)
    if not persona_id:
        return None
    from config import settings
    from services.persona_service import PersonaService
    persona = await PersonaService.get_owned(db, persona_id)
    if not persona or not persona.is_active:
        # A missing/inactive persona must not block an own streaming session that may
        # not even need 2FA — degrade to no persona context (no OTP serving).
        return None
    persona_cfg = {
        "persona_id": persona.id,
        "twofa_method": persona.twofa_method or "none",
        "otp_extract_config": persona.otp_extract_config or {},
        # Short-lived token the agent presents to POST /api/personas/{id}/otp. The
        # TOTP seed / mailbox tokens never leave the backend.
        "otp_token": PersonaService.make_otp_token(persona.id, None),
        "coordinator_url": settings.coordinator_url,
    }
    # The persona's saved auth (cookies/localStorage) so the live browser is logged
    # in before any 2FA challenge — same as the run path's persona_session.
    #
    # VERIFIED, not merely loaded. A streamed browser is the one place a USER may
    # sign in by hand, and restoring a dead session first is what poisons that: the
    # sign-in page renders under the stale session, so its CSRF token is rejected
    # and the login silently fails. Falls back to the stored blob when the refresh
    # can't run — never worse than handing it over unconditionally.
    persona_session = None
    try:
        from services.persona_login import ensure_fresh_session
        _ok, _err, _fresh = await ensure_fresh_session(persona.id)
        if _ok and _fresh:
            persona_session = _fresh
        else:
            logger.info(
                f"Streaming: persona {persona.id} could not be freshened ({_err}); "
                f"handing over its stored session as-is"
            )
    except Exception as _e:  # noqa: BLE001 — never block a stream on the guard
        logger.warning(f"Streaming: persona {persona.id} session check failed: {_e}")
    if persona_session is None:
        persona_session = PersonaService.load_session(persona)
    return {
        "persona": persona_cfg,
        "session_state": persona_session,
    }


async def _resolve_streaming_workflow(db, auth, req):
    """Resolve the streaming workflow to run for THIS caller (single-user self-host).

    Returns (workflow, None, consumer_data):
      - The caller owns req.workflow_id. consumer_data carries the workflow's own
        default persona OTP/2FA context (or None), forwarded ENCRYPTED to the live
        browser so a login that hits a 2FA challenge can be served.

    (The hosted build's installed-workflow proxy branch is not part of this
    coordinator.)
    """
    from models.automation_workflow import AutomationWorkflow

    wf = await db.get(AutomationWorkflow, req.workflow_id)
    if wf is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    if wf.workflow_type != "streaming":
        raise HTTPException(status_code=400, detail="Workflow is not a streaming workflow")

    # Resolve the workflow's default persona's OTP-mint context so a login that hits a
    # 2FA/code challenge can be served during the session — the shape the streaming
    # engine consumes as config["persona"]. The persona_cfg carries a short-lived
    # otp_token (sensitive) → it rides ENCRYPTED through the consumer_data blob path,
    # never plaintext. No persona → (wf, None, None).
    own_consumer = await _resolve_own_streaming_persona(db, auth, wf)
    return wf, None, own_consumer


# ── Session lifecycle ───────────────────────────────────────────────────────

@router.post("/sessions/start")
async def start_session(
    req: SessionStartRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
    _gate=Depends(require_feature("streaming")),
):
    """Start a streaming session. Resolves agent with IP distribution awareness.

    The effective advanced script + its declared callables are resolved in-process
    by the StreamingManager when it assembles the agent start config (an
    advanced_script STEP is authoritative; it falls back to
    streaming_config.advanced_script for legacy workflows), so this endpoint does
    not need to materialize them.
    """
    if auth.auth_method == "api_key":
        auth.require_scope("streaming", "write")

    from routers.automation import _pick_recorder
    from models.automation_workflow import AutomationWorkflow
    from models.streaming_session import StreamingSession

    # Resolve the caller's OWN streaming workflow (+ optional 2FA persona data for the
    # live browser). marketplace_ctx is always None on the self-host coordinator.
    wf, marketplace_ctx, consumer_data = await _resolve_streaming_workflow(db, auth, req)

    sc = wf.streaming_config or {}
    distribute_ips = sc.get("distribute_ips", False)

    # Warm session: prefer the agent that holds the saved session-state affinity so
    # a consecutive re-run lands on the box that can restore it. Without this, a
    # fresh start picks any agent and the warm session is never found.
    preferred_agent_id = None
    if wf.session_persistence:
        try:
            from services.session_state_service import SessionStateService
            preferred = await SessionStateService.get_preferred_affinity(db, wf.id)
            if preferred and preferred.session_state_encrypted and \
                    not SessionStateService.is_expired(preferred, wf.session_ttl_seconds):
                preferred_agent_id = preferred.agent_id
        except Exception as e:
            logger.warning(f"[start_session] Failed to load session affinity for wf {wf.id}: {e}")

    # The session always runs on `wf` — use wf.id.
    _run_workflow_id = wf.id

    # Get agents already running sessions for this workflow
    existing = (await db.execute(
        select(StreamingSession.agent_id)
        .where(StreamingSession.workflow_id == _run_workflow_id)
        .where(StreamingSession.status.in_(["running", "starting"]))
    )).scalars().all()
    occupied_agents = set(a for a in existing if a)

    wf._runtime_execution_target = req.execution_target

    recorder = await _resolve_streaming_recorder(
        db, wf,
        distribute_ips=distribute_ips,
        occupied_agents=occupied_agents,
        preferred_agent_id=preferred_agent_id,
    )

    # OWN session WITH a 2FA persona: the persona_cfg (otp_token) + persona auth
    # session_state must reach the live browser ENCRYPTED, so we pre-create the row
    # carrying the same _consumer_stream_enc blob the streaming-service decrypts into
    # config["persona"]/session_state. NO billing reserve (own-allotment is free).
    if consumer_data is not None:
        return await _start_own_persona_streaming_session(
            db, auth, wf, req, consumer_data,
            recorder=recorder, distribute_ips=distribute_ips,
        )

    if recorder:
        return await _native_start(db, wf, req, recorder, distribute_ips=distribute_ips)

    # No free agent → queue and WAIT (start ASAP when one frees up). The user
    # clicked run; the session should execute as soon as an agent is available
    # rather than fail on a timer.
    return await _enqueue_streaming_session(db, auth, wf, req, distribute_ips)


async def _start_own_persona_streaming_session(
    db, auth, wf, req, consumer_data, *, recorder, distribute_ips,
):
    """Pre-create + dispatch an OWN streaming session that carries a 2FA persona.

    The persona OTP context + persona auth session_state are encrypted into
    _consumer_stream_enc so no plaintext otp_token / auth cookies ride through
    queued_config / the DB / logs. The StreamingManager decrypts the blob in-process
    (_decrypt_consumer_blob) and injects config["persona"] + session_state before
    building the agent start config. Own-allotment streaming is free, so there is no
    gate_and_hold / settle here.
    """
    from models.streaming_session import StreamingSession
    import uuid as _uuid

    qcount = await db.scalar(
        select(func.count(StreamingSession.id))
        .where(StreamingSession.status == "queued")
    ) or 0
    if qcount >= 20:
        raise HTTPException(429, "Too many queued streaming sessions; try again shortly.")

    _stream_blob = _encrypt_consumer_stream_blob(consumer_data or {})
    session_key = _uuid.uuid4().hex
    sess = StreamingSession(
        workflow_id=wf.id,
        session_key=session_key,
        status="queued",
        target_url=req.target_url,
        max_duration_seconds=req.max_duration_seconds,
        queued_config={
            "target_url": req.target_url,
            "headless": req.headless,
            "execution_target": req.execution_target,
            "max_duration_seconds": req.max_duration_seconds,
            "distribute_ips": distribute_ips,
            # ENCRYPTED persona OTP context + persona auth session_state (master-key
            # Fernet). The StreamingManager decrypts it in-process and injects the
            # owner's persona into config["persona"]/session_state at start. NEVER
            # plaintext.
            "_consumer_stream_enc": _stream_blob,
        },
    )
    db.add(sess)
    await db.commit()

    # Promote the exact pre-created row natively (reusing our session_key) so the
    # StreamingManager's decrypt-and-inject path runs. If no agent is free, the
    # coordinator's queue processor promotes it ASAP — the encrypted blob survives
    # in queued_config.
    if recorder:
        # Promote the exact pre-created row (its queued_config carries the encrypted
        # persona blob, which _assemble_config decrypts + injects) natively.
        sess.status = "starting"
        sess.agent_id = recorder["agent_id"]
        sess._headless = req.headless
        from services.streaming_manager import manager
        ok = await manager.start_on_agent(db, wf, sess, recorder["agent_id"])
        if ok:
            await db.commit()
            return _session_response(sess)
        sess.status = "queued"
        sess.agent_id = None
        await db.commit()

    wake_streaming_queue()
    logger.info(
        f"[start_session] queued own 2FA-persona streaming session {session_key} "
        f"(wf {wf.id}) — waiting for agent"
    )
    return _session_response(sess)


@router.get("/sessions")
async def list_sessions(
    status: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    if auth.auth_method == "api_key":
        auth.require_scope("streaming", "read")
    from models.streaming_session import StreamingSession
    q = select(StreamingSession).order_by(StreamingSession.created_at.desc()).limit(limit)
    if status:
        q = q.where(StreamingSession.status == status)
    rows = (await db.execute(q)).scalars().all()
    return [_session_response(r) for r in rows]


@router.get("/sessions/{session_key}")
async def get_session(
    session_key: str,
    auth: AuthContext = Depends(_verify_session_owner),
    db: AsyncSession = Depends(get_db),
):
    if auth.auth_method == "api_key":
        auth.require_scope("streaming", "read")
    from models.streaming_session import StreamingSession
    row = (await db.execute(
        select(StreamingSession).where(StreamingSession.session_key == session_key)
    )).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Session not found")
    return _session_response(row)


@router.post("/sessions/{session_key}/end")
async def end_session(
    session_key: str,
    auth: AuthContext = Depends(_verify_session_owner),
    db: AsyncSession = Depends(get_db),
):
    if auth.auth_method == "api_key":
        auth.require_scope("streaming", "write")
    # StreamingManager tells the agent to stop, marks the row terminal (idempotent),
    # and wakes the queue so the freed agent slot is redispatchable.
    from services.streaming_manager import manager
    await manager.end_session(db, session_key, "user_ended")
    return {"session_key": session_key, "status": "ended"}


# ── Handler invocation ──────────────────────────────────────────────────────

@router.post("/sessions/{session_key}/invoke/{handler_name}")
async def invoke_handler(
    session_key: str,
    handler_name: str,
    req: InvokeRequest,
    auth: AuthContext = Depends(_verify_session_owner),
):
    if auth.auth_method == "api_key":
        auth.require_scope("streaming", "write")

    from services.streaming_manager import manager
    result = await manager.invoke(
        session_key, handler_name, req.data or {}, timeout=req.timeout,
    )
    if result is None:
        raise HTTPException(status_code=504, detail="Handler timed out or session not running")
    return result


# ── Dynamic handler management ──────────────────────────────────────────────
# Handlers are declared in the workflow's streaming_config / advanced_script and
# injected into the browser at session start. The native coordinator does not
# support mutating a live session's handler set (the cloud streaming-service kept
# a server-side registry the ws-gateway re-injected); add/remove therefore report
# a clear 501 rather than silently no-op. Edit the workflow to change handlers.

@router.post("/sessions/{session_key}/handlers")
async def add_handler(
    session_key: str,
    handler: HandlerDefinition,
    auth: AuthContext = Depends(_verify_session_owner),
):
    if auth.auth_method == "api_key":
        auth.require_scope("streaming", "write")
    raise HTTPException(
        status_code=501,
        detail="Dynamic handler management is not supported; declare handlers on the workflow.",
    )


@router.delete("/sessions/{session_key}/handlers/{handler_name}")
async def remove_handler(
    session_key: str,
    handler_name: str,
    auth: AuthContext = Depends(_verify_session_owner),
):
    if auth.auth_method == "api_key":
        auth.require_scope("streaming", "write")
    raise HTTPException(
        status_code=501,
        detail="Dynamic handler management is not supported; declare handlers on the workflow.",
    )


# ── Workflow-scoped routes (resolve active session automatically) ──────────


def _normalize_fp(text: str) -> str:
    """Normalize text for fingerprinting (same logic as streaming-service)."""
    text = unicodedata.normalize("NFC", text)
    return " ".join(text.strip().split())[:200]


def _conv_fingerprint(messages: list) -> Optional[str]:
    """Stable conversation fingerprint from first user + first assistant message."""
    first_user = None
    first_assistant = None
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if role == "user" and first_user is None:
            if isinstance(content, list):
                parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
                first_user = _normalize_fp(" ".join(parts))
            elif isinstance(content, str) and content.strip():
                first_user = _normalize_fp(content)
        elif role == "assistant" and first_assistant is None:
            if isinstance(content, str) and len(content.strip()) > 3:
                first_assistant = _normalize_fp(content)
        if first_user and first_assistant:
            break
    if not first_user:
        return None
    raw = first_user + ("|" + first_assistant if first_assistant else "")
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


async def _get_active_sessions(db: AsyncSession, workflow_id: int) -> List[str]:
    """Get all active session keys for a workflow."""
    from models.streaming_session import StreamingSession
    result = await db.execute(
        select(StreamingSession.session_key)
        .where(StreamingSession.workflow_id == workflow_id)
        .where(StreamingSession.status.in_(["running"]))
        .order_by(StreamingSession.started_at.asc())
    )
    return [r for r in result.scalars().all()]


async def _resolve_workflow_session(
    db: AsyncSession, workflow_id: int,
    session_key: str = None,
) -> str:
    """Find an active streaming session for a workflow.

    If session_key is provided, validates it.
    Otherwise returns the most recent active session.
    """
    from models.streaming_session import StreamingSession

    if session_key:
        result = await db.execute(
            select(StreamingSession.session_key)
            .where(StreamingSession.session_key == session_key)
            .where(StreamingSession.workflow_id == workflow_id)
            .where(StreamingSession.status.in_(["running", "starting"]))
        )
        row = result.scalar_one_or_none()
        if not row:
            raise HTTPException(status_code=404, detail=f"Session {session_key} not found or not active.")
        return row

    result = await db.execute(
        select(StreamingSession.session_key)
        .where(StreamingSession.workflow_id == workflow_id)
        .where(StreamingSession.status.in_(["running", "starting"]))
        .order_by(StreamingSession.started_at.desc())
        .limit(1)
    )
    row = result.scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="No active session for this workflow. Start a session first.")
    return row


async def _route_conversation(
    redis_client: aioredis.Redis,
    db: AsyncSession,
    workflow_id: int,
    messages: list,
    explicit_session: Optional[str] = None,
    explicit_user: Optional[str] = None,
) -> str:
    """
    Route an OpenAI API call to the right session with conversation affinity.

    1. Explicit X-Session-Key header → use it
    2. Conversation fingerprint → cached session affinity
    3. New conversation → round-robin across active sessions (least-loaded)

    Once a conversation starts on a session, all subsequent messages
    in that conversation go to the same session.
    """
    sessions = await _get_active_sessions(db, workflow_id)
    if not sessions:
        raise HTTPException(status_code=404, detail="No active sessions. Start a session first.")

    # Explicit targeting
    if explicit_session:
        if explicit_session in sessions:
            return explicit_session
        raise HTTPException(status_code=404, detail=f"Session {explicit_session} not active.")

    # Single session — no routing needed
    if len(sessions) == 1:
        return sessions[0]

    # ── Multi-session: conversation affinity ──

    # Build fingerprints
    fp = _conv_fingerprint(messages)
    ufp = None
    for msg in messages:
        if msg.get("role") == "user":
            c = msg.get("content", "")
            text = c if isinstance(c, str) else " ".join(p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text")
            if text.strip():
                ufp = hashlib.sha256(_normalize_fp(text).encode()).hexdigest()[:16]
                break

    # Check affinity cache
    for check_fp in [fp, ufp]:
        if not check_fp:
            continue
        cached = await redis_client.get(f"session:affinity:{workflow_id}:{check_fp}")
        if cached and cached in sessions:
            logger.debug(f"Session affinity hit: wf={workflow_id} → {cached}")
            return cached

    # ── New conversation: pick least-loaded session ──
    # Use round-robin with a counter (simple, fair)
    rr_key = f"session:rr:{workflow_id}"
    idx = await redis_client.incr(rr_key)
    await redis_client.expire(rr_key, 86400)
    chosen = sessions[idx % len(sessions)]

    # Cache affinity
    effective = chosen
    for cache_fp in [fp, ufp]:
        if cache_fp:
            await redis_client.set(f"session:affinity:{workflow_id}:{cache_fp}", effective, ex=86400)

    logger.info(f"Session routed: wf={workflow_id} → {chosen} (round-robin, {len(sessions)} active)")
    return chosen


def _extract_text_from_content(content) -> str:
    """Extract plain text from content that may be a string or OpenAI multimodal blocks list."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(parts)
    return str(content) if content else ""


async def _cache_session_affinity_for_next_turn(
    redis_client: aioredis.Redis,
    workflow_id: int,
    session_key: str,
    messages: list,
    response_content,
):
    """Pre-cache the enriched fingerprint so the next turn routes to the same session."""
    text_content = _extract_text_from_content(response_content)
    if not text_content or len(text_content.strip()) < 3:
        return
    first_user = None
    for msg in messages:
        if msg.get("role") == "user":
            c = msg.get("content", "")
            text = c if isinstance(c, str) else " ".join(p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text")
            if text.strip():
                first_user = _normalize_fp(text)
                break
    if not first_user:
        return
    try:
        enriched = first_user + "|" + _normalize_fp(text_content)
        fp = hashlib.sha256(enriched.encode()).hexdigest()[:16]
        await redis_client.set(f"session:affinity:{workflow_id}:{fp}", session_key, ex=86400)
    except Exception:
        pass


@router.post("/workflows/{workflow_id}/v1/chat/completions")
async def workflow_openai_chat(
    workflow_id: int,
    req: OpenAIChatRequest,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
    redis_client: aioredis.Redis = Depends(get_redis_client),
):
    """OpenAI-compatible chat completions with multi-session load balancing.

    When multiple sessions are active, conversations are routed with affinity:
    same conversation always goes to the same session/agent.
    New conversations are round-robin distributed across sessions.

    Use X-Session-Key header to force a specific session.
    """
    if auth.auth_method == "api_key":
        auth.require_scope("streaming", "write")

    explicit_session = request.headers.get("x-session-key")
    session_key = await _route_conversation(
        redis_client, db, workflow_id,
        req.messages, explicit_session=explicit_session, explicit_user=req.user,
    )

    # Route to the chosen session (native handler).
    resp = await openai_chat_completions(session_key, req, auth, db)

    # Cache affinity for next turn (non-streaming only — a StreamingResponse can't be inspected)
    if not req.stream and isinstance(resp, dict):
        content = ""
        choices = resp.get("choices", [])
        if choices:
            content = choices[0].get("message", {}).get("content", "")
        # content may be a string or multimodal list — _cache handles both
        if content:
            await _cache_session_affinity_for_next_turn(
                redis_client, workflow_id, session_key, req.messages, content,
            )

    return resp


@router.post("/workflows/{workflow_id}/v1/responses")
async def workflow_openai_responses(
    workflow_id: int,
    req: OpenAIResponsesRequest,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
    redis_client: aioredis.Redis = Depends(get_redis_client),
):
    """OpenAI Responses API with multi-session load balancing.

    Returns multimodal output (text + images) in Responses API format.
    """
    if auth.auth_method == "api_key":
        auth.require_scope("streaming", "write")

    # Normalize input to messages for routing
    if isinstance(req.input, str):
        messages = [{"role": "user", "content": req.input}]
    elif isinstance(req.input, list):
        messages = req.input
    else:
        messages = [{"role": "user", "content": str(req.input)}]

    explicit_session = request.headers.get("x-session-key")
    session_key = await _route_conversation(
        redis_client, db, workflow_id,
        messages, explicit_session=explicit_session, explicit_user=req.user,
    )

    # Route to the session-scoped responses endpoint (native handler).
    resp = await openai_responses(session_key, req, auth, db)

    # Cache affinity for non-streaming
    if not req.stream and isinstance(resp, dict):
        output = resp.get("output", [])
        text_parts = []
        for item in output:
            if item.get("type") == "message":
                for c in item.get("content", []):
                    if c.get("type") == "output_text":
                        text_parts.append(c.get("text", ""))
        content = "\n".join(text_parts)
        if content:
            await _cache_session_affinity_for_next_turn(
                redis_client, workflow_id, session_key, messages, content,
            )

    return resp


@router.get("/workflows/{workflow_id}/v1/models")
async def workflow_openai_models(
    workflow_id: int,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """List models for a workflow's active session."""
    if auth.auth_method == "api_key":
        auth.require_scope("streaming", "read")
    session_key = await _resolve_workflow_session(db, workflow_id)
    return await openai_models(session_key, auth, db)


@router.post("/workflows/{workflow_id}/command")
async def workflow_command(
    workflow_id: int,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Send a command to the workflow's active session."""
    if auth.auth_method == "api_key":
        auth.require_scope("streaming", "write")
    session_key = await _resolve_workflow_session(db, workflow_id)
    body = await request.json()
    action = body.get("action") or body.get("handler") or "default_handler"
    from services.streaming_manager import manager
    result = await manager.invoke(
        session_key, action, body.get("data") or {}, timeout=body.get("timeout", 60),
    )
    if result is None:
        raise HTTPException(status_code=504, detail="Command timed out or session not running")
    return result


@router.get("/workflows/{workflow_id}/events")
async def workflow_events(
    workflow_id: int,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """SSE events from the workflow's active session."""
    if auth.auth_method == "api_key":
        auth.require_scope("streaming", "read")
    session_key = await _resolve_workflow_session(db, workflow_id)
    return await events_sse(session_key, auth)


# ── OpenAI-compat (session-scoped, native) ─────────────────────────────────

# The OpenAI-compatible surface is served NATIVELY by services/streaming_openai.py:
# it assembles the turn (tool-calling + multimodal + multi-conversation thread
# routing), relays through StreamingManager, and shapes the OpenAI response. The
# default browser handler for the OpenAI surface is "default_handler" (the cloud
# convention). SSE generators open their OWN DB session (AsyncSessionLocal): the
# request-scoped `db` from Depends is closed once the endpoint returns, but the
# generator runs afterward for the life of the stream.
_OPENAI_HANDLER = "default_handler"


def _sse_response(gen):
    from fastapi.responses import StreamingResponse
    return StreamingResponse(
        gen,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-store", "X-Accel-Buffering": "no"},
    )


@router.get("/sessions/{session_key}/v1/models")
async def openai_models(
    session_key: str,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    from services.streaming_openai import list_models
    from models.streaming_session import StreamingSession
    from models.automation_workflow import AutomationWorkflow
    row = (await db.execute(
        select(StreamingSession).where(StreamingSession.session_key == session_key)
    )).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Session not found")
    wf = await db.get(AutomationWorkflow, row.workflow_id)
    return list_models(row.workflow_id, (getattr(wf, "streaming_config", None) or {}))


@router.post("/sessions/{session_key}/v1/chat/completions")
async def openai_chat_completions(
    session_key: str,
    req: OpenAIChatRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    if auth.auth_method == "api_key":
        auth.require_scope("streaming", "write")
    from services import streaming_openai
    body = req.model_dump()

    if req.stream:
        async def _gen():
            from database import AsyncSessionLocal
            async with AsyncSessionLocal() as sdb:
                async for chunk in streaming_openai.chat_completions_stream(
                    sdb, session_key, body, handler_name=_OPENAI_HANDLER, thread_routing=True,
                ):
                    yield chunk
        return _sse_response(_gen())

    try:
        return await streaming_openai.chat_completions(
            db, session_key, body, handler_name=_OPENAI_HANDLER, thread_routing=True,
        )
    except TimeoutError:
        raise HTTPException(status_code=504, detail="Handler timed out")


@router.post("/sessions/{session_key}/v1/responses")
async def openai_responses(
    session_key: str,
    req: OpenAIResponsesRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """OpenAI Responses API (session-scoped, native). Multimodal output (text + images)."""
    if auth.auth_method == "api_key":
        auth.require_scope("streaming", "write")
    from services import streaming_openai
    body = req.model_dump()

    if req.stream:
        async def _gen():
            from database import AsyncSessionLocal
            async with AsyncSessionLocal() as sdb:
                async for chunk in streaming_openai.responses_stream(
                    sdb, session_key, body, handler_name=_OPENAI_HANDLER,
                ):
                    yield chunk
        return _sse_response(_gen())

    try:
        return await streaming_openai.responses(
            db, session_key, body, handler_name=_OPENAI_HANDLER,
        )
    except TimeoutError:
        raise HTTPException(status_code=504, detail="Handler timed out")


# ── SSE events (native, in-process) ────────────────────────────────────────

@router.get("/sessions/{session_key}/events")
async def events_sse(
    session_key: str,
    auth: AuthContext = Depends(get_auth_context),
):
    """SSE stream of a session's autonomous events, sourced in-process from the
    StreamingManager (agent `streaming_event` frames fan out to per-session
    subscriber queues — no streaming-service, no Redis)."""
    if auth.auth_method == "api_key":
        auth.require_scope("streaming", "read")

    async def _sse():
        from services.streaming_manager import manager
        # Periodic comment lines keep the connection alive through idle proxies.
        async for event in manager.subscribe_events(session_key):
            yield f"data: {json.dumps(event)}\n\n"

    from fastapi.responses import StreamingResponse
    return StreamingResponse(
        _sse(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "X-Accel-Buffering": "no",
        },
    )


# ── WebSocket bridge (native, in-process) ──────────────────────────────────

@router.websocket("/sessions/{session_key}/ws")
async def session_ws_proxy(websocket: WebSocket, session_key: str):
    """
    Native bidirectional WS: the client authenticates via a first message, then
    sends {action, data, request_id} command frames which are relayed to the agent
    in-process (StreamingManager); command chunks + autonomous events stream back.
    """
    import asyncio

    await websocket.accept()

    # Authenticate the user via first message
    try:
        first_msg_raw = await websocket.receive_text()
        first_msg = json.loads(first_msg_raw)
    except Exception:
        await websocket.close(code=4001, reason="Auth message required")
        return

    token = first_msg.get("token", "")
    if not token:
        await websocket.close(code=4001, reason="Token required")
        return

    # Validate the JWT
    from security.jwt import decode_access_token
    try:
        payload = decode_access_token(token)
        user_id = payload.get("user_id")
        if not user_id:
            await websocket.close(code=4001, reason="Invalid token")
            return
    except Exception:
        await websocket.close(code=4001, reason="Invalid token")
        return

    # Native bidirectional bridge (no streaming-service). The client sends
    # {"action": handler, "data": {...}, "request_id": id}; each command streams
    # back {"type": "chunk"|"done", "request_id": id, "data": ...}. Autonomous
    # session events are pushed as {"type": "event", ...}.
    from services.streaming_manager import manager

    async def _client_commands():
        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    frame = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                action = frame.get("action") or "default_handler"
                data = frame.get("data") or {}
                rid = frame.get("request_id")
                # Stream this command's chunks/terminal back to the client, tagged
                # with its request_id, without blocking the read loop.
                async def _run(a=action, d=data, r=rid):
                    async for ev in manager.invoke_streaming(session_key, a, d):
                        if ev.get("type") == "keepalive":
                            continue
                        await websocket.send_text(json.dumps({
                            "type": ev.get("type"), "request_id": r, "data": ev.get("data"),
                        }))
                asyncio.create_task(_run())
        except Exception:
            pass

    async def _session_events():
        try:
            async for event in manager.subscribe_events(session_key):
                await websocket.send_text(json.dumps(event))
        except Exception:
            pass

    reader = asyncio.create_task(_client_commands())
    events = asyncio.create_task(_session_events())
    try:
        done, pending = await asyncio.wait(
            [reader, events], return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
    except Exception as e:
        logger.error(f"WS bridge error: {e}")
    finally:
        try:
            await websocket.close()
        except Exception:
            pass
