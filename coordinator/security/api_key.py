"""
API Key authentication and management.
Uses Argon2 for secure password hashing.
"""
import hashlib
import secrets
import logging
from datetime import datetime
from typing import Optional
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from sqlalchemy.orm.attributes import set_committed_value
from database import get_db

logger = logging.getLogger(__name__)

# Initialize Argon2 password hasher with secure defaults
ph = PasswordHasher(
    time_cost=2,        # Number of iterations
    memory_cost=65536,  # Memory usage in KiB (64 MiB)
    parallelism=4,      # Number of parallel threads
    hash_len=32,        # Length of the hash in bytes
    salt_len=16,        # Length of random salt in bytes
)

# Bearer token authentication scheme
security = HTTPBearer(auto_error=False)

# How often a key's last_used_at is actually written. See the usage-recording
# block in get_api_key for why this is throttled rather than written per request.
_LAST_USED_THROTTLE_SECONDS = 60

# Endpoints a CLIENT key (the only kind the owner can mint) may call.
#
# This list was inherited from the multi-tenant cloud fork as just
# ("/api/v1/", "/api/webhooks/trigger/"). On the self-host coordinator the ENTIRE
# /api/v1 surface is five file routes (routers/files.py) — there is no versioned
# workflow API here — so that allowlist let a key reach essentially nothing, and
# every advertised API-key integration failed with 403. The MCP server is the
# clearest case: it serves /mcp by calling this coordinator's own REST API over
# loopback with the caller's bearer, so each of the paths below is reached with
# the key the owner pasted into their MCP client.
#
# Deliberately an explicit list, not a blanket allow: /api/admin* and every
# platform/ops route stay unreachable (and the Role.ADMIN check below still runs).
# This coordinator is single-owner — a key already acts as that owner — so the
# boundary that matters is "programmatic surfaces vs. platform administration".
CLIENT_KEY_ALLOWED_PREFIXES = (
    "/mcp",                      # MCP endpoint itself (JSON-RPC tool calls)
    "/api/v1/",                  # versioned public API (files)
    "/api/webhooks/",            # webhook triggers + trigger management
    "/api/automation/",          # workflows, their data and exports
    "/api/runs",                 # run history feed
    "/api/crawl",                # start/poll crawls
    "/api/triggers",             # schedules
    "/api/targets",              # monitors
    "/api/recorder",             # live record/browser sessions
)


async def record_key_usage_throttled(api_key) -> None:
    """Stamp an API key's last_used_at, throttled, off the caller's transaction.

    The shared entry point for every auth path (see get_api_key below and
    get_auth_context in security/dependencies.py) so none of them can go back to
    writing on the request's own session.
    """
    now = datetime.utcnow()
    last_used = getattr(api_key, "last_used_at", None)
    if last_used is not None and (now - last_used).total_seconds() < _LAST_USED_THROTTLE_SECONDS:
        return
    await _record_key_usage(api_key.id, now)
    # Keep the in-session object consistent with what was just persisted, without
    # marking it dirty for the request's transaction.
    try:
        set_committed_value(api_key, "last_used_at", now)
    except Exception:
        pass


async def _record_key_usage(api_key_id: int, when: "datetime") -> None:
    """Stamp last_used_at in its OWN short transaction. Never raises.

    Deliberately not the request's session: that session flushes without
    committing, so the row lock lives until the request ends AND the new value
    stays invisible to anyone else. Both matter here, because /mcp calls back
    into this same coordinator over loopback inside its own request — the nested
    request would queue behind the outer write, wait out SQLite's busy timeout,
    and only then proceed. Committing immediately releases the lock at once and
    lets the nested request SEE the fresh timestamp, so the throttle above skips
    the redundant write instead of blocking on it.
    """
    from database import AsyncSessionLocal
    from models.api_key import APIKey as _APIKey

    try:
        async with AsyncSessionLocal() as session:
            await session.execute(
                update(_APIKey).where(_APIKey.id == api_key_id).values(last_used_at=when)
            )
            await session.commit()
    except Exception as e:  # lock contention, disk full, … — never deny a valid key
        logger.warning(f"Could not record API key usage (continuing): {e}")


def compute_key_prefix(raw_key: str) -> str:
    """Compute a non-secret prefix for O(1) API key lookup.

    Uses SHA-256 (fast) truncated to 8 hex chars. This is NOT the security
    hash -- Argon2 is still used for full verification. The prefix only
    narrows the candidate set from O(n) to O(1).
    """
    return hashlib.sha256(raw_key.encode()).hexdigest()[:8]


def generate_api_key(prefix: str = "wt") -> str:
    """
    Generate a cryptographically secure API key.

    Args:
        prefix: Key prefix for identification (default: "wt" for Writ)

    Returns:
        API key string in format: {prefix}_{random_token}

    Example:
        >>> key = generate_api_key()
        >>> key.startswith("wt_")
        True
        >>> len(key)
        47  # wt_ + 44 characters
    """
    # Generate 32 bytes (256 bits) of random data
    token = secrets.token_urlsafe(32)
    return f"{prefix}_{token}"


def _oauth_scopes_to_api_key_scopes(oauth_scopes: Optional[list]) -> dict:
    """Translate a granted OAuth scope list into the per-resource permission dict
    that ``check_api_key_scope`` enforces.

    Without this, the OAuth branch of ``get_current_api_key`` returned no ``scopes``
    key, so ``check_api_key_scope`` short-circuited and OAuth tokens passed EVERY
    endpoint scope check unchecked (finding #25). OAuth uses a product-facing
    vocabulary (targets / triggers / reports / changes) while the endpoints gate on
    the API-key resource vocabulary (checks / automations / files / workflows), so
    the two are mapped here. A ``:write`` grant also confers ``delete`` and
    ``:execute`` also confers ``run`` — matching how the linked-desktop controls
    (workflow update/delete, monitor pause/resume) actually operate — so legitimately
    scoped callers keep working. ``ids`` is ``None`` (all rows) because an OAuth token
    is a full-account link, not an ID-scoped key.
    """
    # OAuth resource -> API-key resource(s) the endpoints actually gate on.
    resource_map = {
        "targets": ("targets", "checks"),
        "changes": ("checks",),
        "workflows": ("workflows",),
        "triggers": ("automations",),
        "reports": ("files",),
        "notifications": ("notifications",),
    }
    # OAuth action -> API-key permission(s).
    action_map = {
        "read": ("read",),
        "write": ("write", "delete"),
        "execute": ("execute", "run"),
    }
    out: dict = {}
    for scope in oauth_scopes or []:
        if ":" not in scope:
            continue  # marker scopes (e.g. agent:connect) carry no resource grant
        resource, _, action = scope.partition(":")
        for api_resource in resource_map.get(resource, (resource,)):
            bucket = out.setdefault(api_resource, {"permissions": [], "ids": None})
            for perm in action_map.get(action, (action,)):
                if perm not in bucket["permissions"]:
                    bucket["permissions"].append(perm)
    return out


def hash_api_key(api_key: str) -> str:
    """
    Hash an API key using Argon2.

    Args:
        api_key: The plaintext API key

    Returns:
        Argon2 hash string

    Example:
        >>> key = "wt_test123"
        >>> hash_val = hash_api_key(key)
        >>> hash_val.startswith("$argon2")
        True
    """
    return ph.hash(api_key)


def verify_api_key(api_key: str, hash_value: str) -> bool:
    """
    Verify an API key against its hash.

    Args:
        api_key: The plaintext API key to verify
        hash_value: The Argon2 hash to verify against

    Returns:
        True if the key matches, False otherwise

    Example:
        >>> key = "wt_test123"
        >>> hash_val = hash_api_key(key)
        >>> verify_api_key(key, hash_val)
        True
        >>> verify_api_key("wrong_key", hash_val)
        False
    """
    try:
        ph.verify(hash_value, api_key)

        # Check if rehashing is needed (parameters changed)
        if ph.check_needs_rehash(hash_value):
            logger.info("API key hash needs rehashing with updated parameters")

        return True
    except (VerifyMismatchError, VerificationError) as e:
        logger.debug(f"API key verification failed: {e}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error during API key verification: {e}")
        return False


async def get_current_api_key(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Dependency to get and validate the current API key from request.

    CLIENT-role keys are restricted to /api/v1/* and webhook-trigger endpoints.

    Args:
        request: FastAPI request (for path checking)
        credentials: HTTP Bearer token credentials
        db: Database session (injected via dependency)

    Returns:
        Dictionary containing API key info

    Raises:
        HTTPException: If authentication fails

    Example:
        @app.get("/protected")
        async def protected_route(
            api_key: dict = Depends(get_current_api_key)
        ):
            return {"user": api_key["label"]}
    """
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials

    # Try JWT decode first (new auth system — web UI users)
    try:
        # Blacklist-aware decode so revoked/logged-out JWTs are rejected (parity with get_auth_context)
        from security.jwt import decode_access_token_with_blacklist
        jwt_payload = await decode_access_token_with_blacklist(token)
        if jwt_payload:
            # Valid JWT — return auth context compatible with legacy format
            is_platform_admin = bool(jwt_payload.get("is_platform_admin"))
            role = "admin" if is_platform_admin else jwt_payload.get("role", "viewer")
            return {
                "id": None,
                "label": "jwt",
                "role": role,
                # Trustworthy platform-admin signal (signed JWT claim). NOTE: `role`
                # alone is NOT a platform-admin check — org-admins also get role=="admin".
                # Gate infra/topology disclosure on this flag, not on role.
                "is_platform_admin": is_platform_admin,
                "user_id": jwt_payload.get("sub"),
            }
    except Exception:
        pass  # Not a JWT, try API key below

    # OAuth access token (wto_/pso_) — user-hosted device tokens (desktop "Connect a
    # cloud account", CLI `writ login`) and third-party integrations. Previously this
    # dependency only accepted JWT + API keys, so OAuth tokens were rejected here even
    # though `get_auth_context` (used by e.g. /personas) already accepts them. Accept
    # them here too so a linked desktop can read/sync workflows + targets. Mirrors the
    # JWT branch's dict shape; the token is never platform-admin (re-enforced
    # server-side per call). Unlike JWT, we DO emit a "scopes" dict translated from
    # the token's granted OAuth scopes so `check_api_key_scope` actually holds the
    # token to its grant instead of passing every scope check unchecked (finding #25).
    try:
        from security.dependencies import _try_oauth_token
        oauth_ctx = await _try_oauth_token(token, db)
        if oauth_ctx and oauth_ctx.user_id:
            return {
                "id": None,
                "label": "oauth",
                "role": oauth_ctx.role,
                "is_platform_admin": False,
                "scopes": _oauth_scopes_to_api_key_scopes(oauth_ctx.oauth_scopes),
                "user_id": str(oauth_ctx.user_id),
            }
    except Exception:
        pass  # Not an OAuth token, try API key below

    # Fall back to API key verification
    from models.api_key import APIKey

    try:
        # Use SHA-256 prefix for O(1) lookup instead of iterating all keys
        prefix = compute_key_prefix(token)
        result = await db.execute(
            select(APIKey).where(
                APIKey.key_prefix == prefix,
                APIKey.revoked_at.is_(None),
            )
        )
        candidates = result.scalars().all()

        # Verify token with Argon2 only against prefix-matched candidates (usually 0 or 1)
        for api_key in candidates:
            if verify_api_key(token, api_key.key_hash):
                # Per-key gates: expiry + rate limit (before recording usage).
                if api_key.is_expired():
                    raise HTTPException(
                        status_code=status.HTTP_401_UNAUTHORIZED,
                        detail="API key expired",
                        headers={"WWW-Authenticate": "Bearer"},
                    )
                from security.dependencies import enforce_api_key_rate_limit
                await enforce_api_key_rate_limit(request, api_key)

                # Record usage — THROTTLED, and never fatal.
                #
                # This is a WRITE, flushed (not committed) so the lock is held until
                # the request handler's transaction ends. SQLite allows exactly one
                # writer, and the coordinator runs single-worker over one file, so an
                # endpoint that calls BACK into this coordinator inside its own request
                # — which /mcp does for every tool, hitting its own REST API over
                # loopback — made the inner request wait on the outer request's lock,
                # time out with "database is locked", and fall through to the generic
                # 500 handler below as "Authentication error". Every MCP tool call
                # failed that way after a ~5s stall.
                #
                # Writing at most once per _LAST_USED_THROTTLE_SECONDS removes the
                # contention (the nested call skips the write the outer one just did)
                # and keeps last_used_at accurate enough for "when was this key last
                # seen". A failure here must never deny an otherwise-valid key.
                await record_key_usage_throttled(api_key)

                # Route gate — the same one `get_auth_context` applies, so a key
                # behaves identically whichever dependency an endpoint uses.
                #
                # This replaces a prefix allowlist that lived only on THIS path:
                # the modern dependency had no equivalent, so the confinement
                # evaporated the moment an endpoint switched to get_auth_context.
                # Coverage now comes from one (method, path) -> scope map, and a
                # path absent from it is denied rather than waved through.
                from models.api_key import Role
                from security.dependencies import _enforce_key_route_scope
                request_path = request.url.path if request else ""
                await _enforce_key_route_scope(request, api_key)

                # Block non-admin keys from admin endpoints
                if request_path.startswith("/api/admin") and not api_key.role == Role.ADMIN:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="Admin endpoints require admin API key",
                    )

                logger.info(f"API key authenticated: {api_key.label}")

                return {
                    "id": api_key.id,
                    "api_key_id": api_key.id,
                    "label": api_key.label,
                    "role": api_key.role.value,
                    # API keys are never treated as platform-admin for infra/topology
                    # disclosure. Platform admins use the signed-JWT web console.
                    "is_platform_admin": False,
                    "scopes": api_key.scopes,
                    # Single-owner coordinator: the key belongs to the one owner.
                    "user_id": str(api_key.user_id) if api_key.user_id else None,
                    "obj": api_key,  # ORM object for per-key limit/metering checks
                }

        # No matching key found
        logger.warning(f"Invalid API key attempt: {token[:10]}...")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
            headers={"WWW-Authenticate": "Bearer"},
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error during API key authentication: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Authentication error",
        )


def get_optional_api_key(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> Optional[dict]:
    """
    Optional API key dependency - doesn't raise error if not provided.

    Returns:
        API key info or None
    """
    if not credentials:
        return None

    # Resolving the key requires an async context; this synchronous variant
    # returns None until that path is implemented.
    return None
