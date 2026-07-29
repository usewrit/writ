"""
Webhook router for notifications and triggers.

Handles:
- Outgoing webhook configuration (recipients for notifications)
- Incoming webhook triggers (external systems triggering workflows)
"""
import hashlib
import hmac
import json
import logging
import secrets
from datetime import datetime, timezone

from utils.redis_client import get_redis
from typing import List, Optional, Dict, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Header, Query, status
from pydantic import BaseModel, Field, HttpUrl
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from security.api_key import get_current_api_key
from security.encryption import SecretEncryption

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


# ============================================
# Pydantic Models
# ============================================

class WebhookConfigUpdate(BaseModel):
    enabled: Optional[bool] = None
    default_timeout: Optional[int] = None
    default_retries: Optional[int] = None
    default_method: Optional[str] = None
    global_secret: Optional[str] = None
    rate_limit_enabled: Optional[bool] = None
    rate_limit_per_minute: Optional[int] = None


class WebhookRecipientCreate(BaseModel):
    name: str
    url: str
    enabled: bool = True
    method: str = "POST"
    secret: Optional[str] = None
    headers: Optional[Dict[str, str]] = None
    event_types: Optional[List[str]] = None
    timeout: Optional[int] = None
    max_retries: Optional[int] = None
    include_diff: bool = True
    include_content: bool = False


class WebhookRecipientUpdate(BaseModel):
    name: Optional[str] = None
    url: Optional[str] = None
    enabled: Optional[bool] = None
    method: Optional[str] = None
    secret: Optional[str] = None
    headers: Optional[Dict[str, str]] = None
    event_types: Optional[List[str]] = None
    timeout: Optional[int] = None
    max_retries: Optional[int] = None
    include_diff: Optional[bool] = None
    include_content: Optional[bool] = None


class WebhookTriggerCreate(BaseModel):
    name: str
    enabled: bool = True
    secret: Optional[str] = None
    workflow_id: Optional[int] = None
    target_id: Optional[int] = None
    action: str = "run_workflow"
    payload_mapping: Optional[Dict[str, str]] = None
    conditions: Optional[Dict[str, Any]] = None
    wait_for_result: bool = False
    wait_timeout: int = 120
    # Segments joined by `/`, matching the column (String(100)) and what the
    # API-recorder writes (`{prefix}/{function_name}`). The old single-segment
    # pattern (max 64, no `/`) could not express the paths this coordinator mints
    # itself, so a user could not create — or repair — one through the API.
    #
    # The regex is the traversal guard: no `.`, no empty segment, no leading or
    # trailing slash. `{custom_path:path}` on the route captures greedily, so the
    # stored value is the only thing keeping a path from being ambiguous.
    custom_path: Optional[str] = Field(
        None, max_length=100, pattern=r'^[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+)*$'
    )


class WebhookTriggerUpdate(BaseModel):
    name: Optional[str] = None
    enabled: Optional[bool] = None
    secret: Optional[str] = None
    workflow_id: Optional[int] = None
    target_id: Optional[int] = None
    action: Optional[str] = None
    payload_mapping: Optional[Dict[str, str]] = None
    conditions: Optional[Dict[str, Any]] = None
    wait_for_result: Optional[bool] = None
    wait_timeout: Optional[int] = None
    # Segments joined by `/`, matching the column (String(100)) and what the
    # API-recorder writes (`{prefix}/{function_name}`). The old single-segment
    # pattern (max 64, no `/`) could not express the paths this coordinator mints
    # itself, so a user could not create — or repair — one through the API.
    #
    # The regex is the traversal guard: no `.`, no empty segment, no leading or
    # trailing slash. `{custom_path:path}` on the route captures greedily, so the
    # stored value is the only thing keeping a path from being ambiguous.
    custom_path: Optional[str] = Field(
        None, max_length=100, pattern=r'^[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+)*$'
    )


# ============================================
# Webhook Config Endpoints
# ============================================

@router.get("/config")
async def get_webhook_config(
    db: AsyncSession = Depends(get_db),
    api_key = Depends(get_current_api_key),
):
    """Get webhook configuration."""
    from models.webhook_config import WebhookConfig

    result = await db.execute(
        select(WebhookConfig)
    )
    config = result.scalar_one_or_none()

    if not config:
        # Create the default config row.
        config = WebhookConfig(enabled=False)
        db.add(config)
        await db.commit()
        await db.refresh(config)

    return {
        "enabled": config.enabled,
        "default_timeout": config.default_timeout,
        "default_retries": config.default_retries,
        "default_method": config.default_method,
        "has_global_secret": bool(config.global_secret),
        "rate_limit_enabled": config.rate_limit_enabled,
        "rate_limit_per_minute": config.rate_limit_per_minute,
    }


@router.put("/config")
async def update_webhook_config(
    config_update: WebhookConfigUpdate,
    db: AsyncSession = Depends(get_db),
    api_key = Depends(get_current_api_key),
):
    """Update webhook configuration."""
    role = api_key.get("role", "").lower() if isinstance(api_key, dict) else ""
    if role not in ("admin", "owner"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin role required to modify webhook config")

    from models.webhook_config import WebhookConfig

    result = await db.execute(
        select(WebhookConfig)
    )
    config = result.scalar_one_or_none()

    if not config:
        config = WebhookConfig()
        db.add(config)

    # Update fields
    for field, value in config_update.dict(exclude_unset=True).items():
        if field == "global_secret" and value:
            value = SecretEncryption.encrypt_secret(value)
        setattr(config, field, value)

    await db.commit()
    await db.refresh(config)

    return {"success": True, "message": "Webhook config updated"}


# ============================================
# Webhook Recipients (Outgoing) Endpoints
# ============================================

@router.get("/recipients")
async def list_webhook_recipients(
    db: AsyncSession = Depends(get_db),
    api_key = Depends(get_current_api_key),
):
    """List webhook recipients."""
    from models.webhook_recipient import WebhookRecipient

    result = await db.execute(
        select(WebhookRecipient)
        .order_by(WebhookRecipient.name)
    )
    recipients = result.scalars().all()

    return {"recipients": [r.to_dict() for r in recipients]}


@router.post("/recipients")
async def create_webhook_recipient(
    recipient: WebhookRecipientCreate,
    db: AsyncSession = Depends(get_db),
    api_key = Depends(get_current_api_key),
):
    """Create a new webhook recipient."""
    from security.validation import InputValidator
    InputValidator.validate_url_with_dns(recipient.url, allow_private=False)

    from models.webhook_recipient import WebhookRecipient

    from services import domain_guard
    await domain_guard.enforce(db, recipient.url, actor=f"apikey:{api_key.get('id')}")

    # robots.txt posture: no-op unless opted in. Backend-only.
    from services import robots_guard
    await robots_guard.enforce(
        recipient.url,
        await robots_guard.respect_robots_for_tenant(db),
    )

    encrypted_recipient_secret = SecretEncryption.encrypt_secret(recipient.secret) if recipient.secret else None
    new_recipient = WebhookRecipient(
        name=recipient.name,
        url=recipient.url,
        enabled=recipient.enabled,
        method=recipient.method,
        secret=encrypted_recipient_secret,
        headers=recipient.headers,
        event_types=recipient.event_types,
        timeout=recipient.timeout,
        max_retries=recipient.max_retries,
        include_diff=recipient.include_diff,
        include_content=recipient.include_content,
    )

    db.add(new_recipient)
    await db.commit()
    await db.refresh(new_recipient)

    return {"success": True, "recipient": new_recipient.to_dict()}


@router.get("/recipients/{recipient_id}")
async def get_webhook_recipient(
    recipient_id: int,
    db: AsyncSession = Depends(get_db),
    api_key = Depends(get_current_api_key),
):
    """Get a specific webhook recipient."""
    from models.webhook_recipient import WebhookRecipient

    result = await db.execute(
        select(WebhookRecipient).where(WebhookRecipient.id == recipient_id)
    )
    recipient = result.scalar_one_or_none()

    if not recipient:
        raise HTTPException(status_code=404, detail="Webhook recipient not found")

    return recipient.to_dict()


@router.put("/recipients/{recipient_id}")
async def update_webhook_recipient(
    recipient_id: int,
    update: WebhookRecipientUpdate,
    db: AsyncSession = Depends(get_db),
    api_key = Depends(get_current_api_key),
):
    """Update a webhook recipient."""
    if update.url:
        from security.validation import InputValidator
        InputValidator.validate_url_with_dns(update.url, allow_private=False)
        from services import domain_guard
        await domain_guard.enforce(db, update.url, actor=f"apikey:{api_key.get('id')}")
        # robots.txt posture: no-op unless opted in.
        from services import robots_guard
        await robots_guard.enforce(
            update.url,
            await robots_guard.respect_robots_for_tenant(db),
        )

    from models.webhook_recipient import WebhookRecipient

    result = await db.execute(
        select(WebhookRecipient).where(WebhookRecipient.id == recipient_id)
    )
    recipient = result.scalar_one_or_none()

    if not recipient:
        raise HTTPException(status_code=404, detail="Webhook recipient not found")

    ALLOWED_UPDATE_FIELDS = {"name", "url", "enabled", "method", "secret", "headers", "event_types", "timeout", "max_retries", "include_diff", "include_content"}
    for field, value in update.dict(exclude_unset=True).items():
        if field not in ALLOWED_UPDATE_FIELDS:
            continue
        if field == "secret" and value:
            value = SecretEncryption.encrypt_secret(value)
        setattr(recipient, field, value)

    await db.commit()
    await db.refresh(recipient)

    return {"success": True, "recipient": recipient.to_dict()}


@router.delete("/recipients/{recipient_id}")
async def delete_webhook_recipient(
    recipient_id: int,
    db: AsyncSession = Depends(get_db),
    api_key = Depends(get_current_api_key),
):
    """Delete a webhook recipient."""
    role = api_key.get("role", "").lower() if isinstance(api_key, dict) else ""
    if role not in ["operator", "admin"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Requires operator role or higher")

    from models.webhook_recipient import WebhookRecipient

    result = await db.execute(
        select(WebhookRecipient).where(WebhookRecipient.id == recipient_id)
    )
    recipient = result.scalar_one_or_none()

    if not recipient:
        raise HTTPException(status_code=404, detail="Webhook recipient not found")

    await db.delete(recipient)
    await db.commit()

    return {"success": True, "message": "Webhook recipient deleted"}


@router.post("/recipients/{recipient_id}/test")
async def test_webhook_recipient(
    recipient_id: int,
    db: AsyncSession = Depends(get_db),
    api_key = Depends(get_current_api_key),
):
    """Send a test webhook to the recipient."""
    from models.webhook_recipient import WebhookRecipient
    from models.webhook_config import WebhookConfig
    from notifications.webhook import WebhookNotifier
    from config import settings as app_settings

    result = await db.execute(
        select(WebhookRecipient).where(WebhookRecipient.id == recipient_id)
    )
    recipient = result.scalar_one_or_none()

    if not recipient:
        raise HTTPException(status_code=404, detail="Webhook recipient not found")

    # Re-validate stored URL before sending (SSRF protection).
    # Use DNS-resolving validation so a hostname that resolves to a
    # private/internal IP is blocked (consistent with create/update paths).
    # WebhookNotifier uses httpx with follow_redirects=False (default), so a
    # 3xx to an internal host is not followed; the destination IP is checked
    # here at send time.
    from security.validation import InputValidator
    InputValidator.validate_url_with_dns(recipient.url, allow_private=app_settings.allow_private_targets)

    config_result = await db.execute(select(WebhookConfig).limit(1))
    config = config_result.scalar_one_or_none()

    notifier = WebhookNotifier(
        timeout=recipient.timeout or (config.default_timeout if config else 30),
        max_retries=1,  # Only try once for test
    )

    test_payload = {
        "event": "test",
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "message": "This is a test webhook from Writ",
        "recipient_id": recipient.id,
        "recipient_name": recipient.name,
    }

    signing_secret = None
    raw_secret = recipient.secret or (config.global_secret if config else None)
    if raw_secret:
        try:
            signing_secret = SecretEncryption.decrypt_secret(raw_secret)
        except Exception:
            signing_secret = raw_secret

    result = await notifier.send_webhook(
        url=recipient.url,
        payload=test_payload,
        secret=signing_secret,
        headers=recipient.headers,
        method=recipient.method,
    )

    return {
        "success": result.get("success", False),
        "status_code": result.get("status_code"),
        "error": result.get("error"),
        "response": result.get("response"),
    }


# ============================================
# Webhook Triggers (Incoming) Endpoints
# ============================================

@router.get("/triggers")
async def list_webhook_triggers(
    db: AsyncSession = Depends(get_db),
    api_key = Depends(get_current_api_key),
):
    """List all webhook triggers."""
    from models.webhook_trigger import WebhookTrigger

    result = await db.execute(
        select(WebhookTrigger).order_by(WebhookTrigger.name)
    )
    triggers = result.scalars().all()

    return {"triggers": [t.to_dict() for t in triggers]}


async def _verify_trigger_refs(db: AsyncSession, workflow_id, target_id):
    """Ensure any referenced workflow/target actually exists (404 otherwise)."""
    from models.automation_workflow import AutomationWorkflow
    from models.target import Target

    if workflow_id is not None:
        owned = await db.execute(
            select(AutomationWorkflow.id).where(
                AutomationWorkflow.id == workflow_id,
            )
        )
        if owned.scalar_one_or_none() is None:
            raise HTTPException(status_code=404, detail="Workflow not found")

    if target_id is not None:
        owned = await db.execute(
            select(Target.id).where(
                Target.id == target_id,
            )
        )
        if owned.scalar_one_or_none() is None:
            raise HTTPException(status_code=404, detail="Target not found")


@router.post("/triggers")
async def create_webhook_trigger(
    trigger: WebhookTriggerCreate,
    db: AsyncSession = Depends(get_db),
    api_key = Depends(get_current_api_key),
):
    """Create a new webhook trigger."""
    from sqlalchemy.exc import IntegrityError
    from models.webhook_trigger import WebhookTrigger

    # Normalize empty custom_path to None to avoid unique constraint issues
    custom_path = trigger.custom_path.strip() if trigger.custom_path else None
    if custom_path == "":
        custom_path = None

    # Auto-generate secret if not provided, then encrypt for storage
    trigger_secret = trigger.secret if trigger.secret else secrets.token_urlsafe(32)
    encrypted_secret = SecretEncryption.encrypt_secret(trigger_secret)

    # Reject references to non-existent workflows/targets.
    await _verify_trigger_refs(db, trigger.workflow_id, trigger.target_id)

    new_trigger = WebhookTrigger(
        name=trigger.name,
        enabled=trigger.enabled,
        secret=encrypted_secret,
        workflow_id=trigger.workflow_id,
        target_id=trigger.target_id,
        action=trigger.action,
        payload_mapping=trigger.payload_mapping,
        conditions=trigger.conditions,
        wait_for_result=trigger.wait_for_result,
        wait_timeout=trigger.wait_timeout,
        custom_path=custom_path,
    )

    try:
        db.add(new_trigger)
        await db.commit()
        await db.refresh(new_trigger)
    except IntegrityError as e:
        await db.rollback()
        error_msg = str(e.orig) if e.orig else str(e)
        if "custom_path" in error_msg:
            raise HTTPException(status_code=400, detail=f"Custom path '{custom_path}' is already in use")
        raise HTTPException(status_code=400, detail="A webhook trigger with these values already exists")

    return {"success": True, "trigger": new_trigger.to_dict()}


@router.get("/triggers/{trigger_id}")
async def get_webhook_trigger(
    trigger_id: int,
    db: AsyncSession = Depends(get_db),
    api_key = Depends(get_current_api_key),
):
    """Get a specific webhook trigger."""
    from models.webhook_trigger import WebhookTrigger

    result = await db.execute(
        select(WebhookTrigger).where(WebhookTrigger.id == trigger_id)
    )
    trigger = result.scalar_one_or_none()

    if not trigger:
        raise HTTPException(status_code=404, detail="Webhook trigger not found")

    return trigger.to_dict()


@router.put("/triggers/{trigger_id}")
async def update_webhook_trigger(
    trigger_id: int,
    update: WebhookTriggerUpdate,
    db: AsyncSession = Depends(get_db),
    api_key = Depends(get_current_api_key),
):
    """Update a webhook trigger."""
    from models.webhook_trigger import WebhookTrigger

    result = await db.execute(
        select(WebhookTrigger).where(WebhookTrigger.id == trigger_id)
    )
    trigger = result.scalar_one_or_none()

    if not trigger:
        raise HTTPException(status_code=404, detail="Webhook trigger not found")

    # Prevent clearing the secret (HMAC verification bypass)
    if "secret" in update.dict(exclude_unset=True) and not update.secret:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Webhook trigger secret cannot be empty"
        )

    ALLOWED_TRIGGER_FIELDS = {"name", "enabled", "secret", "workflow_id", "target_id", "action", "payload_mapping", "conditions", "wait_for_result", "wait_timeout", "custom_path"}
    updates = update.dict(exclude_unset=True)

    # If the caller is re-pointing the trigger, validate the effective
    # (post-update) references still exist.
    if "workflow_id" in updates or "target_id" in updates:
        await _verify_trigger_refs(
            db,
            updates.get("workflow_id", trigger.workflow_id),
            updates.get("target_id", trigger.target_id),
        )

    for field, value in updates.items():
        if field not in ALLOWED_TRIGGER_FIELDS:
            continue
        if field == "secret" and value:
            value = SecretEncryption.encrypt_secret(value)
        setattr(trigger, field, value)

    await db.commit()
    await db.refresh(trigger)

    return {"success": True, "trigger": trigger.to_dict()}


@router.delete("/triggers/{trigger_id}")
async def delete_webhook_trigger(
    trigger_id: int,
    db: AsyncSession = Depends(get_db),
    api_key = Depends(get_current_api_key),
):
    """Delete a webhook trigger."""
    role = api_key.get("role", "").lower() if isinstance(api_key, dict) else ""
    if role not in ["operator", "admin"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Requires operator role or higher")

    from models.webhook_trigger import WebhookTrigger

    result = await db.execute(
        select(WebhookTrigger).where(WebhookTrigger.id == trigger_id)
    )
    trigger = result.scalar_one_or_none()

    if not trigger:
        raise HTTPException(status_code=404, detail="Webhook trigger not found")

    await db.delete(trigger)
    await db.commit()

    return {"success": True, "message": "Webhook trigger deleted"}


# ============================================
# Incoming Webhook Endpoint (No Auth - Uses Secret)
# ============================================

def verify_signature(payload: bytes, signature: str, secret: str, timestamp: str = "") -> bool:
    """Verify HMAC signature of incoming webhook.

    When ``timestamp`` is supplied (sender included X-Writ-Timestamp),
    it must be part of the signed material so the signature is bound to that
    moment, enabling replay-window enforcement by the caller.
    """
    if not signature or not secret:
        return False

    # Handle "sha256=xxx" format
    if signature.startswith("sha256="):
        signature = signature[7:]

    signed = (f"{timestamp}.".encode("utf-8") + payload) if timestamp else payload
    expected = hmac.new(
        secret.encode('utf-8'),
        signed,
        hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(signature.lower(), expected.lower())


def extract_value_from_path(data: dict, path: str) -> Any:
    """Extract value from nested dict using JSONPath-like syntax.

    Examples:
        $.data.user.email -> data["data"]["user"]["email"]
        $.items[0].name -> data["items"][0]["name"]
    """
    if not path.startswith("$."):
        return data.get(path)

    path = path[2:]  # Remove "$."
    current = data

    for part in path.split("."):
        if not current:
            return None

        # Handle array indexing
        if "[" in part:
            key, idx = part.split("[")
            idx = int(idx.rstrip("]"))
            current = current.get(key, [])
            if isinstance(current, list) and len(current) > idx:
                current = current[idx]
            else:
                return None
        else:
            current = current.get(part) if isinstance(current, dict) else None

    return current


@router.post("/hook/{token}")
async def handle_incoming_webhook_by_token(
    token: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    x_writ_signature: Optional[str] = Header(None),
    x_hub_signature_256: Optional[str] = Header(None),  # GitHub style
    wait: Optional[bool] = Query(None, description="Wait for workflow completion and return extracted data"),
    timeout: Optional[int] = Query(None, ge=10, le=300, description="Max wait time in seconds (default 120)"),
):
    # Rate limit by token (max 30 calls per minute per token)
    try:
        from security.rate_limit import RateLimiter
        _redis = get_redis()  # shared pooled client (do not close)
        limiter = RateLimiter(_redis, max_requests=30, window_seconds=60, prefix="webhook_rl")
        allowed, info = await limiter.is_allowed(token)
        if not allowed:
            raise HTTPException(status_code=429, detail=f"Webhook rate limit exceeded. Retry after {info.get('reset_at', 60)}s")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Webhook rate limit check failed: {e}")
        raise HTTPException(status_code=503, detail="Service temporarily unavailable")
    """
    Handle incoming webhook trigger using secure token URL.

    This endpoint does NOT require API key authentication.
    Instead, it uses HMAC signature verification if a secret is configured.

    Add ?wait=true to wait for the workflow to complete and get extracted data back.
    Add &timeout=60 to set max wait time (default 120s, max 300s).
    """
    from models.webhook_trigger import WebhookTrigger

    # Get the trigger by token
    result = await db.execute(
        select(WebhookTrigger).where(WebhookTrigger.token == token)
    )
    trigger = result.scalar_one_or_none()

    if not trigger:
        raise HTTPException(status_code=404, detail="Webhook not found")

    result = await _process_webhook(trigger, request, db, x_writ_signature, x_hub_signature_256)

    # Determine if we should wait: explicit param overrides, else use trigger default
    should_wait = wait if wait is not None else getattr(trigger, 'wait_for_result', False)

    if should_wait and result.get("triggered") and result.get("data", {}).get("task_id"):
        task_id = result["data"]["task_id"]
        wait_timeout = timeout or getattr(trigger, 'wait_timeout', 120) or 120

        import asyncio
        from models.automation_task import AutomationTask

        elapsed = 0
        poll_interval = 2
        while elapsed < wait_timeout:
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

            task_result = await db.execute(
                select(AutomationTask).where(AutomationTask.id == task_id)
            )
            task = task_result.scalar_one_or_none()

            if task and task.status in ("success", "failed", "cancelled"):
                result["status"] = task.status
                result["success"] = task.success
                result["result_data"] = task.result_data
                result["extracted_data"] = (task.result_data or {}).get("extracted_data")
                result["error"] = task.error_message
                result["duration_ms"] = task.duration_ms
                return result

        result["status"] = "timeout"
        result["error"] = f"Task did not complete within {wait_timeout}s"

    return result


@router.post("/trigger/{trigger_id}")
async def handle_incoming_webhook(
    trigger_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    x_writ_signature: Optional[str] = Header(None),
    x_hub_signature_256: Optional[str] = Header(None),  # GitHub style
    wait: Optional[bool] = Query(None),
    timeout: Optional[int] = Query(None, ge=10, le=300),
):
    """
    Handle incoming webhook trigger by ID (deprecated, use /hook/{token} instead).

    Add ?wait=true to wait for completion and get extracted data.
    """
    from models.webhook_trigger import WebhookTrigger

    # Get the trigger by ID
    result = await db.execute(
        select(WebhookTrigger).where(WebhookTrigger.id == trigger_id)
    )
    trigger = result.scalar_one_or_none()

    if not trigger:
        raise HTTPException(status_code=404, detail="Webhook trigger not found")

    result = await _process_webhook(trigger, request, db, x_writ_signature, x_hub_signature_256)

    if wait and result.get("triggered") and result.get("data", {}).get("task_id"):
        task_id = result["data"]["task_id"]
        wait_timeout = timeout or 120

        import asyncio
        from models.automation_task import AutomationTask

        elapsed = 0
        while elapsed < wait_timeout:
            await asyncio.sleep(2)
            elapsed += 2
            task_result = await db.execute(
                select(AutomationTask).where(AutomationTask.id == task_id)
            )
            task = task_result.scalar_one_or_none()
            if task and task.status in ("success", "failed", "cancelled"):
                result["status"] = task.status
                result["success"] = task.success
                result["result_data"] = task.result_data
                result["extracted_data"] = (task.result_data or {}).get("extracted_data")
                result["error"] = task.error_message
                result["duration_ms"] = task.duration_ms
                return result

        result["status"] = "timeout"
        result["error"] = f"Task did not complete within {wait_timeout}s"

    return result


# ---------------------------------------------------------------------------
# Custom-path webhooks: POST /api/v1/webhooks/{custom_path}
#
# A SECOND router, because this path does not live under this module's `/webhooks`
# prefix — the shape `/api/v1/webhooks/...` is what `WebhookTrigger.custom_path`
# has always documented and what the API-recorder hands back to callers. Until now
# no route served it, so every custom_path URL this coordinator minted 404'd.
#
# Auth differs from `/hook/{token}` on purpose. A token is unguessable, so an HMAC
# signature can carry that route. A custom_path is HUMAN-CHOSEN and guessable
# ("myapp/getUser"), so it cannot be its own credential: this route requires an API
# key with `workflows:execute` and fails closed without one. A signature remains
# optional here and is still verified when presented.
# ---------------------------------------------------------------------------

# Self-prefixed with the ABSOLUTE path, matching the house convention for the /api/v1
# surface (routers/files.py `v1_router`, routers/local_workflows.py) — mounted with no
# prefix in main.py. Absolute so that grepping "/api/v1/webhooks" finds the route.
custom_path_router = APIRouter(tags=["webhooks"])


@custom_path_router.post("/api/v1/webhooks/{custom_path:path}")
async def handle_incoming_webhook_by_custom_path(
    custom_path: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    api_key=Depends(get_current_api_key),
    x_writ_signature: Optional[str] = Header(None),
    x_hub_signature_256: Optional[str] = Header(None),
    wait: Optional[bool] = Query(None, description="Wait for workflow completion and return extracted data"),
    timeout: Optional[int] = Query(None, ge=10, le=300, description="Max wait time in seconds (default 120)"),
):
    """Run the workflow behind a webhook trigger's custom path.

    The friendly twin of ``POST /api/webhooks/hook/{token}``: same execution path
    (``_process_webhook``), addressed by the readable path the trigger was created
    with instead of an opaque token.

    ``?wait=true`` blocks for the workflow's extracted data (default from the
    trigger's own ``wait_for_result``); ``&timeout=`` bounds that wait.
    """
    from models.webhook_trigger import WebhookTrigger
    from security.dependencies import check_api_key_scope

    # Check the FIRING scope before we disclose whether a path exists — otherwise a
    # read-scoped key could enumerate the owner's endpoints by watching 404 vs 403.
    # `triggers:execute` matches the sibling `/api/webhooks/trigger/{id}` route; the
    # per-workflow pin and run budgets are enforced below, once the target is known.
    if isinstance(api_key, dict):
        check_api_key_scope(api_key, "triggers", "execute")

    # Normalize: tolerate a leading/trailing slash from a hand-typed URL, since
    # `{custom_path:path}` captures them verbatim and stored paths carry neither.
    lookup = (custom_path or "").strip("/")
    if not lookup:
        raise HTTPException(status_code=404, detail="Webhook not found")

    trigger = (
        await db.execute(
            select(WebhookTrigger).where(WebhookTrigger.custom_path == lookup)
        )
    ).scalar_one_or_none()
    if not trigger:
        raise HTTPException(status_code=404, detail=f"Webhook '{lookup}' not found")

    # Rate limit per PATH, mirroring the token route's 30/min. Keyed by the resolved
    # trigger id rather than the raw path so two spellings of the same endpoint
    # (trailing slash, different case in a query) share one budget.
    try:
        from security.rate_limit import RateLimiter
        _redis = get_redis()  # shared pooled client (do not close)
        limiter = RateLimiter(_redis, max_requests=30, window_seconds=60, prefix="webhook_rl")
        allowed, info = await limiter.is_allowed(f"custom:{trigger.id}")
        if not allowed:
            raise HTTPException(
                status_code=429,
                detail=f"Webhook rate limit exceeded. Retry after {info.get('reset_at', 60)}s",
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Webhook rate limit check failed: {e}")
        raise HTTPException(status_code=503, detail="Service temporarily unavailable")

    # Per-key authorization + run budgets, exactly as the direct run endpoint applies
    # them. Without this the custom-path webhook would be a way to run a workflow the
    # key is not scoped to, and to run it past the key's own execution limit.
    key_obj = api_key.get("obj") if isinstance(api_key, dict) else None
    if key_obj is not None and trigger.workflow_id:
        from services.api_key_scope_resolver import key_can_run_workflow, enforce_key_run_limits
        if not await key_can_run_workflow(db, key_obj, trigger.workflow_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="API key cannot run this workflow",
            )
        await enforce_key_run_limits(db, key_obj)

    result = await _process_webhook(
        trigger, request, db, x_writ_signature, x_hub_signature_256,
        # The API key above IS the authentication for this route; a signature is
        # optional here (and still verified when one is sent).
        require_signature=False,
    )

    should_wait = wait if wait is not None else getattr(trigger, "wait_for_result", False)

    if should_wait and result.get("triggered") and result.get("data", {}).get("task_id"):
        task_id = result["data"]["task_id"]
        wait_timeout = timeout or getattr(trigger, "wait_timeout", 120) or 120

        import asyncio
        from models.automation_task import AutomationTask

        elapsed = 0
        poll_interval = 2
        while elapsed < wait_timeout:
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval
            # The run converges in a DIFFERENT session (the dispatch worker), so the
            # identity map has to be dropped or this loop re-reads its own stale row
            # forever and never observes the task finishing.
            db.expire_all()
            task = (
                await db.execute(select(AutomationTask).where(AutomationTask.id == task_id))
            ).scalar_one_or_none()

            if task and task.status in ("success", "failed", "cancelled"):
                result["status"] = task.status
                result["success"] = task.success
                result["result_data"] = task.result_data
                result["extracted_data"] = (task.result_data or {}).get("extracted_data")
                result["error"] = task.error_message
                result["duration_ms"] = task.duration_ms
                return result

        result["status"] = "timeout"
        result["error"] = f"Task did not complete within {wait_timeout}s"

    return result


def _verify_webhook_signature(trigger, request: Request, body: bytes, signature: str) -> None:
    """Verify a presented HMAC signature, or raise 401.

    SECURITY (#19): replay protection is MANDATORY. The sender must include a signed
    ``X-Writ-Timestamp`` header; it must be fresh (±5 min) and is bound into the HMAC
    (see :func:`verify_signature`) so a captured body-only signature cannot be replayed
    forever. A missing timestamp is rejected (fail closed) rather than falling back to
    body-only verification.
    """
    if not trigger.secret:
        # A signed request we cannot check is not "probably fine" — it is a caller
        # believing they are authenticated when nothing verified them.
        logger.warning(
            f"Webhook trigger {trigger.id} received a signature but has no secret to verify it"
        )
        raise HTTPException(
            status_code=401,
            detail="Webhook trigger has no signing secret configured",
        )
    try:
        decrypted_secret = SecretEncryption.decrypt_secret(trigger.secret)
    except Exception:
        decrypted_secret = trigger.secret

    timestamp = request.headers.get("X-Writ-Timestamp", "")
    if not timestamp:
        raise HTTPException(status_code=401, detail="Missing X-Writ-Timestamp header")
    import time
    try:
        if abs(time.time() - float(timestamp)) > 300:
            raise HTTPException(status_code=401, detail="Stale timestamp")
    except (TypeError, ValueError):
        raise HTTPException(status_code=401, detail="Invalid timestamp")
    if not verify_signature(body, signature, decrypted_secret, timestamp):
        logger.warning(f"Invalid webhook signature for trigger {trigger.id}")
        raise HTTPException(status_code=401, detail="Invalid signature")


async def _process_webhook(
    trigger,
    request: Request,
    db: AsyncSession,
    x_writ_signature: Optional[str],
    x_hub_signature_256: Optional[str],
    *,
    require_signature: bool = True,
):
    """Process an incoming webhook request.

    ``require_signature`` states, per route, whether the HMAC IS the authentication:

    * ``True`` (default — ``/hook/{token}``, ``/trigger/{id}``): those routes are
      UNAUTHENTICATED, so the signature is the only thing between an anonymous caller
      and the owner's automations. Fail closed: a trigger with no secret must never
      execute.
    * ``False`` (``/v1/webhooks/{custom_path}``): that route already required a valid
      API key with ``workflows:execute``, so the caller IS authenticated and a
      signature is optional belt-and-braces. It is still VERIFIED when presented —
      accepting a bad signature just because a key was valid would make the header
      meaningless.

    Defaulting to ``True`` keeps the fail-closed behaviour for anything that forgets to
    say: a new unauthenticated route cannot accidentally opt out of signing.
    """
    from models.automation_workflow import AutomationWorkflow
    from models.automation_task import AutomationTask

    if not trigger.enabled:
        raise HTTPException(status_code=403, detail="Webhook trigger is disabled")

    # Get raw body for signature verification
    body = await request.body()

    signature = x_writ_signature or x_hub_signature_256

    if require_signature:
        # SECURITY (#7): fail closed. The trigger is reachable by an enumerable integer
        # id (/trigger/{id}) or an unguessable token (/hook/{token}) with no credential,
        # so a trigger with no secret configured must never execute. All write paths that
        # mint webhook triggers now always generate a secret, so a secret-less trigger is
        # a stale/legacy row.
        if not trigger.secret:
            logger.warning(
                f"Webhook trigger {trigger.id} has no secret configured; refusing unsigned execution"
            )
            raise HTTPException(
                status_code=401,
                detail="Webhook trigger is not configured for signed delivery",
            )
        _verify_webhook_signature(trigger, request, body, signature)
    elif signature:
        _verify_webhook_signature(trigger, request, body, signature)

    # Parse payload
    try:
        payload = json.loads(body) if body else {}
    except json.JSONDecodeError:
        payload = {}

    # Consumer-wait anchor for response_latency_ms.
    #
    # SECURITY: response_latency_ms is a billing + ranking signal. The webhook
    # body is externally supplied (authenticated by HMAC/secret, but still
    # attacker-shaped), so a caller who knows the webhook secret could forge
    # _latency.request_received_at to backdate/postdate the consumer-wait anchor.
    # We therefore stamp request_received_at SERVER-SIDE from the webhook's own
    # arrival time and NEVER trust the client-supplied value (mirrors the public
    # run_workflow path at automation.py:3322). The _latency field is a control
    # field, not workflow input, so strip any inbound copy from payload/form_data.
    if isinstance(payload, dict) and "_latency" in payload:
        payload = {k: v for k, v in payload.items() if k != "_latency"}
    latency_ctx = {"request_received_at": datetime.now(timezone.utc).isoformat()}

    # FILE ASSETS (§4.5): an optional nested `files` map { slot_or_input_key: file_id }
    # in the webhook body binds the CALLER's own stored files to upload-step slots —
    # the same shape the /run endpoint accepts (automation.py: body.get("files")).
    # It is a binding CONTROL field, not workflow input, so strip it from payload
    # BEFORE the flatten-into-form_data below (otherwise it would land as a literal
    # dict form value and never bind) and thread it into trigger_context["files"],
    # where the single dispatch choke point (_dispatch_to_recorder_or_queue) resolves
    # each file_id (fail-closed). Only the slot NAME is bound here; never log
    # file_id values. Normalized to {str: str}.
    run_files_req = None
    if isinstance(payload, dict) and isinstance(payload.get("files"), dict):
        run_files_req = {
            str(k): str(v) for k, v in payload["files"].items()
            if isinstance(v, str) and v
        } or None
        payload = {k: v for k, v in payload.items() if k != "files"}

    # Check conditions
    if trigger.conditions:
        for key, expected_value in trigger.conditions.items():
            actual_value = extract_value_from_path(payload, f"$.{key}")
            if actual_value != expected_value:
                logger.info(f"Webhook trigger {trigger.id} conditions not met: {key}={actual_value} (expected {expected_value})")
                return {"success": True, "triggered": False, "reason": "Conditions not met"}

    # Build form_data: auto-map URL params + body + explicit overrides
    form_data = {}

    # 1. Auto-map URL query params
    query_params = dict(request.query_params)
    # Remove internal params
    for internal_key in ['wait', 'timeout']:
        query_params.pop(internal_key, None)
    form_data.update(query_params)

    # 2. Auto-map POST body fields
    if payload and isinstance(payload, dict):
        form_data.update(payload)

    # 3. Explicit mapping overrides
    if trigger.payload_mapping:
        for target_field, source_path in trigger.payload_mapping.items():
            value = extract_value_from_path(payload, source_path)
            if value is not None:
                form_data[target_field] = value

    # Extract and encrypt any $secret: prefixed fields
    encrypted_secrets = None
    try:
        from security.encryption import extract_and_encrypt_secrets
        form_data, encrypted_secrets = extract_and_encrypt_secrets(form_data)
    except Exception as e:
        logger.error(f"Secret extraction failed: {e}")

    # Execute action
    triggered = False
    result_data = {}

    if trigger.action == "run_workflow" and trigger.workflow_id:
        # Get the referenced workflow.
        workflow_result = await db.execute(
            select(AutomationWorkflow).where(
                AutomationWorkflow.id == trigger.workflow_id,
            )
        )
        workflow = workflow_result.scalar_one_or_none()

        if workflow:
            # Dispatch to recorder (instant) or queue for desktop agent (fallback)
            from routers.automation import _dispatch_to_recorder_or_queue
            task = await _dispatch_to_recorder_or_queue(
                db=db,
                workflow=workflow,
                target_id=trigger.target_id or 0,
                trigger_type="webhook",
                trigger_context={
                    "type": "webhook",
                    "trigger_id": trigger.id,
                    "trigger_name": trigger.name,
                    "form_data": form_data,
                    "payload": payload,
                    "encrypted_secrets": encrypted_secrets,
                    **({"_latency": latency_ctx} if latency_ctx else {}),
                    **({"files": run_files_req} if run_files_req else {}),
                },
                form_data=form_data,
            )
            await db.commit()

            triggered = True
            result_data = {"task_id": task.id, "workflow_id": workflow.id}
            logger.info(f"Webhook trigger {trigger.id} created task {task.id} (form_data keys: {list(form_data.keys())})")

            # Smart wait: if workflow has a "return" step, auto-wait for result
            has_return_step = any(
                (step.get('type') == 'return' or
                 (step.get('type') == 'extract' and step.get('options', {}).get('extract_type') == 'return'))
                for step in (workflow.steps or [])
            )

            if has_return_step:
                import asyncio
                wait_timeout = getattr(trigger, 'wait_timeout', 120) or 120
                elapsed = 0
                while elapsed < wait_timeout:
                    await asyncio.sleep(2)
                    elapsed += 2
                    task_result = await db.execute(
                        select(AutomationTask).where(AutomationTask.id == task.id)
                    )
                    task_obj = task_result.scalar_one_or_none()
                    if task_obj and task_obj.status in ("success", "failed", "cancelled"):
                        return {
                            "success": task_obj.success or False,
                            "status": task_obj.status,
                            "extracted_data": (task_obj.result_data or {}).get("extracted_data"),
                            "result_data": task_obj.result_data,
                            "error": task_obj.error_message,
                            "duration_ms": task_obj.duration_ms,
                            "task_id": task_obj.id,
                        }

                return {
                    "success": False,
                    "status": "timeout",
                    "error": f"Workflow did not complete within {wait_timeout}s",
                    "task_id": task.id,
                }

    elif trigger.action == "check_target" and trigger.target_id:
        # Queue a target check (would need to integrate with agent system)
        triggered = True
        result_data = {"target_id": trigger.target_id, "action": "check_queued"}
        logger.info(f"Webhook trigger {trigger.id} queued check for target {trigger.target_id}")

    # Fire trigger rules with event_type='webhook_received' linked to this webhook
    from models.trigger_rule import TriggerRule, TriggerExecution
    from services.unified_trigger_service import UnifiedTriggerService
    from sqlalchemy import or_

    # Find trigger rules that:
    # 1. Are specifically linked to this webhook trigger (webhook_trigger_id matches)
    # 2. OR are webhook_received triggers for this target without a specific webhook link
    rules_result = await db.execute(
        select(TriggerRule).where(
            TriggerRule.event_type == "webhook_received",
            TriggerRule.enabled == True,
            or_(
                TriggerRule.webhook_trigger_id == trigger.id,  # Specifically linked to this webhook
                # Or generic webhook_received for target (no specific webhook linked)
                (TriggerRule.target_id == trigger.target_id) & (TriggerRule.webhook_trigger_id == None) if trigger.target_id else False
            )
        ).order_by(TriggerRule.priority.desc())
    )
    trigger_rules = rules_result.scalars().all()

    if trigger_rules:
        trigger_service = UnifiedTriggerService(db)
        for rule in trigger_rules:
            trigger_context = {
                "event_type": "webhook_received",
                "webhook_trigger_id": trigger.id,
                "webhook_trigger_name": trigger.name,
                "payload": payload,
                "form_data": form_data,
                # FILE ASSETS (§4.5): thread the CALLER's bound files map
                # { slot_or_input_key: file_id } through generic trigger rules
                # too. Already stripped from `payload` server-side above (so it
                # never lands as a literal form value) and normalized to
                # {str: str}. _dispatch_workflow reads context["files"] and the
                # single dispatch choke point resolves each file_id against the
                # run's owner (ownership-checked, fail-closed). Only the
                # slot NAME is bound here; never log file_id values.
                **({"files": run_files_req} if run_files_req else {}),
            }

            # Record + dispatch the rule's actions / block chain. Mirrors the
            # worker fan-out (worker/tasks/process_trigger.py) and the manual-run
            # path (routers/triggers.py): _dispatch_actions is the public dispatch
            # entrypoint. Non-blocking — any workflow continuation is stored on the
            # execution and resumed when the matching workflow_completed arrives.
            execution = TriggerExecution(
                trigger_rule_id=rule.id,
                status="running",
                trigger_context=trigger_context,
            )
            db.add(execution)
            await db.flush()

            try:
                action_results = await trigger_service._dispatch_actions(
                    rule, trigger_context, execution, blocking=False,
                )
                execution.status = "completed"
                execution.action_results = action_results
                execution.completed_at = datetime.utcnow()
                logger.info(f"Fired trigger rule {rule.id} ({rule.name}) for webhook_received event")
            except Exception as e:
                execution.status = "failed"
                execution.error_message = str(e)
                logger.error(f"Error firing trigger rule {rule.id}: {e}")

        result_data["trigger_rules_fired"] = len(trigger_rules)

    # Update trigger stats
    trigger.last_triggered_at = datetime.utcnow()
    trigger.trigger_count = (trigger.trigger_count or 0) + 1
    await db.commit()

    return {
        "success": True,
        "triggered": triggered,
        "action": trigger.action,
        "data": result_data,
    }
