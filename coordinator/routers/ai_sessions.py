"""AI sessions — dispatch proxy for autonomous AI sessions that run on a fleet agent.

The self-host coordinator does NOT run an AI brain/loop of its own. It dispatches a
single ``ai_session_start`` frame to a connected ``writ-agent`` (built with the
``fleet,local`` features), the agent runs the whole autonomous session locally and
replies with ``ai_session_complete`` / ``ai_session_failed``; the coordinator records
the request + outcome so the UI can list past sessions and link to the workflow the
agent recorded.

Endpoints (all admin-scoped, single owner):

  * POST /api/ai-sessions/start — pick an online agent, seal any secret fill values
    under that agent's channel key, persist an AiSession row (status "running"),
    FIRE the ``ai_session_start`` frame to the agent WITHOUT awaiting the (up to
    15-minute) terminal reply, and return the persisted ``running`` row immediately.
    The row is later moved to its terminal state by the WS layer, where the agent's
    ``ai_session_complete`` / ``ai_session_failed`` frame lands (see
    ``routers.user_recorder_ws``). This keeps the HTTP request fast and mirrors the
    desktop daemon's detached-task model.
  * GET  /api/ai-sessions       — list rows, newest first.
  * GET  /api/ai-sessions/{id}  — get one row.

Secret handling mirrors the fleet deploy path exactly: secret ``credentials`` values
are re-sealed under the agent's per-agent channel key into ``credentials_encrypted``
on the wire frame (``routers.fleet._seal_plaintext_for_agent``); non-secret
``available_data`` rides in plaintext. Raw secrets are NEVER stored on the row and
never leave the process unsealed. Agent identity is the AUTHENTICATED agent chosen
from the live in-process fleet registry, never a value from the agent's reply.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models.ai_session import AiSession
from security.dependencies import require_platform_admin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ai-sessions", tags=["AI Sessions"])


class StartAISessionRequest(BaseModel):
    """Start an autonomous AI session on a fleet agent.

    goal              — the natural-language objective the agent pursues.
    entry_url         — optional starting URL opened before the loop.
    available_data    — NON-secret hints (e.g. {"email": "me@x.com"}), plaintext.
    credentials       — OPTIONAL secret fill values {name: value}; re-sealed under the
                        agent channel key into credentials_encrypted (never stored).
    persona_id        — optional persona ALREADY deployed to the agent (opaque handle).
    max_steps         — iteration budget for the agent's loop.
    generate_workflow — record + save a workflow at the end only if true.
    agent_id          — explicit target agent; else an online agent is chosen.
    """

    goal: str = Field(..., min_length=1)
    entry_url: Optional[str] = None
    available_data: Optional[Dict[str, Any]] = None
    credentials: Optional[Dict[str, str]] = None
    persona_id: Optional[str] = None
    max_steps: int = 20
    generate_workflow: bool = True
    agent_id: Optional[str] = None


def _pick_agent(explicit_agent_id: Optional[str]) -> str:
    """Choose the connected fleet agent to run the session, or raise 4xx.

    Reads ONLY the live in-process fleet registry (the same source the dispatcher and
    fleet deploy use). If an explicit agent is named it must be online; otherwise the
    best available online recorder is chosen (most free session slots), matching how
    the coordinator selects an agent everywhere else.
    """
    from routers.user_recorder_ws import (
        _connections,
        get_connected_recorder,
    )

    if explicit_agent_id:
        if explicit_agent_id not in _connections:
            raise HTTPException(
                status_code=409,
                detail=f"Agent {explicit_agent_id} is not connected.",
            )
        return explicit_agent_id

    best = get_connected_recorder()
    if not best:
        raise HTTPException(
            status_code=409,
            detail="No online agent to run the AI session. Connect a fleet agent "
                   "built with the local AI capability (--features fleet,local).",
        )
    return best["agent_id"]


@router.post("/start")
async def start_ai_session(
    body: StartAISessionRequest,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(require_platform_admin),
):
    """Fire one autonomous AI session at a fleet agent and return the ``running`` row.

    Non-blocking: the fast, synchronous parts run inline (pick agent, seal creds,
    persist the row, build the frame), then the ``ai_session_start`` frame is sent to
    the agent WITHOUT awaiting the terminal reply — the agent's ``ai_session_complete``
    / ``ai_session_failed`` frame updates the row from the WS layer. Returns immediately
    so the UI can navigate away while the session runs (mirrors the desktop daemon's
    detached-task model).
    """
    from routers import user_recorder_ws
    from routers.fleet import _resolve_deploy_target, _seal_plaintext_for_agent

    agent_id = _pick_agent(body.agent_id)

    # Seal any secret fill values under the agent's per-agent channel key — the SAME
    # sealing path the fleet deploy uses. Also validates the agent is local-capable +
    # has a channel key (raises 404/409 otherwise) even when there are no credentials,
    # since the agent needs the local AI engine to run the session at all.
    channel_key = _resolve_deploy_target(agent_id)
    credentials_encrypted: Optional[str] = None
    if body.credentials:
        credentials_encrypted = _seal_plaintext_for_agent(
            json.dumps(body.credentials), channel_key
        )

    session_id = str(uuid.uuid4())
    name = f"AI: {body.goal}"[:500]

    # Persist the row FIRST (status "running") so a crash mid-dispatch — or the async
    # gap before the terminal frame arrives — still leaves a durable record the UI
    # can show and the WS layer can find by session_id.
    row = AiSession(
        session_id=session_id,
        agent_id=agent_id,
        name=name,
        goal=body.goal,
        entry_url=body.entry_url,
        status="running",
        generate_workflow=bool(body.generate_workflow),
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)

    frame = {
        "type": "ai_session_start",
        "session_id": session_id,
        "request_id": str(uuid.uuid4()),
        "name": name,
        "goal": body.goal,
        "entry_url": body.entry_url,
        "available_data": body.available_data or {},
        "credentials_encrypted": credentials_encrypted,
        "persona_id": body.persona_id,
        "max_steps": body.max_steps,
        "generate_workflow": bool(body.generate_workflow),
    }

    # FIRE-AND-FORGET: write the frame straight to the agent's socket without awaiting
    # its (up-to-15-minute) terminal reply. `push_fire_and_forget` is the direct
    # in-process send the WS layer exposes over `_connections[agent_id].send_json` and
    # returns False if the send fails / the agent vanished between the pick and now.
    sent = await user_recorder_ws.push_fire_and_forget(agent_id, frame)
    if not sent:
        # The agent went away between _pick_agent and the send. Mark the row terminal
        # so it isn't stuck 'running' forever, and surface a clear 409.
        row.status = "error"
        row.error = "agent_disconnected"
        row.completed_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(row)
        raise HTTPException(
            status_code=409,
            detail="The chosen agent disconnected before the AI session could start.",
        )

    # Return the persisted 'running' row immediately. The terminal state lands later
    # via the WS handler (routers.user_recorder_ws), keyed by session_id.
    return row.to_dict()


@router.post("/{session_pk}/cancel")
async def cancel_ai_session(
    session_pk: int,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(require_platform_admin),
):
    """Force-Stop a running fleet AI session: dispatch an ``ai_session_cancel`` frame to the agent
    holding it (the agent sets the session's cancel flag and its loop aborts mid-turn, then replies
    with a terminal ``ai_session_complete`` that the WS layer records), and mark the row cancelled so
    the UI reflects the Stop immediately. Mirrors the desktop app's force-stop for discovery."""
    from routers import user_recorder_ws

    row = await db.get(AiSession, session_pk)
    if not row:
        raise HTTPException(status_code=404, detail="AI session not found")
    if row.status in ("completed", "failed", "cancelled", "error"):
        return row.to_dict()  # already terminal — nothing to stop

    # Best-effort dispatch: even if the socket is gone, we still mark the row cancelled below so it
    # doesn't hang 'running' forever (the agent, if alive, finalizes independently).
    try:
        await user_recorder_ws.push_fire_and_forget(row.agent_id, {
            "type": "ai_session_cancel",
            "session_id": row.session_id,
            "request_id": str(uuid.uuid4()),
        })
    except Exception:
        pass

    row.status = "cancelled"
    row.error = "Aborted by user"
    row.completed_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(row)
    return row.to_dict()


@router.get("")
async def list_ai_sessions(
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(require_platform_admin),
):
    """List AI sessions, newest first."""
    limit = max(1, min(int(limit), 500))
    rows = (
        await db.execute(
            select(AiSession).order_by(AiSession.created_at.desc()).limit(limit)
        )
    ).scalars().all()
    return {"sessions": [r.to_dict() for r in rows]}


@router.get("/{session_pk}")
async def get_ai_session(
    session_pk: int,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(require_platform_admin),
):
    """Get one AI session by its integer id."""
    row = (
        await db.execute(select(AiSession).where(AiSession.id == session_pk))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=f"AI session {session_pk} not found")
    return row.to_dict()
