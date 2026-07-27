"""
OAuth 2.0 Authorization Server endpoints.

Implements RFC 6749 (Authorization Code Grant), RFC 7009 (Token Revocation),
RFC 7636 (PKCE), and RFC 8414 (Server Metadata) for universal third-party
integration support (Zapier, n8n, Make, Power Automate, etc.).
"""
import hashlib
import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlencode, urlparse

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from database import get_db
from models.oauth import OAuthApplication, OAuthAuthorizationCode, OAuthAccessToken
from security.api_key import generate_api_key, hash_api_key, verify_api_key
from security.dependencies import AuthContext, get_auth_context
from security.oauth_scopes import (
    OAUTH_SCOPES,
    validate_scopes,
    parse_scope_string,
    scopes_to_scope_string,
    get_scope_descriptions,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/oauth", tags=["OAuth"])


# --- Helper Functions ---

def _generate_token_prefix(token: str) -> str:
    """Generate a non-secret prefix for O(1) token lookup."""
    return hashlib.sha256(token.encode()).hexdigest()[:12]


def _verify_pkce(code_verifier: str, code_challenge: str, method: str) -> bool:
    """Verify PKCE code_verifier against stored code_challenge."""
    if method == "S256":
        computed = hashlib.sha256(code_verifier.encode("ascii")).digest()
        import base64
        computed_challenge = base64.urlsafe_b64encode(computed).rstrip(b"=").decode("ascii")
        return computed_challenge == code_challenge
    elif method == "plain":
        return code_verifier == code_challenge
    return False


async def _authenticate_client(
    client_id: str,
    client_secret: str,
    db: AsyncSession,
) -> OAuthApplication:
    """Authenticate an OAuth client by client_id and client_secret."""
    result = await db.execute(
        select(OAuthApplication).where(
            OAuthApplication.client_id == client_id,
            OAuthApplication.is_active == True,
        )
    )
    app = result.scalar_one_or_none()
    if not app:
        raise HTTPException(status_code=401, detail="invalid_client")
    if not verify_api_key(client_secret, app.client_secret_hash):
        raise HTTPException(status_code=401, detail="invalid_client")
    return app


def _require_session_auth(auth: "AuthContext") -> None:
    """Gate OAuth-app CRUD behind first-party session auth.

    OAuth-app management (register/update/delete) mints and rewrites the trust
    anchors of the OAuth server itself — most dangerously `redirect_uris`, which
    the authorize/token flow trusts for exact-match code delivery. `get_auth_context`
    flattens JWT sessions, third-party OAuth tokens, and API keys into one identity,
    so without this gate a delegated OAuth token (e.g. Zapier `targets:read`) or a
    scoped API key could rewrite an app's redirect_uris and hijack authorization
    codes. Only a first-party browser session (`auth_method == "jwt"`) may manage apps.
    """
    if getattr(auth, "auth_method", None) != "jwt":
        raise HTTPException(
            status_code=403,
            detail="OAuth application management requires a first-party session.",
        )


def _require_app_ownership(app: OAuthApplication, auth: "AuthContext") -> None:
    """Verify the session caller owns the app before mutating/deleting it."""
    if app.user_id is None or app.user_id != auth.user_id:
        raise HTTPException(status_code=403, detail="You do not own this application.")


def _validate_redirect_uris(redirect_uris: list[str]) -> None:
    """Validate + HTTPS-enforce redirect URIs (shared by create_app / update_app)."""
    for uri in redirect_uris:
        if len(uri) > 2000:
            raise HTTPException(400, f"Redirect URI too long (max 2000 chars): {uri[:100]}...")
        parsed = urlparse(uri)
        if not parsed.scheme or not parsed.netloc:
            raise HTTPException(400, f"Invalid redirect URI: {uri}")
        if settings.environment == "production" and parsed.scheme != "https":
            raise HTTPException(400, f"Redirect URIs must use HTTPS in production: {uri}")


# --- Server Metadata (RFC 8414) ---

@router.get("/.well-known/oauth-authorization-server")
async def oauth_metadata(request: Request):
    """
    OAuth 2.0 Authorization Server Metadata.

    Auto-discovered by Zapier, n8n, Make, and other integration platforms
    to configure themselves without manual endpoint entry.
    """
    base_url = str(request.base_url).rstrip("/")
    return {
        "issuer": base_url,
        "authorization_endpoint": f"{base_url}/api/oauth/authorize",
        "token_endpoint": f"{base_url}/api/oauth/token",
        "revocation_endpoint": f"{base_url}/api/oauth/revoke",
        "scopes_supported": list(OAUTH_SCOPES.keys()),
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "token_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic"],
        "code_challenge_methods_supported": ["S256"],
        "revocation_endpoint_auth_methods_supported": ["client_secret_post"],
    }


# --- Authorization Endpoint ---

@router.get("/authorize")
async def authorize_redirect(
    response_type: str = Query(...),
    client_id: str = Query(...),
    redirect_uri: str = Query(...),
    scope: str = Query(""),
    state: str = Query(""),
    code_challenge: Optional[str] = Query(None),
    code_challenge_method: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """
    OAuth authorization endpoint (GET).

    Validates the request then redirects to the frontend consent page
    where the user can approve or deny the third-party application.
    """
    if response_type != "code":
        raise HTTPException(400, "unsupported_response_type")

    # Validate client
    result = await db.execute(
        select(OAuthApplication).where(
            OAuthApplication.client_id == client_id,
            OAuthApplication.is_active == True,
        )
    )
    app = result.scalar_one_or_none()
    if not app:
        raise HTTPException(400, "invalid_client")

    # Validate redirect_uri (exact match required per RFC 6749 Section 3.1.2.3)
    if redirect_uri not in (app.redirect_uris or []):
        raise HTTPException(400, "invalid_redirect_uri")

    # Validate scopes
    requested_scopes = parse_scope_string(scope)
    valid_scopes = validate_scopes(requested_scopes)
    # Intersect with app's allowed scopes
    allowed = [s for s in valid_scopes if s in (app.scopes or [])]
    if not allowed and requested_scopes:
        raise HTTPException(400, "invalid_scope")

    # Validate PKCE — require S256 ("plain" offers no protection against
    # authorization-code/challenge interception).
    if code_challenge_method and code_challenge_method != "S256":
        raise HTTPException(400, "invalid_code_challenge_method")

    # Redirect to frontend consent page
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scopes_to_scope_string(allowed or valid_scopes),
        "state": state,
    }
    if code_challenge:
        params["code_challenge"] = code_challenge
        params["code_challenge_method"] = "S256"

    consent_url = f"{settings.frontend_url}/oauth/consent?{urlencode(params)}"
    return RedirectResponse(url=consent_url, status_code=302)


# --- Consent Submission (called by frontend) ---

class ConsentRequest(BaseModel):
    client_id: str
    redirect_uri: str
    scope: str
    state: str = ""
    code_challenge: Optional[str] = None
    code_challenge_method: Optional[str] = None
    approved: bool


@router.post("/authorize")
async def authorize_consent(
    body: ConsentRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """
    Process user consent decision (called by frontend consent page).

    If approved: generates authorization code and returns redirect URL.
    If denied: returns redirect URL with error.
    """
    # Validate client and redirect_uri BEFORE checking approved/denied
    result = await db.execute(
        select(OAuthApplication).where(
            OAuthApplication.client_id == body.client_id,
            OAuthApplication.is_active == True,
        )
    )
    app = result.scalar_one_or_none()
    if not app:
        raise HTTPException(400, "invalid_client")

    if body.redirect_uri not in (app.redirect_uris or []):
        raise HTTPException(400, "invalid_redirect_uri")

    if not body.approved:
        params = urlencode({"error": "access_denied", "state": body.state})
        return {"redirect_url": f"{body.redirect_uri}?{params}"}

    # Validate requested scopes and clamp to the app's allowed scope ceiling
    # (mirrors the GET /authorize path). Without this, the consent endpoint
    # would persist arbitrary scopes — and scopes_to_role escalates to "admin"
    # when all write scopes are present — letting an app exceed its provisioned
    # scope/role.
    requested_scopes = parse_scope_string(body.scope)
    valid_scopes = validate_scopes(requested_scopes)
    scopes = [s for s in valid_scopes if s in (app.scopes or [])]
    if not scopes and requested_scopes:
        raise HTTPException(400, "invalid_scope")

    # Generate authorization code
    code = secrets.token_urlsafe(64)

    auth_code = OAuthAuthorizationCode(
        code=code,
        client_id=body.client_id,
        user_id=auth.user_id,
        redirect_uri=body.redirect_uri,
        scopes=scopes,
        code_challenge=body.code_challenge,
        code_challenge_method=body.code_challenge_method,
        expires_at=datetime.now(timezone.utc) + timedelta(
            seconds=settings.oauth_authorization_code_expire_seconds
        ),
    )
    db.add(auth_code)
    await db.flush()

    params = urlencode({"code": code, "state": body.state})
    return {"redirect_url": f"{body.redirect_uri}?{params}"}


# --- Token Endpoint (RFC 6749 Section 4.1.3) ---

@router.post("/token")
async def token_exchange(
    grant_type: str = Form(...),
    code: Optional[str] = Form(None),
    redirect_uri: Optional[str] = Form(None),
    client_id: str = Form(...),
    client_secret: str = Form(...),
    refresh_token: Optional[str] = Form(None),
    code_verifier: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
):
    """
    OAuth token endpoint.

    Supports:
    - authorization_code: exchange code for access + refresh tokens
    - refresh_token: get new access token using refresh token
    """
    # Authenticate client
    app = await _authenticate_client(client_id, client_secret, db)

    if grant_type == "authorization_code":
        return await _handle_authorization_code(
            code=code,
            redirect_uri=redirect_uri,
            client_id=client_id,
            code_verifier=code_verifier,
            app=app,
            db=db,
        )
    elif grant_type == "refresh_token":
        return await _handle_refresh_token(
            refresh_token_value=refresh_token,
            client_id=client_id,
            app=app,
            db=db,
        )
    else:
        raise HTTPException(400, "unsupported_grant_type")


async def _handle_authorization_code(
    code: Optional[str],
    redirect_uri: Optional[str],
    client_id: str,
    code_verifier: Optional[str],
    app: OAuthApplication,
    db: AsyncSession,
):
    """Exchange authorization code for tokens."""
    if not code:
        raise HTTPException(400, "invalid_request: code required")

    # Look up code
    result = await db.execute(
        select(OAuthAuthorizationCode).where(
            OAuthAuthorizationCode.code == code,
            OAuthAuthorizationCode.client_id == client_id,
        )
    )
    auth_code = result.scalar_one_or_none()

    if not auth_code:
        raise HTTPException(400, "invalid_grant")
    if auth_code.used:
        raise HTTPException(400, "invalid_grant: code already used")
    if auth_code.expires_at < datetime.now(timezone.utc):
        raise HTTPException(400, "invalid_grant: code expired")
    if redirect_uri and auth_code.redirect_uri != redirect_uri:
        raise HTTPException(400, "invalid_grant: redirect_uri mismatch")

    # Verify PKCE if code_challenge was stored
    if auth_code.code_challenge:
        if not code_verifier:
            raise HTTPException(400, "invalid_request: code_verifier required")
        if not _verify_pkce(code_verifier, auth_code.code_challenge, auth_code.code_challenge_method or "S256"):
            raise HTTPException(400, "invalid_grant: PKCE verification failed")

    # Mark code as used
    auth_code.used = True

    # Generate tokens
    access_token = generate_api_key(prefix="wto")
    refresh_token_value = generate_api_key(prefix="wtr")

    token_record = OAuthAccessToken(
        token_prefix=_generate_token_prefix(access_token),
        token_hash=hash_api_key(access_token),
        refresh_token_prefix=_generate_token_prefix(refresh_token_value),
        refresh_token_hash=hash_api_key(refresh_token_value),
        client_id=client_id,
        user_id=auth_code.user_id,
        scopes=auth_code.scopes,
        expires_at=datetime.now(timezone.utc) + timedelta(
            seconds=settings.oauth_access_token_expire_seconds
        ),
        refresh_expires_at=datetime.now(timezone.utc) + timedelta(
            days=settings.oauth_refresh_token_expire_days
        ),
    )
    db.add(token_record)
    await db.flush()

    return JSONResponse(content={
        "access_token": access_token,
        "token_type": "bearer",
        "expires_in": settings.oauth_access_token_expire_seconds,
        "refresh_token": refresh_token_value,
        "scope": scopes_to_scope_string(auth_code.scopes or []),
    })


async def _handle_refresh_token(
    refresh_token_value: Optional[str],
    client_id: str,
    app: OAuthApplication,
    db: AsyncSession,
):
    """Exchange refresh token for new access token."""
    if not refresh_token_value:
        raise HTTPException(400, "invalid_request: refresh_token required")

    # Look up by prefix for O(1) lookup
    prefix = _generate_token_prefix(refresh_token_value)
    result = await db.execute(
        select(OAuthAccessToken).where(
            OAuthAccessToken.refresh_token_prefix == prefix,
            OAuthAccessToken.client_id == client_id,
            OAuthAccessToken.revoked_at.is_(None),
        )
    )
    candidates = result.scalars().all()

    token_record = None
    for candidate in candidates:
        if candidate.refresh_token_hash and verify_api_key(refresh_token_value, candidate.refresh_token_hash):
            token_record = candidate
            break

    if not token_record:
        raise HTTPException(400, "invalid_grant: refresh token not found or revoked")

    if token_record.refresh_expires_at and token_record.refresh_expires_at < datetime.now(timezone.utc):
        raise HTTPException(400, "invalid_grant: refresh token expired")

    # Revoke old token
    token_record.revoked_at = datetime.now(timezone.utc)

    # Issue new tokens (rotation)
    new_access_token = generate_api_key(prefix="wto")
    new_refresh_token = generate_api_key(prefix="wtr")

    new_record = OAuthAccessToken(
        token_prefix=_generate_token_prefix(new_access_token),
        token_hash=hash_api_key(new_access_token),
        refresh_token_prefix=_generate_token_prefix(new_refresh_token),
        refresh_token_hash=hash_api_key(new_refresh_token),
        client_id=client_id,
        user_id=token_record.user_id,
        scopes=token_record.scopes,
        expires_at=datetime.now(timezone.utc) + timedelta(
            seconds=settings.oauth_access_token_expire_seconds
        ),
        refresh_expires_at=datetime.now(timezone.utc) + timedelta(
            days=settings.oauth_refresh_token_expire_days
        ),
    )
    db.add(new_record)
    await db.flush()

    return JSONResponse(content={
        "access_token": new_access_token,
        "token_type": "bearer",
        "expires_in": settings.oauth_access_token_expire_seconds,
        "refresh_token": new_refresh_token,
        "scope": scopes_to_scope_string(token_record.scopes or []),
    })


# --- Token Revocation (RFC 7009) ---

@router.post("/revoke")
async def revoke_token(
    token: str = Form(...),
    token_type_hint: Optional[str] = Form(None),
    client_id: str = Form(...),
    client_secret: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    """
    Revoke an access or refresh token.
    Always returns 200 per RFC 7009, regardless of whether the token existed.
    """
    await _authenticate_client(client_id, client_secret, db)

    prefix = _generate_token_prefix(token)

    # Try access token
    if token_type_hint != "refresh_token":
        result = await db.execute(
            select(OAuthAccessToken).where(
                OAuthAccessToken.token_prefix == prefix,
                OAuthAccessToken.client_id == client_id,
            )
        )
        for candidate in result.scalars().all():
            if verify_api_key(token, candidate.token_hash):
                candidate.revoked_at = datetime.now(timezone.utc)
                await db.flush()
                return JSONResponse(content={}, status_code=200)

    # Try refresh token
    result = await db.execute(
        select(OAuthAccessToken).where(
            OAuthAccessToken.refresh_token_prefix == prefix,
            OAuthAccessToken.client_id == client_id,
        )
    )
    for candidate in result.scalars().all():
        if candidate.refresh_token_hash and verify_api_key(token, candidate.refresh_token_hash):
            candidate.revoked_at = datetime.now(timezone.utc)
            await db.flush()
            return JSONResponse(content={}, status_code=200)

    # Per RFC 7009: always return 200
    return JSONResponse(content={}, status_code=200)


# --- App Info (for consent page) ---

@router.get("/app-info")
async def get_app_info(
    client_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """
    Get OAuth app info for the consent page.
    Public endpoint — only returns non-sensitive info.
    """
    result = await db.execute(
        select(OAuthApplication).where(
            OAuthApplication.client_id == client_id,
            OAuthApplication.is_active == True,
        )
    )
    app = result.scalar_one_or_none()
    if not app:
        raise HTTPException(404, "Application not found")

    return {
        "name": app.name,
        "description": app.description,
        "logo_url": app.logo_url,
        "scopes": app.scopes or [],
    }


# --- OAuth App Management (CRUD) ---

class CreateAppRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    redirect_uris: list[str] = Field(..., max_length=10)
    scopes: list[str]
    description: Optional[str] = Field(None, max_length=1000)
    logo_url: Optional[str] = Field(None, max_length=2000)


class UpdateAppRequest(BaseModel):
    name: Optional[str] = None
    redirect_uris: Optional[list[str]] = None
    scopes: Optional[list[str]] = None
    description: Optional[str] = None
    logo_url: Optional[str] = None
    is_active: Optional[bool] = None


@router.get("/apps")
async def list_apps(
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """List all OAuth apps."""
    _require_session_auth(auth)
    result = await db.execute(
        select(OAuthApplication).order_by(OAuthApplication.created_at.desc())
    )
    apps = result.scalars().all()

    # Get connection counts
    app_data = []
    for app in apps:
        count_result = await db.execute(
            select(func.count(OAuthAccessToken.id)).where(
                OAuthAccessToken.client_id == app.client_id,
                OAuthAccessToken.revoked_at.is_(None),
                OAuthAccessToken.expires_at > datetime.now(timezone.utc),
            )
        )
        connection_count = count_result.scalar() or 0
        d = app.to_dict()
        d["connection_count"] = connection_count
        app_data.append(d)

    return app_data


@router.post("/apps")
async def create_app(
    body: CreateAppRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """
    Create a new OAuth application.
    Returns client_id and client_secret (shown only once).
    """
    _require_session_auth(auth)

    # Validate redirect URIs
    _validate_redirect_uris(body.redirect_uris)

    # Validate logo_url
    if body.logo_url and not body.logo_url.startswith("https://"):
        raise HTTPException(status_code=400, detail="logo_url must use HTTPS")

    # Validate scopes
    valid_scopes = validate_scopes(body.scopes)
    if not valid_scopes:
        raise HTTPException(400, "At least one valid scope is required")

    # Generate credentials
    client_id = secrets.token_urlsafe(32)
    client_secret = generate_api_key(prefix="wtc")

    app = OAuthApplication(
        user_id=auth.user_id,
        name=body.name,
        client_id=client_id,
        client_secret_hash=hash_api_key(client_secret),
        redirect_uris=body.redirect_uris,
        scopes=valid_scopes,
        description=body.description,
        logo_url=body.logo_url,
    )
    db.add(app)
    await db.flush()

    return {
        "id": app.id,
        "name": app.name,
        "client_id": client_id,
        "client_secret": client_secret,  # Only shown once!
        "redirect_uris": app.redirect_uris,
        "scopes": app.scopes,
        "created_at": app.created_at.isoformat() if app.created_at else None,
    }


@router.get("/apps/{app_id}")
async def get_app(
    app_id: int,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Get OAuth app details."""
    _require_session_auth(auth)
    result = await db.execute(
        select(OAuthApplication).where(
            OAuthApplication.id == app_id,
        )
    )
    app = result.scalar_one_or_none()
    if not app:
        raise HTTPException(404, "Application not found")
    _require_app_ownership(app, auth)
    return app.to_dict()


@router.patch("/apps/{app_id}")
async def update_app(
    app_id: int,
    body: UpdateAppRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Update an OAuth application."""
    _require_session_auth(auth)
    result = await db.execute(
        select(OAuthApplication).where(
            OAuthApplication.id == app_id,
        )
    )
    app = result.scalar_one_or_none()
    if not app:
        raise HTTPException(404, "Application not found")
    _require_app_ownership(app, auth)

    if body.name is not None:
        app.name = body.name
    if body.redirect_uris is not None:
        # Same validation as create_app (HTTPS-in-production + scheme/netloc +
        # length). Without this, update_app was a redirect_uri-rewrite → auth-code
        # hijack primitive.
        _validate_redirect_uris(body.redirect_uris)
        app.redirect_uris = body.redirect_uris
    if body.scopes is not None:
        app.scopes = validate_scopes(body.scopes)
    if body.description is not None:
        app.description = body.description
    if body.logo_url is not None:
        app.logo_url = body.logo_url
    if body.is_active is not None:
        app.is_active = body.is_active
        # If deactivating, revoke all tokens
        if not body.is_active:
            tokens_result = await db.execute(
                select(OAuthAccessToken).where(
                    OAuthAccessToken.client_id == app.client_id,
                    OAuthAccessToken.revoked_at.is_(None),
                )
            )
            for token in tokens_result.scalars().all():
                token.revoked_at = datetime.now(timezone.utc)

    await db.flush()
    return app.to_dict()


@router.delete("/apps/{app_id}")
async def delete_app(
    app_id: int,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Deactivate an OAuth application and revoke all its tokens."""
    _require_session_auth(auth)
    result = await db.execute(
        select(OAuthApplication).where(
            OAuthApplication.id == app_id,
        )
    )
    app = result.scalar_one_or_none()
    if not app:
        raise HTTPException(404, "Application not found")
    _require_app_ownership(app, auth)

    app.is_active = False

    # Revoke all tokens
    tokens_result = await db.execute(
        select(OAuthAccessToken).where(
            OAuthAccessToken.client_id == app.client_id,
            OAuthAccessToken.revoked_at.is_(None),
        )
    )
    for token in tokens_result.scalars().all():
        token.revoked_at = datetime.now(timezone.utc)

    await db.flush()
    return {"status": "deleted"}


# --- Connection Management ---

@router.get("/connections")
async def list_connections(
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """List active OAuth connections (tokens) for the current user."""
    result = await db.execute(
        select(OAuthAccessToken, OAuthApplication.name).join(
            OAuthApplication,
            OAuthAccessToken.client_id == OAuthApplication.client_id,
        ).where(
            OAuthAccessToken.revoked_at.is_(None),
            OAuthAccessToken.expires_at > datetime.now(timezone.utc),
        ).order_by(OAuthAccessToken.created_at.desc())
    )
    rows = result.all()

    connections = []
    for token, app_name in rows:
        connections.append({
            "id": token.id,
            "app_name": app_name,
            "client_id": token.client_id,
            "scopes": token.scopes or [],
            "created_at": token.created_at.isoformat() if token.created_at else None,
            "last_used_at": token.last_used_at.isoformat() if token.last_used_at else None,
            "expires_at": token.expires_at.isoformat() if token.expires_at else None,
        })

    return connections


@router.delete("/connections/{token_id}")
async def revoke_connection(
    token_id: int,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Revoke a specific OAuth connection."""
    result = await db.execute(
        select(OAuthAccessToken).where(
            OAuthAccessToken.id == token_id,
        )
    )
    token = result.scalar_one_or_none()
    if not token:
        raise HTTPException(404, "Connection not found")

    token.revoked_at = datetime.now(timezone.utc)
    await db.flush()
    return {"status": "revoked"}


# --- Scopes info (public) ---

@router.get("/scopes")
async def list_scopes():
    """List all available OAuth scopes with descriptions."""
    return [
        {"scope": scope, "description": desc}
        for scope, desc in OAUTH_SCOPES.items()
    ]


# ---------------------------------------------------------------------------
# Device Authorization Grant (RFC 8628)
#
# Used by the writ-agent CLI to link to a user's SaaS account.
# This is a "public client" flow — no client_secret required.
# Device codes are stored in Redis with a 15-minute TTL.
# ---------------------------------------------------------------------------

DEVICE_CODE_TTL = 900  # 15 minutes
DEVICE_POLL_INTERVAL = 5
DEVICE_CLIENT_ID = "writ-agent"

# Scopes a linked desktop/CLI device is granted. This is a FULL-TRUST link to the
# user's own account (device flow, RFC 8628): the desktop "dual view" reflects AND
# controls the user's own cloud workflows, monitors (targets), detected changes,
# extracted data and triggers. So the device token must carry every READ scope
# those surfaces call plus the WRITES its controls perform (workflow update/delete
# + monitor pause/resume) — NOT just `workflows:*`.
#
# The prior grant was `["agent:connect", "workflows:read", "workflows:execute"]`,
# which omitted `targets:read` — so the desktop's Monitors dual-view reflection
# (`GET /api/targets`) came back scope-starved and the cloud monitor list / empty-
# state count showed nothing even when the account had targets.
#
# `agent:connect` is the device-flow MARKER scope (intentionally NOT in
# OAUTH_SCOPES; it survives only because the device grants below set token scopes
# DIRECTLY, never through validate_scopes' allowlist filter).
#
# NOTE: kept to workflows+targets writes only (not triggers/notifications) so
# `scopes_to_role` stays "operator" — granting all four `*:write` would escalate
# the token to org-"admin" role, which a device link must never silently become.
DEVICE_AGENT_SCOPES = [
    "agent:connect",
    "workflows:read", "workflows:write", "workflows:execute",
    "targets:read", "targets:write",
    "changes:read",
    "triggers:read",
    "reports:read",
    "notifications:read",
    "org:read",
    "profile:read",
]


async def _ensure_device_app(db: AsyncSession):
    """Ensure the writ-agent OAuth application exists."""
    result = await db.execute(
        select(OAuthApplication).where(
            OAuthApplication.client_id == DEVICE_CLIENT_ID,
        )
    )
    if result.scalar_one_or_none():
        return

    app = OAuthApplication(
        name="Writ Agent (Device Flow)",
        client_id=DEVICE_CLIENT_ID,
        client_secret_hash="public-client-no-secret",
        redirect_uris=[],
        scopes=list(DEVICE_AGENT_SCOPES),
        is_confidential=False,
        is_active=True,
    )
    db.add(app)
    try:
        await db.flush()
    except Exception:
        await db.rollback()


# Global default recorder role for device-flow registration. Self-hosted /
# single-operator deployments (where every agent is the operator's own shared
# fleet) set DEFAULT_RECORDER_MODE=infrastructure so an agent that doesn't
# explicitly request a mode registers as INFRA (is_trusted, user_hosted=False)
# instead of user-hosted BYO. The platform-admin approval gate on infrastructure
# mode is UNCHANGED — this only sets the default the operator then approves.
import os as _os_recorder_mode
DEFAULT_RECORDER_MODE = _os_recorder_mode.getenv("DEFAULT_RECORDER_MODE", "user-hosted")
if DEFAULT_RECORDER_MODE not in ("user-hosted", "infrastructure"):
    DEFAULT_RECORDER_MODE = "user-hosted"


class DeviceAuthRequest(BaseModel):
    client_id: str
    mode: Optional[str] = None  # "infrastructure" for infra recorder linking, None → DEFAULT_RECORDER_MODE


class DeviceTokenRequest(BaseModel):
    grant_type: str
    device_code: str
    client_id: str


@router.post("/device")
async def device_authorization(
    body: DeviceAuthRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    RFC 8628 — Device Authorization Request.

    The CLI calls this to get a device_code and user_code.
    The user then opens verification_uri in their browser and enters the user_code.
    """
    if body.client_id != DEVICE_CLIENT_ID:
        raise HTTPException(400, "invalid_client: unknown client_id")

    redis_client = getattr(request.app.state, "redis", None)
    if not redis_client:
        raise HTTPException(503, "Device flow requires Redis")

    await _throttle(
        redis_client,
        request,
        bucket="device_rate",
        limit=20,
        window_s=3600,
        message="Too many device authorization requests",
    )

    device_code = secrets.token_urlsafe(32)
    user_code = _generate_user_code()

    base_url = str(request.base_url).rstrip("/")
    verification_uri = f"{settings.frontend_url}/oauth/device"

    # Store in Redis
    device_data = {
        "user_code": user_code,
        "status": "pending",  # pending | approved | expired
        "mode": body.mode or DEFAULT_RECORDER_MODE,  # explicit request > global default
        "user_id": None,
        "email": None,
    }
    import json as _json
    await redis_client.setex(
        f"device_code:{device_code}",
        DEVICE_CODE_TTL,
        _json.dumps(device_data),
    )
    # Reverse mapping: user_code -> device_code (for the approval page)
    await redis_client.setex(
        f"device_user_code:{user_code}",
        DEVICE_CODE_TTL,
        device_code,
    )

    return {
        "device_code": device_code,
        "user_code": user_code,
        "verification_uri": verification_uri,
        "verification_uri_complete": f"{verification_uri}?code={user_code}",
        "expires_in": DEVICE_CODE_TTL,
        "interval": DEVICE_POLL_INTERVAL,
    }


@router.post("/device/token")
async def device_token(
    body: DeviceTokenRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    RFC 8628 — Device Access Token Request.

    The CLI polls this endpoint until the user approves or the code expires.
    Returns tokens on approval, 428 while pending, 410 if expired.
    """
    if body.grant_type != "urn:ietf:params:oauth:grant-type:device_code":
        raise HTTPException(400, "unsupported_grant_type")
    if body.client_id != DEVICE_CLIENT_ID:
        raise HTTPException(400, "invalid_client")

    redis_client = getattr(request.app.state, "redis", None)
    if not redis_client:
        raise HTTPException(503, "Device flow requires Redis")

    import json as _json

    # Non-destructive read first to check status (pending clients keep polling)
    raw = await redis_client.get(f"device_code:{body.device_code}")
    if not raw:
        raise HTTPException(410, "expired_token: device code expired or invalid")

    device_data = _json.loads(raw)

    if device_data["status"] == "pending":
        # authorization_pending — client should keep polling
        return JSONResponse(
            status_code=428,
            content={"error": "authorization_pending"},
        )

    if device_data["status"] != "approved":
        raise HTTPException(410, "expired_token")

    # Atomically fetch-and-delete the device code to prevent race conditions
    # where two concurrent poll requests both succeed in issuing tokens.
    raw = await redis_client.getdel(f"device_code:{body.device_code}")
    if not raw:
        # Another concurrent request already consumed this code
        raise HTTPException(410, "expired_token: device code already consumed")
    device_data = _json.loads(raw)

    # Approved — issue tokens + per-agent channel key
    user_id = device_data["user_id"]
    if isinstance(user_id, str):
        user_id = uuid.UUID(user_id)  # OAuthAccessToken.user_id is a Uuid column
    mode = device_data.get("mode", "user-hosted")

    # Generate a per-agent Fernet channel key for encrypting credentials
    # in transit over the WebSocket. The global Fernet key never leaves
    # the server — credentials are re-encrypted with this channel key
    # before dispatch, and the recorder decrypts with its copy.
    from cryptography.fernet import Fernet as _Fernet
    channel_key_raw = _Fernet.generate_key()
    channel_key_plaintext = channel_key_raw.decode()

    # Fail CLOSED. This used to fall back to storing the key in plaintext "for
    # dev" — but SECRET_ENCRYPTION_KEY is only *required* in production, so on
    # any other environment the except branch was the normal path, silently
    # writing the per-agent channel key to Redis in the clear. A sealing step
    # that quietly stops sealing is worse than one that stops working.
    channel_key_encrypted = _seal_channel_key(channel_key_plaintext)

    # Clean up user_code mapping
    user_code = device_data.get("user_code")
    if user_code:
        await redis_client.delete(f"device_user_code:{user_code}")

    # ----------------------------------------------------------------
    # Infrastructure mode: issue a signed JWT service token (long-lived)
    # ----------------------------------------------------------------
    if mode == "infrastructure":
        import os as _os
        from utils.recorder_auth import generate_service_token

        recorder_secret = _os.getenv("RECORDER_AUTH_SECRET", "")
        if not recorder_secret:
            raise HTTPException(500, "RECORDER_AUTH_SECRET not configured — cannot issue infrastructure token")

        # Bind the token to a STABLE agent id. /recorder/connect reads this when
        # the agent sends no id of its own (recorder_proxy: requested_agent_id =
        # request.agent_id or _auth["agent_id"]). Without it safe_agent_id is empty
        # → the gateway mints a random id AND the channel-key mirror is skipped,
        # so dispatch can't find the key (credential_encryption_failed).
        import uuid as _uuid
        infra_agent_id = f"writ-{_uuid.uuid4().hex[:12]}"

        service_token = generate_service_token(
            "",  # single-owner coordinator: no org scoping baked into the token
            max_sessions=5,
            secret=recorder_secret,
            agent_id=infra_agent_id,
            ttl_hours=24 * 365,  # long-lived infra token (response advertises no expiry)
        )

        # Persist the channel key under BOTH (a) the token prefix the JWT derives
        # at validation time — so the /connect mirror copies it to whatever
        # agent_id is used — and (b) directly under the baked agent_id, so the
        # ws-gateway dispatch path finds it even before the first mirror runs.
        infra_token_prefix = _generate_token_prefix(service_token)
        await redis_client.set(f"agent_channel_key:{infra_token_prefix}", channel_key_encrypted)
        await redis_client.set(f"agent_channel_key:{infra_agent_id}", channel_key_encrypted)

        # The bridge also caches the key in memory from this response.
        return {
            "access_token": service_token,
            "token_type": "bearer",
            "mode": "infrastructure",
            "expires_in": 0,  # no expiry (token TTL is generous; re-link to rotate)
            "channel_key": channel_key_plaintext,
            "agent_id": infra_agent_id,
            # Wire-compat: the light writ-agent stores creds["tenant_id"] from this
            # response (saas_bridge). Single-owner self-host → the constant "local".
            "tenant_id": "local",
            "email": device_data.get("email"),
        }

    # ----------------------------------------------------------------
    # User-hosted mode: issue wto_ OAuth tokens (standard flow)
    # ----------------------------------------------------------------
    await _ensure_device_app(db)

    access_token = generate_api_key(prefix="wto")
    refresh_token_value = generate_api_key(prefix="wtr")

    token_record = OAuthAccessToken(
        token_prefix=_generate_token_prefix(access_token),
        token_hash=hash_api_key(access_token),
        refresh_token_prefix=_generate_token_prefix(refresh_token_value),
        refresh_token_hash=hash_api_key(refresh_token_value),
        client_id=DEVICE_CLIENT_ID,
        user_id=user_id,
        scopes=list(DEVICE_AGENT_SCOPES),
        expires_at=datetime.now(timezone.utc) + timedelta(
            seconds=settings.oauth_access_token_expire_seconds
        ),
        # Desktop/CLI device link: long-lived, sliding refresh so the install stays signed in.
        refresh_expires_at=datetime.now(timezone.utc) + timedelta(
            days=settings.oauth_device_refresh_token_expire_days
        ),
    )
    db.add(token_record)
    await db.flush()

    await redis_client.set(
        f"agent_channel_key:{token_record.token_prefix}",
        channel_key_encrypted,
    )

    return {
        "access_token": access_token,
        "token_type": "bearer",
        "mode": "user-hosted",
        "expires_in": settings.oauth_access_token_expire_seconds,
        "refresh_token": refresh_token_value,
        "channel_key": channel_key_plaintext,
        # Wire-compat: single-owner self-host → constant "local" (saas_bridge stores
        # creds["tenant_id"] from this response).
        "tenant_id": "local",
        "email": device_data.get("email"),
    }


@router.post("/device/approve")
async def device_approve(
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """
    Approve a device authorization request.

    Called by the frontend when the user enters the user_code and confirms.
    Requires an authenticated user session (JWT).
    """
    body = await request.json()
    user_code = body.get("user_code", "").strip().upper()
    if not user_code:
        raise HTTPException(400, "user_code required")

    redis_client = getattr(request.app.state, "redis", None)
    if not redis_client:
        raise HTTPException(503, "Device flow requires Redis")

    import json as _json

    # Look up device_code from user_code
    device_code = await redis_client.get(f"device_user_code:{user_code}")
    if not device_code:
        raise HTTPException(400, "Invalid or expired code")
    if isinstance(device_code, bytes):
        device_code = device_code.decode()

    raw = await redis_client.get(f"device_code:{device_code}")
    if not raw:
        raise HTTPException(400, "Device code expired")

    device_data = _json.loads(raw)
    if device_data["status"] != "pending":
        raise HTTPException(400, "Code already used")

    # Infrastructure linking requires PLATFORM-ADMIN approval — an infrastructure
    # agent is TRUSTED fleet (is_trusted=True, user_hosted=False) that can serve
    # shared runs, so a non-platform admin must NOT be able to enroll one
    # (privilege escalation). An org role ("admin"/"owner") is NOT sufficient here;
    # only the signed is_platform_admin claim is. A non-platform-admin should link
    # user-hosted (BYO) instead.
    if device_data.get("mode") == "infrastructure":
        if not auth.is_platform_admin:
            raise HTTPException(
                403,
                "Only a platform administrator can approve infrastructure recorder "
                "linking. Link this agent as user-hosted (BYO) instead, or ask a "
                "platform administrator to approve infrastructure enrollment.",
            )

    # Get user info
    from models.user import User

    user = await db.get(User, auth.user_id)

    # Mark as approved
    device_data["status"] = "approved"
    device_data["user_id"] = str(auth.user_id)
    device_data["email"] = user.email if user else None
    device_data["user_code"] = user_code

    # Store back with remaining TTL
    ttl = await redis_client.ttl(f"device_code:{device_code}")
    if ttl and ttl > 0:
        await redis_client.setex(
            f"device_code:{device_code}",
            ttl,
            _json.dumps(device_data),
        )
    else:
        await redis_client.setex(
            f"device_code:{device_code}",
            60,
            _json.dumps(device_data),
        )

    # For infrastructure mode, pre-generate the service token so the
    # approval page can display it for manual copy (fallback when CLI
    # polling can't reach the coordinator — e.g. Docker networking issues).
    display_token = None
    mode = device_data.get("mode", "user-hosted")
    if mode == "infrastructure":
        try:
            import os as _os
            from utils.recorder_auth import generate_service_token
            recorder_secret = _os.getenv("RECORDER_AUTH_SECRET", "")
            if recorder_secret:
                display_token = generate_service_token(
                    "",  # single-owner coordinator: no org scoping baked into the token
                    max_sessions=5,
                    secret=recorder_secret,
                )
        except Exception:
            pass

    response = {
        "status": "approved",
        "email": device_data["email"],
        "mode": mode,
    }
    if display_token:
        response["service_token"] = display_token

    return response


@router.post("/device/info")
async def device_info(
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
):
    """
    Get metadata about a pending device code (by user_code).

    Called by the frontend approval page to display context before
    the user clicks approve — e.g. "Infrastructure recorder" vs
    "User-hosted agent".

    Requires authentication (the approver must be logged in).
    """
    body = await request.json()
    user_code = body.get("user_code", "").strip().upper()
    if not user_code:
        raise HTTPException(400, "user_code required")

    redis_client = getattr(request.app.state, "redis", None)
    if not redis_client:
        raise HTTPException(503, "Requires Redis")

    import json as _json

    device_code = await redis_client.get(f"device_user_code:{user_code}")
    if not device_code:
        raise HTTPException(404, "Invalid or expired code")
    if isinstance(device_code, bytes):
        device_code = device_code.decode()

    raw = await redis_client.get(f"device_code:{device_code}")
    if not raw:
        raise HTTPException(404, "Device code expired")

    device_data = _json.loads(raw)

    mode = device_data.get("mode", "user-hosted")
    requires_admin = mode == "infrastructure"

    # Infrastructure linking requires a PLATFORM admin (not an org admin/owner) —
    # mirror the enforcement in /device/approve so the UI hides the approve button
    # for users who would be rejected anyway.
    can_approve = True
    if requires_admin and not auth.is_platform_admin:
        can_approve = False

    return {
        "status": device_data["status"],
        "mode": mode,
        "requires_admin": requires_admin,
        "can_approve": can_approve,
    }


@router.post("/device/refresh")
async def device_refresh_token(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Refresh an agent's access token using a refresh token.

    Separate from the main /token endpoint because device flow
    is a public client (no client_secret required).
    """
    body = await request.json()
    refresh_token_value = body.get("refresh_token")
    client_id = body.get("client_id", DEVICE_CLIENT_ID)

    if not refresh_token_value:
        raise HTTPException(400, "refresh_token required")
    if client_id != DEVICE_CLIENT_ID:
        raise HTTPException(400, "invalid_client")

    prefix = _generate_token_prefix(refresh_token_value)
    result = await db.execute(
        select(OAuthAccessToken).where(
            OAuthAccessToken.refresh_token_prefix == prefix,
            OAuthAccessToken.client_id == DEVICE_CLIENT_ID,
            OAuthAccessToken.revoked_at.is_(None),
        )
    )
    candidates = result.scalars().all()

    token_record = None
    for candidate in candidates:
        if candidate.refresh_token_hash and verify_api_key(
            refresh_token_value, candidate.refresh_token_hash
        ):
            token_record = candidate
            break

    if not token_record:
        raise HTTPException(400, "invalid_grant: refresh token not found or revoked")

    if (
        token_record.refresh_expires_at
        and token_record.refresh_expires_at < datetime.now(timezone.utc)
    ):
        raise HTTPException(400, "invalid_grant: refresh token expired")

    # Carry channel key forward: copy from old token prefix to new
    old_prefix = token_record.token_prefix
    redis_client = getattr(request.app.state, "redis", None)
    channel_key_encrypted = None
    if redis_client:
        channel_key_encrypted = await redis_client.get(f"agent_channel_key:{old_prefix}")

    # Rotate: revoke old, issue new
    token_record.revoked_at = datetime.now(timezone.utc)

    new_access_token = generate_api_key(prefix="wto")
    new_refresh_token = generate_api_key(prefix="wtr")

    from models.user import User

    user = await db.get(User, token_record.user_id)

    new_record = OAuthAccessToken(
        token_prefix=_generate_token_prefix(new_access_token),
        token_hash=hash_api_key(new_access_token),
        refresh_token_prefix=_generate_token_prefix(new_refresh_token),
        refresh_token_hash=hash_api_key(new_refresh_token),
        client_id=DEVICE_CLIENT_ID,
        user_id=token_record.user_id,
        # Self-heal scopes on refresh: this endpoint is the writ-agent device flow
        # ONLY (client_id is enforced == DEVICE_CLIENT_ID above), so every token here
        # is a full-trust desktop/CLI link. Upgrade links minted with the old narrow
        # set (`workflows`-only, no `targets:read`) to the canonical DEVICE_AGENT_SCOPES
        # on their next sliding refresh, so existing installs gain the Monitors/Data
        # reflection scopes WITHOUT a forced unlink + re-link.
        scopes=list(DEVICE_AGENT_SCOPES),
        expires_at=datetime.now(timezone.utc) + timedelta(
            seconds=settings.oauth_access_token_expire_seconds
        ),
        # Desktop/CLI device link: re-extend the long-lived, sliding refresh window on each rotation.
        refresh_expires_at=datetime.now(timezone.utc) + timedelta(
            days=settings.oauth_device_refresh_token_expire_days
        ),
        # Carry the per-machine agent binding forward so a disconnect after a
        # token refresh still revokes the live token (revoke-by-agent_id).
        agent_id=token_record.agent_id,
    )
    db.add(new_record)
    await db.flush()

    # Migrate channel key to new token prefix
    if redis_client and channel_key_encrypted:
        await redis_client.set(
            f"agent_channel_key:{new_record.token_prefix}",
            channel_key_encrypted,
        )
        await redis_client.delete(f"agent_channel_key:{old_prefix}")

    return {
        "access_token": new_access_token,
        "token_type": "bearer",
        "expires_in": settings.oauth_access_token_expire_seconds,
        "refresh_token": new_refresh_token,
        # Wire-compat: single-owner self-host → constant "local".
        "tenant_id": "local",
        "email": user.email if user else None,
    }


def _seal_channel_key(channel_key_plaintext: str) -> str:
    """Encrypt a per-agent channel key at rest, or refuse to issue one.

    The channel key seals the agent's command channel, so storing it in the clear
    defeats the point of having it. Callers previously wrapped this in
    ``try/except`` and fell back to the plaintext "for dev" — but
    SECRET_ENCRYPTION_KEY is only mandatory in production, so outside production
    that fallback WAS the normal path and the downgrade was invisible.

    Raising here turns a silent security downgrade into an actionable 500 with a
    message that names the missing variable.
    """
    from security.encryption import SecretEncryption

    try:
        return SecretEncryption.encrypt_secret(channel_key_plaintext)
    except Exception as exc:
        logger.error("Refusing to issue an agent channel key unsealed: %s", exc)
        raise HTTPException(
            500,
            "Cannot seal the agent channel key: SECRET_ENCRYPTION_KEY is not "
            "configured or is invalid. Generate one with: python3 -c \"from "
            "cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"",
        ) from exc


async def _throttle(
    redis_client,
    request: Request,
    *,
    bucket: str,
    limit: int,
    window_s: int,
    message: str,
    subject: Optional[str] = None,
) -> None:
    """Per-IP (or per-subject) fixed-window throttle. Raises 429 when exceeded.

    Note the exception handling. An earlier version wrapped the whole counter in
    ``try/except Exception: pass`` so that a Redis hiccup could not take the
    endpoint down — but ``HTTPException`` is an ``Exception``, so the ``raise``
    was caught by its own handler and the limit never actually fired. Redis
    errors are swallowed (fail-open: this is an abuse control, not an authz
    gate); the 429 is raised outside the try so it always propagates.
    """
    if not redis_client:
        return

    ip = request.client.host if request.client else "unknown"
    key = f"{bucket}:{subject or ip}"
    over_limit = False
    try:
        count = await redis_client.incr(key)
        if count == 1:
            await redis_client.expire(key, window_s)
        over_limit = count > limit
    except Exception:  # pragma: no cover - Redis outage must not 500 the endpoint
        logger.warning("throttle bucket %s unavailable — allowing the request", bucket)
        return

    if over_limit:
        raise HTTPException(429, message)


def _generate_user_code() -> str:
    """Generate a human-friendly 8-character code (e.g. ABCD-1234).

    SECURITY: uses ``secrets``, not ``random``. ``random.choices`` is the Mersenne
    Twister — an unseeded-but-observable PRNG whose internal state can be
    reconstructed from enough emitted output, which would let an attacker who has
    sampled a few codes from the public ``POST /device`` endpoint PREDICT the code
    the operator's next legitimate CLI login is about to display, and redeem it
    first. Redeeming a code yields a full-scope agent token, so this must be
    unguessable, not merely unique.

    O/0 and I/1 are excluded because the code is read off a screen and typed.
    """
    import secrets
    import string

    letters_alphabet = string.ascii_uppercase.replace("O", "").replace("I", "")
    digits_alphabet = string.digits.replace("0", "").replace("1", "")
    letters = "".join(secrets.choice(letters_alphabet) for _ in range(4))
    digits = "".join(secrets.choice(digits_alphabet) for _ in range(4))
    return f"{letters}-{digits}"


# ---------------------------------------------------------------------------
# CLI fallback endpoints (for when device flow polling fails)
# ---------------------------------------------------------------------------

@router.post("/device/token-by-code")
async def device_token_by_code(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Retrieve an already-issued token using the user_code.

    Called by the CLI when device flow polling failed but the user may
    have already approved in the browser. Unlike /device/token (which
    polls by device_code), this uses the user_code as the lookup key.

    Returns the token if the device code was already approved and a
    token was issued, or 404 if not yet approved / expired.
    """
    body = await request.json()
    user_code = body.get("user_code", "").strip().upper()
    client_id = body.get("client_id", "")

    if not user_code or client_id != DEVICE_CLIENT_ID:
        raise HTTPException(400, "user_code and valid client_id required")

    redis_client = getattr(request.app.state, "redis", None)
    if not redis_client:
        raise HTTPException(503, "Device flow requires Redis")

    # This endpoint is UNAUTHENTICATED and redeems by user_code alone for a
    # full-scope agent token, so it is the one place an attacker can guess at.
    # The code space is ~1.4e9 and each code lives for DEVICE_CODE_TTL, which is
    # only out of reach if guessing is bounded — POST /device is throttled, this
    # was not.
    await _throttle(
        redis_client,
        request,
        bucket="device_code_redeem",
        limit=30,
        window_s=3600,
        message="Too many code redemption attempts",
    )

    import json as _json

    # Atomically fetch-and-delete the user_code mapping to prevent race conditions
    # where two concurrent requests both succeed in issuing tokens.
    device_code = await redis_client.getdel(f"device_user_code:{user_code}")
    if not device_code:
        # Burn a per-code attempt budget as well as the per-IP one, so a
        # distributed guessing attempt cannot simply rotate source addresses.
        await _throttle(
            redis_client,
            request,
            bucket="device_code_miss",
            limit=10,
            window_s=900,
            message="Too many code redemption attempts",
            subject=user_code,
        )
        raise HTTPException(404, "Code not found or expired")
    if isinstance(device_code, bytes):
        device_code = device_code.decode()

    # Atomically fetch-and-delete the device code data
    raw = await redis_client.getdel(f"device_code:{device_code}")
    if not raw:
        raise HTTPException(404, "Device code expired")

    device_data = _json.loads(raw)

    if device_data["status"] != "approved":
        raise HTTPException(404, "Not yet approved")

    # The token was already issued by the /device/token endpoint's polling.
    # But if the CLI missed it, we can re-issue from the same approval.
    user_id = device_data["user_id"]
    if isinstance(user_id, str):
        user_id = uuid.UUID(user_id)  # OAuthAccessToken.user_id is a Uuid column

    await _ensure_device_app(db)

    access_token = generate_api_key(prefix="wto")
    refresh_token_value = generate_api_key(prefix="wtr")

    from cryptography.fernet import Fernet as _Fernet
    channel_key_raw = _Fernet.generate_key()
    channel_key_plaintext = channel_key_raw.decode()

    channel_key_encrypted = _seal_channel_key(channel_key_plaintext)

    token_record = OAuthAccessToken(
        token_prefix=_generate_token_prefix(access_token),
        token_hash=hash_api_key(access_token),
        refresh_token_prefix=_generate_token_prefix(refresh_token_value),
        refresh_token_hash=hash_api_key(refresh_token_value),
        client_id=DEVICE_CLIENT_ID,
        user_id=user_id,
        scopes=list(DEVICE_AGENT_SCOPES),
        expires_at=datetime.now(timezone.utc) + timedelta(
            seconds=settings.oauth_access_token_expire_seconds
        ),
        # Desktop/CLI device link: long-lived, sliding refresh so the install stays signed in.
        refresh_expires_at=datetime.now(timezone.utc) + timedelta(
            days=settings.oauth_device_refresh_token_expire_days
        ),
    )
    db.add(token_record)
    await db.flush()

    await redis_client.set(
        f"agent_channel_key:{token_record.token_prefix}",
        channel_key_encrypted,
    )

    from models.user import User
    user = await db.get(User, user_id)

    return {
        "access_token": access_token,
        "token_type": "bearer",
        "expires_in": settings.oauth_access_token_expire_seconds,
        "refresh_token": refresh_token_value,
        "channel_key": channel_key_plaintext,
        # Wire-compat: single-owner self-host → constant "local".
        "tenant_id": "local",
        "email": user.email if user else device_data.get("email"),
    }


@router.post("/device/validate-token")
async def device_validate_token(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Validate a manually-pasted wto_ (legacy: pso_) access token.

    Called by the CLI when the user pastes a token they copied from the
    browser approval page. Returns user/org info if the token is valid.
    """
    body = await request.json()
    access_token = body.get("access_token", "")
    client_id = body.get("client_id", "")

    if not access_token or not access_token.startswith(("wto_", "pso_")) or client_id != DEVICE_CLIENT_ID:
        raise HTTPException(400, "Valid wto_ access_token and client_id required")

    token_prefix = _generate_token_prefix(access_token)

    result = await db.execute(
        select(OAuthAccessToken).where(
            OAuthAccessToken.token_prefix == token_prefix,
            OAuthAccessToken.revoked_at.is_(None),
            OAuthAccessToken.expires_at > datetime.now(timezone.utc),
        )
    )
    candidates = result.scalars().all()

    for candidate in candidates:
        if verify_api_key(access_token, candidate.token_hash):
            # Valid token — return info
            from models.user import User

            user = await db.get(User, candidate.user_id)

            # Retrieve channel key from Redis
            redis_client = getattr(request.app.state, "redis", None)
            channel_key = None
            if redis_client:
                try:
                    stored = await redis_client.get(f"agent_channel_key:{token_prefix}")
                    if stored:
                        try:
                            from security.encryption import SecretEncryption
                            channel_key = SecretEncryption.decrypt_secret(stored)
                        except Exception:
                            if len(stored) == 44 and stored.endswith('='):
                                channel_key = stored
                except Exception:
                    pass

            return {
                "valid": True,
                "expires_in": int((candidate.expires_at - datetime.now(timezone.utc)).total_seconds()),
                "refresh_token": None,  # Don't expose refresh token on validation
                "channel_key": channel_key,
                # Wire-compat: single-owner self-host → constant "local".
                "tenant_id": "local",
                "email": user.email if user else None,
            }

    raise HTTPException(401, "Invalid or expired token")
