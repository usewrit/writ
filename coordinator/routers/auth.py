"""
Authentication router — single-owner auth (register/login/JWT) + API key management.

The coordinator has exactly ONE owner account (the platform admin). Registration
is a first-boot bootstrap that creates that single admin User if none exists yet;
once an account exists, registration is closed. Login/refresh/me/change-password
all operate against the single user.

The web-UI JWT carries an ``org_id`` claim for wire compatibility with the
token/`AuthContext` code, but it simply mirrors the owner's user id (the
single-owner alias lives in security/dependencies.py). API keys belong to the
single owner and are scoped by resource, not by owner.
"""
import asyncio
import hashlib
import logging
import secrets as secrets_mod
import uuid
import re
from datetime import datetime, timezone, timedelta
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError

from config import settings
from database import get_db
from models.user import User
from models.api_key import APIKey, Role
from security.jwt import (
    create_access_token,
    create_refresh_token,
    decode_access_token,
    decode_refresh_token,
    decode_token,
    blacklist_decoded_token,
    blacklist_user_tokens,
    is_jti_blacklisted,
    is_user_invalidated,
    remember_rotation,
    get_rotation_successor,
)
from security.dependencies import get_auth_context, get_current_user, AuthContext
from security.api_key import generate_api_key, hash_api_key
from security.brute_force import (
    check_brute_force_multi,
    record_failure_multi,
    record_success_multi,
)
from security.ip_ban import record_offense

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["Authentication"])

# Cookie lifetimes. Access cookie TTL must match the 15-min access-JWT expiry.
ACCESS_COOKIE_MAX_AGE = 15 * 60
REFRESH_COOKIE_MAX_AGE = 7 * 24 * 3600

# Refresh-cookie path. Broad enough that the browser sends the cookie to BOTH
# /api/auth/refresh (to rotate) and /api/auth/logout (to revoke server-side) —
# scoped to "/api/auth/refresh" it never reached logout, so logout could not
# blacklist the refresh jti and a copied token stayed valid for its full 7 days.
# httpOnly + SameSite=strict, so the wider path adds no CSRF exposure.
REFRESH_COOKIE_PATH = "/api/auth"
# The pre-fix scope. Still deleted everywhere the session is written or cleared:
# a cookie left at the narrow path SHADOWS the real one (see
# _refresh_cookie_values) instead of merely lingering.
LEGACY_REFRESH_COOKIE_PATH = "/api/auth/refresh"


def _refresh_cookie_values(request: Request) -> List[str]:
    """Every ``refresh_token`` value on the request, most-specific path first.

    The browser sends ONE Cookie header that may carry the SAME name more than
    once — one per path scope — and ``request.cookies`` is a dict, so the LAST
    duplicate silently wins. Since browsers order by descending path length, the
    winner is the BROADEST-path cookie, which is not necessarily ours:

        Cookie: refresh_token=<ours @ /api/auth/refresh>; refresh_token=<foreign @ /api/auth>

    Anything else served from the same origin — notably the cloud backend, whose
    docker-compose also publishes on localhost:8000 and writes `refresh_token` at
    `/api/auth` under a DIFFERENT signing key — therefore shadows our cookie.
    Every refresh then 401s with "invalid or expired", the SPA reads that as
    "session expired" and bounces to /login on each hard reload, and logging in
    again cannot fix it: login only rewrites OUR path, never the shadowing one.

    So: consider ALL of them and let the caller accept whichever actually
    verifies, rather than trusting the dict's arbitrary winner.
    """
    raw = request.headers.get("cookie") or ""
    values: List[str] = []
    for part in raw.split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name == "refresh_token" and value and value not in values:
            values.append(value)
    if not values:
        single = request.cookies.get("refresh_token")
        if single:
            values.append(single)
    return values


# ============================================================================
# Request/Response Models
# ============================================================================

class RegisterRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=255)
    password: str = Field(..., min_length=8, max_length=128)
    name: Optional[str] = Field(None, max_length=255)


class LoginRequest(BaseModel):
    email: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: dict


class RefreshResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserResponse(BaseModel):
    id: str
    email: str
    name: Optional[str]
    is_verified: bool
    is_platform_admin: bool
    created_at: str


class APIKeyLimits(BaseModel):
    """Optional per-key AI + limit settings. All None/omitted = unlimited."""
    ai_enabled: bool = False
    credit_budget: Optional[int] = None
    budget_reset_period: str = "none"  # none/daily/weekly/monthly
    rate_limit_per_min: Optional[int] = None
    rate_limit_per_hour: Optional[int] = None
    expires_at: Optional[datetime] = None
    daily_cost_cap_usd: Optional[int] = None
    sessions_per_hour_limit: Optional[int] = None
    max_concurrent_browsers: Optional[int] = None
    execution_limit: Optional[int] = None


class CreateAPIKeyRequest(APIKeyLimits):
    label: str = Field(..., min_length=1, max_length=255)
    # Scope strings, e.g. ["workflows:read", "workflows:execute"]. `resource:*` is
    # accepted and expanded at grant time, so a key never widens later.
    scopes: List[str] = Field(default_factory=list)
    # Shorthand for a named preset ("read_only" | "run" | "full"). When set it
    # REPLACES `scopes` — never merges with it.
    preset: Optional[str] = None
    # Optional per-resource object pinning: {"workflows": [12, 15]}.
    resource_ids: dict[str, List[int]] = Field(default_factory=dict)


class CreateAPIKeyResponse(BaseModel):
    id: int
    label: str
    api_key: str
    scopes: List[str] = []
    resource_ids: dict = {}
    scope_summary: str = ""
    created_at: str


class APIKeyInfo(BaseModel):
    id: int
    label: str
    scopes: List[str] = []
    resource_ids: dict = {}
    scope_summary: str = ""
    preset: Optional[str] = None
    created: str
    lastUsed: Optional[str] = None
    status: str
    is_scoped: bool = False
    ai_enabled: bool = False
    credit_budget: Optional[int] = None
    credit_used: int = 0
    budget_reset_period: str = "none"
    budget_reset_at: Optional[str] = None
    rate_limit_per_min: Optional[int] = None
    rate_limit_per_hour: Optional[int] = None
    expires_at: Optional[str] = None
    daily_cost_cap_usd: Optional[int] = None
    sessions_per_hour_limit: Optional[int] = None
    max_concurrent_browsers: Optional[int] = None
    execution_limit: Optional[int] = None
    runs_used: int = 0


def _hash_reset_token(token: str) -> str:
    """SHA-256 of a reset/verification token.

    The plaintext token is mailed to the user; we persist only its hash so a
    read of the users table can't be replayed to take over the account.
    """
    return hashlib.sha256(token.encode()).hexdigest()


def validate_password_strength(password: str):
    """Validate password meets minimum complexity requirements."""
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    if not re.search(r'[A-Z]', password):
        raise HTTPException(status_code=400, detail="Password must contain at least one uppercase letter")
    if not re.search(r'[a-z]', password):
        raise HTTPException(status_code=400, detail="Password must contain at least one lowercase letter")
    if not re.search(r'[0-9]', password):
        raise HTTPException(status_code=400, detail="Password must contain at least one number")


def _require_session_auth(auth: AuthContext) -> None:
    """Gate credential-management to first-party JWT sessions (finding #1).

    ``get_auth_context`` flattens JWT sessions, delegated third-party OAuth tokens,
    and API keys into one ``AuthContext``. Handlers that only checked ``auth.user_id``
    let a scoped OAuth token or a narrow API key mint/alter/enumerate API keys with
    arbitrary scopes — a confused-deputy privilege escalation. API-key/OAuth CRUD
    must therefore require the owner's own browser session (``auth_method == 'jwt'``).
    """
    if not auth.user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Authentication required")
    if getattr(auth, "auth_method", None) != "jwt":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="API keys can only be managed from a first-party session",
        )


def _client_ip(req: Request) -> str:
    """Best-effort client IP for per-IP throttling (never raises)."""
    client = getattr(req, "client", None)
    return (getattr(client, "host", None) if client else None) or "unknown"


def _get_redis(req: Request):
    """Fetch the shared Redis handle off app.state (may be None → memory fallback)."""
    state = getattr(getattr(req, "app", None), "state", None)
    return getattr(state, "redis", None)


# Precomputed Argon2 hash for the user-not-found path so a real verify runs and
# login timing doesn't leak account existence (finding #10). Populated lazily on
# first use to avoid an import-time Argon2 hash.
_DUMMY_PASSWORD_HASH: Optional[str] = None


def _constant_time_dummy_verify(ph, password: str) -> None:
    """Run one Argon2 verify against a dummy hash and swallow the mismatch.

    Mirrors the work the success path does so an attacker can't distinguish
    "no such user" from "wrong password" by response timing.
    """
    global _DUMMY_PASSWORD_HASH
    try:
        if _DUMMY_PASSWORD_HASH is None:
            _DUMMY_PASSWORD_HASH = ph.hash("writ-login-timing-equalizer")
        ph.verify(_DUMMY_PASSWORD_HASH, password)
    except Exception:
        pass


# ============================================================================
# User Authentication Endpoints
# ============================================================================

def _set_session_cookies(response: Response, access_token: str, refresh_token: str) -> None:
    """Attach the refresh + access session cookies.

    Single source of truth — login, first-boot register/onboarding and /refresh
    all route through here, so the flags can never drift apart and every one of
    them clears the legacy narrow-path cookie that would otherwise shadow this
    one for its full 7-day life.
    """
    response.set_cookie(
        key="refresh_token",
        value=refresh_token,
        httponly=True,
        secure=settings.is_production,  # dev is HTTP; a hardcoded Secure cookie would be dropped
        samesite="strict",
        max_age=REFRESH_COOKIE_MAX_AGE,
        path=REFRESH_COOKIE_PATH,
    )
    # Drop any cookie left at the old narrow scope. Without this the stale value
    # keeps riding along on every /api/auth/refresh request and wins the
    # duplicate-name tie-break — see _refresh_cookie_values.
    response.delete_cookie("refresh_token", path=LEGACY_REFRESH_COOKIE_PATH)
    response.set_cookie(
        key="access_token",
        value=access_token,
        httponly=True,
        secure=settings.is_production,
        samesite="strict",
        max_age=ACCESS_COOKIE_MAX_AGE,
        path="/api/recorder",
    )


class SetupStatusResponse(BaseModel):
    needs_setup: bool
    # None once the owner exists — the prefill is only meaningful during setup,
    # and it is the owner's login identity. See setup_status().
    suggested_email: Optional[str] = None


@router.get("/setup-status", response_model=SetupStatusResponse)
async def setup_status(db: AsyncSession = Depends(get_db)):
    """Public first-run probe: is this coordinator waiting for its owner account?

    Returns ``needs_setup=True`` when NO user exists yet, so the SPA can route a
    fresh install to the onboarding page (create the admin password) instead of
    the login form. Unauthenticated by design — a user who cannot log in still
    needs to know whether this instance is waiting to be claimed.

    ``suggested_email`` is returned ONLY while setup is still pending. It is
    WRIT_ADMIN_EMAIL, i.e. the owner's login identity, and it exists purely to
    prefill the onboarding form. Once the owner account exists that prefill has
    no purpose, and continuing to serve it would hand every anonymous caller half
    of the credential pair forever.
    """
    try:
        count = (await db.execute(select(func.count(User.id)))).scalar() or 0
    except Exception:
        # Never break the first paint on a transient DB blip — treat as "has owner"
        # so the app falls back to the (safe) login form rather than re-onboarding.
        count = 1

    needs_setup = count == 0
    suggested = None
    if needs_setup:
        import os as _os
        suggested = (_os.getenv("WRIT_ADMIN_EMAIL") or "admin@local").strip().lower()
    return SetupStatusResponse(needs_setup=needs_setup, suggested_email=suggested)


# Serializes the first-boot registration path. Without it, two concurrent
# POST /auth/register requests can BOTH pass the count-check before either
# inserts, minting two platform admins. Single process (enforced at boot —
# see main._enforce_single_worker), so an asyncio.Lock fully serializes it;
# the count is re-checked inside the lock in the SAME transaction as the
# insert, so the check-then-insert is atomic.
_register_lock = asyncio.Lock()


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def register(request: RegisterRequest, req: Request, response: Response, db: AsyncSession = Depends(get_db)):
    """First-boot bootstrap: create the single admin owner if no account exists.

    Single-owner coordinator: there is exactly one user. If an account already
    exists, registration is closed (403). No organization/membership is created —
    the owner IS the platform admin. On success the session cookies are set so the
    onboarding page lands the user straight into the app (durable across reload).
    """
    validate_password_strength(request.password)

    from argon2 import PasswordHasher
    ph = PasswordHasher()

    async with _register_lock:
        # Registration is a one-time bootstrap: refuse once any user exists.
        # Checked INSIDE the lock, in the same transaction as the insert below,
        # so a concurrent register cannot slip between check and commit.
        existing_count = (await db.execute(select(func.count(User.id)))).scalar() or 0
        if existing_count > 0:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "message": "Registration is closed — this coordinator already has an owner.",
                    "code": "registration_closed",
                },
            )

        now = datetime.now(timezone.utc)
        user = User(
            email=request.email.lower().strip(),
            password_hash=ph.hash(request.password),
            name=request.name,
            is_active=True,
            # First-boot bootstrap: the sole owner is the platform admin and does not
            # need to verify email to use their own self-hosted coordinator.
            is_verified=True,
            is_platform_admin=True,
            terms_accepted_at=now,
            terms_version=settings.terms_version,
            privacy_version=settings.privacy_version,
            age_confirmed_at=now,
        )
        db.add(user)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            raise HTTPException(status_code=400, detail="Registration failed. Please try again.")
        await db.refresh(user)

    logger.info(f"Owner account created (first-boot bootstrap): {user.email}")

    # Single owner: the JWT org_id claim mirrors the user_id (see module docstring).
    access_token = create_access_token(
        user_id=str(user.id),
        org_id=str(user.id),
        role="owner",
        is_platform_admin=True,
    )
    # Set the session cookies so onboarding = a full login (survives reload).
    _set_session_cookies(response, access_token, create_refresh_token(user_id=str(user.id)))
    return TokenResponse(access_token=access_token, user=user.to_dict())


@router.post("/login", response_model=TokenResponse)
async def login(request: LoginRequest, response: Response, req: Request, db: AsyncSession = Depends(get_db)):
    """Login with email and password. Returns a JWT access token + refresh cookie."""
    from argon2 import PasswordHasher
    from argon2.exceptions import VerifyMismatchError

    ph = PasswordHasher()

    email = request.email.lower().strip()
    ip = _client_ip(req)
    redis = _get_redis(req)
    # Throttle on BOTH the IP (one host spraying many accounts) and the account
    # (many hosts hammering one account) — see check_brute_force_multi.
    bf_ids = [f"ip:{ip}", f"account:{email}"]

    # Brute-force gate BEFORE any password verification (finding #10).
    allowed, retry_after = await check_brute_force_multi(redis, bf_ids)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed login attempts. Please try again later.",
            headers={"Retry-After": str(retry_after)},
        )

    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    if not user or not user.password_hash:
        # Constant-time dummy verify so timing doesn't leak account existence.
        _constant_time_dummy_verify(ph, request.password)
        await record_failure_multi(redis, bf_ids)
        await record_offense(redis, ip)
        raise HTTPException(status_code=401, detail="Invalid email or password")

    try:
        ph.verify(user.password_hash, request.password)
    except VerifyMismatchError:
        await record_failure_multi(redis, bf_ids)
        await record_offense(redis, ip)
        raise HTTPException(status_code=401, detail="Invalid email or password")

    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account is deactivated")

    # Successful password auth — clear the failure counters for this IP/account.
    await record_success_multi(redis, bf_ids)

    user.last_login_at = datetime.now(timezone.utc)
    await db.commit()

    access_token = create_access_token(
        user_id=str(user.id),
        org_id=str(user.id),
        role="owner",
        is_platform_admin=user.is_platform_admin,
    )
    # Refresh cookie (used by /auth/refresh + /auth/logout) and the access cookie
    # the recorder WebSocket proxy reads, since a WS can't send headers.
    _set_session_cookies(response, access_token, create_refresh_token(user_id=str(user.id)))

    logger.info(f"User login: {user.email}")
    return TokenResponse(access_token=access_token, user=user.to_dict())


@router.post("/refresh", response_model=RefreshResponse)
async def refresh_token(request: Request, db: AsyncSession = Depends(get_db)):
    """Refresh the access token using the refresh token cookie (single-use rotation).

    Rotation is single-use, with a short grace window: a token that was rotated
    moments ago returns the SAME successor it already minted instead of 401ing.
    Without that, the ordinary browser races (a reload issued while the previous
    page's refresh is still in flight, two tabs booting together) make the loser
    present an already-rotated cookie — and the SPA reads that 401 as "session
    expired" and bounces the owner to /login. See security/jwt.py for the
    trade-off and REFRESH_ROTATION_GRACE_SECONDS to disable it.

    Every 401 below is logged: a spurious logout is otherwise invisible server-side.
    """
    candidates = _refresh_cookie_values(request)
    if not candidates:
        logger.warning("/auth/refresh rejected: no refresh_token cookie presented")
        raise HTTPException(status_code=401, detail="No refresh token")

    # Signature/expiry/type only — the revocation checks are split out below so a
    # ROTATED token (recoverable) is distinguishable from an INVALIDATED one.
    #
    # Try EVERY refresh_token the browser sent, not just the dict's winner: a
    # same-origin neighbour's cookie at a broader path outranks ours and would
    # otherwise wedge the session permanently (see _refresh_cookie_values).
    payload = None
    for candidate in candidates:
        decoded = decode_token(candidate)
        if decoded and decoded.get("type") == "refresh":
            payload = decoded
            break
    if not payload:
        logger.warning(
            "/auth/refresh rejected: %d refresh_token cookie(s) presented, none "
            "valid (malformed, expired, or signed by another issuer on this origin)",
            len(candidates),
        )
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")
    if len(candidates) > 1:
        logger.warning(
            "/auth/refresh: %d refresh_token cookies on this origin — using the one "
            "that verifies; the stale duplicate is being cleared", len(candidates),
        )

    user_id = payload.get("sub")
    jti = payload.get("jti")
    iat = payload.get("iat")
    if isinstance(iat, datetime):
        iat = int(iat.timestamp())

    # Password change / logout-all — never recoverable.
    if await is_user_invalidated(user_id, iat):
        logger.warning("/auth/refresh rejected: all tokens invalidated for this user")
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")

    # Already rotated? Redeemable only inside the grace window, and only for the
    # exact successor it minted — a later replay is a theft signal and still 401s.
    grace_successor: Optional[str] = None
    if await is_jti_blacklisted(jti or ""):
        grace_successor = await get_rotation_successor(jti or "")
        if not grace_successor:
            logger.warning(
                "/auth/refresh rejected: refresh token already rotated and past the "
                "%ss grace window (replay)", settings.refresh_rotation_grace_seconds,
            )
            raise HTTPException(status_code=401, detail="Invalid or expired refresh token")
        logger.info("/auth/refresh: serving in-grace replay of a just-rotated token")

    result = await db.execute(select(User).where(User.id == uuid.UUID(user_id)))
    user = result.scalar_one_or_none()
    if not user or not user.is_active:
        logger.warning("/auth/refresh rejected: user missing or deactivated")
        raise HTTPException(status_code=401, detail="User not found")

    access_token = create_access_token(
        user_id=str(user.id),
        org_id=str(user.id),
        role="owner",
        is_platform_admin=user.is_platform_admin,
    )
    if grace_successor:
        # Hand back the successor this token already minted — replaying the SAME
        # rotation rather than starting a competing one, so racing callers
        # converge on one refresh token instead of invalidating each other.
        new_refresh_token = grace_successor
    else:
        new_refresh_token = create_refresh_token(user_id=str(user.id))
        # Rotate: revoke the presented jti, but remember what it minted so an
        # in-flight duplicate carrying the same cookie can still be served.
        await blacklist_decoded_token(payload)
        await remember_rotation(jti or "", new_refresh_token)

    response = JSONResponse(content={"access_token": access_token})
    _set_session_cookies(response, access_token, new_refresh_token)
    return response


@router.post("/logout")
async def logout(request: Request, response: Response):
    """Logout this session — revoke the presented refresh + access tokens server-side.

    Reachable at all only because the refresh cookie is scoped to /api/auth: at
    the old /api/auth/refresh scope the browser never sent it here, so logout
    silently failed to revoke anything.
    """
    for refresh in _refresh_cookie_values(request):
        payload = await decode_refresh_token(refresh)
        if payload:
            await blacklist_decoded_token(payload)
            break

    access = request.cookies.get("access_token")
    auth_header = request.headers.get("Authorization", "")
    if auth_header.lower().startswith("bearer "):
        access = auth_header[7:].strip() or access
    if access:
        payload = decode_access_token(access)
        if payload:
            await blacklist_decoded_token(payload)

    response.delete_cookie("refresh_token", path=REFRESH_COOKIE_PATH)
    response.delete_cookie("refresh_token", path=LEGACY_REFRESH_COOKIE_PATH)
    response.delete_cookie("access_token", path="/api/recorder")
    return {"success": True}


@router.post("/logout-all")
async def logout_all(response: Response, auth: AuthContext = Depends(get_auth_context)):
    """Revoke every session for the owner (all devices)."""
    if not auth.user_id:
        raise HTTPException(status_code=401, detail="Authentication required")
    await blacklist_user_tokens(str(auth.user_id))
    response.delete_cookie("refresh_token", path=REFRESH_COOKIE_PATH)
    response.delete_cookie("refresh_token", path=LEGACY_REFRESH_COOKIE_PATH)
    response.delete_cookie("access_token", path="/api/recorder")
    return {"success": True}


@router.post("/forgot-password")
async def forgot_password(request: Request, db: AsyncSession = Depends(get_db)):
    """Request a password reset. Sends an email with a reset link (if configured)."""
    ip = _client_ip(request)
    redis = _get_redis(request)
    ip_ids = [f"ip:{ip}"]

    # Per-IP throttle so this endpoint can't be used to spray reset emails or as
    # an oracle (finding #10). Checked before any DB/email work.
    allowed, retry_after = await check_brute_force_multi(redis, ip_ids)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many password-reset requests. Please try again later.",
            headers={"Retry-After": str(retry_after)},
        )
    # Every request counts toward the per-IP window; repeated abuse trips the
    # lockout and (via record_offense) the ip_ban auto-ban.
    await record_failure_multi(redis, ip_ids)
    await record_offense(redis, ip)

    body = await request.json()
    email = body.get("email", "").lower().strip()
    if not email:
        raise HTTPException(status_code=400, detail="Email is required")

    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    # Always return success (don't reveal whether the email exists).
    if user:
        token = secrets_mod.token_urlsafe(32)
        user.reset_token = _hash_reset_token(token)
        user.reset_token_expires = datetime.now(timezone.utc) + timedelta(hours=1)
        await db.commit()

        from services.email_service import send_password_reset_email
        base_url = settings.frontend_url.rstrip("/")
        import asyncio
        loop = asyncio.get_event_loop()
        loop.run_in_executor(None, send_password_reset_email, email, token, base_url)
        logger.info(f"Password reset requested for {email}")

    return {"success": True, "message": "If that email exists, a reset link has been sent."}


@router.post("/reset-password")
async def reset_password(request: dict, req: Request, db: AsyncSession = Depends(get_db)):
    """Reset password using a valid token."""
    from argon2 import PasswordHasher
    ph_local = PasswordHasher()

    token = request.get("token")
    new_password = request.get("password")

    if not token or not new_password:
        raise HTTPException(status_code=400, detail="Token and password are required")
    validate_password_strength(new_password)

    result = await db.execute(select(User).where(User.reset_token == _hash_reset_token(token)))
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(status_code=400, detail="Invalid or expired reset token")

    if user.reset_token_expires and user.reset_token_expires < datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="Reset token has expired")

    user.password_hash = ph_local.hash(new_password)
    user.reset_token = None
    user.reset_token_expires = None
    await db.commit()

    await blacklist_user_tokens(str(user.id))
    logger.info(f"Password reset completed for {user.email}")
    return {"success": True, "message": "Password has been reset. You can now log in."}


@router.post("/verify-email")
async def verify_email(request: Request, db: AsyncSession = Depends(get_db)):
    """Verify email address using the token sent during a change/resend."""
    body = await request.json()
    token = body.get("token")
    if not token:
        raise HTTPException(status_code=400, detail="Verification token is required")

    result = await db.execute(select(User).where(User.verification_token == _hash_reset_token(token)))
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(status_code=400, detail="Invalid or expired verification token")

    if user.verification_token_expires and user.verification_token_expires < datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="Verification token has expired. Please request a new one.")

    if user.is_verified:
        return {"success": True, "message": "Email already verified"}

    user.is_verified = True
    user.verification_token = None
    user.verification_token_expires = None
    await db.commit()

    logger.info(f"Email verified for {user.email}")
    return {"success": True, "message": "Email verified successfully"}


@router.post("/resend-verification")
async def resend_verification(
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Resend a verification email to the owner."""
    if not auth.user_id:
        raise HTTPException(status_code=401, detail="Authentication required")

    result = await db.execute(select(User).where(User.id == auth.user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if user.is_verified:
        return {"success": True, "message": "Email already verified"}

    token = secrets_mod.token_urlsafe(32)
    user.verification_token = _hash_reset_token(token)
    user.verification_token_expires = datetime.now(timezone.utc) + timedelta(hours=24)
    await db.commit()

    from services.email_service import send_verification_email
    base_url = settings.frontend_url.rstrip("/")
    import asyncio
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, send_verification_email, user.email, token, base_url)

    return {"success": True, "message": "Verification email sent"}


@router.post("/change-password")
async def change_password(
    request: dict,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Change password (requires current password)."""
    from argon2 import PasswordHasher
    from argon2.exceptions import VerifyMismatchError
    ph_local = PasswordHasher()

    if not auth.user_id:
        raise HTTPException(status_code=401, detail="Authentication required")

    current_password = request.get("current_password")
    new_password = request.get("new_password")

    if not new_password:
        raise HTTPException(status_code=400, detail="New password is required")
    validate_password_strength(new_password)

    result = await db.execute(select(User).where(User.id == auth.user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if user.password_hash:
        if not current_password:
            raise HTTPException(status_code=400, detail="Current password is required")
        try:
            ph_local.verify(user.password_hash, current_password)
        except VerifyMismatchError:
            raise HTTPException(status_code=400, detail="Current password is incorrect")

    user.password_hash = ph_local.hash(new_password)
    await db.commit()

    await blacklist_user_tokens(str(auth.user_id))
    return {"success": True, "message": "Password changed successfully"}


@router.patch("/me")
async def update_profile(
    request: dict,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Update the owner's profile (name)."""
    if not auth.user_id:
        raise HTTPException(status_code=401, detail="Authentication required")

    result = await db.execute(select(User).where(User.id == auth.user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if "name" in request:
        user.name = request["name"]

    await db.commit()
    return user.to_dict()


@router.get("/me", response_model=UserResponse)
async def get_me(
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Get the current owner's info."""
    if not auth.user_id:
        # API-key auth without a user — return minimal info.
        return UserResponse(
            id="",
            email="",
            name="API Key",
            is_verified=True,
            is_platform_admin=auth.is_platform_admin,
            created_at="",
        )

    result = await db.execute(select(User).where(User.id == auth.user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    return UserResponse(
        id=str(user.id),
        email=user.email,
        name=user.name,
        is_verified=user.is_verified,
        is_platform_admin=user.is_platform_admin,
        created_at=user.created_at.isoformat() if user.created_at else "",
    )


# ============================================================================
# Self-serve email change — mirrors the reset-token flow
# ============================================================================

@router.post("/request-email-change")
async def request_email_change(
    request: dict,
    req: Request,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Request changing the account email. Sends a verification link to the NEW
    address; the change is applied only after confirmation."""
    if not auth.user_id:
        raise HTTPException(status_code=401, detail="Authentication required")

    new_email = (request.get("new_email") or "").lower().strip()
    if not new_email or "@" not in new_email:
        raise HTTPException(status_code=400, detail="A valid new email is required")

    result = await db.execute(select(User).where(User.id == auth.user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if new_email == user.email:
        raise HTTPException(status_code=400, detail="That is already your email address")

    existing = await db.execute(select(User).where(User.email == new_email))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="That email is already in use")

    token = secrets_mod.token_urlsafe(32)
    user.pending_email = new_email
    user.email_change_token = _hash_reset_token(token)
    user.email_change_token_expires = datetime.now(timezone.utc) + timedelta(hours=1)
    await db.commit()

    base_url = settings.frontend_url.rstrip("/")
    confirm_url = f"{base_url}/confirm-email-change?token={token}"
    import asyncio
    loop = asyncio.get_event_loop()

    from services.email_service import send_email
    loop.run_in_executor(
        None,
        lambda: send_email(
            new_email,
            "Confirm your new email address",
            f'<p>Confirm this address for your Writ account by clicking '
            f'<a href="{confirm_url}">this link</a>. It expires in 1 hour. '
            f"If you didn't request this, ignore this email.</p>",
            text_body=f"Confirm your new Writ email address: {confirm_url}\n\nThis link expires in 1 hour. If you didn't request this, ignore this email.",
            category="transactional",
        ),
    )
    loop.run_in_executor(
        None,
        lambda: send_email(
            user.email,
            "Email change requested on your account",
            f"<p>A request was made to change your Writ account email to "
            f"<strong>{new_email}</strong>. If this wasn't you, reset your "
            f"password immediately.</p>",
            text_body=f"A request was made to change your Writ account email to {new_email}. If this wasn't you, reset your password immediately.",
            category="transactional",
        ),
    )

    logger.info(f"Email change requested for {user.email} -> {new_email}")
    return {"success": True, "message": "A confirmation link has been sent to the new address."}


@router.post("/confirm-email-change")
async def confirm_email_change(
    request: dict,
    req: Request,
    db: AsyncSession = Depends(get_db),
):
    """Confirm an email change using the token mailed to the new address.

    Applies ``pending_email`` -> ``email``, clears the token, and revokes all
    sessions (the login identity changed). Token-based + unauthenticated (the link
    is opened in the new mailbox), mirroring reset-password.
    """
    token = request.get("token")
    if not token:
        raise HTTPException(status_code=400, detail="Token is required")

    result = await db.execute(
        select(User).where(User.email_change_token == _hash_reset_token(token))
    )
    user = result.scalar_one_or_none()
    if not user or not user.pending_email:
        raise HTTPException(status_code=400, detail="Invalid or expired email-change token")

    if user.email_change_token_expires and user.email_change_token_expires < datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="Email-change token has expired")

    new_email = user.pending_email
    collision = await db.execute(
        select(User).where(User.email == new_email, User.id != user.id)
    )
    if collision.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="That email is already in use")

    old_email = user.email
    user.email = new_email
    user.pending_email = None
    user.email_change_token = None
    user.email_change_token_expires = None
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=400, detail="That email is already in use")

    await blacklist_user_tokens(str(user.id))
    logger.info(f"Email change confirmed: {old_email} -> {new_email}")
    return {"success": True, "message": "Your email has been updated. Please log in again."}


# ============================================================================
# API Key Management (owner-scoped, resource-scoped)
# ============================================================================

@router.get("/api-keys", response_model=List[APIKeyInfo])
async def list_api_keys(
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    _require_session_auth(auth)
    query = select(APIKey).order_by(APIKey.created_at.desc())
    result = await db.execute(query)
    keys = result.scalars().all()
    from security import api_scopes
    return [
        APIKeyInfo(
            id=key.id,
            label=key.label,
            scopes=key.granted_scopes(),
            resource_ids=(key.scopes or {}).get("ids") or {},
            scope_summary=key.scope_summary(),
            preset=api_scopes.match_preset(key.granted_scopes()),
            created=key.created_at.isoformat() if key.created_at else "",
            lastUsed=key.last_used_at.isoformat() if key.last_used_at else None,
            status="revoked" if key.revoked_at else "active",
            is_scoped=key.is_scoped,
            ai_enabled=key.ai_enabled,
            credit_budget=key.credit_budget,
            credit_used=key.credit_used or 0,
            budget_reset_period=key.budget_reset_period or "none",
            budget_reset_at=key.budget_reset_at.isoformat() if key.budget_reset_at else None,
            rate_limit_per_min=key.rate_limit_per_min,
            rate_limit_per_hour=key.rate_limit_per_hour,
            expires_at=key.expires_at.isoformat() if key.expires_at else None,
            daily_cost_cap_usd=key.daily_cost_cap_usd,
            sessions_per_hour_limit=key.sessions_per_hour_limit,
            max_concurrent_browsers=key.max_concurrent_browsers,
            execution_limit=key.execution_limit,
            runs_used=key.runs_used or 0,
        )
        for key in keys
    ]


@router.get("/api-keys/catalog")
async def api_key_scope_catalog(
    auth: AuthContext = Depends(get_auth_context),
):
    """The scope vocabulary — resources, actions and presets — for the key editor.

    Served rather than hardcoded so the key screen cannot drift from what the
    coordinator actually enforces.
    """
    from security import api_scopes
    return api_scopes.catalog()


@router.post("/api-keys", response_model=CreateAPIKeyResponse, status_code=status.HTTP_201_CREATED)
async def create_api_key(
    request: CreateAPIKeyRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    _require_session_auth(auth)

    from models.api_key import BUDGET_RESET_PERIODS
    from security import api_scopes

    # A preset REPLACES an explicit scope list rather than merging with it, so
    # "Read-only" always means exactly read-only.
    requested = request.scopes
    if request.preset:
        preset = api_scopes.preset_scopes(request.preset)
        if preset is None:
            raise HTTPException(status_code=400, detail=f"Unknown preset: {request.preset}")
        requested = preset

    invalid = [s for s in requested if not api_scopes.is_valid_scope(s)]
    if invalid:
        raise HTTPException(status_code=400, detail=f"Invalid scopes: {', '.join(sorted(invalid))}")

    scopes_dict = api_scopes.build_scopes_blob(requested, request.resource_ids)
    if not scopes_dict["scopes"]:
        raise HTTPException(
            status_code=400,
            detail="A key must grant at least one scope — a key with none can do nothing",
        )

    if request.budget_reset_period not in BUDGET_RESET_PERIODS:
        raise HTTPException(status_code=400, detail=f"Invalid budget_reset_period: {request.budget_reset_period}")

    plaintext_key = generate_api_key()
    key_hash = hash_api_key(plaintext_key)
    from security.api_key import compute_key_prefix
    key_prefix = compute_key_prefix(plaintext_key)

    api_key = APIKey(
        label=request.label,
        key_hash=key_hash,
        key_prefix=key_prefix,
        role=Role.CLIENT,
        user_id=auth.user_id,
        scopes=scopes_dict,
        created_at=datetime.now(timezone.utc),
        ai_enabled=request.ai_enabled,
        credit_budget=request.credit_budget,
        budget_reset_period=request.budget_reset_period,
        rate_limit_per_min=request.rate_limit_per_min,
        rate_limit_per_hour=request.rate_limit_per_hour,
        expires_at=request.expires_at,
        daily_cost_cap_usd=request.daily_cost_cap_usd,
        sessions_per_hour_limit=request.sessions_per_hour_limit,
        max_concurrent_browsers=request.max_concurrent_browsers,
        execution_limit=request.execution_limit,
    )
    api_key.maybe_reset_budget()
    db.add(api_key)
    await db.commit()
    await db.refresh(api_key)

    logger.info(f"API key created: {request.label}")

    return CreateAPIKeyResponse(
        id=api_key.id,
        label=api_key.label,
        api_key=plaintext_key,
        scopes=api_key.granted_scopes(),
        resource_ids=(api_key.scopes or {}).get("ids") or {},
        scope_summary=api_key.scope_summary(),
        created_at=api_key.created_at.isoformat(),
    )


@router.patch("/api-keys/{key_id}")
async def update_api_key(
    key_id: int,
    request: dict,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    _require_session_auth(auth)
    result = await db.execute(select(APIKey).where(APIKey.id == key_id))
    api_key = result.scalar_one_or_none()
    if not api_key:
        raise HTTPException(status_code=404, detail="API key not found")

    if "label" in request:
        api_key.label = request["label"]
    if "scopes" in request or "preset" in request:
        # Validate + normalize exactly as create_api_key does. This handler used
        # to assign `request["scopes"]` STRAIGHT to the column with no validation
        # at all, so any JSON body became a grant.
        from security import api_scopes
        incoming = request.get("scopes") or []
        if request.get("preset"):
            preset = api_scopes.preset_scopes(request["preset"])
            if preset is None:
                raise HTTPException(status_code=400, detail=f"Unknown preset: {request['preset']}")
            incoming = preset
        if not isinstance(incoming, list):
            raise HTTPException(status_code=400, detail="scopes must be a list of scope strings")
        invalid = [s for s in incoming if not api_scopes.is_valid_scope(s)]
        if invalid:
            raise HTTPException(status_code=400, detail=f"Invalid scopes: {', '.join(sorted(invalid))}")
        resource_ids = request.get("resource_ids")
        if resource_ids is None:
            resource_ids = (api_key.scopes or {}).get("ids") or {}
        if not isinstance(resource_ids, dict):
            raise HTTPException(status_code=400, detail="resource_ids must be an object")
        rebuilt = api_scopes.build_scopes_blob(incoming, resource_ids)
        if not rebuilt["scopes"]:
            raise HTTPException(
                status_code=400,
                detail="A key must grant at least one scope — revoke it instead of emptying it",
            )
        api_key.scopes = rebuilt
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(api_key, "scopes")

    from models.api_key import BUDGET_RESET_PERIODS
    if "budget_reset_period" in request:
        period = request["budget_reset_period"]
        if period not in BUDGET_RESET_PERIODS:
            raise HTTPException(status_code=400, detail=f"Invalid budget_reset_period: {period}")
        changed = period != (api_key.budget_reset_period or "none")
        api_key.budget_reset_period = period
        if changed:
            api_key.budget_reset_at = None
            api_key.maybe_reset_budget()

    for field in (
        "ai_enabled", "credit_budget", "rate_limit_per_min", "rate_limit_per_hour",
        "expires_at", "daily_cost_cap_usd", "sessions_per_hour_limit",
        "max_concurrent_browsers", "execution_limit",
    ):
        if field in request:
            value = request[field]
            if field == "expires_at" and value:
                from datetime import datetime as _dt
                value = _dt.fromisoformat(value) if isinstance(value, str) else value
            setattr(api_key, field, value)

    await db.commit()
    return {"success": True}


@router.delete("/api-keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_api_key(
    key_id: int,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Delete an API key permanently."""
    _require_session_auth(auth)
    result = await db.execute(select(APIKey).where(APIKey.id == key_id))
    api_key = result.scalar_one_or_none()
    if not api_key:
        raise HTTPException(status_code=404, detail="API key not found")

    label = api_key.label
    await db.delete(api_key)
    await db.commit()
    logger.info(f"API key deleted: {label}")


@router.get("/api-keys/{key_id}/usage")
async def get_api_key_usage(
    key_id: int,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Per-key usage metering: run counters + a per-workflow run breakdown."""
    _require_session_auth(auth)

    result = await db.execute(select(APIKey).where(APIKey.id == key_id))
    key = result.scalar_one_or_none()
    if not key:
        raise HTTPException(status_code=404, detail="API key not found")

    from models.automation_task import AutomationTask
    from models.automation_workflow import AutomationWorkflow

    runs_result = await db.execute(
        select(
            AutomationTask.workflow_id,
            func.count(AutomationTask.id).label("runs"),
            func.max(AutomationTask.created_at).label("last_used"),
        )
        .where(AutomationTask.api_key_id == key_id)
        .group_by(AutomationTask.workflow_id)
    )
    run_rows = runs_result.all()

    wf_ids = [r.workflow_id for r in run_rows if r.workflow_id]
    names: dict = {}
    if wf_ids:
        nm = await db.execute(
            select(AutomationWorkflow.id, AutomationWorkflow.name).where(AutomationWorkflow.id.in_(wf_ids))
        )
        names = {row.id: row.name for row in nm.all()}

    by_scope = [
        {
            "scope_type": "workflows",
            "scope_id": r.workflow_id,
            "name": names.get(r.workflow_id, f"Workflow #{r.workflow_id}" if r.workflow_id else "Ad-hoc / AI task"),
            "runs": r.runs,
            "last_used": r.last_used.isoformat() if r.last_used else None,
        }
        for r in run_rows
    ]
    total_runs = sum(r.runs for r in run_rows)

    return {
        "key_id": key.id,
        "label": key.label,
        "runs": {
            "used": key.runs_used or 0,
            "total": total_runs,
            "execution_limit": key.execution_limit,
        },
        "rate_limits": {
            "per_min": key.rate_limit_per_min,
            "per_hour": key.rate_limit_per_hour,
        },
        "expires_at": key.expires_at.isoformat() if key.expires_at else None,
        "by_scope": by_scope,
    }


# ============================================================================
# Verify endpoint (for frontend auth check)
# ============================================================================

@router.get("/verify")
async def verify(auth: AuthContext = Depends(get_auth_context)):
    """Verify current authentication is valid."""
    return {
        "success": True,
        "data": {
            "role": "admin" if auth.is_platform_admin else auth.role,
        }
    }
