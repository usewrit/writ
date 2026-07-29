"""
JWT authentication module.

Handles creation and validation of access/refresh tokens for the web UI.
API keys remain the primary auth method for programmatic access.
"""
import uuid
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any

from jose import jwt, JWTError

from config import settings

logger = logging.getLogger(__name__)

# JWT Configuration — uses api_secret_key from settings (same key used for API token generation).
#
# SECURITY: this signing key must never be a shipped/well-known default, or session
# and API tokens (HS256) become forgeable. That invariant is enforced fail-closed at
# startup by config.Settings._validate_production_secrets, which refuses to boot when
# jwt_secret_key / api_secret_key is still a shipped default in ANY environment (the
# lone exception being an explicit local trial: ENVIRONMENT=development AND
# ALLOW_INSECURE_DEV=true). By the time this module imports settings, that guard has
# already run, so the fallback below is safe.
JWT_SECRET_KEY = getattr(settings, 'jwt_secret_key', None) or settings.api_secret_key

# Defence in depth. The startup validator above is the primary gate, but this
# module is the single place every token is signed and verified, so it asserts the
# invariant itself rather than trusting that it was checked upstream. A blank key
# is the dangerous case: python-jose will happily sign and verify HS256 with a
# zero-length key, so an empty JWT_SECRET_KEY is not an error that surfaces later —
# it is a publicly known signing key that lets anyone mint an is_platform_admin
# session. Fail at import instead.
if not (JWT_SECRET_KEY or "").strip():
    raise RuntimeError(
        "JWT_SECRET_KEY resolved to an empty value. Set JWT_SECRET_KEY (or "
        "API_SECRET_KEY, which it falls back to) to a strong secret — generate one "
        "with: openssl rand -hex 32. Refusing to start: HS256 signing with an empty "
        "key would let anyone forge an administrator session."
    )

JWT_ALGORITHM = "HS256"
# Built-in defaults. The OPERATOR's values live in the `coordinator_security`
# settings section and are applied over these by `set_session_policy` — see below.
ACCESS_TOKEN_EXPIRE_MINUTES = 15
REFRESH_TOKEN_EXPIRE_DAYS = 7

# ============================================================================
# Session policy (Settings → Security)
# ============================================================================
# The two TTLs were rendered as editable fields and read by NOTHING: token
# minting used the module constants above, so changing "Session lifetime" in the
# UI had no effect whatsoever. Worse, the settings section defaulted
# `refresh_ttl_days` to 30 while the real constant was 7, so the UI actively
# reported a lifetime the coordinator did not honour.
#
# Token creation is synchronous and on the hot path, so it cannot read the DB.
# The values are instead pushed into these module-level slots — at boot from the
# persisted section, and again whenever the owner saves — which is the same
# live-apply contract `security.trusted_hosts` uses for the Host allowlist.
_access_ttl_minutes: int = ACCESS_TOKEN_EXPIRE_MINUTES
_refresh_ttl_days: int = REFRESH_TOKEN_EXPIRE_DAYS


def set_session_policy(access_ttl_minutes: int, refresh_ttl_days: int) -> None:
    """Apply the operator's session TTLs to all subsequently minted tokens.

    Existing tokens keep the lifetime they were signed with — a JWT's `exp` is in
    the token. Shortening the TTL therefore takes effect as sessions renew, not
    retroactively; use "Sign out everywhere" to cut current sessions immediately.
    """
    global _access_ttl_minutes, _refresh_ttl_days
    # Clamp defensively even though the settings layer coerces: this is called
    # with whatever is in the DB, including rows written by an older build.
    _access_ttl_minutes = max(1, min(int(access_ttl_minutes), 1_440))
    _refresh_ttl_days = max(1, min(int(refresh_ttl_days), 3_650))
    logger.info(
        "Session policy applied: access=%dmin refresh=%dd",
        _access_ttl_minutes, _refresh_ttl_days,
    )


def access_ttl_minutes() -> int:
    """Access-token lifetime currently in force."""
    return _access_ttl_minutes


def refresh_ttl_days() -> int:
    """Refresh-token lifetime currently in force."""
    return _refresh_ttl_days


# ============================================================================
# JWT Blacklist (Redis-backed)
# ============================================================================

_redis_client = None


def set_redis_client(redis):
    """Set the Redis client for JWT blacklisting. Called from main.py startup."""
    global _redis_client
    _redis_client = redis


async def blacklist_token(jti: str, expires_in_seconds: Optional[int] = None):
    """Add a token JTI to the blacklist. Token auto-expires from Redis when it would naturally expire."""
    if not _redis_client:
        return
    if expires_in_seconds is None:
        # Follow the LIVE access TTL: a blacklist entry that expires before the
        # token it revokes would let a revoked token work again.
        expires_in_seconds = access_ttl_minutes() * 60
    try:
        await _redis_client.setex(f"jwt_blacklist:{jti}", expires_in_seconds, "1")
    except Exception as e:
        logger.error(f"Failed to blacklist token: {e}")


async def blacklist_decoded_token(payload: Dict[str, Any]) -> None:
    """Blacklist a decoded token by its jti for its remaining lifetime.

    Used to revoke a specific token (e.g. the refresh token being rotated, or
    both tokens on logout) without affecting the user's other sessions. The TTL
    matches the token's own `exp` so the blacklist entry self-cleans.
    """
    jti = payload.get("jti")
    if not jti:
        return
    exp = payload.get("exp")
    if isinstance(exp, datetime):
        exp = int(exp.timestamp())
    now = int(datetime.now(timezone.utc).timestamp())
    ttl = max(1, int(exp) - now) if exp else access_ttl_minutes() * 60
    await blacklist_token(jti, expires_in_seconds=ttl)


async def blacklist_user_tokens(user_id: str):
    """Blacklist all tokens for a user by storing a 'tokens_invalid_before' timestamp."""
    from security.memory_fallback import MemoryFallback

    # Always record in memory as well, so fail-closed logic can check it
    MemoryFallback.invalidate_user_tokens(user_id)

    if not _redis_client:
        logger.warning(f"Redis unavailable — user token invalidation stored in memory only for user {user_id}")
        return
    try:
        now = int(datetime.now(timezone.utc).timestamp())
        await _redis_client.setex(
            f"jwt_user_invalidated:{user_id}",
            # Must outlive the longest token it invalidates. Pinned to the LIVE
            # refresh TTL, not the build-time constant: once that TTL became
            # operator-configurable, an owner raising it to 30 days would have
            # had this marker expire after 7 — resurrecting refresh tokens that a
            # password change or "sign out everywhere" had already revoked.
            refresh_ttl_days() * 86400,
            str(now),
        )
    except Exception as e:
        logger.error(f"Failed to invalidate user tokens in Redis (in-memory fallback active): {e}")


async def is_jti_blacklisted(jti: str) -> bool:
    """Is this specific token revoked by jti? Fails CLOSED when Redis is down.

    Split out of :func:`is_token_blacklisted` so the refresh endpoint can tell
    "this exact token was rotated" (recoverable via the rotation grace window
    below) apart from "every token for this user was invalidated" (a password
    change / logout-all — never recoverable).
    """
    if not jti:
        return False
    if not _redis_client:
        logger.warning("Redis unavailable for jti blacklist check — failing closed")
        return True
    try:
        return bool(await _redis_client.exists(f"jwt_blacklist:{jti}"))
    except Exception as e:
        logger.error(f"jti blacklist check error — failing closed: {e}")
        return True


async def is_user_invalidated(user_id: str, iat: Optional[int]) -> bool:
    """Was every token issued before ``iat`` invalidated for this user?

    Set by :func:`blacklist_user_tokens` on password change / logout-all. Fails
    CLOSED when Redis is down (the in-memory fallback is consulted first).
    """
    from security.memory_fallback import MemoryFallback

    if not user_id or iat is None:
        return False
    if not _redis_client:
        logger.warning("Redis unavailable for user-invalidation check — failing closed")
        if MemoryFallback.is_user_token_invalidated(user_id, iat):
            return True
        return True
    try:
        invalidated_at = await _redis_client.get(f"jwt_user_invalidated:{user_id}")
        if invalidated_at:
            if isinstance(invalidated_at, bytes):
                invalidated_at = invalidated_at.decode()
            return iat < int(invalidated_at)
        return False
    except Exception as e:
        logger.error(f"User-invalidation check error — failing closed: {e}")
        return True


# ============================================================================
# Refresh-token rotation grace window
# ============================================================================
#
# Rotation is single-use: /auth/refresh blacklists the presented token and issues
# a successor. That is correct until two requests carrying the SAME cookie are in
# flight at once — which the browser produces routinely:
#
#   * a hard reload issued while the previous page's refresh is still in flight
#     (the new page sends the cookie value the browser held at navigation time),
#   * two tabs booting together (each JS context has its own single-flight guard),
#   * a background tab waking after another tab rotated.
#
# The loser presents an already-rotated token, gets a 401, and the SPA treats
# that as "session expired" — a spurious logout.
#
# So a rotated token stays redeemable for a SHORT window, during which it returns
# the SAME successor it originally minted (idempotent replay) instead of failing.
# A genuine stolen-token replay arrives long after that window and still 401s.
# This is the standard "refresh reuse interval" trade-off; set
# REFRESH_ROTATION_GRACE_SECONDS=0 to restore strict single-use.
def _rotation_grace_seconds() -> int:
    try:
        return max(0, int(getattr(settings, "refresh_rotation_grace_seconds", 30)))
    except (TypeError, ValueError):
        return 30


async def remember_rotation(old_jti: str, successor_token: str) -> None:
    """Record the successor a refresh token minted, for the grace window."""
    grace = _rotation_grace_seconds()
    if not old_jti or not grace or not _redis_client:
        return
    try:
        await _redis_client.setex(f"jwt_refresh_successor:{old_jti}", grace, successor_token)
    except Exception as e:
        logger.error(f"Failed to record refresh rotation successor: {e}")


async def get_rotation_successor(old_jti: str) -> Optional[str]:
    """The successor token a just-rotated refresh token minted, if still in grace."""
    if not old_jti or not _rotation_grace_seconds() or not _redis_client:
        return None
    try:
        value = await _redis_client.get(f"jwt_refresh_successor:{old_jti}")
        if isinstance(value, bytes):
            value = value.decode()
        return value or None
    except Exception as e:
        logger.error(f"Failed to read refresh rotation successor: {e}")
        return None


async def is_token_blacklisted(jti: str, user_id: str = None, iat: int = None) -> bool:
    """Check if a token is blacklisted (by JTI or user-level invalidation).

    SECURITY: Fails CLOSED when Redis is unavailable — tokens are treated as
    blacklisted. This ensures that a password change or forced logout is honoured
    even if Redis is temporarily down.
    """
    from security.memory_fallback import MemoryFallback

    if not _redis_client:
        logger.warning("Redis unavailable for token blacklist check — failing closed")
        # Check in-memory invalidations as best-effort
        if user_id and iat and MemoryFallback.is_user_token_invalidated(user_id, iat):
            return True
        # Fail closed: treat token as blacklisted when Redis is down
        return True
    try:
        # Check JTI blacklist
        if jti and await _redis_client.exists(f"jwt_blacklist:{jti}"):
            return True
        # Check user-level invalidation
        if user_id and iat:
            invalidated_at = await _redis_client.get(f"jwt_user_invalidated:{user_id}")
            if invalidated_at:
                if isinstance(invalidated_at, bytes):
                    invalidated_at = invalidated_at.decode()
                if iat < int(invalidated_at):
                    return True
        return False
    except Exception as e:
        logger.error(f"Token blacklist check error — failing closed: {e}")
        # Fail closed on Redis error
        return True


def create_access_token(
    user_id: str,
    org_id: str,
    role: str = "member",
    is_platform_admin: bool = False,
) -> str:
    """Create a JWT access token for web UI authentication."""
    expires = datetime.now(timezone.utc) + timedelta(minutes=access_ttl_minutes())
    payload = {
        "sub": str(user_id),
        "org_id": str(org_id),
        "role": role,
        "is_platform_admin": is_platform_admin,
        "type": "access",
        "exp": expires,
        "iat": datetime.now(timezone.utc),
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)


MFA_CHALLENGE_EXPIRE_MINUTES = 5


def create_mfa_challenge_token(user_id: str) -> str:
    """Short-lived token proving primary auth (password/social) succeeded.

    It is NOT a session token — it only authorizes completing the MFA step at
    /auth/mfa/login. Cannot be used as an access or refresh token (distinct type).
    """
    expires = datetime.now(timezone.utc) + timedelta(minutes=MFA_CHALLENGE_EXPIRE_MINUTES)
    payload = {
        "sub": str(user_id),
        "type": "mfa_challenge",
        "exp": expires,
        "iat": datetime.now(timezone.utc),
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)


async def decode_mfa_challenge_token(token: str) -> Optional[Dict[str, Any]]:
    """Decode + validate an MFA challenge token (type, expiry, blacklist)."""
    payload = decode_token(token)
    if not payload or payload.get("type") != "mfa_challenge":
        return None
    jti = payload.get("jti")
    user_id = payload.get("sub")
    iat = payload.get("iat")
    if isinstance(iat, datetime):
        iat = int(iat.timestamp())
    if jti and await is_token_blacklisted(jti, user_id=user_id, iat=iat):
        return None
    return payload


def create_refresh_token(user_id: str) -> str:
    """Create a JWT refresh token for token renewal."""
    expires = datetime.now(timezone.utc) + timedelta(days=refresh_ttl_days())
    payload = {
        "sub": str(user_id),
        "type": "refresh",
        "exp": expires,
        "iat": datetime.now(timezone.utc),
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> Optional[Dict[str, Any]]:
    """Decode and validate a JWT token. Returns payload or None if invalid."""
    try:
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        return payload
    except JWTError:
        return None


def decode_access_token(token: str) -> Optional[Dict[str, Any]]:
    """Decode an access token and verify it's the correct type."""
    payload = decode_token(token)
    if payload and payload.get("type") == "access":
        return payload
    return None


async def decode_access_token_with_blacklist(token: str) -> Optional[Dict[str, Any]]:
    """Decode access token and check against blacklist."""
    payload = decode_token(token)
    if not payload or payload.get("type") != "access":
        return None
    jti = payload.get("jti")
    user_id = payload.get("sub")
    iat = payload.get("iat")
    if isinstance(iat, datetime):
        iat = int(iat.timestamp())
    if jti and await is_token_blacklisted(jti, user_id=user_id, iat=iat):
        return None
    return payload


async def decode_refresh_token(token: str) -> Optional[Dict[str, Any]]:
    """Decode a refresh token, verify type, and check blacklist.

    Passes the real jti so a rotated (single-use) refresh token whose jti has
    been blacklisted is rejected — this is what makes refresh-token rotation
    effective. Also honours the user-level invalidation watermark.
    """
    payload = decode_token(token)
    if not payload or payload.get("type") != "refresh":
        return None
    user_id = payload.get("sub")
    jti = payload.get("jti")
    iat = payload.get("iat")
    if isinstance(iat, datetime):
        iat = int(iat.timestamp())
    if await is_token_blacklisted(jti or "", user_id=user_id, iat=iat):
        return None
    return payload
