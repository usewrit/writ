"""
HTTP endpoints for managing user-hosted recorders.

Provides CRUD for user-hosted recorder agents:
- List connected agents
- Rename/update agent config
- Disconnect/unlink an agent
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from security.dependencies import AuthContext, get_auth_context

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/user-recorder", tags=["User Recorder"])


class AgentUpdateRequest(BaseModel):
    name: Optional[str] = None


@router.get("/agents")
async def list_agents(
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """List user-hosted recorder agents (single global fleet)."""
    from routers.user_recorder_ws import get_connected_recorders

    # Get live WS-connected recorders
    connected = {
        r["agent_id"]: r
        for r in get_connected_recorders()
    }

    # Query DB for persisted agents.
    # Only show: currently-connected agents + the most recent offline one.
    db_agents = {}
    from models.agent import Agent as AgentModel, AgentStatus
    result = await db.execute(
        select(AgentModel)
        .where(
            AgentModel.user_hosted == True,
            AgentModel.status != AgentStatus.REVOKED,
        ).order_by(AgentModel.last_seen_at.desc().nulls_last())
    )
    seen_offline = False
    for agent in result.scalars().all():
        is_online = agent.agent_id in connected
        if is_online:
            db_agents[agent.agent_id] = agent
        elif not seen_offline:
            # Only keep the most recent offline agent
            db_agents[agent.agent_id] = agent
            seen_offline = True

    # Merge: DB agents + live connections
    all_agent_ids = set(list(db_agents.keys()) + list(connected.keys()))

    items = []
    for agent_id in all_agent_ids:
        live = connected.get(agent_id)
        db_agent = db_agents.get(agent_id)

        # Determine status: WS-connected = online; a row the WS handler already
        # marked INACTIVE/REVOKED is offline immediately (no stale "online" grace);
        # otherwise fall back to a recent-heartbeat (< 2min) window.
        if live:
            status = "online"
        elif db_agent and db_agent.status != AgentStatus.ACTIVE:
            status = "offline"
        elif db_agent and db_agent.last_seen_at:
            from datetime import datetime, timedelta, timezone
            now = datetime.now(timezone.utc)
            last_seen = db_agent.last_seen_at if db_agent.last_seen_at.tzinfo else db_agent.last_seen_at.replace(tzinfo=timezone.utc)
            age = now - last_seen
            status = "online" if age < timedelta(minutes=2) else "offline"
        else:
            status = "offline"

        _meta = (db_agent.meta or {}) if db_agent else {}
        items.append({
            "agent_id": agent_id,
            "name": _meta.get("name", agent_id) if db_agent else agent_id,
            "platform": db_agent.platform.value if db_agent and db_agent.platform else (live.get("platform") if live else None),
            # OS / device identity self-reported by a linked desktop (LinkedAgentBridge)
            # on /recorder/connect and persisted to agent.meta. Display-only; falls
            # back to the live connection blob so a just-connected agent shows too.
            "os_platform": _meta.get("os_platform") or (live.get("platform") if live else None),
            "device_name": _meta.get("device_name") or (live.get("device_name") if live else None),
            "capabilities": _meta.get("capabilities") or [],
            "status": status,
            "is_trusted": db_agent.is_trusted if db_agent else False,
            "created_at": db_agent.created_at.isoformat() if db_agent and db_agent.created_at else None,
            "last_seen_at": db_agent.last_seen_at.isoformat() if db_agent and db_agent.last_seen_at else (live.get("connected_at") if live else None),
            "active_sessions": live["active_sessions"] if live else 0,
            "max_sessions": live["max_sessions"] if live else (_meta.get("max_sessions", 2) if db_agent else 2),
            "ai_provider": live.get("ai_provider") if live else None,
            "version": (live.get("version") if live else None) or _meta.get("version"),
        })

    # Sort: online first
    items.sort(key=lambda x: (0 if x["status"] == "online" else 1))

    # Single-user coordinator: no plan limits on the number of connected agents.
    active_count = len(connected)

    return {
        "agents": items,
        "limit": None,  # unlimited (self-host)
        "active_count": active_count,
    }


@router.get("/capability")
async def recording_capability(
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Plan-aware recording capability for the creation wizard.

    Tells the frontend, in one call, whether recording can run in the cloud (and
    how much monthly quota remains), whether a local agent must be connected once
    the cloud quota is exhausted, and how many local agents are currently online.
    The wizard uses this to pick a target and to show the blocking "connect a
    local agent" gate instead of a cryptic WebSocket failure.
    """
    from routers.user_recorder_ws import get_connected_recorders

    online_local_agents = len(get_connected_recorders())

    # Single-user coordinator: no plan enforcer / no cloud recording tier. Recording
    # runs on the user's own connected agent(s); there is no per-minute billed cloud
    # lane. Report an unlimited local capability with cloud disabled.
    cap = {
        "plan": "self-host",
        "cloud_recording_allowed": False,
        "cloud_recording_unlimited": False,
        "cloud_quota_limit": 0,
        "cloud_quota_used": 0,
        "cloud_quota_remaining": 0,
        "requires_local_agent_when_exhausted": True,
        "max_user_recorders": None,
    }

    cap["online_local_agents"] = online_local_agents
    cap["cloud_available"] = False
    cap["cloud_billed_beyond_quota"] = False
    return cap


@router.patch("/agents/{agent_id}")
async def update_agent(
    agent_id: str,
    body: AgentUpdateRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Rename or update a user-hosted recorder agent."""
    from models.agent import Agent as AgentModel

    try:
        result = await db.execute(
            select(AgentModel).where(
                AgentModel.agent_id == agent_id,
                AgentModel.user_hosted == True,
            )
        )
        agent = result.scalar_one_or_none()
    except Exception:
        raise HTTPException(404, "Agent not found")

    if not agent:
        raise HTTPException(404, "Agent not found")

    if body.name is not None:
        meta = agent.meta or {}
        meta["name"] = body.name
        agent.meta = meta
        await db.flush()

    return {"status": "updated"}


async def _revoke_agent_oauth_tokens(
    db: AsyncSession,
    *,
    bound_agent_id: Optional[str],
    token_prefix: Optional[str],
) -> int:
    """Revoke ONLY the OAuth (wto_) token(s) the given agent uses to connect.

    Setting revoked_at blocks both the access token (validate_recorder_token
    filters revoked_at IS NULL) AND the refresh token (device/refresh does the
    same), so the agent can neither reconnect nor mint a new token.

    Per-machine precision (Pro plans allow multiple agents per user):
      1. Every non-revoked token bound to this agent_id. The binding is stamped
         by /internal/agent/connected and carried across refresh rotations, so
         this catches the live token even after a mid-session refresh.
      2. The exact stored token_prefix, as a fallback for tokens connected
         before the binding existed.

    Deliberately does NOT revoke by user_id — that would log out
    the owner's OTHER agents. Returns the number revoked.
    """
    from datetime import datetime, timezone
    from models.oauth import OAuthAccessToken

    now = datetime.now(timezone.utc)
    revoked = 0
    seen: set = set()

    async def _revoke_where(*conds):
        nonlocal revoked
        result = await db.execute(
            select(OAuthAccessToken).where(
                *conds, OAuthAccessToken.revoked_at.is_(None)
            )
        )
        for tok in result.scalars().all():
            if tok.id in seen:
                continue
            tok.revoked_at = now
            seen.add(tok.id)
            revoked += 1

    if bound_agent_id:
        await _revoke_where(OAuthAccessToken.agent_id == bound_agent_id)

    if token_prefix:
        await _revoke_where(OAuthAccessToken.token_prefix == token_prefix)

    return revoked


@router.delete("/agents/{agent_id}")
async def disconnect_agent(
    agent_id: str,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Disconnect and unlink a user-hosted recorder agent.

    This fully revokes the agent: it marks the DB row REVOKED, invalidates the
    OAuth token(s) it authenticates with (so it cannot reconnect or refresh),
    and force-closes any live WebSocket.
    """
    from models.agent import Agent as AgentModel, AgentStatus
    from routers.user_recorder_ws import (
        disconnect_agent as ws_disconnect,
        get_agent_live_identity,
    )

    try:
        result = await db.execute(
            select(AgentModel).where(
                AgentModel.agent_id == agent_id,
                AgentModel.user_hosted == True,
            )
        )
        agent = result.scalar_one_or_none()
    except Exception:
        agent = None

    if agent:
        meta = agent.meta or {}
        # Persisted identity first, falling back to the live connection's meta
        # for agents that connected before identity was stored on the DB row.
        live = get_agent_live_identity(agent_id)
        revoked = await _revoke_agent_oauth_tokens(
            db,
            bound_agent_id=agent_id,
            token_prefix=meta.get("oauth_token_prefix") or live.get("token_prefix"),
        )
        agent.status = AgentStatus.REVOKED
        # Commit REVOKED + token revocation BEFORE closing the socket, so the
        # WS handler's disconnect cleanup (separate session) sees the committed
        # REVOKED status and does not downgrade it back to INACTIVE.
        await db.commit()
        logger.info(
            f"Disconnected user-hosted agent {agent_id}; "
            f"revoked {revoked} OAuth token(s)"
        )

    # Relay-side enforcement: a revoked agent may still hold a non-expired
    # gateway JWT (≤5min TTL). Publish a short-lived revocation flag the
    # ws-gateway checks on every agent connect, so it is refused at the relay
    # even before its token revocation takes full effect. After the TTL the JWT
    # is expired and /connect refuses to mint a new one (token is revoked).
    try:
        from utils.redis_client import get_redis
        await get_redis().set(f"recorder_revoked:{agent_id}", "1", ex=600)
    except Exception as e:
        logger.warning(f"Could not set relay revocation flag for {agent_id}: {e}")

    # Force disconnect if currently connected
    await ws_disconnect(agent_id, reason="Disconnected by user")

    return {"status": "disconnected"}
