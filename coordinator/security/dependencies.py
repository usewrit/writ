"""
FastAPI authentication dependencies.

Provides unified auth that works with JWT (web UI), OAuth tokens (integrations),
and API keys (programmatic). All resolve to the single owner's context.
"""
import hashlib
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models.user import User
from models.api_key import APIKey, Role
from security.jwt import decode_access_token, decode_access_token_with_blacklist
from security.api_key import verify_api_key

logger = logging.getLogger(__name__)

# Optional bearer token extractor (doesn't fail if no token)
optional_bearer = HTTPBearer(auto_error=False)


# In-memory fixed-window fallback counters for the API-key rate limiter, used ONLY
# when Redis is unavailable (down at request time or the backing op raises). Pure
# fail-open lets an attacker disable a key's explicit limit just by knocking Redis
# over, so for keys that DO carry a limit we fall back to a best-effort per-process
# counter instead. This is per-worker (not shared across the 4 uvicorn workers), so
# the effective ceiling is roughly limit*workers — looser than Redis but a real cap,
# not unlimited. Keys with no limit set are never throttled here.
# Map: bucket_key -> (window_start_epoch, count). Guarded by a lock (sync, no await).
_inmem_rate_buckets: dict[str, tuple[float, int]] = {}
_inmem_rate_lock = threading.Lock()
# Cap the dict so a flood of distinct keys can't grow it without bound.
_INMEM_RATE_MAX_BUCKETS = 100_000


def _inmem_rate_exceeded(bucket_key: str, ttl: int, limit: int) -> bool:
    """Best-effort in-process fixed-window counter. Returns True if over `limit`.

    Used as a fallback when Redis is unavailable so keys with an explicit limit
    fail (closed-ish) toward a cap rather than fully open.
    """
    now = time.monotonic()
    with _inmem_rate_lock:
        window_start, count = _inmem_rate_buckets.get(bucket_key, (now, 0))
        if now - window_start >= ttl:
            # Window elapsed — reset.
            window_start, count = now, 0
        count += 1
        # Opportunistic GC of stale buckets when the map gets large.
        if len(_inmem_rate_buckets) >= _INMEM_RATE_MAX_BUCKETS:
            for k in [
                bk for bk, (ws, _) in _inmem_rate_buckets.items() if now - ws >= ttl
            ]:
                _inmem_rate_buckets.pop(k, None)
        _inmem_rate_buckets[bucket_key] = (window_start, count)
        return count > limit


async def enforce_api_key_rate_limit(request: Request, key: "APIKey") -> None:
    """Sliding per-minute/per-hour rate limit for an API key, backed by Redis.

    No-op when both limits are unset. When Redis is unavailable for a key that DOES
    carry an explicit limit, falls back to a best-effort in-process counter (see
    `_inmem_rate_exceeded`) instead of failing fully open — a Redis outage must not
    silently disable a configured rate limit. Keys with no limit are unaffected.
    """
    per_min = getattr(key, "rate_limit_per_min", None)
    per_hour = getattr(key, "rate_limit_per_hour", None)
    if per_min is None and per_hour is None:
        return

    windows = []
    if per_min is not None:
        windows.append((f"apikey_rate:{key.id}:min", 60, per_min))
    if per_hour is not None:
        windows.append((f"apikey_rate:{key.id}:hour", 3600, per_hour))

    def _enforce_fallback() -> None:
        """Apply the in-memory fallback for every configured window."""
        for rk, ttl, limit in windows:
            if _inmem_rate_exceeded(rk, ttl, limit):
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=f"API key rate limit exceeded ({limit} per {ttl // 60 or 1}min window).",
                )

    redis = getattr(getattr(request, "app", None), "state", None)
    redis = getattr(redis, "redis", None)
    if redis is None:
        # No Redis configured/available — enforce the limit in-process rather than
        # failing open (which would nullify the key's explicit limit).
        _enforce_fallback()
        return

    try:
        for rk, ttl, limit in windows:
            count = await redis.incr(rk)
            if count == 1:
                await redis.expire(rk, ttl)
            if count > limit:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=f"API key rate limit exceeded ({limit} per {ttl // 60 or 1}min window).",
                )
    except HTTPException:
        raise
    except Exception as e:  # Redis hiccup — fall back to the in-memory counter.
        logger.warning(f"API key rate-limit check failed (in-memory fallback): {e}")
        _enforce_fallback()


class AuthContext:
    """Unified authentication context resolved from JWT, OAuth token, or API key.

    Single-owner coordinator: there is one implicit workspace and one owner;
    resource queries return all rows for that owner and downstream readers scope
    on ``user_id``.
    """

    def __init__(
        self,
        user_id: Optional[UUID] = None,
        role: str = "member",
        is_platform_admin: bool = False,
        auth_method: str = "none",
        oauth_scopes: Optional[list] = None,
        api_key_scopes: Optional[dict] = None,
        api_key_id: Optional[int] = None,
        api_key: Optional["APIKey"] = None,
    ):
        self.user_id = user_id
        self.role = role
        self.is_platform_admin = is_platform_admin
        self.auth_method = auth_method  # "jwt", "oauth", or "api_key"
        self.oauth_scopes = oauth_scopes  # Granted OAuth scopes (None if not OAuth)
        self.api_key_scopes = api_key_scopes  # Resource scopes from API key (None if not API key)
        self.api_key_id = api_key_id  # API key row id (None unless auth_method == "api_key")
        self.api_key = api_key  # API key ORM object, for per-key limit/metering checks

    def require_scope(self, resource_type: str, action: str, resource_id: int = None):
        """Raise 403 if API key lacks the required scope. JWT/OAuth pass through."""
        if not self.has_scope(resource_type, action, resource_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"API key lacks '{action}' permission on '{resource_type}'",
            )

    def has_scope(self, resource_type: str, action: str, resource_id: int = None) -> bool:
        """Object-level check. Endpoint reachability is decided at the choke point.

        A scope-less key is no longer "full access". That shortcut existed because
        the key screen let you create a key without picking anything and then the
        docs told you to use it — creation now requires at least one scope, so an
        empty grant means exactly what it says: nothing.
        """
        if self.auth_method != "api_key":
            return True
        from security import api_scopes
        return api_scopes.check(self.api_key_scopes, resource_type, action, resource_id)


async def _touch_presence(user_id) -> None:
    """No-op on self-host. This build has no live-presence tracking."""
    return None


async def _try_oauth_token(token: str, db: AsyncSession) -> Optional[AuthContext]:
    """
    Try to authenticate via OAuth access token.

    Uses prefix-based routing (wto_ prefix; legacy pso_ still accepted) for fast
    rejection of non-OAuth tokens, then SHA-256 prefix for O(1) database lookup,
    then Argon2 verification.
    """
    if not token.startswith(("wto_", "pso_")):
        return None

    from models.oauth import OAuthAccessToken
    from security.oauth_scopes import scopes_to_role

    # Compute lookup prefix (SHA-256 of full token, first 12 chars)
    token_prefix = hashlib.sha256(token.encode()).hexdigest()[:12]

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
            # Update last_used_at
            candidate.last_used_at = datetime.now(timezone.utc)
            await db.flush()

            scopes = candidate.scopes or []
            return AuthContext(
                user_id=candidate.user_id,
                role=scopes_to_role(scopes),
                is_platform_admin=False,
                auth_method="oauth",
                oauth_scopes=scopes,
            )

    return None


async def get_auth_context(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(optional_bearer),
    db: AsyncSession = Depends(get_db),
) -> AuthContext:
    """
    Unified authentication dependency.

    Tries in order:
    1. JWT (web UI users)
    2. OAuth access token (third-party integrations: Zapier, n8n, Make, etc.)
    3. API key (programmatic access)

    Returns AuthContext with user_id and role.
    """
    token = None
    if credentials:
        token = credentials.credentials
    elif "authorization" in request.headers:
        auth_header = request.headers["authorization"]
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 1. Try JWT first (with blacklist check)
    payload = await decode_access_token_with_blacklist(token)
    if payload:
        user_id = UUID(payload["sub"])
        # Live presence: a JWT request is a human using the web UI. No-op on
        # self-host; never blocks or fails auth.
        await _touch_presence(user_id)
        return AuthContext(
            user_id=user_id,
            role=payload.get("role", "member"),
            is_platform_admin=payload.get("is_platform_admin", False),
            auth_method="jwt",
        )

    # 2. Try OAuth access token (prefix-based fast path)
    oauth_ctx = await _try_oauth_token(token, db)
    if oauth_ctx:
        return oauth_ctx

    # 3. Fall back to API key (prefix-based O(1) lookup)
    from security.api_key import compute_key_prefix
    prefix = compute_key_prefix(token)
    result = await db.execute(
        select(APIKey).where(
            APIKey.key_prefix == prefix,
            APIKey.revoked_at.is_(None),
        )
    )
    candidates = result.scalars().all()

    # Fallback for legacy keys without prefix stored
    if not candidates:
        from sqlalchemy import or_
        result = await db.execute(
            select(APIKey).where(
                or_(APIKey.key_prefix == '', APIKey.key_prefix.is_(None)),
                APIKey.revoked_at.is_(None),
            )
        )
        candidates = result.scalars().all()

    for key in candidates:
        if verify_api_key(token, key.key_hash):
            # Per-key gates: expiry + rate limit (before recording usage).
            if key.is_expired():
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="API key expired",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            await enforce_api_key_rate_limit(request, key)

            # Record usage OUTSIDE this request's transaction.
            #
            # Assigning to the ORM object and flushing takes SQLite's single write
            # lock and HOLDS it until the request ends. That breaks any endpoint
            # that calls back into this coordinator inside its own request — which
            # /mcp does for every tool, over loopback HTTP: the nested request's
            # own INSERT/UPDATE waited out the 5s busy_timeout and died with
            # "database is locked". Stamping in a short, immediately-committed
            # transaction (and only once a minute) keeps the lock free.
            from security.api_key import record_key_usage_throttled

            await record_key_usage_throttled(key)

            # THE choke point — every surface a key may reach is named in
            # api_scopes' route map; anything absent is refused here, before the
            # endpoint runs.
            await _enforce_key_route_scope(request, key)

            return AuthContext(
                user_id=key.user_id,
                role=key.role.value if key.role else "viewer",
                is_platform_admin=False,
                auth_method="api_key",
                api_key_scopes=key.scopes,
                api_key_id=key.id,
                api_key=key,
            )

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired token",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_current_user(
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Get the authenticated user object."""
    if not auth.user_id:
        raise HTTPException(status_code=401, detail="User authentication required")

    result = await db.execute(select(User).where(User.id == auth.user_id))
    user = result.scalar_one_or_none()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or inactive")
    return user


async def require_verified(
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
) -> AuthContext:
    """Require the current user to have a verified email before a sensitive action.

    Gates things like storing credentials, wallet top-up, and publishing a listing.
    Resolves the User row each time (the JWT can't carry live
    verification state).

    Pass-through for non-user auth (API key / OAuth without a user): those contexts
    have no User to verify and are already scoped+rate-limited at issuance, mirroring
    get_me's api-key branch (auth.py). Raises 403 only for a resolvable, unverified
    user.
    """
    if not auth.user_id:
        # API-key / OAuth context without a user — nothing to verify, pass through.
        return auth
    result = await db.execute(select(User).where(User.id == auth.user_id))
    user = result.scalar_one_or_none()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or inactive")
    if not user.is_verified:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Email verification required for this action",
        )
    return auth


async def require_platform_admin(
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
) -> AuthContext:
    """Require the admin owner, on a FIRST-PARTY SESSION, verified from the database.

    Two independent conditions, and both are load-bearing:

    1. ``auth_method == "jwt"``. On a single-owner coordinator every credential
       resolves to the same person: API keys are minted with ``user_id`` set to the
       owner (routers/auth.py), and OAuth grants carry the owner's ``user_id`` too.
       So a User-row check ALONE would let a deliberately read-only scoped API key —
       or a third-party OAuth token the operator granted a narrow scope to — reach
       every administrative route, including fleet service-token minting and the
       vault-decrypting deploy path. Scopes are enforced by ``RequireScope`` /
       ``RequireOAuthScope``, which the admin routers do not use, so this dependency
       is the only thing standing between a scoped token and full administration.
       Administration requires the operator's own browser session, nothing else.

    2. The User row still says ``is_platform_admin``. A JWT claim alone could be
       stale (rights revoked after the token was minted), so it is re-read here on
       every request rather than trusted from the token body.
    """
    if not auth.user_id:
        raise HTTPException(status_code=401, detail="Authentication required")

    if getattr(auth, "auth_method", None) != "jwt":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Platform administration requires a first-party session. API keys "
                "and OAuth tokens cannot reach administrative endpoints."
            ),
        )

    result = await db.execute(select(User).where(User.id == auth.user_id))
    user = result.scalar_one_or_none()
    if not user or not user.is_platform_admin:
        raise HTTPException(status_code=403, detail="Platform admin required")

    return auth


async def _enforce_key_route_scope(request: Request, key: "APIKey") -> None:
    """Refuse an API key that does not hold the scope this route requires.

    Fail-closed: a path with no rule in `api_scopes` is not part of the API-key
    surface and is refused rather than waved through, so adding a router cannot
    silently widen what existing keys can do.

    Role.ADMIN keys (the bootstrap/admin key minted by scripts/bootstrap_admin.py)
    are exempt — that key exists precisely to administer the coordinator.
    """
    from security import api_scopes

    if key.role == Role.ADMIN:
        return

    path = request.url.path if request else ""
    method = request.method if request else "GET"

    if api_scopes.is_always_allowed(path):
        return

    needed = api_scopes.required_scope(method, path)
    if needed is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This endpoint cannot be called with an API key",
        )

    if needed not in api_scopes.granted_scopes(key.scopes):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"API key is missing the '{needed}' scope",
        )


def RequireScope(resource_type: str, action: str):
    """
    FastAPI dependency that enforces API key resource scopes.
    JWT and OAuth users pass through (existing RBAC handles them).

    Usage:
        @router.get("/workflows")
        async def list_workflows(auth: AuthContext = Depends(RequireScope("workflows", "read"))):
            ...
    """
    async def dependency(auth: AuthContext = Depends(get_auth_context)):
        if auth.auth_method == "api_key" and not auth.has_scope(resource_type, action):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"API key lacks '{action}' permission on '{resource_type}'",
            )
        return auth
    return dependency


def check_api_key_scope(api_key_dict: dict, resource_type: str, action: str, resource_id: int = None):
    """
    Check scopes on the legacy get_current_api_key dict.
    JWT users (no scopes key) pass through. Raises 403 if denied.
    """
    from security import api_scopes
    scopes = api_key_dict.get("scopes")
    if scopes is None:
        return
    if not api_scopes.check(scopes, resource_type, action):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"API key lacks '{action}' permission on '{resource_type}'",
        )
    ids = api_scopes.allowed_ids(scopes, resource_type)
    if ids is not None and resource_id is not None and resource_id not in ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"API key does not have access to {resource_type} #{resource_id}",
        )


def filter_by_scope(api_key_dict: dict, resource_type: str) -> Optional[list]:
    """
    Return the list of allowed IDs for a resource type, or None if all are allowed.
    Returns empty list if no access at all.
    """
    from security import api_scopes
    scopes = api_key_dict.get("scopes")
    if scopes is None:
        return None
    if not api_scopes.check(scopes, resource_type, "read"):
        return []
    return api_scopes.allowed_ids(scopes, resource_type)


def RequireOAuthScope(scope: str):
    """
    FastAPI dependency that enforces an OAuth scope when auth_method is oauth.
    For non-OAuth auth methods, this is a no-op (existing RBAC handles it).

    Usage:
        @router.get("/targets")
        async def list_targets(auth: AuthContext = Depends(RequireOAuthScope("targets:read"))):
            ...
    """
    async def dependency(auth: AuthContext = Depends(get_auth_context)):
        if auth.auth_method == "oauth" and auth.oauth_scopes is not None:
            from security.oauth_scopes import check_scope
            if not check_scope(auth.oauth_scopes, scope):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"OAuth scope '{scope}' required",
                )
        return auth
    return dependency
