"""
Secret Resolver — resolves {{vault:key}} references in workflow form_data.

Resolution chain (with Redis caching):
  1. Redis cache (vault:{key}, 5min TTL)
  2. vault_secrets table (PostgreSQL)
  3. External providers by priority (if configured)

Cache invalidation: on create/update/delete via the vault router.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Dict, List, Optional, Tuple

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

VAULT_REF = re.compile(r"\{\{vault:([a-zA-Z_][a-zA-Z0-9_.-]*)\}\}")
CACHE_TTL = 300  # 5 minutes
CACHE_PREFIX = "vault"


async def _get_redis():
    """Get the app's Redis client (None if unavailable)."""
    try:
        from main import redis_client
        return redis_client
    except Exception:
        return None


async def resolve_secret(
    db: AsyncSession,
    key: str = "",
) -> Optional[str]:
    """
    Resolve a single secret: cache → vault DB.
    Returns plaintext value or None.

    Vault secrets are sealed with the global key and belong to the single owner.
    """
    cache_key = f"{CACHE_PREFIX}:{key}"

    # 1. Redis cache
    redis = await _get_redis()
    if redis:
        try:
            cached = await redis.get(cache_key)
            if cached is not None:
                if cached == "__NOT_FOUND__":
                    pass  # Negative cache — fall through to providers
                else:
                    return cached
        except Exception:
            pass

    # 2. Vault DB. A "credentials" secret stores a {username,password} JSON pair;
    #    it's referenced via subfields: {{vault:name.username}} / {{vault:name.password}}.
    #    Plain secrets store an opaque string. Exact-key match wins (back-compat
    #    for secrets whose own name contains a dot); otherwise fall back to
    #    base.subfield on a credential pair.
    from models.vault_secret import VaultSecret
    from security.encryption import SecretEncryption

    async def _load(k: str):
        r = await db.execute(
            select(VaultSecret).where(VaultSecret.key == k)
        )
        return r.scalar_one_or_none()

    def _decode(entry, subfield):
        # Single-user coordinator: vault values are sealed with the global key
        # so decrypt without a per-owner salt.
        raw = SecretEncryption.decrypt_secret(entry.value_encrypted)
        category = entry.category or ""
        if category == "credentials":
            data = json.loads(raw)
            if isinstance(data, dict):
                # Bare reference to a credential secret resolves to the password.
                return data.get(subfield) if subfield else data.get("password")
            return None
        if category == "card":
            # Structured payment card: {name, number, expiry, cvc, zip} JSON.
            # Referenced per field ({{vault:mycard.number}}); a bare reference
            # resolves to the card number.
            data = json.loads(raw)
            if isinstance(data, dict):
                return data.get(subfield) if subfield else data.get("number")
            return None
        return raw  # plain single-value secret

    plaintext = None
    used_entry = None
    entry = await _load(key)
    if entry:
        try:
            plaintext = _decode(entry, None)
            used_entry = entry
        except Exception as e:
            logger.error(f"Failed to decrypt vault secret '{key}': {e}")
            return None
    elif "." in key:
        base, subfield = key.rsplit(".", 1)
        base_entry = await _load(base)
        if base_entry:
            try:
                plaintext = _decode(base_entry, subfield)
                used_entry = base_entry
            except Exception as e:
                logger.error(f"Failed to decrypt vault secret '{base}': {e}")
                return None

    if plaintext is not None and used_entry is not None:
        # Cache + track usage (fire-and-forget, don't slow down resolution)
        if redis:
            try:
                await redis.setex(cache_key, CACHE_TTL, plaintext)
            except Exception:
                pass

        from datetime import datetime, timezone
        used_entry.last_used_at = datetime.now(timezone.utc)
        used_entry.use_count = (used_entry.use_count or 0) + 1

        return plaintext

    # The vault DB is the only resolution source.

    # Negative cache (short TTL) to avoid hammering DB for missing keys
    if redis:
        try:
            await redis.setex(cache_key, 30, "__NOT_FOUND__")
        except Exception:
            pass

    return None


async def resolve_form_data(
    db: AsyncSession,
    form_data: dict,
) -> Tuple[dict, dict]:
    """
    Scan form_data for {{vault:key}} patterns and resolve them.

    Returns:
        (clean_form_data, resolved_secrets)
        - clean_form_data: vault refs replaced with {{secret:key}} placeholders
        - resolved_secrets: {key: plaintext_value} for encryption into credentials
    """
    # Collect unique vault references
    refs = set()
    for value in form_data.values():
        if isinstance(value, str):
            refs.update(VAULT_REF.findall(value))

    if not refs:
        return form_data, {}

    # Batch resolve
    resolved = {}
    missing = []
    for ref_key in refs:
        value = await resolve_secret(db, ref_key)
        if value is not None:
            resolved[ref_key] = value
        else:
            missing.append(ref_key)

    if missing:
        from fastapi import HTTPException
        raise HTTPException(
            status_code=422,
            detail={
                "message": "Vault secrets not found",
                "missing_secrets": missing,
            },
        )

    # Replace {{vault:key}} with {{secret:key}} in form_data
    clean = {}
    for k, v in form_data.items():
        if isinstance(v, str) and "{{vault:" in v:
            clean[k] = VAULT_REF.sub(
                lambda m: f"{{{{secret:{m.group(1)}}}}}",
                v,
            )
        else:
            clean[k] = v

    return clean, resolved


async def resolve_vault_in_credentials(
    db: AsyncSession,
    creds: dict,
) -> dict:
    """Resolve {{vault:key}} references inside a flat credentials dict in place.

    Used for persona login credentials that are *linked* to vault secrets: the
    persona stores the reference (e.g. password = "{{vault:shopify_pw}}") so the
    link stays live, and we swap in the real plaintext at dispatch time.

    Raises HTTPException(422) if a referenced secret is missing.
    """
    if not creds:
        return creds
    resolved: dict = {}
    missing: List[str] = []
    out = dict(creds)
    for field, value in creds.items():
        if not isinstance(value, str) or "{{vault:" not in value:
            continue
        for ref_key in set(VAULT_REF.findall(value)):
            if ref_key not in resolved:
                v = await resolve_secret(db, ref_key)
                if v is None:
                    missing.append(ref_key)
                else:
                    resolved[ref_key] = v
        out[field] = VAULT_REF.sub(
            lambda m: resolved.get(m.group(1), m.group(0)), value
        )
    if missing:
        from fastapi import HTTPException
        raise HTTPException(
            status_code=422,
            detail={"message": "Linked vault secrets not found", "missing_secrets": missing},
        )
    return out


async def invalidate_cache(key: str = "") -> None:
    """Invalidate a cached secret (called on create/update/delete)."""
    redis = await _get_redis()
    if redis:
        try:
            await redis.delete(f"{CACHE_PREFIX}:{key}")
        except Exception:
            pass


async def invalidate_cache_deep(key: str = "", subfields: Optional[List[str]] = None) -> None:
    """Invalidate a secret's base cache key AND every derived sub-field key.

    ``resolve_secret`` caches sub-field references (e.g. ``vault:acct.password``)
    under their OWN cache keys, not under the base ``vault:acct`` key. So clearing
    only the base key on rotate/delete leaves stale sub-field values resolving for
    up to CACHE_TTL (finding #33). Callers pass the secret's stored field names
    (``username``/``password`` for credentials, the card fields for cards) so those
    derived keys are cleared too. Deleting keys that were never cached is a harmless
    no-op, so passing a superset of possible sub-fields is safe.
    """
    redis = await _get_redis()
    if not redis:
        return
    keys = [f"{CACHE_PREFIX}:{key}"]
    for sf in (subfields or []):
        keys.append(f"{CACHE_PREFIX}:{key}.{sf}")
    try:
        await redis.delete(*keys)
    except Exception:
        pass
