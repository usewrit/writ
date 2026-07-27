"""
Recorder Proxy — Backend-mediated access to Playwright Recorder instances.

Supports MULTIPLE recorder instances for production scaling.
Each recorder registers as an agent and reports load via heartbeat.
Sessions are tracked in Redis for WebSocket routing.

The recorder runs on a private network (agent server) and is NEVER exposed publicly.
All frontend communication goes through this proxy:
  Frontend WebSocket → Backend → Recorder (private network)
  Frontend HTTP → Backend → Recorder (private network)

This ensures:
- Recorder auth token stays server-side (never sent to browser)
- Recorder is only accessible from the private network
- All access is authenticated via the coordinator's API keys
- WebSocket connections are routed to the correct recorder instance
"""
import asyncio
import json
import logging
import os
import posixpath
import uuid
from typing import Optional

import httpx
from utils.http_client import http_session
from utils.redis_client import get_redis
from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from security.api_key import get_current_api_key
from security.jwt import decode_access_token_with_blacklist
from utils.recorder_auth import validate_recorder_token

logger = logging.getLogger(__name__)


async def _authenticate_ws(websocket: WebSocket) -> Optional[dict]:
    """Authenticate a WebSocket and return the decoded JWT payload, or None.

    Accepts a ?token=<JWT> query param or an access_token cookie. Uses the
    blacklist-aware decoder so tokens invalidated by logout / password change
    are rejected (a plain decode would keep honouring them until expiry).
    Authentication is required unconditionally (no environment-based bypass).
    """
    token = websocket.query_params.get("token") or websocket.cookies.get("access_token")
    if not token:
        return None
    return await decode_access_token_with_blacklist(token)


async def _authorize_session_ws(websocket: WebSocket, session_id: str) -> bool:
    """Authenticate the socket before it may access a recorder session.

    Spectate/interact route purely on session_id (Redis), which carries no auth.
    On the single-owner coordinator every session belongs to the one owner, so a
    valid authenticated socket is authorized; unauthenticated sockets are refused.
    """
    payload = await _authenticate_ws(websocket)
    if not payload:
        return False
    return True

router = APIRouter(prefix="/recorder", tags=["Recorder Proxy"])

# Legacy single-recorder fallback (used when Redis/multi-recorder not configured)
RECORDER_URL = os.getenv("RECORDER_URL", "http://127.0.0.1:8081")
RECORDER_TOKEN = os.getenv("RECORDER_AUTH_TOKEN", "") or os.getenv("RECORDER_AUTH_SECRET", "")
REDIS_URL = os.getenv("REDIS_URL", "")

# Redis client (lazy-initialized)
_redis_client = None
_redis_available = False


async def _get_redis():
    """Get the in-process Redis client for the recorder session registry."""
    global _redis_client, _redis_available
    if _redis_client is not None:
        return _redis_client if _redis_available else None
    try:
        from utils.redis_client import get_redis
        _redis_client = get_redis()
        await _redis_client.ping()
        _redis_available = True
        logger.info("In-process Redis ready for session registry")
        return _redis_client
    except Exception as e:
        logger.warning(f"In-process Redis unavailable ({e}), using single-recorder fallback")
        _redis_available = False
        return None


async def _get_active_recorders(captcha_trusted_only: bool = False) -> list:
    """Get all active recorder agents from the database.

    Returns list of dicts with: agent_id, recorder_url, active_sessions, max_sessions, captcha_trusted
    """
    try:
        from database import get_db
        from models.agent import Agent, AgentStatus, Platform
        from sqlalchemy import select
        from sqlalchemy.ext.asyncio import AsyncSession

        # Use a fresh DB session
        from database import AsyncSessionLocal
        async with AsyncSessionLocal() as db:
            query = (
                select(Agent)
                .where(Agent.platform == Platform.RECORDER)
                .where(Agent.status == AgentStatus.ACTIVE)
            )
            result = await db.execute(query)
            agents = result.scalars().all()

            recorders = []
            for agent in agents:
                meta = agent.meta or {}
                recorder_url = meta.get('recorder_url', '')
                if not recorder_url:
                    continue

                # Check if agent was seen recently (within 2 minutes)
                if agent.last_seen_at:
                    from datetime import datetime, timedelta
                    if (datetime.utcnow() - agent.last_seen_at) > timedelta(minutes=2):
                        continue

                captcha_trusted = meta.get('captcha_trusted', False)
                if captcha_trusted_only and not captcha_trusted:
                    continue

                recorders.append({
                    'agent_id': agent.agent_id,
                    'recorder_url': recorder_url.rstrip('/'),
                    'active_sessions': meta.get('active_sessions', 0),
                    'max_sessions': meta.get('max_sessions', 5),
                    'captcha_trusted': captcha_trusted,
                    'is_trusted': agent.is_trusted,
                })

            return recorders
    except Exception as e:
        logger.error(f"Error fetching recorder agents: {e}")
        return []


async def _pick_least_loaded_recorder(captcha_trusted_only: bool = False) -> Optional[str]:
    """Pick the recorder with the most available capacity.

    Returns recorder_url or None if no recorders available.
    """
    recorders = await _get_active_recorders(captcha_trusted_only=captcha_trusted_only)
    if not recorders:
        return None

    # Sort by available capacity (max_sessions - active_sessions), descending
    recorders.sort(key=lambda r: r['max_sessions'] - r['active_sessions'], reverse=True)

    best = recorders[0]
    available = best['max_sessions'] - best['active_sessions']
    if available <= 0:
        logger.warning(f"All recorders at capacity")
        # Still return the least-loaded one
        return best['recorder_url']

    logger.info(f"Picked recorder {best['agent_id']} ({best['recorder_url']}) "
                f"with {available} available slots")
    return best['recorder_url']


async def _resolve_recorder_url(session_id: str = None) -> str:
    """Resolve which recorder to route to.

    For session-specific endpoints: look up Redis to find the owning recorder.
    For new sessions: pick the least-loaded recorder.
    Falls back to RECORDER_URL if no active recorder agents found.
    """
    redis = await _get_redis()

    if session_id and redis:
        data = await redis.get(f"recorder:session:{session_id}")
        if data:
            info = json.loads(data)
            recorder_url = info.get('recorder_url', '').rstrip('/')
            if recorder_url:
                return recorder_url

    # Try to pick a recorder from registered agents
    try:
        recorder_url = await _pick_least_loaded_recorder()
        if recorder_url:
            return recorder_url
    except Exception as e:
        logger.debug(f"Multi-recorder lookup failed: {e}")

    # Fallback: use configured RECORDER_URL
    return RECORDER_URL.rstrip('/')


async def _register_session_in_redis(session_id: str, recorder_url: str, extra: dict = None):
    """Register a session in Redis for WebSocket routing."""
    redis = await _get_redis()
    if not redis:
        return
    info = {
        'recorder_url': recorder_url,
        'status': 'active',
    }
    if extra:
        info.update(extra)
    await redis.setex(f"recorder:session:{session_id}", 7200, json.dumps(info))
    logger.debug(f"Registered session {session_id} -> {recorder_url}")


async def _unregister_session_from_redis(session_id: str):
    """Remove a session from Redis."""
    redis = await _get_redis()
    if not redis:
        return
    await redis.delete(f"recorder:session:{session_id}")


def _make_ws_url(recorder_url: str, path: str) -> str:
    """Convert recorder HTTP URL to WebSocket URL with auth token."""
    base = recorder_url.replace("https://", "wss://").replace("http://", "ws://")
    token_param = f"?token={RECORDER_TOKEN}" if RECORDER_TOKEN else ""
    return f"{base}{path}{token_param}"


def _make_http_url(recorder_url: str, path: str) -> str:
    """Get recorder HTTP URL for a path."""
    return f"{recorder_url}{path}"


def _validate_recorder_subpath(path: str, prefix: str = "/api") -> str:
    """Validate a user-supplied proxy subpath before forwarding to a recorder.

    A ``{path:path}`` route can carry ``../`` (or URL-encoded / backslash
    equivalents) that normalizes past the intended ``/api/`` prefix and reaches
    arbitrary recorder endpoints (authenticated open proxy + path traversal).
    Reject anything that could escape the prefix; return the original subpath
    when it is safe, or raise ``HTTPException(400)``.
    """
    if not isinstance(path, str):
        raise HTTPException(status_code=400, detail="Invalid path")

    lowered = path.lower()
    if (
        ".." in path
        or "\\" in path
        or path.startswith("/")
        or "\x00" in path
        or "%2e" in lowered   # URL-encoded '.'
        or "%2f" in lowered   # URL-encoded '/'
        or "%5c" in lowered   # URL-encoded '\'
    ):
        raise HTTPException(status_code=400, detail="Invalid path")

    # Defense in depth: normalize and confirm the result still lives under the
    # intended prefix (guards against any traversal vector missed above).
    normalized = posixpath.normpath(f"{prefix}/{path}")
    if normalized != prefix and not normalized.startswith(f"{prefix}/"):
        raise HTTPException(status_code=400, detail="Invalid path")

    return path


# =============================================================================
# Recorder Assignment — pick a recorder for new sessions
# =============================================================================

class AssignRecorderRequest(BaseModel):
    """Request to assign a recorder for a new session."""
    purpose: str = Field(default="recording", description="Purpose: recording, workflow, ai_task")
    captcha_trusted: bool = Field(default=False, description="Require captcha_trusted recorder")


class AssignRecorderResponse(BaseModel):
    """Response with assigned recorder session info.

    `recorder_url` is an INTERNAL routing address and is only populated for
    platform admins. Ordinary callers route to the recorder exclusively via
    `ws_path` (gateway proxy keyed by `session_id`); they never see the raw
    address.
    """
    session_id: str
    recorder_url: Optional[str] = None
    ws_path: str


@router.post("/assign", response_model=AssignRecorderResponse)
async def assign_recorder(
    request: AssignRecorderRequest,
    _api_key: dict = Depends(get_current_api_key),
):
    """Assign a recorder for a new session.

    Picks the least-loaded recorder (optionally captcha_trusted) and
    pre-registers a session in Redis for WebSocket routing.
    """
    recorder_url = await _pick_least_loaded_recorder(
        captcha_trusted_only=request.captcha_trusted
    )
    if not recorder_url:
        # Fallback to legacy URL
        recorder_url = RECORDER_URL.rstrip('/')

    session_id = f"assigned-{uuid.uuid4().hex[:12]}"

    # Pre-register in Redis so the proxy knows where to route (session_id is
    # non-secret routing info; access is gated by authentication, not ownership).
    await _register_session_in_redis(session_id, recorder_url, {
        'purpose': request.purpose,
        'pre_assigned': True,
    })

    ws_path = f"/api/recorder/ws/record?session_id={session_id}"

    # Internal recorder address is topology — expose it only to platform admins
    # (ops/debug). Gate on is_platform_admin, NOT role=="admin" (which is also true
    # for admin API keys). Ordinary callers connect via ws_path/gateway.
    is_admin = bool((_api_key or {}).get("is_platform_admin"))

    return AssignRecorderResponse(
        session_id=session_id,
        recorder_url=recorder_url if is_admin else None,
        ws_path=ws_path,
    )


# =============================================================================
# Agent Gateway Discovery
# =============================================================================

GATEWAY_TOKEN_TTL = 300  # 5 minutes — agent must connect within this window


# NOTE: the old _discover_gateway_public_url() (which read an external ws-gateway's
# self-registered public URL from Redis) is intentionally REMOVED. The self-host
# coordinator is its own gateway: agent_connect() builds gateway_ws_url from
# WRIT_PUBLIC_URL / the request host and points back at this app's own
# /ws/ai-gateway ingress.


def _generate_gateway_token(
    agent_id: str = "",
    max_sessions: int = 5,
    token_prefix: str = "",
    user_id: str = "",
    role: str = "recorder",
    speed_class: str = "",
    perf_score: int = 0,
    tier: str = "",
    captcha_trusted: bool = False,
) -> str:
    """Issue a short-lived JWT that the ws-gateway validates.

    token_prefix/user_id (device-flow wto_ tokens only) are echoed back to the
    backend by the gateway at /internal/agent/connected so the OAuth token can be
    bound to the gateway-assigned agent_id for per-machine revocation.

    `role` carries the agent's authenticated kind into the gateway. The gateway
    only accepts the literals "recorder" (the owner's own local machine) or
    "infrastructure" (a shared fleet). Map anything that isn't infrastructure to
    "recorder".
    """
    from jose import jwt as jose_jwt
    secret = RECORDER_TOKEN
    if not secret:
        raise HTTPException(500, "RECORDER_AUTH_SECRET not configured")
    import time as _time
    gw_role = "infrastructure" if role == "infrastructure" else "recorder"
    payload = {
        "role": gw_role,
        "agent_id": agent_id,
        "max_sessions": max_sessions,
        "iat": int(_time.time()),
        "exp": int(_time.time()) + GATEWAY_TOKEN_TTL,
    }
    # Speed profile drives capacity/fastness-aware dispatch in the gateway and
    # (via the gateway's Redis registry) the backend. Kept lean — only the class
    # + single-core score the selectors need; richer specs (cores/clock/ram) go
    # to the DB via heartbeat for the admin view.
    if speed_class:
        payload["speed_class"] = speed_class
    if perf_score:
        payload["perf_score"] = int(perf_score)
    # Isolation pool. Only "isolated" is meaningful (a gVisor box); everything else
    # is the shared fleet. Stamped only for infra agents (a user's own machine is
    # never part of the cloud isolated pool).
    if gw_role == "infrastructure" and tier == "isolated":
        payload["tier"] = "isolated"
    # SECURITY: captcha-trust is a BACKEND-derived trust attribute (platform-admin
    # set, stored in agent.meta), NOT something the agent can self-declare. Carry it
    # as a signed JWT claim so the gateway reads it from verified claims like
    # role/tier — never from an attacker-controllable query param. Only stamped for
    # the SHARED cloud fleet (infrastructure); a user's own local machine is never
    # promoted to captcha-trusted via this path.
    if gw_role == "infrastructure" and captcha_trusted:
        payload["captcha_trusted"] = True
    if token_prefix:
        payload["token_prefix"] = token_prefix
    if user_id:
        payload["user_id"] = user_id
    return jose_jwt.encode(payload, secret, algorithm="HS256")


class ConnectRequest(BaseModel):
    max_sessions: int = Field(default=5, ge=1, le=50)
    captcha_trusted: bool = False
    # The agent's previously-assigned id, persisted locally across restarts. We
    # bake it into the gateway JWT so the ws-gateway (which derives identity ONLY
    # from verified JWT claims) keeps the SAME agent_id after a restart. Without
    # this the agent gets a fresh random id each connect, orphaning warm-session
    # affinity (keyed by workflow_id+agent_id) → every run logs in fresh.
    agent_id: Optional[str] = Field(default=None, max_length=128)

    # --- Performance profile (capacity/fastness-aware dispatch) ---------------
    # speed_class is the operator's explicit AGENT_SPEED_CLASS tag when set
    # ("fast" | "throughput" | "balanced"); blank lets the backend derive it
    # from the benchmark + capacity. perf_score is a normalised single-core
    # benchmark (modern fast core ~>=2000, old low-clock core ~<=1200). The
    # cpu_*/ram_mb fields are display-only specs for the admin Agents page.
    speed_class: Optional[str] = Field(default=None, max_length=20)
    perf_score: int = Field(default=0, ge=0, le=1_000_000)
    # Isolation pool this agent serves: "isolated" = a gVisor-hardened box that may
    # run sensitive (credential-bearing) cloud work; "shared" (default) = the warm
    # fleet for non-sensitive runs. Set by the isolated-tier provisioner via the
    # agent's WRIT_AGENT_TIER env. Only ever honored for infrastructure agents.
    tier: Optional[str] = Field(default=None, max_length=20)
    cpu_cores: int = Field(default=0, ge=0, le=4096)
    cpu_threads: int = Field(default=0, ge=0, le=8192)
    cpu_clock_mhz: int = Field(default=0, ge=0, le=100_000)
    ram_mb: int = Field(default=0, ge=0, le=100_000_000)

    # --- Monitoring capability (scheduled parallel target checks) -------------
    # When present, this recorder can also run the desktop-agent's monitoring
    # workload: the capacity dict (parallel_workers/targets_per_timeslot/…) feeds
    # the capacity-aware distributor; check_modes advertises which check types it
    # can run so the distributor includes it in the monitoring pool.
    capacity: Optional[dict] = None
    check_modes: Optional[list] = None

    # --- BYO full-agent advertisement (desktop LinkedAgentBridge, Workstream B) ---
    # A linked desktop connects as a full execution agent. These fields are
    # DISPLAY/ELIGIBILITY hints self-reported by the agent; the backend NEVER trusts
    # them for identity or isolation — they only populate the admin/my-agents view.
    # capabilities the bridge can service (e.g. "local_workflows", "execute_workflow",
    # "execute_ai_task", "streaming", "monitoring"). Display/diagnostics only.
    capabilities: list[str] = Field(default_factory=list)
    # OS platform (std::env::consts::OS — "macos"/"linux"/"windows"), device name
    # (hostname) and daemon version. Stamped into agent.meta for the agents view.
    platform: Optional[str] = Field(default=None, max_length=64)
    device_name: Optional[str] = Field(default=None, max_length=200)
    version: Optional[str] = Field(default=None, max_length=64)


class ConnectResponse(BaseModel):
    # WS URL the agent opens:
    # "<WRIT_PUBLIC_URL-or-request-host>/ws/ai-gateway" — this SAME coordinator
    # serves that route. saas_bridge reads it as connect_data["gateway_ws_url"].
    gateway_ws_url: str = Field(description="WebSocket URL to connect to (this coordinator's /ws/ai-gateway)")
    # Freshly-minted short-lived gateway JWT (carries the constant tenant_id claim
    # "local"). saas_bridge reads it as connect_data["gateway_token"] and sends it
    # in the Authorization header on the WS handshake.
    gateway_token: str = Field(description="Short-lived gateway JWT — sent in the WS Authorization header")
    # Alias of gateway_token under the generic 'jwt' name the wire contract also
    # documents; kept additively so either name resolves the same token.
    jwt: str = Field(description="Alias of gateway_token")
    expires_in: int = Field(description="Seconds until token expires")
    # Wire-compat with saas_bridge (creds['tenant_id']): a constant "local" so the
    # agent's downstream AI-gateway calls carry a stable claim.
    tenant_id: str = Field(default="local", description="Constant 'local' (single-owner self-host)")
    # Stable agent id echoed back so the agent persists it across restarts.
    agent_id: str = Field(default="", description="Agent id the gateway will honor across restarts")
    # Per-agent channel key (Fernet) for credential re-encryption on the WS. May be
    # empty if none is provisioned yet (the WS auth_ok frame also carries it).
    channel_key: Optional[str] = Field(default=None, description="Per-agent Fernet channel key")
    # Max concurrent sessions this agent is authorized for.
    max_sessions: int = Field(default=5, description="Max concurrent sessions")


async def _get_agent_auth(request: Request) -> dict:
    """Authenticate agent — accepts infrastructure JWT or OAuth wto_ token."""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
        auth_ctx = await validate_recorder_token(token)
        if auth_ctx:
            return {
                "role": auth_ctx.role,
                "max_sessions": auth_ctx.max_sessions,
                "token_prefix": auth_ctx.token_prefix,
                "user_id": auth_ctx.user_id,
                "agent_id": getattr(auth_ctx, "agent_id", None),
            }
    raise HTTPException(status_code=401, detail="Invalid or missing auth token")


async def _persist_agent_perf(
    *,
    agent_id: str,
    speed_class: str,
    perf_score: int,
    cpu_cores: int,
    cpu_threads: int,
    cpu_clock_mhz: int,
    ram_mb: int,
    max_sessions: int,
    capacity: Optional[dict] = None,
    check_modes: Optional[list] = None,
    role: str = "user-hosted",
    tier: Optional[str] = None,
    capabilities: Optional[list] = None,
    platform: Optional[str] = None,
    device_name: Optional[str] = None,
    version: Optional[str] = None,
) -> None:
    """Store an agent's perf profile + speed class in its DB meta (best-effort).

    Only writes fields the agent actually reported, so a sparse update never
    clobbers richer specs captured earlier. Used by the admin Agents page and
    the HTTP-pool dispatcher (which read agent.meta, not the gateway registry).

    When the agent advertises monitoring `capacity`/`check_modes`, those are
    persisted into the same `agent.meta['capacity']` shape the capacity-aware
    distributor reads, and a per-agent HMAC secret is minted if missing (the
    gateway monitoring-report path server-signs reports on the agent's behalf).

    A linked desktop (LinkedAgentBridge) additionally advertises `capabilities`
    and display identity (`platform`/`device_name`/`version`, stamped into
    agent.meta for the my-agents/admin view). These are self-reported hints —
    never trusted for identity or isolation.
    """
    from database import AsyncSessionLocal
    from models.agent import Agent
    from sqlalchemy import select
    from sqlalchemy.orm.attributes import flag_modified
    from datetime import datetime

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Agent).where(Agent.agent_id == agent_id))
        agent = result.scalar_one_or_none()
        # A full-agent bridge (advertised capabilities) or a monitoring recorder
        # must have a row NOW so its eligibility/capacity lands before the WS
        # connect that would otherwise create it.
        _create_row = bool(check_modes) or bool(capabilities)
        if not agent:
            # First connect: the gateway hasn't created the Agent row yet (that
            # happens on WS connect, which the agent only does AFTER this /connect
            # returns the token). Create it eagerly for a monitoring-capable or
            # full-agent bridge; a plain perf update stays best-effort (no row).
            if not _create_row:
                return
            from models.agent import Platform, AgentStatus
            is_trusted = role == "infrastructure"
            agent = Agent(
                agent_id=agent_id,
                platform=Platform.RECORDER,
                status=AgentStatus.ACTIVE,
                secret_hash="gateway-auth",
                is_trusted=is_trusted,
                user_hosted=not is_trusted,
                last_seen_at=datetime.utcnow(),
                meta={},
            )
            db.add(agent)
        meta = dict(agent.meta or {})
        perf = dict(meta.get("perf") or {})
        if perf_score:
            perf["perf_score"] = int(perf_score)
        if cpu_cores:
            perf["cpu_cores"] = int(cpu_cores)
        if cpu_threads:
            perf["cpu_threads"] = int(cpu_threads)
        if cpu_clock_mhz:
            perf["cpu_clock_mhz"] = int(cpu_clock_mhz)
        if ram_mb:
            perf["ram_mb"] = int(ram_mb)
        perf["speed_class"] = speed_class
        meta["perf"] = perf
        meta["speed_class"] = speed_class
        if max_sessions:
            meta["max_sessions"] = int(max_sessions)
        # Isolation pool. Only an infrastructure agent can be 'isolated' (a gVisor
        # box for sensitive cloud work); a user's own machine is always 'shared'.
        meta["tier"] = "isolated" if (role == "infrastructure" and tier == "isolated") else "shared"
        if capacity:
            meta["capacity"] = dict(capacity)
        if check_modes:
            meta["check_modes"] = list(check_modes)

        # --- Full-agent (LinkedAgentBridge) advertisement -----------------------
        # Display identity for the my-agents/admin view. The `platform` column is
        # the enum bucket (RECORDER); the OS/hostname/version are self-reported and
        # live in meta only. Only stamp fields the agent actually reported so a
        # sparse reconnect never clears a richer earlier value.
        if platform:
            meta["os_platform"] = str(platform)[:64]
        if device_name:
            meta["device_name"] = str(device_name)[:200]
        if version:
            meta["version"] = str(version)[:64]
        if capabilities:
            # Defensive: coerce to a clean list[str] (self-reported, untrusted).
            meta["capabilities"] = [str(c)[:64] for c in list(capabilities)[:32]]

        agent.meta = meta
        flag_modified(agent, "meta")

        # Mint an HMAC secret for the monitoring-report path if the agent has none
        # (recorders authenticate via OAuth, not agents/register, so they start
        # without one). The agent never holds it — the backend signs gateway
        # monitoring reports on its behalf.
        if check_modes and not getattr(agent, "encrypted_secret", None):
            try:
                import secrets as _secrets
                from security.encryption import SecretEncryption
                agent.encrypted_secret = SecretEncryption.encrypt_secret(_secrets.token_hex(32))
            except Exception as _e:
                logger.warning(f"could not mint monitoring secret for {agent_id}: {_e}")

        await db.commit()


async def _agent_captcha_trusted(agent_id: str) -> bool:
    """Read the agent's AUTHORITATIVE captcha-trust flag from its DB row.

    captcha_trusted is set by a platform admin (agents.set_captcha_trusted →
    agent.meta['captcha_trusted']); it is a backend-derived trust attribute that
    must never be self-declared by the agent. Best-effort: a lookup failure or an
    unknown agent defaults to NOT trusted (fail-closed for trust).
    """
    try:
        from database import AsyncSessionLocal
        from models.agent import Agent
        from sqlalchemy import select

        async with AsyncSessionLocal() as db:
            result = await db.execute(select(Agent).where(Agent.agent_id == agent_id))
            agent = result.scalar_one_or_none()
            return bool(agent and agent.captcha_trusted)
    except Exception as e:
        logger.warning(f"captcha-trust lookup failed for {agent_id}: {e}")
        return False


@router.post("/connect", response_model=ConnectResponse)
async def agent_connect(
    request: ConnectRequest,
    http_request: Request,
):
    """
    Agent connect — returns THIS coordinator's own /ws/ai-gateway WS URL plus a
    freshly-minted gateway JWT (tenant_id="local"), agent_id, channel_key and
    max_sessions — the shape the light writ-agent (saas_bridge) expects.

    The coordinator is its own gateway: both infrastructure agents (the operator's
    own fleet servers) and user-hosted agents connect back to this same process.
    The WS base URL comes from WRIT_PUBLIC_URL (or the request host behind a
    reverse proxy) — never a separate external ws-gateway.
    """
    _auth = await _get_agent_auth(http_request)
    role = _auth.get("role", "user-hosted")

    # The coordinator IS its own gateway — there is NO separate ws-gateway
    # process to discover. Resolve the coordinator's own reachable base URL and
    # point the agent back at THIS process's /ws/ai-gateway ingress.
    #
    # Preference order:
    #   1. WRIT_PUBLIC_URL (settings.writ_public_url) — the operator-declared
    #      public base URL of this coordinator (e.g. https://writ.example.com).
    #   2. The request's own scheme+host (X-Forwarded-Proto + Host) — the exact
    #      origin the agent used to reach us, correct behind a reverse proxy.
    # We NEVER hand back a hardcoded ws://localhost:8082: there is no separate
    # external gateway to point at.
    from config import settings
    gateway_url = (settings.writ_public_url or "").strip()
    if not gateway_url:
        request_host = http_request.headers.get("host", "")
        request_scheme = http_request.headers.get("x-forwarded-proto", "") or http_request.url.scheme or "http"
        if request_host:
            gateway_url = f"{request_scheme}://{request_host}"

    if not gateway_url:
        # No public URL configured and no usable Host header — fail loudly rather
        # than silently handing back an unreachable URL.
        raise HTTPException(
            status_code=500,
            detail="Coordinator public URL is not configured. Set WRIT_PUBLIC_URL.",
        )

    # Normalize scheme to ws/wss (the agent opens a WebSocket to this URL).
    if gateway_url.startswith("http://"):
        gateway_url = gateway_url.replace("http://", "ws://", 1)
    elif gateway_url.startswith("https://"):
        gateway_url = gateway_url.replace("https://", "wss://", 1)

    # Root-level ingress served by THIS app (main.py add_api_websocket_route +
    # user_recorder_ws /ws/ai-gateway alias) — NOT an external gateway.
    gateway_ws_url = f"{gateway_url.rstrip('/')}/ws/ai-gateway"

    # wss-only in production for BYO (user-hosted) agents: never hand an end-user
    # machine a plaintext gateway URL. A missing x-forwarded-proto or an http://
    # WRIT_PUBLIC_URL would otherwise downgrade the link to ws:// and leak the
    # short-lived gateway JWT + every session frame on the wire. Fail loudly here so
    # the misconfig is caught at deploy, not silently tolerated. Infrastructure agents
    # (own servers on a trusted private network) are exempt — they may use ws:// and
    # the agent-side check still guards them via saas.allow_insecure.
    if (
        settings.is_production  # @property, not a method
        and role != "infrastructure"
        and gateway_ws_url.startswith("ws://")
    ):
        logger.error(
            "Refusing to issue a plaintext ws:// gateway URL to a user-hosted agent "
            "in production (resolved '%s'). Set WRIT_PUBLIC_URL to an https:// URL "
            "and ensure the proxy forwards X-Forwarded-Proto.",
            gateway_ws_url,
        )
        raise HTTPException(
            status_code=500,
            detail="Gateway is misconfigured for secure connections. Please contact support.",
        )

    # Preserve the agent's stable id across restarts so warm-session affinity
    # survives. The JWT is minted only after authenticating the owner's
    # credential; the gateway additionally rejects duplicate live connections for
    # the same id.
    import re as _re
    # Prefer the agent's own stored id; fall back to the id BAKED INTO the service
    # token (auto-provisioned servers carry an agent-bound token but start with no
    # stored id) so the autoscaler can match the registered agent to its record.
    requested_agent_id = (request.agent_id or _auth.get("agent_id") or "").strip()
    safe_agent_id = requested_agent_id if _re.fullmatch(r"[A-Za-z0-9._-]{1,128}", requested_agent_id) else ""

    # Resolve the agent's speed class server-side: an explicit operator tag wins,
    # otherwise derive from the reported benchmark + capacity + clock. Single
    # source of truth in services/agent_speed so the gateway, dispatcher and
    # admin view all agree.
    from services.agent_speed import derive_speed_class
    resolved_speed_class = derive_speed_class(
        explicit=request.speed_class,
        perf_score=request.perf_score,
        max_sessions=request.max_sessions,
        clock_mhz=request.cpu_clock_mhz,
    )

    # SECURITY: captcha-trust is an AUTHORITATIVE, platform-admin-set attribute
    # (agent.meta['captcha_trusted'] via /agents/{id}/captcha-trusted) — NEVER the
    # agent's self-reported request.captcha_trusted, which would let any agent
    # self-promote into the captcha-trusted pool. Read it from the agent's DB row
    # and bake it into the signed gateway JWT. Only meaningful for infra agents.
    captcha_trusted = False
    if role == "infrastructure" and safe_agent_id:
        captcha_trusted = await _agent_captcha_trusted(safe_agent_id)

    token = _generate_gateway_token(
        agent_id=safe_agent_id,
        max_sessions=request.max_sessions,
        token_prefix=_auth.get("token_prefix") or "",
        user_id=str(_auth.get("user_id") or ""),
        role=role,  # propagate infrastructure vs user-hosted (was hardcoded "recorder")
        speed_class=resolved_speed_class,
        perf_score=request.perf_score,
        tier=request.tier or "",
        captcha_trusted=captcha_trusted,
    )

    # Persist the full profile (incl. display-only specs) to the agent's DB row
    # so the admin Agents page and the HTTP-pool dispatcher have it even before
    # the first heartbeat. Best-effort: never block the connect handshake on it.
    if safe_agent_id:
        try:
            await _persist_agent_perf(
                agent_id=safe_agent_id,
                speed_class=resolved_speed_class,
                perf_score=request.perf_score,
                cpu_cores=request.cpu_cores,
                cpu_threads=request.cpu_threads,
                cpu_clock_mhz=request.cpu_clock_mhz,
                ram_mb=request.ram_mb,
                max_sessions=request.max_sessions,
                capacity=request.capacity,
                check_modes=request.check_modes,
                role=role,
                tier=request.tier,
                capabilities=request.capabilities,
                platform=request.platform,
                device_name=request.device_name,
                version=request.version,
            )
        except Exception as e:
            logger.warning(f"agent perf persist failed for {safe_agent_id}: {e}")

        # Monitoring-capable recorder: (re)distribute target time-slots and push
        # this agent's assignment over the gateway so it can start checking
        # without waiting on the HTTP config-poll loop. Best-effort, off the
        # connect critical path.
        if request.check_modes:
            try:
                from services.monitoring_dispatch import distribute_and_push_to_agent
                import asyncio as _asyncio
                _asyncio.create_task(distribute_and_push_to_agent(safe_agent_id))
            except Exception as e:
                logger.warning(f"monitoring assignment push failed for {safe_agent_id}: {e}")

    # Bridge the device-flow channel key (stored under token_prefix) into the
    # agent_id namespace so the ws-gateway dispatch path can re-encrypt credentials
    # with the SAME key the agent holds. Without this it mints a mismatched key and
    # the agent can't decrypt credentials → {{secret:...}} resolves empty.
    token_prefix = _auth.get("token_prefix")
    if safe_agent_id and token_prefix:
        try:
            from routers.user_recorder_ws import mirror_channel_key_to_agent_id
            await mirror_channel_key_to_agent_id(token_prefix, safe_agent_id)
        except Exception as e:
            logger.warning(f"channel-key mirror failed for {safe_agent_id}: {e}")

    # Best-effort: surface the per-agent channel key in the connect response too
    # (the WS auth_ok frame also carries it, so this is belt-and-suspenders). Look
    # it up under the token_prefix first (wto_ device flow), then the agent_id
    # (infra service token). Never fail the handshake if the key can't be read.
    channel_key: Optional[str] = None
    try:
        from routers.user_recorder_ws import (
            _get_channel_key_for_token,
            _get_channel_key_for_agent_readonly,
        )
        if token_prefix:
            channel_key = await _get_channel_key_for_token(token_prefix)
        if not channel_key and safe_agent_id:
            channel_key = await _get_channel_key_for_agent_readonly(safe_agent_id)
    except Exception as e:
        logger.debug(f"channel-key lookup for connect response failed: {e}")

    return ConnectResponse(
        gateway_ws_url=gateway_ws_url,
        gateway_token=token,
        jwt=token,  # wire alias — same short-lived gateway JWT
        expires_in=GATEWAY_TOKEN_TTL,
        # Single-owner self-host: constant "local" (wire-compat, NOT multi-tenancy).
        tenant_id="local",
        agent_id=safe_agent_id,
        channel_key=channel_key,
        max_sessions=request.max_sessions,
    )


# =============================================================================
# WebSocket Proxy — bidirectional frame forwarding with dynamic routing
# =============================================================================

async def _proxy_websocket(client_ws: WebSocket, recorder_url: str, recorder_path: str):
    """Proxy WebSocket connection between frontend client and a specific recorder."""
    await client_ws.accept()

    ws_url = _make_ws_url(recorder_url, recorder_path)
    # Never log ws_url: it carries ?token={RECORDER_TOKEN} (also the gateway JWT
    # signing secret). Log only token-free parts (recorder host + path).
    logger.info(f"Proxying WebSocket: client -> {recorder_url}{recorder_path}")

    recorder_ws = None
    try:
        import websockets
        recorder_ws = await websockets.connect(ws_url, open_timeout=10)
        logger.info(f"Recorder WebSocket connected ({recorder_url})")

        async def client_to_recorder():
            try:
                while True:
                    data = await client_ws.receive_text()
                    await recorder_ws.send(data)
            except (WebSocketDisconnect, Exception):
                pass

        async def recorder_to_client():
            try:
                async for message in recorder_ws:
                    if isinstance(message, str):
                        await client_ws.send_text(message)
                    elif isinstance(message, bytes):
                        await client_ws.send_bytes(message)
            except Exception:
                pass

        done, pending = await asyncio.wait(
            [
                asyncio.create_task(client_to_recorder()),
                asyncio.create_task(recorder_to_client()),
            ],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()

    except Exception as e:
        logger.error(f"WebSocket proxy error: {e}")
    finally:
        if recorder_ws:
            try:
                await recorder_ws.close()
            except Exception:
                pass
        try:
            await client_ws.close()
        except Exception:
            pass


@router.websocket("/ws/record")
async def proxy_ws_record(websocket: WebSocket, session_id: str = None):
    """Proxy recording WebSocket to recorder.

    If session_id provided, routes to the pre-assigned recorder via Redis.
    Otherwise picks the least-loaded captcha_trusted recorder automatically.
    The frontend never needs to know about multiple recorders.
    """
    if session_id:
        # A specific session must belong to the caller.
        if not await _authorize_session_ws(websocket, session_id):
            await websocket.close(code=4403, reason="Forbidden")
            return
        recorder_url = await _resolve_recorder_url(session_id)
    else:
        if not await _authenticate_ws(websocket):
            await websocket.close(code=4001, reason="Authentication required")
            return
        # Interactive recording: prefer captcha_trusted recorders
        recorder_url = await _pick_least_loaded_recorder(captcha_trusted_only=True)
        if not recorder_url:
            # Fall back to any available recorder
            recorder_url = await _resolve_recorder_url()
    await _proxy_websocket(websocket, recorder_url, "/ws/record")


@router.websocket("/ws/ai-tasks")
async def proxy_ws_ai_tasks(websocket: WebSocket):
    """Proxy AI tasks WebSocket to a recorder."""
    if not await _authenticate_ws(websocket):
        await websocket.close(code=4001, reason="Authentication required")
        return
    recorder_url = await _resolve_recorder_url()
    await _proxy_websocket(websocket, recorder_url, "/ws/ai-tasks")


@router.websocket("/ws/spectate/{session_id}")
async def proxy_ws_spectate(websocket: WebSocket, session_id: str):
    """Proxy spectate WebSocket — routes to the recorder that owns the session."""
    if not await _authorize_session_ws(websocket, session_id):
        await websocket.close(code=4403, reason="Forbidden")
        return
    recorder_url = await _resolve_recorder_url(session_id)
    await _proxy_websocket(websocket, recorder_url, f"/ws/spectate/{session_id}")


@router.websocket("/ws/interact/{session_id}")
async def proxy_ws_interact(websocket: WebSocket, session_id: str):
    """Proxy interact WebSocket — routes to the recorder that owns the session."""
    if not await _authorize_session_ws(websocket, session_id):
        await websocket.close(code=4403, reason="Forbidden")
        return
    recorder_url = await _resolve_recorder_url(session_id)
    await _proxy_websocket(websocket, recorder_url, f"/ws/interact/{session_id}")


# =============================================================================
# HTTP Proxy — forward REST calls to recorder
# =============================================================================

@router.get("/health")
async def proxy_health(
    _api_key: dict = Depends(get_current_api_key),
):
    """Report the coordinator's own fleet health.

    Self-host has no separate "recorder" microservice to proxy to — execution
    agents connect straight to this process over the WS gateway
    (/ws/ai-gateway) and live in the in-process connected-recorder registry.
    Proxying to a (nonexistent) recorder URL just 502s and makes the fleet look
    dead. Instead we return this coordinator's live view: whether any agent is
    connected and how many, plus aggregate session capacity from the registry.
    Always 200 so callers get a truthful, non-erroring answer.

    AUTHENTICATED, unlike the liveness probe at GET /health. This reports fleet
    topology — how many agents are dialed in and how much capacity is free —
    which is operational detail about the deployment, not a liveness signal, and
    an anonymous caller has no business enumerating it. Container and uptime
    probes should use /health; every sibling endpoint in this router is already
    key-gated the same way.
    """
    try:
        from routers.user_recorder_ws import get_all_connected_recorders
        connected = get_all_connected_recorders()
    except Exception as e:  # registry must never make health 500
        logger.warning("Fleet registry read failed in /recorder/health: %s", e)
        connected = []

    online = len(connected)
    total_slots = 0
    used_slots = 0
    for r in connected:
        ms = r.get("max_sessions")
        act = r.get("active_sessions")
        if isinstance(ms, int):
            total_slots += ms
        if isinstance(act, int):
            used_slots += act

    return {
        # "healthy" = the coordinator is up; "degraded" only signals that no
        # execution agent is currently connected (nothing can run yet), which is
        # a normal state for a freshly-started self-host with no agents dialed in.
        "status": "healthy" if online > 0 else "degraded",
        "coordinator": "ok",
        "agents_connected": online,
        "capacity": {
            "agent_slots_total": total_slots,
            "agent_slots_used": used_slots,
            "agent_slots_free": max(total_slots - used_slots, 0),
        },
    }


@router.get("/recorders")
async def list_recorders(
    _api_key: dict = Depends(get_current_api_key),
):
    """List all active recorder instances with their load info.

    API-key reachable (get_current_api_key): MUST NOT disclose recorder URLs or
    the internal fallback address. Only agent ids + load/capacity are returned;
    routing addresses stay server-side.
    """
    recorders = await _get_active_recorders()
    safe_recorders = [
        {
            "agent_id": r.get("agent_id"),
            "active_sessions": r.get("active_sessions", 0),
            "max_sessions": r.get("max_sessions", 5),
            "captcha_trusted": r.get("captcha_trusted", False),
            "is_trusted": r.get("is_trusted"),
        }
        for r in recorders
    ]
    return {
        "recorders": safe_recorders,
        "count": len(safe_recorders),
    }


def _recorder_auth_headers() -> dict:
    """Build Authorization header for outbound HTTP calls to the recorder."""
    if RECORDER_TOKEN:
        return {"Authorization": f"Bearer {RECORDER_TOKEN}"}
    return {}


@router.post("/convert/python")
async def proxy_convert(
    request: Request,
    _api_key: dict = Depends(get_current_api_key),
):
    """Proxy step conversion to recorder."""
    try:
        recorder_url = await _resolve_recorder_url()
        body = await request.body()
        async with http_session() as client:
            resp = await client.post(
                f"{recorder_url}/convert/python",
                content=body,
                headers={"Content-Type": "application/json", **_recorder_auth_headers()},
                timeout=30,
            )
            return Response(content=resp.content, status_code=resp.status_code,
                          media_type=resp.headers.get("content-type"))
    except Exception as e:
        logger.error(f"Recorder convert proxy failed: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail="Execution agent temporarily unavailable. Please retry.")


@router.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def proxy_api(
    request: Request,
    path: str,
    _api_key: dict = Depends(get_current_api_key),
):
    """Proxy any recorder API call."""
    # Reject traversal/escape before forwarding (must be outside the try below,
    # whose `except Exception` would otherwise mask the 400 as a 502).
    safe_path = _validate_recorder_subpath(path, prefix="/api")
    try:
        recorder_url = await _resolve_recorder_url()
        body = await request.body() if request.method in ("POST", "PUT") else None
        async with http_session() as client:
            resp = await client.request(
                method=request.method,
                url=f"{recorder_url}/api/{safe_path}",
                content=body,
                headers={"Content-Type": request.headers.get("content-type", "application/json"), **_recorder_auth_headers()},
                timeout=30,
            )
            return Response(content=resp.content, status_code=resp.status_code,
                          media_type=resp.headers.get("content-type"))
    except Exception as e:
        logger.error(f"Recorder api proxy failed (path={path}): {e}", exc_info=True)
        raise HTTPException(status_code=502, detail="Execution agent temporarily unavailable. Please retry.")
