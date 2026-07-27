"""
Settings router - manage system configuration and API keys.
"""
import os
import logging
from typing import Optional
from datetime import datetime

from utils.http_client import http_session

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel

from config import settings as app_settings
from database import get_db
from models.config import Config
from security.api_key import get_current_api_key
from security.dependencies import require_platform_admin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings", tags=["settings"])


# NOTE: these endpoints read/write GLOBAL platform Config rows such as the
# shared OpenAI/Anthropic keys and recorder URL. Global config requires a
# verified platform admin (re-checked against the DB).
require_admin = require_platform_admin


def _encrypt_value(value: str) -> str:
    """Encrypt a sensitive value using Fernet symmetric encryption."""
    if not value:
        return ""
    from security.encryption import SecretEncryption
    return SecretEncryption.encrypt_secret(value)


def _decrypt_value(encrypted: str) -> str:
    """Decrypt a sensitive value using Fernet symmetric encryption.

    Fail-closed: when a master SECRET_ENCRYPTION_KEY IS configured but the value
    cannot be decrypted (rotation/corruption/wrong key), we must NOT silently
    return a usable secret or hide the failure — we log loudly and return "".
    The legacy "may be old XOR encoding" tolerance is only acceptable in pure
    local-dev where no master key is configured.
    """
    if not encrypted:
        return ""
    from security.encryption import SecretEncryption
    try:
        return SecretEncryption.decrypt_secret(encrypted)
    except Exception as e:
        if app_settings.secret_encryption_key:
            # Master key configured — a decrypt failure is a real error, not a
            # legacy-format quirk. Fail loud and closed (never return plaintext).
            logger.error(
                "Failed to decrypt config secret while SECRET_ENCRYPTION_KEY is "
                "configured (possible key rotation or corruption): %s", e
            )
            return ""
        if app_settings.environment.lower() == "development":
            logger.warning(
                "DEV ONLY: failed to decrypt config secret with no "
                "SECRET_ENCRYPTION_KEY configured - may be stored with old XOR "
                "encoding: %s", e
            )
            return ""
        # No master key but not dev: still fail closed and log loudly.
        logger.error("Failed to decrypt config secret (no SECRET_ENCRYPTION_KEY configured): %s", e)
        return ""


def _mask_api_key(key: str) -> str:
    """Mask API key for display (show only last 4 chars)."""
    if not key or len(key) < 8:
        return "****"
    return "*" * (len(key) - 4) + key[-4:]


# ============================================================
# Pydantic Models
# ============================================================

class APIKeySettings(BaseModel):
    openai_api_key: Optional[str] = None
    openai_api_key_masked: Optional[str] = None
    openai_model: str = "gpt-4"
    anthropic_api_key: Optional[str] = None
    anthropic_api_key_masked: Optional[str] = None


class UpdateAPIKeyRequest(BaseModel):
    key_name: str  # e.g., "openai_api_key", "anthropic_api_key"
    value: str


class UpdateSettingRequest(BaseModel):
    key: str
    value: str


class RecorderSettings(BaseModel):
    recorder_url: Optional[str] = None
    recorder_auth_token: Optional[str] = None


# ============================================================
# Admin Endpoints
# ============================================================

@router.get("/api-keys")
async def get_api_key_settings(
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(require_admin),
):
    """Get API key settings (masked for security)."""
    settings = {}

    # OpenAI
    result = await db.execute(select(Config).where(Config.key == "openai_api_key"))
    openai_config = result.scalar_one_or_none()
    if openai_config and openai_config.value:
        decrypted = _decrypt_value(openai_config.value.get("encrypted", ""))
        settings["openai_api_key_set"] = bool(decrypted)
        settings["openai_api_key_masked"] = _mask_api_key(decrypted) if decrypted else None
    else:
        settings["openai_api_key_set"] = False
        settings["openai_api_key_masked"] = None

    # OpenAI Model
    result = await db.execute(select(Config).where(Config.key == "openai_model"))
    model_config = result.scalar_one_or_none()
    settings["openai_model"] = model_config.value.get("value", "gpt-4") if model_config else "gpt-4"

    # Anthropic
    result = await db.execute(select(Config).where(Config.key == "anthropic_api_key"))
    anthropic_config = result.scalar_one_or_none()
    if anthropic_config and anthropic_config.value:
        decrypted = _decrypt_value(anthropic_config.value.get("encrypted", ""))
        settings["anthropic_api_key_set"] = bool(decrypted)
        settings["anthropic_api_key_masked"] = _mask_api_key(decrypted) if decrypted else None
    else:
        settings["anthropic_api_key_set"] = False
        settings["anthropic_api_key_masked"] = None

    # Anthropic model + base URL
    result = await db.execute(select(Config).where(Config.key == "anthropic_model"))
    cfg = result.scalar_one_or_none()
    settings["anthropic_model"] = cfg.value.get("value", "") if cfg else ""

    result = await db.execute(select(Config).where(Config.key == "anthropic_base_url"))
    cfg = result.scalar_one_or_none()
    settings["anthropic_base_url"] = cfg.value.get("value", "") if cfg else ""

    # OpenAI base URL
    result = await db.execute(select(Config).where(Config.key == "openai_base_url"))
    cfg = result.scalar_one_or_none()
    settings["openai_base_url"] = cfg.value.get("value", "") if cfg else ""

    # Recorder URL
    result = await db.execute(select(Config).where(Config.key == "recorder_url"))
    recorder_config = result.scalar_one_or_none()
    settings["recorder_url"] = recorder_config.value.get("value", "") if recorder_config else ""

    # Recorder auth token from environment (masked — never expose the full token)
    import os
    token = os.getenv("RECORDER_AUTH_TOKEN", "")
    if len(token) > 8:
        settings["recorder_auth_token"] = f"{token[:4]}...{token[-4:]}"
    elif token:
        settings["recorder_auth_token"] = "****"
    else:
        settings["recorder_auth_token"] = ""

    return settings


@router.post("/api-keys")
async def update_api_key(
    request: UpdateAPIKeyRequest,
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(require_admin),
):
    """Update an API key (encrypted storage)."""
    allowed_keys = ["openai_api_key", "anthropic_api_key"]

    if request.key_name not in allowed_keys:
        raise HTTPException(status_code=400, detail=f"Invalid key name. Allowed: {allowed_keys}")

    # Encrypt the value
    encrypted = _encrypt_value(request.value)

    # Upsert the config
    result = await db.execute(select(Config).where(Config.key == request.key_name))
    config = result.scalar_one_or_none()

    if config:
        config.value = {"encrypted": encrypted, "updated_at": datetime.utcnow().isoformat()}
    else:
        config = Config(
            key=request.key_name,
            value={"encrypted": encrypted, "updated_at": datetime.utcnow().isoformat()}
        )
        db.add(config)

    await db.commit()

    logger.info(f"API key updated: {request.key_name}")

    return {
        "success": True,
        "message": f"{request.key_name} updated successfully",
        "masked": _mask_api_key(request.value),
    }


@router.delete("/api-keys/{key_name}")
async def delete_api_key_setting(
    key_name: str,
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(require_admin),
):
    """Delete an API key."""
    allowed_keys = ["openai_api_key", "anthropic_api_key"]

    if key_name not in allowed_keys:
        raise HTTPException(status_code=400, detail=f"Invalid key name. Allowed: {allowed_keys}")

    result = await db.execute(select(Config).where(Config.key == key_name))
    config = result.scalar_one_or_none()

    if config:
        await db.delete(config)
        await db.commit()
        logger.info(f"API key deleted: {key_name}")

    return {"success": True, "message": f"{key_name} deleted"}


@router.post("/general")
async def update_general_setting(
    request: UpdateSettingRequest,
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(require_admin),
):
    """Update a general (non-sensitive) setting."""
    allowed_keys = ["openai_model", "openai_base_url", "anthropic_model", "anthropic_base_url", "recorder_url"]

    if request.key not in allowed_keys:
        raise HTTPException(status_code=400, detail=f"Invalid key. Allowed: {allowed_keys}")

    result = await db.execute(select(Config).where(Config.key == request.key))
    config = result.scalar_one_or_none()

    if config:
        config.value = {"value": request.value, "updated_at": datetime.utcnow().isoformat()}
    else:
        config = Config(
            key=request.key,
            value={"value": request.value, "updated_at": datetime.utcnow().isoformat()}
        )
        db.add(config)

    await db.commit()

    return {"success": True, "message": f"{request.key} updated"}


# NOTE: agents never fetch platform API keys from the backend. A BYO agent uses
# the owner's own keys configured locally, and otherwise routes LLM calls through
# the AI gateway (which holds the keys server-side). Handing the global plaintext
# OpenAI key to any authenticated agent would be an unnecessary exposure.


# ============================================================
# Public endpoint for checking if AI is configured
# ============================================================

@router.get("/ai-status")
async def get_ai_status(
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(get_current_api_key),
):
    """Check if AI services are configured."""
    result = await db.execute(select(Config).where(Config.key == "openai_api_key"))
    openai_config = result.scalar_one_or_none()

    openai_configured = bool(
        openai_config and
        openai_config.value and
        _decrypt_value(openai_config.value.get("encrypted", ""))
    )

    return {
        "openai_configured": openai_configured,
        "ai_available": openai_configured,
    }


# ============================================================
# AI Provider Config (Gateway providers)
# ============================================================

class AIProviderRequest(BaseModel):
    provider: str  # anthropic, openai, openrouter, local
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    model: Optional[str] = None
    is_active: bool = True
    priority: int = 0


class AIProviderResponse(BaseModel):
    id: int
    provider: str
    api_key_masked: Optional[str] = None
    base_url: Optional[str] = None
    model: Optional[str] = None
    is_active: bool
    priority: int

    class Config:
        from_attributes = True


async def _validate_provider_base_url(provider: str, base_url: Optional[str]) -> None:
    """Screen a custom provider base_url before it is stored.

    Every completion sends the DECRYPTED provider API key to this URL, so it is
    validated at WRITE time as well as at request time (services/local_ai.py posts
    through safe_fetch). Rejecting it here means a bad value never reaches the
    database, and the operator gets the error while they are looking at the form
    rather than as a failed run later.

    The ``local`` provider is exempt by design: it exists to reach an
    OpenAI-compatible server on the operator's own machine (Ollama, llama.cpp),
    so a private address is the whole point there.
    """
    if not base_url or provider == "local":
        return

    from services import url_policy

    verdict = await url_policy.check_url(base_url)
    if not verdict.allowed:
        raise HTTPException(
            status_code=400,
            detail=(
                (verdict.message or "base_url is not an allowed target.")
                + " Use a public https:// API endpoint, or select the 'local' "
                "provider for a server running on this machine."
            ),
        )


@router.get("/ai-providers")
async def list_ai_providers(
    db: AsyncSession = Depends(get_db),
    # GLOBAL provider config (single-owner coordinator) — require the owner (DB-verified)
    _user=Depends(require_platform_admin),
):
    """List configured AI providers (keys masked)."""
    from models.ai_provider_config import AIProviderConfig
    result = await db.execute(
        select(AIProviderConfig).order_by(AIProviderConfig.priority)
    )
    providers = result.scalars().all()
    return [
        AIProviderResponse(
            id=p.id,
            provider=p.provider,
            api_key_masked=_mask_api_key(_decrypt_value(p.api_key_encrypted)) if p.api_key_encrypted else None,
            base_url=p.base_url,
            model=p.model,
            is_active=p.is_active,
            priority=p.priority,
        )
        for p in providers
    ]


@router.post("/ai-providers")
async def create_ai_provider(
    req: AIProviderRequest,
    db: AsyncSession = Depends(get_db),
    # GLOBAL provider config (single-owner coordinator) — require the owner (DB-verified)
    _user=Depends(require_platform_admin),
):
    """Add a new AI provider configuration."""
    from models.ai_provider_config import AIProviderConfig
    from security.encryption import SecretEncryption

    valid_providers = {"anthropic", "openai", "openai_responses", "openrouter", "local"}
    if req.provider not in valid_providers:
        raise HTTPException(status_code=400, detail=f"Provider must be one of: {valid_providers}")

    await _validate_provider_base_url(req.provider, req.base_url)

    encrypted_key = None
    if req.api_key:
        encrypted_key = SecretEncryption.encrypt_secret(req.api_key)

    provider = AIProviderConfig(
        provider=req.provider,
        api_key_encrypted=encrypted_key,
        base_url=req.base_url,
        model=req.model,
        is_active=req.is_active,
        priority=req.priority,
    )
    db.add(provider)
    await db.commit()
    await db.refresh(provider)

    # Push updated config to AI gateway
    await _reload_gateway_config(db)

    return AIProviderResponse(
        id=provider.id,
        provider=provider.provider,
        api_key_masked=_mask_api_key(req.api_key) if req.api_key else None,
        base_url=provider.base_url,
        model=provider.model,
        is_active=provider.is_active,
        priority=provider.priority,
    )


@router.put("/ai-providers/{provider_id}")
async def update_ai_provider(
    provider_id: int,
    req: AIProviderRequest,
    db: AsyncSession = Depends(get_db),
    # GLOBAL provider config (single-owner coordinator) — require the owner (DB-verified)
    _user=Depends(require_platform_admin),
):
    """Update an AI provider configuration."""
    from models.ai_provider_config import AIProviderConfig
    from security.encryption import SecretEncryption

    result = await db.execute(
        select(AIProviderConfig).where(AIProviderConfig.id == provider_id)
    )
    provider = result.scalar_one_or_none()
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")

    await _validate_provider_base_url(req.provider, req.base_url)

    provider.provider = req.provider
    provider.base_url = req.base_url
    provider.model = req.model
    provider.is_active = req.is_active
    provider.priority = req.priority

    if req.api_key:
        provider.api_key_encrypted = SecretEncryption.encrypt_secret(req.api_key)

    await db.commit()
    await _reload_gateway_config(db)

    return {"status": "updated"}


@router.delete("/ai-providers/{provider_id}")
async def delete_ai_provider(
    provider_id: int,
    db: AsyncSession = Depends(get_db),
    # GLOBAL provider config (single-owner coordinator) — require the owner (DB-verified)
    _user=Depends(require_platform_admin),
):
    """Remove an AI provider configuration."""
    from models.ai_provider_config import AIProviderConfig
    result = await db.execute(
        select(AIProviderConfig).where(AIProviderConfig.id == provider_id)
    )
    provider = result.scalar_one_or_none()
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")

    await db.delete(provider)
    await db.commit()
    await _reload_gateway_config(db)

    return {"status": "deleted"}


@router.post("/ai-providers/test/{provider_id}")
async def test_ai_provider(
    provider_id: int,
    db: AsyncSession = Depends(get_db),
    # GLOBAL provider config (single-owner coordinator) — require the owner (DB-verified)
    _user=Depends(require_platform_admin),
):
    """Test an AI provider by sending a simple completion request."""
    from models.ai_provider_config import AIProviderConfig

    result = await db.execute(
        select(AIProviderConfig).where(AIProviderConfig.id == provider_id)
    )
    provider = result.scalar_one_or_none()
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")

    api_key = _decrypt_value(provider.api_key_encrypted) if provider.api_key_encrypted else None
    if not api_key and provider.provider != "local":
        raise HTTPException(status_code=400, detail="No API key configured for this provider")

    try:
        # Test THIS specific provider directly. Going through the AI gateway would
        # exercise the gateway's primary/fallback chain (i.e. whichever provider
        # has the lowest priority), not necessarily the row the admin clicked
        # "Test" on — which makes per-row testing misleading. So we hit the
        # provider's own endpoint directly here.
        #
        # SSRF (audit #14): the admin-supplied ``base_url`` is fetched and part of
        # the response is reflected back, so an unvalidated fetch is an internal
        # probe. Every branch now goes through ``safe_fetch.safe_fetch`` which
        # resolves + screens the host, pins the connection, and refuses redirects
        # into internal space. The cloud providers (anthropic/openai/openrouter)
        # must be public, so they use the default posture (private targets
        # blocked unless ALLOW_PRIVATE_TARGETS is set); the ``local`` provider
        # is definitionally on-box, so it explicitly opts into
        # ``allow_private=True``.
        from services import safe_fetch

        if provider.provider == "anthropic":
            resp = await safe_fetch.safe_fetch(
                provider.base_url or "https://api.anthropic.com/v1/messages",
                method="POST", timeout=30.0,
                json={"model": provider.model or "claude-sonnet-4-20250514", "max_tokens": 20,
                      "messages": [{"role": "user", "content": "Say hello in one word."}]},
                headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            )
            if resp.status_code == 200:
                data = resp.json()
                content = data.get("content", [{}])[0].get("text", "") if data.get("content") else ""
                return {"status": "ok", "response": content[:100], "model": data.get("model", provider.model)}
            return {"status": "error", "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}

        elif provider.provider in ("openai", "openrouter"):
            base = provider.base_url or ("https://openrouter.ai/api/v1" if provider.provider == "openrouter" else "https://api.openai.com/v1")
            resp = await safe_fetch.safe_fetch(
                f"{base.rstrip('/')}/chat/completions",
                method="POST", timeout=30.0,
                json={"model": provider.model or "gpt-4o-mini", "max_tokens": 20,
                      "messages": [{"role": "user", "content": "Say hello in one word."}]},
                headers={"Authorization": f"Bearer {api_key}", "content-type": "application/json"},
            )
            if resp.status_code == 200:
                data = resp.json()
                content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                return {"status": "ok", "response": content[:100], "model": data.get("model", provider.model)}
            return {"status": "error", "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}

        elif provider.provider == "openai_responses":
            # OpenAI-compatible endpoint that speaks the Responses API.
            base = provider.base_url or "https://api.openai.com/v1"
            resp = await safe_fetch.safe_fetch(
                f"{base.rstrip('/')}/responses",
                method="POST", timeout=30.0,
                json={"model": provider.model or "gpt-4o-mini", "max_output_tokens": 64,
                      "input": "Say hello in one word."},
                headers={"Authorization": f"Bearer {api_key}", "content-type": "application/json"},
            )
            if resp.status_code == 200:
                data = resp.json()
                content = data.get("output_text", "")
                if not content:
                    for item in data.get("output", []):
                        if item.get("type") == "message":
                            for block in item.get("content", []):
                                if block.get("type") == "output_text":
                                    content += block.get("text", "")
                return {"status": "ok", "response": content[:100], "model": data.get("model", provider.model)}
            return {"status": "error", "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}

        elif provider.provider == "local":
            base = provider.base_url or "http://localhost:11434"
            # The local model server is on-box by definition — permit private/
            # loopback targets for THIS branch only (still redirect-guarded).
            resp = await safe_fetch.safe_fetch(
                f"{base}/v1/chat/completions",
                method="POST", timeout=30.0, allow_private=True,
                json={"model": provider.model or "llama3", "max_tokens": 20,
                      "messages": [{"role": "user", "content": "Say hello in one word."}]},
                headers={"content-type": "application/json"},
            )
            if resp.status_code == 200:
                data = resp.json()
                content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                return {"status": "ok", "response": content[:100], "model": data.get("model", provider.model)}
            return {"status": "error", "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}

        else:
            return {"status": "error", "error": f"Unknown provider: {provider.provider}"}

    except Exception as e:
        return {"status": "error", "error": str(e)}


async def _reload_gateway_config(db: AsyncSession):
    """Push current provider configs to the AI gateway for hot-reload."""
    from models.ai_provider_config import AIProviderConfig
    import httpx

    result = await db.execute(
        select(AIProviderConfig).where(AIProviderConfig.is_active == True).order_by(AIProviderConfig.priority)
    )
    providers = result.scalars().all()

    rows = []
    for p in providers:
        api_key = _decrypt_value(p.api_key_encrypted) if p.api_key_encrypted else ""
        rows.append({
            "provider": p.provider,
            "api_key": api_key,
            "base_url": p.base_url,
            "model": p.model,
            "is_active": p.is_active,
            "priority": p.priority,
        })

    gateway_url = os.getenv("AI_GATEWAY_URL", "http://localhost:8090")
    gateway_secret = os.getenv("GATEWAY_SECRET", "")

    try:
        async with http_session() as client:
            await client.post(
                f"{gateway_url}/v1/config/reload",
                json={"providers": rows},
                headers={"X-Gateway-Secret": gateway_secret} if gateway_secret else {},
                timeout=10.0,
            )
        logger.info(f"AI gateway config reloaded with {len(rows)} provider(s)")
    except Exception as e:
        logger.warning(f"Failed to reload AI gateway config: {e}")


# ---------------------------------------------------------------------------
# Platform-global AI output-token limits (cost control).
#
# A cap is a CEILING applied per feature bucket: the effective budget for an AI
# call is min(built-in default, cap). Missing/null = "use the built-in default".
# These apply platform-wide, so require a platform admin.
# ---------------------------------------------------------------------------
class AILimitsRequest(BaseModel):
    # Present-but-null clears the bucket's cap; omitted leaves it unchanged.
    assist: Optional[int] = None
    agent: Optional[int] = None
    optimize: Optional[int] = None
    repair: Optional[int] = None


@router.get("/ai-limits")
async def get_ai_limits(
    db: AsyncSession = Depends(get_db),
    _user=Depends(require_platform_admin),
):
    """Return the current per-bucket output-token caps and their built-in defaults."""
    from services import ai_limits

    return {
        "limits": await ai_limits.get_limits(db),
        "defaults": ai_limits.DEFAULTS,
    }


@router.put("/ai-limits")
async def update_ai_limits(
    request: AILimitsRequest,
    db: AsyncSession = Depends(get_db),
    _user=Depends(require_platform_admin),
):
    """Set per-bucket output-token caps. Only provided buckets are changed; a
    bucket sent as null clears its cap (reverts to the built-in default)."""
    from services import ai_limits

    updates = request.model_dump(exclude_unset=True)
    limits = await ai_limits.set_limits(db, updates)
    return {"limits": limits, "defaults": ai_limits.DEFAULTS}
