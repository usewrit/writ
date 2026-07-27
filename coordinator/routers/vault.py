"""
Vault router — CRUD for secrets + provider management.

Secrets are NEVER returned in plaintext from any endpoint.
"""
import json
import logging
from typing import Optional, List
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from security.dependencies import get_auth_context, AuthContext
from security.encryption import SecretEncryption
from security.oauth_scopes import check_scope
from models.vault_secret import VaultSecret

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/vault", tags=["Vault"])


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------
#
# Vault routes previously depended only on get_auth_context with NO scope/role
# check (finding #3), so any authenticated principal — including a viewer-role
# API key or an OAuth token holding only e.g. profile:read — could list, create,
# rotate, and delete secrets. Enforce per auth method:
#   * OAuth token  -> must carry the matching secrets:read / secrets:write scope.
#   * API key      -> must hold a non-viewer role (viewer keys are read-only-nothing
#                     here; the vault is never exposed to the least-privileged key).
#   * JWT          -> the single-owner web console; passes through as before.
VAULT_READ_SCOPE = "secrets:read"
VAULT_WRITE_SCOPE = "secrets:write"


def _require_vault_access(auth: AuthContext, *, write: bool) -> None:
    """Authorize a vault operation for the caller, or raise 403."""
    required = VAULT_WRITE_SCOPE if write else VAULT_READ_SCOPE
    if auth.auth_method == "oauth":
        if not check_scope(auth.oauth_scopes or [], required):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"OAuth scope '{required}' required",
            )
    elif auth.auth_method == "api_key":
        if (auth.role or "viewer") == "viewer":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This API key is not permitted to access the vault",
            )
    # JWT (owner web console) passes through.


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

# A "credentials" secret stores an email/username + password pair as JSON in
# value_encrypted; every other category stores a single opaque value. The pair
# is referenced as {{vault:name.username}} / {{vault:name.password}}.
CREDENTIAL_CATEGORY = "credentials"

# A "card" secret stores a payment card as JSON ({name, number, expiry, cvc, zip})
# in value_encrypted, referenced per field as {{vault:name.number}} etc. Card
# values are never returned by the API; only a non-secret last-4 is exposed.
CARD_CATEGORY = "card"
CARD_FIELDS = ("name", "number", "expiry", "cvc", "zip")


class SecretCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255, pattern=r'^[a-zA-Z_][a-zA-Z0-9_.-]*$')
    # Single-value secrets supply `value`; credential secrets supply
    # username+password; card secrets supply `card` (a {field: value} dict).
    value: Optional[str] = Field(None, min_length=1)
    username: Optional[str] = None
    password: Optional[str] = None
    card: Optional[dict] = None
    description: Optional[str] = None
    category: Optional[str] = None


class SecretUpdate(BaseModel):
    value: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    card: Optional[dict] = None
    description: Optional[str] = None
    category: Optional[str] = None


class SecretResponse(BaseModel):
    id: int
    name: str
    description: Optional[str]
    category: Optional[str]
    is_credential: bool = False
    is_card: bool = False
    # For credential secrets, the (non-secret) identifier is exposed for editing;
    # the password is never returned.
    username: Optional[str] = None
    # For card secrets, the last 4 digits are exposed (non-secret) for display;
    # the full PAN/CVC are never returned.
    card_last4: Optional[str] = None
    created_at: datetime
    updated_at: Optional[datetime]
    last_used_at: Optional[datetime]
    use_count: int
    # NOTE: No value / password / card field — secret values never exposed via API


# All sub-field names a secret can ever be referenced by ({{vault:name.<field>}}).
# Passed to invalidate_cache_deep so a rotated/deleted secret's per-sub-field cache
# entries are cleared too, regardless of (or across a change in) its category.
ALL_DERIVED_SUBFIELDS = tuple(dict.fromkeys(("username", "password", *CARD_FIELDS)))


def _is_credential(entry: VaultSecret) -> bool:
    return (entry.category or "") == CREDENTIAL_CATEGORY


def _is_card(entry: VaultSecret) -> bool:
    return (entry.category or "") == CARD_CATEGORY


def _credential_username(entry: VaultSecret) -> Optional[str]:
    """Decode just the identifier from a credential secret (never the password)."""
    if not _is_credential(entry):
        return None
    try:
        return json.loads(SecretEncryption.decrypt_secret(entry.value_encrypted)).get("username")
    except Exception:
        return None


def _card_last4(entry: VaultSecret) -> Optional[str]:
    """Decode just the last 4 digits of a card secret (never the full PAN/CVC)."""
    if not _is_card(entry):
        return None
    try:
        number = json.loads(SecretEncryption.decrypt_secret(entry.value_encrypted)).get("number") or ""
        digits = "".join(ch for ch in str(number) if ch.isdigit())
        return digits[-4:] if len(digits) >= 4 else None
    except Exception:
        return None


def _clean_card(card: dict) -> dict:
    """Keep only known card fields with non-empty string values; strip number spaces."""
    out = {}
    for field in CARD_FIELDS:
        val = card.get(field)
        if val is None:
            continue
        val = str(val).strip()
        if not val:
            continue
        out[field] = val.replace(" ", "") if field == "number" else val
    return out


def _secret_response(entry: VaultSecret) -> "SecretResponse":
    return SecretResponse(
        id=entry.id,
        name=entry.key,
        description=entry.description,
        category=entry.category,
        is_credential=_is_credential(entry),
        is_card=_is_card(entry),
        username=_credential_username(entry),
        card_last4=_card_last4(entry),
        created_at=entry.created_at,
        updated_at=entry.updated_at,
        last_used_at=entry.last_used_at,
        use_count=entry.use_count or 0,
    )


# ---------------------------------------------------------------------------
# Secrets CRUD
# ---------------------------------------------------------------------------

@router.get("/secrets", response_model=List[SecretResponse])
async def list_secrets(
    search: Optional[str] = None,
    category: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """List all secrets (metadata only, never values)."""
    _require_vault_access(auth, write=False)
    query = select(VaultSecret)
    if search:
        query = query.where(VaultSecret.key.ilike(f"%{search}%"))
    if category:
        query = query.where(VaultSecret.category == category)
    query = query.order_by(VaultSecret.key).offset(offset).limit(limit)
    result = await db.execute(query)
    secrets = result.scalars().all()
    return [_secret_response(s) for s in secrets]


@router.post("/secrets", response_model=SecretResponse, status_code=status.HTTP_201_CREATED)
async def create_secret(
    body: SecretCreate,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Create a new vault secret."""
    _require_vault_access(auth, write=True)
    # Check uniqueness
    existing = await db.execute(
        select(VaultSecret).where(
            VaultSecret.key == body.name,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail=f"Secret '{body.name}' already exists")

    # Card secret vs credential secret (email/username + password) vs single value.
    is_card = body.category == CARD_CATEGORY or body.card is not None
    is_cred = (not is_card) and (
        body.category == CREDENTIAL_CATEGORY or body.username is not None or body.password is not None
    )
    if is_card:
        card = _clean_card(body.card or {})
        if not card.get("number") or not card.get("expiry"):
            raise HTTPException(status_code=422, detail="A card secret needs at least a number and an expiry")
        stored = json.dumps(card)
        category = CARD_CATEGORY
    elif is_cred:
        if not (body.username and body.username.strip()) or not (body.password and body.password.strip()):
            raise HTTPException(status_code=422, detail="A credential secret needs both an email/username and a password")
        stored = json.dumps({"username": body.username, "password": body.password})
        category = CREDENTIAL_CATEGORY
    else:
        if not (body.value and body.value.strip()):
            raise HTTPException(status_code=422, detail="A secret value is required")
        stored = body.value
        category = body.category

    entry = VaultSecret(
        key=body.name,
        value_encrypted=SecretEncryption.encrypt_secret(stored),
        description=body.description,
        category=category,
    )
    db.add(entry)
    await db.commit()
    await db.refresh(entry)

    from services.secret_resolver import invalidate_cache
    await invalidate_cache(body.name)
    logger.info(f"Vault secret created: {body.name}")

    return _secret_response(entry)


@router.patch("/secrets/{secret_id}", response_model=SecretResponse)
async def update_secret(
    secret_id: int,
    body: SecretUpdate,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Update a vault secret (value and/or metadata)."""
    _require_vault_access(auth, write=True)
    result = await db.execute(
        select(VaultSecret).where(
            VaultSecret.id == secret_id,
        )
    )
    entry = result.scalar_one_or_none()
    if not entry:
        raise HTTPException(status_code=404, detail="Secret not found")

    if body.card is not None:
        # Updating a card — merge with the existing card so the caller can change
        # just one field (e.g. expiry) without resupplying the whole card.
        existing_card = {}
        try:
            existing_card = json.loads(SecretEncryption.decrypt_secret(entry.value_encrypted))
            if not isinstance(existing_card, dict):
                existing_card = {}
        except Exception:
            existing_card = {}
        merged = {**existing_card, **_clean_card(body.card)}
        if not merged.get("number") or not merged.get("expiry"):
            raise HTTPException(status_code=422, detail="A card secret needs at least a number and an expiry")
        entry.value_encrypted = SecretEncryption.encrypt_secret(json.dumps(merged))
        entry.category = CARD_CATEGORY
    elif body.username is not None or body.password is not None:
        # Updating a credential pair — merge with the existing pair so the caller
        # can change just the username or just the password.
        existing_pair = {}
        try:
            existing_pair = json.loads(SecretEncryption.decrypt_secret(entry.value_encrypted))
            if not isinstance(existing_pair, dict):
                existing_pair = {}
        except Exception:
            existing_pair = {}
        username = body.username if body.username is not None else existing_pair.get("username", "")
        password = body.password if body.password is not None else existing_pair.get("password", "")
        if not (username and password):
            raise HTTPException(status_code=422, detail="A credential secret needs both an email/username and a password")
        entry.value_encrypted = SecretEncryption.encrypt_secret(json.dumps({"username": username, "password": password}))
        entry.category = CREDENTIAL_CATEGORY
    elif body.value is not None:
        entry.value_encrypted = SecretEncryption.encrypt_secret(body.value)
    if body.description is not None:
        entry.description = body.description
    if body.category is not None and body.username is None and body.password is None and body.card is None:
        entry.category = body.category

    entry.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(entry)

    # Clear the base key AND every derived sub-field key (vault:key.username etc.),
    # otherwise a rotated credential/card keeps resolving its stale sub-fields for
    # up to CACHE_TTL (finding #33).
    from services.secret_resolver import invalidate_cache_deep
    await invalidate_cache_deep(entry.key, list(ALL_DERIVED_SUBFIELDS))
    logger.info(f"Vault secret updated: {entry.key}")

    return _secret_response(entry)


@router.delete("/secrets/{secret_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_secret(
    secret_id: int,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Delete a vault secret."""
    _require_vault_access(auth, write=True)
    result = await db.execute(
        select(VaultSecret).where(
            VaultSecret.id == secret_id,
        )
    )
    entry = result.scalar_one_or_none()
    if not entry:
        raise HTTPException(status_code=404, detail="Secret not found")

    name = entry.key
    await db.delete(entry)
    await db.commit()

    # Clear the base key AND every derived sub-field key so a deleted credential/card
    # stops resolving its stale sub-fields from cache (finding #33).
    from services.secret_resolver import invalidate_cache_deep
    await invalidate_cache_deep(name, list(ALL_DERIVED_SUBFIELDS))
    logger.info(f"Vault secret deleted: {name}")

