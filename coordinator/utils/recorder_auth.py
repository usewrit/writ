"""
Unified recorder authentication module.

Provides a single validation function for both token types:
- JWT service tokens (infrastructure recorders) signed with RECORDER_AUTH_SECRET
- OAuth wto_ tokens (user-hosted recorders) validated against the DB

Used by:
- WS endpoint (/api/ws/recorder): first-message auth
- HTTP push middleware (recorder-side): inbound task validation
- Coordinator outbound pushes: JWT generation for Authorization headers

Token type detection:
- Starts with "eyJ" → JWT (base64-encoded JSON header)
- Starts with "wto_" (legacy "pso_") → OAuth access token
"""
from __future__ import annotations

import hashlib
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from jose import jwt, JWTError

logger = logging.getLogger(__name__)

RECORDER_AUTH_SECRET = os.getenv("RECORDER_AUTH_SECRET", "")
JWT_ALGORITHM = "HS256"
PUSH_JWT_TTL_SECONDS = 300  # 5 minutes for coordinator→recorder HTTP pushes


# ---------------------------------------------------------------------------
# Auth context returned by all validation paths
# ---------------------------------------------------------------------------
@dataclass
class RecorderAuthContext:
    """Unified auth context for any recorder connection."""

    # Constant "local" for wire compatibility with the light writ-agent. Every
    # recorder on this coordinator belongs to the one owner; the service/agent JWT
    # also carries this exact claim.
    tenant_id: Optional[str] = "local"
    role: str = "user-hosted"  # "infrastructure" | "user-hosted"
    is_trusted: bool = False
    user_id: Optional[str] = None
    scopes: List[str] = field(default_factory=list)
    token_prefix: Optional[str] = None  # For wto_ tokens: sha256[:12] for Redis lookups
    max_sessions: int = 5
    # Set by the caller after validation:
    agent_id: Optional[str] = None
    # jti claim of a minted fleet service token (None for legacy tokens minted
    # before jti stamping, and for OAuth tokens). Used for revocation checks
    # against the fleet-token KV registry.
    jti: Optional[str] = None


# ---------------------------------------------------------------------------
# Unified token validation
# ---------------------------------------------------------------------------
async def validate_recorder_token(token: str) -> Optional[RecorderAuthContext]:
    """
    Validate a recorder token. Detects type by prefix and delegates.

    Returns RecorderAuthContext on success, None on failure.
    """
    if not token:
        return None

    # JWT service tokens start with "eyJ" (base64-encoded '{"')
    if token.startswith("eyJ"):
        ctx = _validate_jwt_service_token(token)
        # Revocation: fleet tokens minted with a jti claim are checked against
        # the fleet-token KV registry (routers/fleet.py) — flipping the agent
        # row alone is not enough, because a revoked token could be re-bound to
        # a fresh agent_id and would otherwise still validate. BACK-COMPAT:
        # tokens minted BEFORE jti stamping carry no jti and skip this check —
        # they keep validating exactly as before.
        if ctx and ctx.jti and await _is_fleet_jti_revoked(ctx.jti):
            logger.warning("JWT service token rejected: jti %s is revoked", ctx.jti)
            return None
        return ctx

    # OAuth tokens start with "wto_" (legacy: "pso_")
    if token.startswith(("wto_", "pso_")):
        return await _validate_oauth_token(token)

    logger.warning("Unknown token type (doesn't start with 'eyJ' or 'wto_')")
    return None


# ---------------------------------------------------------------------------
# JWT service token validation (infrastructure recorders)
# ---------------------------------------------------------------------------
def _validate_jwt_service_token(token: str) -> Optional[RecorderAuthContext]:
    """
    Validate a JWT service token signed with RECORDER_AUTH_SECRET.

    Expected claims:
        role: str          — "infrastructure" (required)
        max_sessions: int  — optional, defaults to 5
        iat: int           — issued at
        exp: int           — optional expiry
    """
    if not RECORDER_AUTH_SECRET:
        logger.error("RECORDER_AUTH_SECRET not configured — cannot validate JWT service token")
        return None

    try:
        # options: don't require exp (infra tokens can be long-lived)
        payload = jwt.decode(
            token,
            RECORDER_AUTH_SECRET,
            algorithms=[JWT_ALGORITHM],
            options={"require_exp": False, "verify_exp": True},
        )
    except JWTError as e:
        logger.warning(f"JWT service token validation failed: {e}")
        return None

    role = payload.get("role")

    if role != "infrastructure":
        logger.warning(f"JWT service token has unexpected role: {role}")
        return None

    return RecorderAuthContext(
        # Accept whatever the token carries, defaulting to the constant "local"
        # (wire compatibility with the light writ-agent).
        tenant_id=payload.get("tenant_id") or "local",
        role="infrastructure",
        is_trusted=True,
        user_id=None,  # Infrastructure recorders don't map to a user
        scopes=["agent:connect", "workflows:execute"],
        # Derive a stable prefix from the (long-lived) service token — same scheme
        # as wto_ tokens (sha256[:12]) — so /connect's channel-key mirror runs and
        # the buyer's secrets can be re-encrypted with the key the agent holds.
        # The channel key is persisted under this prefix at link time (oauth.py).
        token_prefix=hashlib.sha256(token.encode()).hexdigest()[:12],
        max_sessions=int(payload.get("max_sessions", 5)),
        # Agent-bound provisioning token → the connect handler pins this id when
        # the agent itself supplies none (fresh auto-provisioned server).
        agent_id=payload.get("agent_id"),
        # jti (fleet-token revocation handle); absent on legacy tokens.
        jti=payload.get("jti"),
    )


async def _is_fleet_jti_revoked(jti: str) -> bool:
    """True if the fleet-token registry marks this jti (== registry token_id)
    revoked.

    The registry is the ``fleet_service_tokens`` entry in the ``config`` KV
    table (see routers/fleet.py) — a single primary-key row, so this is one
    cheap SELECT at connect/auth time. Fail-open on lookup errors: only an
    explicit ``revoked_at`` on the matching entry rejects the token (a
    transient DB blip must not take the whole fleet offline); tokens whose jti
    is unknown to the registry are treated like legacy no-jti tokens.
    """
    try:
        from database import AsyncSessionLocal
        from models.config import Config
        from sqlalchemy import select

        async with AsyncSessionLocal() as db:
            row = await db.execute(select(Config).where(Config.key == "fleet_service_tokens"))
            cfg = row.scalar_one_or_none()
            reg = cfg.value if (cfg and isinstance(cfg.value, dict)) else {}
            for entry in reg.get("tokens", []) or []:
                if entry.get("token_id") == jti:
                    return bool(entry.get("revoked_at"))
    except Exception as e:
        logger.warning("Fleet jti revocation lookup failed (allowing token): %s", e)
    return False


# ---------------------------------------------------------------------------
# OAuth wto_ token validation (user-hosted recorders)
# ---------------------------------------------------------------------------
async def _validate_oauth_token(token: str) -> Optional[RecorderAuthContext]:
    """
    Validate an OAuth access token (wto_ prefix; legacy pso_ also accepted).

    Looks up the token hash in the DB, checks expiry, scopes, etc.
    Extracted from user_recorder_ws._validate_token() for reuse.
    """
    from database import AsyncSessionLocal
    from models.oauth import OAuthAccessToken
    from security.api_key import verify_api_key
    from sqlalchemy import select

    token_prefix = hashlib.sha256(token.encode()).hexdigest()[:12]

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(OAuthAccessToken).where(
                OAuthAccessToken.token_prefix == token_prefix,
                OAuthAccessToken.revoked_at.is_(None),
                OAuthAccessToken.expires_at > datetime.now(timezone.utc),
            )
        )
        candidates = result.scalars().all()

        for candidate in candidates:
            if verify_api_key(token, candidate.token_hash):
                candidate.last_used_at = datetime.now(timezone.utc)
                await db.commit()

                scopes = candidate.scopes or []
                if "agent:connect" not in scopes:
                    logger.warning(
                        f"OAuth token {token_prefix} missing agent:connect scope"
                    )
                    return None

                return RecorderAuthContext(
                    # Single-owner self-host: constant "local" (wire-compat).
                    tenant_id="local",
                    role="user-hosted",
                    is_trusted=False,
                    user_id=str(candidate.user_id),
                    scopes=scopes,
                    token_prefix=token_prefix,
                    max_sessions=2,  # Default for user-hosted, overridden by heartbeat
                )

    logger.warning(f"OAuth token validation failed for prefix {token_prefix}")
    return None


# ---------------------------------------------------------------------------
# JWT generation (coordinator → recorder HTTP pushes)
# ---------------------------------------------------------------------------
def generate_push_jwt(tenant_id: str, task_id: str = "") -> str:
    """
    Generate a short-lived JWT for coordinator→recorder HTTP push requests.

    The recorder validates this to ensure the push came from a legitimate
    coordinator, not an arbitrary network actor.
    """
    if not RECORDER_AUTH_SECRET:
        raise RuntimeError("RECORDER_AUTH_SECRET not configured")

    now = int(time.time())
    payload = {
        "tenant_id": str(tenant_id),
        "task_id": str(task_id),
        "role": "coordinator",
        "iat": now,
        "exp": now + PUSH_JWT_TTL_SECONDS,
    }
    return jwt.encode(payload, RECORDER_AUTH_SECRET, algorithm=JWT_ALGORITHM)


def validate_push_jwt(token: str) -> Optional[dict]:
    """
    Validate a coordinator push JWT on the recorder side.

    Returns the payload dict on success, None on failure.
    Used by the recorder's HTTP endpoint auth middleware.
    """
    if not RECORDER_AUTH_SECRET:
        logger.error("RECORDER_AUTH_SECRET not configured — cannot validate push JWT")
        return None

    try:
        payload = jwt.decode(
            token,
            RECORDER_AUTH_SECRET,
            algorithms=[JWT_ALGORITHM],
            options={"require_exp": True, "verify_exp": True},
        )
    except JWTError as e:
        logger.warning(f"Push JWT validation failed: {e}")
        return None

    if payload.get("role") != "coordinator":
        logger.warning(f"Push JWT has unexpected role: {payload.get('role')}")
        return None

    return payload


# ---------------------------------------------------------------------------
# Service token generation utility
# ---------------------------------------------------------------------------
def generate_service_token(
    tenant_id: str,
    max_sessions: int = 5,
    secret: Optional[str] = None,
    agent_id: Optional[str] = None,
    ttl_hours: int = 24,
    jti: Optional[str] = None,
) -> str:
    """
    Generate a JWT service token for infrastructure recorders.

    ``agent_id`` binds the token to a specific agent identity: the backend's
    /recorder/connect uses it (when the agent sends no id of its own) so an
    auto-provisioned server self-links under a KNOWN id — letting the autoscaler
    match the registered agent to its ProvisionedAgent record. ``ttl_hours``
    bounds the token's life (auto-provisioned servers get a generous TTL; the
    server is recycled before it lapses).

    Usage:
        python -c "
        from utils.recorder_auth import generate_service_token
        print(generate_service_token('your-tenant-id', max_sessions=5))
        "

    Then set the output as WRIT_SERVICE_TOKEN in docker-compose.yml.
    """
    signing_secret = secret or RECORDER_AUTH_SECRET
    if not signing_secret:
        raise RuntimeError("RECORDER_AUTH_SECRET not configured (or pass secret= arg)")

    payload = {
        # The claim is ALWAYS the constant "local" (wire compatibility with the
        # light writ-agent). The tenant_id argument is retained for call-site
        # compatibility but does not scope the token.
        "tenant_id": "local",
        "role": "infrastructure",
        "max_sessions": max_sessions,
        "iat": int(time.time()),
        "exp": datetime.utcnow() + timedelta(hours=ttl_hours),
    }
    if agent_id:
        payload["agent_id"] = str(agent_id)
    if jti:
        # Revocation handle: recorded in the fleet-token KV registry at mint
        # time and checked in validate_recorder_token. Tokens minted without a
        # jti (pre-existing fleet tokens) skip the registry check (back-compat).
        payload["jti"] = str(jti)
    return jwt.encode(payload, signing_secret, algorithm=JWT_ALGORITHM)
