"""
Notifications router - notification testing and alert management endpoints.
"""
import logging
from typing import Optional, List
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Request, status, Query
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from database import get_db
from models.pushover_recipient import PushoverRecipient
from models.pushover_config import PushoverConfig
from models.signal_recipient import SignalRecipient
from security.api_key import get_current_api_key
from security.encryption import SecretEncryption
from notifications.pushover import PushoverNotifier, decrypt_stored_credential
from notifications.twilio import TwilioNotifier

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/notifications", tags=["Notifications"])


# Pydantic models
class TestNotificationRequest(BaseModel):
    """Request model for test notification."""
    provider: str = Field(default="pushover", description="Notification provider (pushover, twilio)")
    message: Optional[str] = Field(None, description="Custom test message")


class AlertInfo(BaseModel):
    """Response model for alert information."""
    id: int
    target_url: str
    content_hash: str
    diff_snippet: Optional[str]
    content_before: Optional[str]
    content_after: Optional[str]
    agent_count: int
    received_at: str


@router.post(
    "/test",
    summary="Send Test Notification",
    description="Send a test notification. Requires operator role or higher.",
)
async def send_test_notification(
    request: TestNotificationRequest,
    req: Request,
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """
    Send a test notification.

    Requires operator or admin role. Tests notification providers.
    """
    # Check role
    role = current_api_key.get("role", "").lower()
    if role not in ["operator", "admin"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requires operator role or higher",
        )

    # Rate limit: 5 notification tests per hour
    redis_client = getattr(req.app.state, "redis", None)
    if redis_client:
        rate_key = "notification_test_rate"
        try:
            count = await redis_client.incr(rate_key)
            if count == 1:
                await redis_client.expire(rate_key, 3600)
            if count > 5:
                raise HTTPException(status_code=429, detail="Too many notification tests. Please try again later.")
        except HTTPException:
            raise
        except Exception:
            pass

    try:
        result = None

        if request.provider == "pushover":
            # Read configuration from database to ensure we have the latest values
            config_result = await db.execute(select(PushoverConfig).limit(1))
            pushover_config = config_result.scalar_one_or_none()

            if not pushover_config or not pushover_config.app_token:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Pushover not configured. Please configure Pushover settings first.",
                )

            # Get all enabled recipients.
            recipients_result = await db.execute(
                select(PushoverRecipient).where(
                    PushoverRecipient.enabled == True,
                )
            )
            recipients = recipients_result.scalars().all()

            if not recipients:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="No enabled recipients found. Please add at least one recipient first.",
                )

            # Send test notification to all enabled recipients
            results = []
            for recipient in recipients:
                notifier = PushoverNotifier(
                    app_token=decrypt_stored_credential(pushover_config.app_token),
                    user_key=recipient.user_key,
                )

                if request.message:
                    recipient_result = await notifier.send_notification(
                        message=request.message,
                        title="Writ Test",
                    )
                else:
                    recipient_result = await notifier.send_test_notification()

                results.append({
                    "recipient": recipient.name,
                    "result": recipient_result
                })

            result = {
                "sent_to": len(recipients),
                "results": results
            }

        elif request.provider == "twilio":
            notifier = TwilioNotifier()

            if not notifier.enabled:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Twilio not configured",
                )

            if request.message:
                result = await notifier.send_sms(message=request.message)
            else:
                result = await notifier.send_test_sms()

        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unknown provider: {request.provider}",
            )

        await db.commit()

        logger.info(
            f"Test notification sent via {request.provider} "
            f"by {current_api_key.get('label')}"
        )

        return {
            "status": "success",
            "provider": request.provider,
            "result": result,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error sending test notification: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to send test notification: {str(e)}",
        )


@router.get(
    "/alerts/recent",
    response_model=List[AlertInfo],
    summary="Get Recent Alerts",
    description="Get recent change alerts.",
)
async def get_recent_alerts(
    hours: int = Query(default=24, ge=1, le=168, description="Hours to look back"),
    limit: int = Query(default=50, ge=1, le=500, description="Maximum number of alerts"),
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """
    Get recent change alerts.

    Change history lives in `DetectedChange` (surfaced via the runs/data feeds),
    so this endpoint returns an empty list for backward-compatible clients.
    """
    return []


@router.get(
    "/providers/status",
    summary="Get Notification Provider Status",
    description="Check status of notification providers.",
)
async def get_provider_status(
    current_api_key: dict = Depends(get_current_api_key),
):
    """
    Get status of all notification providers.

    Returns enabled status for each provider.
    """
    try:
        pushover = PushoverNotifier()
        twilio = TwilioNotifier()

        return {
            "providers": {
                "pushover": {
                    "enabled": pushover.enabled,
                    "configured": bool(pushover.app_token and pushover.user_key),
                },
                "twilio": {
                    "enabled": twilio.enabled,
                    "configured": bool(
                        twilio.account_sid
                        and twilio.auth_token
                        and twilio.from_phone
                        and twilio.to_phone
                    ),
                },
            }
        }

    except Exception as e:
        logger.error(f"Error getting provider status: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get provider status",
        )


# Pydantic models for Pushover configuration
class PushoverConfigRequest(BaseModel):
    """Request model for Pushover configuration."""
    app_token: str = Field(..., min_length=1, description="Pushover application API token")
    user_key: str = Field(..., min_length=1, description="Pushover user/group key")
    notification_title: Optional[str] = Field(None, max_length=250, description="Notification title")
    notification_message: Optional[str] = Field(None, max_length=1024, description="Notification message body")
    notification_priority: Optional[int] = Field(None, ge=-2, le=2, description="Priority level: -2 to 2")
    notification_sound: Optional[str] = Field(None, max_length=50, description="Notification sound")
    url_title: Optional[str] = Field(None, max_length=100, description="URL button title")
    html_enabled: Optional[bool] = Field(None, description="Enable HTML formatting")


class PushoverConfigResponse(BaseModel):
    """Response model for Pushover configuration."""
    configured: bool
    enabled: bool
    notification_title: Optional[str] = None
    notification_message: Optional[str] = None
    notification_priority: Optional[int] = None
    notification_sound: Optional[str] = None
    url_title: Optional[str] = None
    html_enabled: Optional[bool] = None


@router.get(
    "/pushover/config",
    response_model=PushoverConfigResponse,
    summary="Get Pushover Configuration Status",
    description="Get current Pushover configuration status without exposing secrets.",
)
async def get_pushover_config(
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """
    Get Pushover configuration status.

    Returns whether Pushover is configured and enabled.
    Does not expose actual tokens for security.
    """
    try:
        # Get config from database
        result = await db.execute(select(PushoverConfig).limit(1))
        config = result.scalar_one_or_none()

        if not config:
            return PushoverConfigResponse(
                configured=False,
                enabled=False,
            )

        # Opportunistic migration: rows written before encryption-at-rest hold
        # plaintext credentials. A failed decrypt identifies them — re-encrypt
        # in place so the plaintext disappears from disk on first read.
        migrated = False
        for field in ("app_token", "user_key"):
            raw = getattr(config, field, None)
            if not raw:
                continue
            try:
                SecretEncryption.decrypt_secret(raw)
            except Exception:
                setattr(config, field, SecretEncryption.encrypt_secret(raw))
                migrated = True
        if migrated:
            await db.commit()
            logger.info("Re-encrypted legacy plaintext Pushover credentials at rest")

        return PushoverConfigResponse(
            configured=bool(config.app_token),
            enabled=config.enabled,
            notification_title=config.notification_title,
            notification_message=config.notification_message,
            notification_priority=config.notification_priority,
            notification_sound=config.notification_sound,
            url_title=config.url_title,
            html_enabled=config.html_enabled,
        )

    except Exception as e:
        logger.error(f"Error getting Pushover config: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get Pushover configuration",
        )


@router.post(
    "/pushover/config",
    response_model=PushoverConfigResponse,
    summary="Update Pushover Configuration",
    description="Update Pushover API credentials. Requires admin role.",
)
async def update_pushover_config(
    config: PushoverConfigRequest,
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """
    Update Pushover configuration.

    The provider config is a single global singleton. Validates credentials
    before saving.
    """
    # Only platform admins configure global notification providers.
    if not current_api_key.get("is_platform_admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requires platform admin role",
        )

    try:
        # Validate credentials
        import os
        from config import settings

        # Create a test notifier with the new credentials
        test_notifier = PushoverNotifier(
            app_token=config.app_token,
            user_key=config.user_key,
        )

        # Validate credentials
        is_valid = await test_notifier.validate_credentials()

        if not is_valid:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid Pushover credentials",
            )

        # Get or create config in database
        result = await db.execute(select(PushoverConfig).limit(1))
        db_config = result.scalar_one_or_none()

        if db_config:
            # Update existing config (credentials encrypted at rest; reads go
            # through notifications.pushover.decrypt_stored_credential).
            db_config.app_token = SecretEncryption.encrypt_secret(config.app_token)
            db_config.user_key = SecretEncryption.encrypt_secret(config.user_key) if config.user_key else config.user_key
            db_config.enabled = True
            db_config.updated_at = datetime.utcnow()

            # Update notification customization fields if provided
            if config.notification_title is not None:
                db_config.notification_title = config.notification_title
            if config.notification_message is not None:
                db_config.notification_message = config.notification_message
            if config.notification_priority is not None:
                db_config.notification_priority = config.notification_priority
            if config.notification_sound is not None:
                db_config.notification_sound = config.notification_sound
            if config.url_title is not None:
                db_config.url_title = config.url_title
            if config.html_enabled is not None:
                db_config.html_enabled = config.html_enabled
        else:
            # Create new config (credentials encrypted at rest).
            db_config = PushoverConfig(
                app_token=SecretEncryption.encrypt_secret(config.app_token),
                user_key=SecretEncryption.encrypt_secret(config.user_key) if config.user_key else config.user_key,
                enabled=True,
                notification_title=config.notification_title or "Writ: Change Detected",
                notification_message=config.notification_message,
                notification_priority=config.notification_priority if config.notification_priority is not None else 1,
                notification_sound=config.notification_sound or "pushover",
                url_title=config.url_title or "View Page",
                html_enabled=config.html_enabled if config.html_enabled is not None else True,
            )
            db.add(db_config)

        # Update settings for immediate availability (until restart)
        # Note: credentials are stored in the database and the settings object only,
        # never written to os.environ to avoid leaking secrets to child processes.
        settings.pushover_app_token = config.app_token
        settings.pushover_user_key = config.user_key


        await db.commit()

        logger.info(f"Pushover configuration updated and saved to database by {current_api_key.get('label')}")

        return PushoverConfigResponse(
            configured=True,
            enabled=True,
        )

    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        from services.error_reporting import internal_http_error
        raise internal_http_error(e, "Failed to update Pushover configuration.", action="notifications.pushover_config")


# =============================================================================
# Unified Recipients Endpoint (for trigger notification recipient selection)
# =============================================================================

class UnifiedRecipient(BaseModel):
    """Unified recipient model for all providers."""
    id: int
    provider: str  # pushover, email, twilio, whatsapp, signal
    name: str
    identifier_preview: str  # Partially masked identifier
    enabled: bool


@router.get(
    "/recipients/all",
    response_model=List[UnifiedRecipient],
    summary="List All Recipients",
    description="Get all recipients across all notification providers for trigger configuration.",
)
async def list_all_recipients(
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
    enabled_only: bool = Query(False, description="Only return enabled recipients"),
):
    """List all recipients from all providers in a unified format."""
    from models.email_recipient import EmailRecipient
    from models.twilio_recipient import TwilioRecipient
    from models.whatsapp_recipient import WhatsAppRecipient


    recipients = []

    # Helper to safely query a recipient table (handles missing tables gracefully)
    async def fetch_recipients(model_class, provider: str, get_identifier, enabled_only: bool):
        try:
            query = select(model_class)
            if enabled_only:
                query = query.where(model_class.enabled == True)
            result = await db.execute(query)
            return [
                UnifiedRecipient(
                    id=r.id,
                    provider=provider,
                    name=r.name,
                    identifier_preview=get_identifier(r),
                    enabled=r.enabled,
                )
                for r in result.scalars().all()
            ]
        except Exception as e:
            # Table might not exist - log and continue
            logger.debug(f"Could not fetch {provider} recipients: {e}")
            return []

    try:
        # Pushover recipients
        pushover_list = await fetch_recipients(
            PushoverRecipient,
            "pushover",
            lambda r: (r.user_key[:8] + "...") if r.user_key else "",
            enabled_only
        )
        recipients.extend(pushover_list)

        # Email recipients (table may not exist)
        try:
            email_list = await fetch_recipients(
                EmailRecipient,
                "email",
                lambda r: (
                    (r.email.split("@")[0][:3] + "***@" + r.email.split("@")[1])
                    if r.email and "@" in r.email else (r.email[:3] + "***" if r.email else "")
                ),
                enabled_only
            )
            recipients.extend(email_list)
        except Exception:
            pass

        # Twilio recipients (table may not exist)
        try:
            twilio_list = await fetch_recipients(
                TwilioRecipient,
                "twilio",
                lambda r: (r.phone_number[:4] + "***" + r.phone_number[-2:]) if r.phone_number and len(r.phone_number) > 6 else (r.phone_number or ""),
                enabled_only
            )
            recipients.extend(twilio_list)
        except Exception:
            pass

        # WhatsApp recipients (table may not exist)
        try:
            whatsapp_list = await fetch_recipients(
                WhatsAppRecipient,
                "whatsapp",
                lambda r: (r.phone_number[:4] + "***" + r.phone_number[-2:]) if r.phone_number and len(r.phone_number) > 6 else (r.phone_number or ""),
                enabled_only
            )
            recipients.extend(whatsapp_list)
        except Exception:
            pass

        # Signal recipients (table may not exist)
        try:
            signal_list = await fetch_recipients(
                SignalRecipient,
                "signal",
                lambda r: (r.phone_number[:4] + "***" + r.phone_number[-2:]) if r.phone_number and len(r.phone_number) > 6 else (r.phone_number or ""),
                enabled_only
            )
            recipients.extend(signal_list)
        except Exception:
            pass

        return recipients

    except Exception as e:
        logger.error(f"Error listing all recipients: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to list recipients",
        )


# Pydantic models for recipients
class PushoverRecipientRequest(BaseModel):
    """Request model for adding Pushover recipient."""
    name: str = Field(..., min_length=1, max_length=255, description="Friendly name for recipient")
    user_key: str = Field(..., min_length=1, description="Pushover user or group key")


class PushoverRecipientResponse(BaseModel):
    """Response model for Pushover recipient."""
    id: int
    name: str
    user_key_preview: str  # Only first 8 chars for security
    enabled: bool
    created_at: str
    last_notified_at: Optional[str]


@router.get(
    "/pushover/recipients",
    response_model=List[PushoverRecipientResponse],
    summary="List Pushover Recipients",
    description="Get all Pushover recipients. Requires admin role.",
)
async def list_pushover_recipients(
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """List all Pushover recipients."""
    # Check role
    role = current_api_key.get("role", "").lower()
    if role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requires admin role",
        )


    try:
        result = await db.execute(
            select(PushoverRecipient)
            .order_by(PushoverRecipient.id)
        )
        recipients = result.scalars().all()

        return [
            PushoverRecipientResponse(
                id=r.id,
                name=r.name,
                user_key_preview=r.user_key[:8] + "..." if len(r.user_key) > 8 else r.user_key,
                enabled=r.enabled,
                created_at=r.created_at.isoformat(),
                last_notified_at=r.last_notified_at.isoformat() if r.last_notified_at else None,
            )
            for r in recipients
        ]

    except Exception as e:
        logger.error(f"Error listing recipients: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to list recipients",
        )


@router.post(
    "/pushover/recipients",
    response_model=PushoverRecipientResponse,
    summary="Add Pushover Recipient",
    description="Add a new Pushover recipient. Requires admin role.",
)
async def add_pushover_recipient(
    recipient: PushoverRecipientRequest,
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """Add a new Pushover recipient."""
    # Check role
    role = current_api_key.get("role", "").lower()
    if role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requires admin role",
        )


    try:
        # Check if user_key already exists
        existing = await db.execute(
            select(PushoverRecipient).where(
                PushoverRecipient.user_key == recipient.user_key,
            )
        )
        if existing.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="This user key is already registered",
            )

        # Validate the user key
        notifier = PushoverNotifier(user_key=recipient.user_key)
        if not notifier.app_token:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Pushover app token not configured. Configure it first.",
            )

        is_valid = await notifier.validate_credentials()
        if not is_valid:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid Pushover user key",
            )

        # Create recipient
        new_recipient = PushoverRecipient(
            name=recipient.name,
            user_key=recipient.user_key,
            enabled=True,
            created_at=datetime.utcnow(),
        )

        db.add(new_recipient)


        await db.commit()
        await db.refresh(new_recipient)

        logger.info(f"Pushover recipient added: {recipient.name}")

        return PushoverRecipientResponse(
            id=new_recipient.id,
            name=new_recipient.name,
            user_key_preview=new_recipient.user_key[:8] + "...",
            enabled=new_recipient.enabled,
            created_at=new_recipient.created_at.isoformat(),
            last_notified_at=None,
        )

    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        from services.error_reporting import internal_http_error
        raise internal_http_error(e, "Failed to add recipient.", action="notifications.pushover_recipient")


@router.delete(
    "/pushover/recipients/{recipient_id}",
    summary="Delete Pushover Recipient",
    description="Delete a Pushover recipient. Requires admin role.",
)
async def delete_pushover_recipient(
    recipient_id: int,
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """Delete a Pushover recipient."""
    # Check role
    role = current_api_key.get("role", "").lower()
    if role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requires admin role",
        )


    try:
        result = await db.execute(
            select(PushoverRecipient).where(
                PushoverRecipient.id == recipient_id,
            )
        )
        recipient = result.scalar_one_or_none()

        if not recipient:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Recipient not found",
            )


        await db.delete(recipient)
        await db.commit()

        logger.info(f"Pushover recipient deleted: {recipient.name}")

        return {"status": "success", "message": "Recipient deleted"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting recipient: {e}")
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete recipient",
        )


@router.patch(
    "/pushover/recipients/{recipient_id}/toggle",
    response_model=PushoverRecipientResponse,
    summary="Toggle Pushover Recipient",
    description="Enable or disable a Pushover recipient. Requires admin role.",
)
async def toggle_pushover_recipient(
    recipient_id: int,
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """Toggle recipient enabled status."""
    # Check role
    role = current_api_key.get("role", "").lower()
    if role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requires admin role",
        )


    try:
        result = await db.execute(
            select(PushoverRecipient).where(
                PushoverRecipient.id == recipient_id,
            )
        )
        recipient = result.scalar_one_or_none()

        if not recipient:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Recipient not found",
            )

        recipient.enabled = not recipient.enabled
        await db.commit()
        await db.refresh(recipient)

        logger.info(f"Pushover recipient {'enabled' if recipient.enabled else 'disabled'}: {recipient.name}")

        return PushoverRecipientResponse(
            id=recipient.id,
            name=recipient.name,
            user_key_preview=recipient.user_key[:8] + "...",
            enabled=recipient.enabled,
            created_at=recipient.created_at.isoformat(),
            last_notified_at=recipient.last_notified_at.isoformat() if recipient.last_notified_at else None,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error toggling recipient: {e}")
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to toggle recipient",
        )

# ============================================================================
# EMAIL NOTIFICATION ENDPOINTS
# ============================================================================

# Pydantic models for Email configuration
class EmailConfigRequest(BaseModel):
    """Request model for Email configuration."""
    smtp_host: str = Field(..., min_length=1, description="SMTP server hostname")
    smtp_port: int = Field(..., ge=1, le=65535, description="SMTP server port")
    smtp_username: str = Field(..., min_length=1, description="SMTP username")
    smtp_password: str = Field(..., min_length=1, description="SMTP password")
    from_email: str = Field(..., min_length=1, description="From email address")
    from_name: str = Field(default="Writ", description="From name")
    use_tls: bool = Field(default=True, description="Use TLS encryption")
    enabled: bool = Field(default=True, description="Enable email notifications")


class EmailConfigResponse(BaseModel):
    """Response model for Email configuration."""
    configured: bool
    enabled: bool
    smtp_host: Optional[str] = None
    smtp_port: Optional[int] = None
    smtp_username: Optional[str] = None
    from_email: Optional[str] = None
    from_name: Optional[str] = None
    use_tls: Optional[bool] = None


@router.get(
    "/email/config",
    response_model=EmailConfigResponse,
    summary="Get Email Configuration",
    description="Get current email SMTP configuration.",
)
async def get_email_config(
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """Get email SMTP configuration (without exposing password)."""
    try:
        from models.email_config import EmailConfig

        result = await db.execute(select(EmailConfig).limit(1))
        config = result.scalar_one_or_none()

        if not config:
            return EmailConfigResponse(
                configured=False,
                enabled=False,
            )

        return EmailConfigResponse(
            configured=True,
            enabled=config.enabled,
            smtp_host=config.smtp_host,
            smtp_port=config.smtp_port,
            smtp_username=config.smtp_username,
            from_email=config.from_email,
            from_name=config.from_name,
            use_tls=config.use_tls,
        )

    except Exception as e:
        logger.error(f"Error getting email config: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get email configuration",
        )


@router.post(
    "/email/config",
    summary="Update Email Configuration",
    description="Update email SMTP configuration. Requires admin role.",
)
async def update_email_config(
    request: EmailConfigRequest,
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """Update email SMTP configuration."""
    # Only platform admins configure the global SMTP provider.
    if not current_api_key.get("is_platform_admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requires platform admin role",
        )
    try:
        from models.email_config import EmailConfig

        result = await db.execute(select(EmailConfig).limit(1))
        config = result.scalar_one_or_none()

        encrypted_password = SecretEncryption.encrypt_secret(request.smtp_password)
        if config:
            config.smtp_host = request.smtp_host
            config.smtp_port = request.smtp_port
            config.smtp_username = request.smtp_username
            config.smtp_password = encrypted_password
            config.from_email = request.from_email
            config.from_name = request.from_name
            config.use_tls = request.use_tls
            config.enabled = request.enabled
        else:
            config = EmailConfig(
                smtp_host=request.smtp_host,
                smtp_port=request.smtp_port,
                smtp_username=request.smtp_username,
                smtp_password=encrypted_password,
                from_email=request.from_email,
                from_name=request.from_name,
                use_tls=request.use_tls,
                enabled=request.enabled,
            )
            db.add(config)

        await db.commit()
        logger.info(f"Email configuration updated: {request.from_email}")

        return {"success": True, "message": "Email configuration updated"}

    except Exception as e:
        logger.error(f"Error updating email config: {e}")
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update email configuration",
        )


# Test request models
class TestEmailRequest(BaseModel):
    """Request model for test email."""
    to_email: EmailStr = Field(..., description="Recipient email address")


@router.post(
    "/email/test",
    summary="Send Test Email",
    description="Send a test email. Requires operator role.",
)
async def send_test_email(
    request: TestEmailRequest,
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """Send a test email."""
    # Check role
    role = current_api_key.get("role", "").lower()
    if role not in ["operator", "admin"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requires operator role or higher",
        )

    try:
        from models.email_config import EmailConfig
        from notifications.email import EmailNotifier

        result = await db.execute(select(EmailConfig).limit(1))
        config = result.scalar_one_or_none()

        if not config or not config.enabled:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email notifications not configured or disabled",
            )

        try:
            decrypted_password = SecretEncryption.decrypt_secret(config.smtp_password)
        except Exception:
            decrypted_password = config.smtp_password

        notifier = EmailNotifier(
            smtp_host=config.smtp_host,
            smtp_port=config.smtp_port,
            smtp_username=config.smtp_username,
            smtp_password=decrypted_password,
            from_email=config.from_email,
            from_name=config.from_name,
            use_tls=config.use_tls,
        )

        result = notifier.send_test_email(request.to_email)

        logger.info(f"Test email sent to {request.to_email}")

        return {"success": True, "message": f"Test email sent to {request.to_email}"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error sending test email: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to send test email: {str(e)}",
        )


class EmailRecipientRequest(BaseModel):
    """Request model for adding Email recipient."""
    name: str = Field(..., min_length=1, max_length=255, description="Friendly name for recipient")
    email: str = Field(..., min_length=1, description="Email address")


class EmailRecipientResponse(BaseModel):
    """Response model for Email recipient."""
    id: int
    name: str
    email: str
    enabled: bool
    created_at: str
    last_notified_at: Optional[str]


@router.get(
    "/email/recipients",
    response_model=List[EmailRecipientResponse],
    summary="List Email Recipients",
    description="Get all email recipients. Requires admin role.",
)
async def list_email_recipients(
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """List all email recipients."""
    role = current_api_key.get("role", "").lower()
    if role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requires admin role",
        )


    try:
        from models.email_recipient import EmailRecipient

        result = await db.execute(
            select(EmailRecipient)
            .order_by(EmailRecipient.id)
        )
        recipients = result.scalars().all()

        return [
            EmailRecipientResponse(
                id=r.id,
                name=r.name,
                email=r.email,
                enabled=r.enabled,
                created_at=r.created_at.isoformat(),
                last_notified_at=r.last_notified_at.isoformat() if r.last_notified_at else None,
            )
            for r in recipients
        ]

    except Exception as e:
        logger.error(f"Error listing email recipients: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to list email recipients",
        )


@router.post(
    "/email/recipients",
    response_model=EmailRecipientResponse,
    summary="Add Email Recipient",
    description="Add a new email recipient. Requires admin role.",
)
async def add_email_recipient(
    recipient: EmailRecipientRequest,
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """Add a new email recipient."""
    role = current_api_key.get("role", "").lower()
    if role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requires admin role",
        )


    try:
        from models.email_recipient import EmailRecipient

        # Check if email already exists
        existing = await db.execute(
            select(EmailRecipient).where(
                EmailRecipient.email == recipient.email,
            )
        )
        if existing.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="This email is already registered",
            )

        # Create recipient
        new_recipient = EmailRecipient(
            name=recipient.name,
            email=recipient.email,
            enabled=True,
            created_at=datetime.utcnow(),
        )

        db.add(new_recipient)


        await db.commit()
        await db.refresh(new_recipient)

        logger.info(f"Email recipient added: {recipient.name}")

        return EmailRecipientResponse(
            id=new_recipient.id,
            name=new_recipient.name,
            email=new_recipient.email,
            enabled=new_recipient.enabled,
            created_at=new_recipient.created_at.isoformat(),
            last_notified_at=None,
        )

    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        from services.error_reporting import internal_http_error
        raise internal_http_error(e, "Failed to add email recipient.", action="notifications.email_recipient")


@router.delete(
    "/email/recipients/{recipient_id}",
    summary="Delete Email Recipient",
    description="Delete an email recipient. Requires admin role.",
)
async def delete_email_recipient(
    recipient_id: int,
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """Delete an email recipient."""
    role = current_api_key.get("role", "").lower()
    if role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requires admin role",
        )


    try:
        from models.email_recipient import EmailRecipient

        result = await db.execute(
            select(EmailRecipient).where(
                EmailRecipient.id == recipient_id,
            )
        )
        recipient = result.scalar_one_or_none()

        if not recipient:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Recipient not found",
            )


        await db.delete(recipient)
        await db.commit()

        logger.info(f"Email recipient deleted: {recipient.name}")

        return {"status": "success", "message": "Recipient deleted"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting email recipient: {e}")
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete email recipient",
        )


@router.patch(
    "/email/recipients/{recipient_id}/toggle",
    response_model=EmailRecipientResponse,
    summary="Toggle Email Recipient",
    description="Enable or disable an email recipient. Requires admin role.",
)
async def toggle_email_recipient(
    recipient_id: int,
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """Toggle recipient enabled status."""
    role = current_api_key.get("role", "").lower()
    if role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requires admin role",
        )


    try:
        from models.email_recipient import EmailRecipient

        result = await db.execute(
            select(EmailRecipient).where(
                EmailRecipient.id == recipient_id,
            )
        )
        recipient = result.scalar_one_or_none()

        if not recipient:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Recipient not found",
            )

        recipient.enabled = not recipient.enabled
        await db.commit()
        await db.refresh(recipient)

        logger.info(f"Email recipient {'enabled' if recipient.enabled else 'disabled'}: {recipient.name}")

        return EmailRecipientResponse(
            id=recipient.id,
            name=recipient.name,
            email=recipient.email,
            enabled=recipient.enabled,
            created_at=recipient.created_at.isoformat(),
            last_notified_at=recipient.last_notified_at.isoformat() if recipient.last_notified_at else None,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error toggling email recipient: {e}")
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to toggle email recipient",
        )


# ============================================================================
# TWILIO SMS NOTIFICATION ENDPOINTS
# ============================================================================

# Pydantic models for Twilio configuration
class TwilioConfigRequest(BaseModel):
    """Request model for Twilio configuration."""
    account_sid: str = Field(..., min_length=1, description="Twilio account SID")
    auth_token: str = Field(..., min_length=1, description="Twilio auth token")
    from_phone: str = Field(..., min_length=1, description="Source phone number (E.164 format)")
    enabled: bool = Field(default=True, description="Enable Twilio SMS notifications")


class TwilioConfigResponse(BaseModel):
    """Response model for Twilio configuration."""
    configured: bool
    enabled: bool
    account_sid: Optional[str] = None
    from_phone: Optional[str] = None


@router.get(
    "/twilio/config",
    response_model=TwilioConfigResponse,
    summary="Get Twilio Configuration",
    description="Get current Twilio SMS configuration.",
)
async def get_twilio_config(
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """Get Twilio SMS configuration (without exposing auth token)."""
    try:
        from models.twilio_config import TwilioConfig

        result = await db.execute(select(TwilioConfig).limit(1))
        config = result.scalar_one_or_none()

        if not config:
            return TwilioConfigResponse(
                configured=False,
                enabled=False,
            )

        return TwilioConfigResponse(
            configured=True,
            enabled=config.enabled,
            account_sid=config.account_sid,
            from_phone=config.from_phone,
        )

    except Exception as e:
        logger.error(f"Error getting Twilio config: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get Twilio configuration",
        )


@router.post(
    "/twilio/config",
    summary="Update Twilio Configuration",
    description="Update Twilio SMS configuration. Requires admin role.",
)
async def update_twilio_config(
    request: TwilioConfigRequest,
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """Update Twilio SMS configuration."""
    # Only platform admins configure global notification providers.
    if not current_api_key.get("is_platform_admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requires platform admin role",
        )

    try:
        from models.twilio_config import TwilioConfig

        result = await db.execute(select(TwilioConfig).limit(1))
        config = result.scalar_one_or_none()

        encrypted_token = SecretEncryption.encrypt_secret(request.auth_token)
        if config:
            config.account_sid = request.account_sid
            config.auth_token = encrypted_token
            config.from_phone = request.from_phone
            config.enabled = request.enabled
            config.updated_at = datetime.utcnow()
        else:
            config = TwilioConfig(
                account_sid=request.account_sid,
                auth_token=encrypted_token,
                from_phone=request.from_phone,
                enabled=request.enabled,
            )
            db.add(config)


        await db.commit()
        logger.info(f"Twilio configuration updated by {current_api_key.get('label')}")

        return {"success": True, "message": "Twilio configuration updated"}

    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        from services.error_reporting import internal_http_error
        raise internal_http_error(e, "Failed to update Twilio configuration.", action="notifications.twilio_config")


class TestSMSRequest(BaseModel):
    """Request model for test SMS."""
    to_phone: str = Field(..., min_length=1, description="Recipient phone number (E.164 format)")


@router.post(
    "/twilio/test",
    summary="Send Test SMS",
    description="Send a test SMS via Twilio. Requires operator role.",
)
async def send_test_twilio_sms(
    request: TestSMSRequest,
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """Send a test SMS via Twilio."""
    # Check role
    role = current_api_key.get("role", "").lower()
    if role not in ["operator", "admin"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requires operator role or higher",
        )

    try:
        from models.twilio_config import TwilioConfig
        from notifications.twilio import TwilioNotifier

        result = await db.execute(select(TwilioConfig).limit(1))
        config = result.scalar_one_or_none()

        if not config or not config.enabled:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Twilio SMS not configured or disabled",
            )

        try:
            decrypted_token = SecretEncryption.decrypt_secret(config.auth_token)
        except Exception:
            decrypted_token = config.auth_token

        notifier = TwilioNotifier(
            account_sid=config.account_sid,
            auth_token=decrypted_token,
            from_phone=config.from_phone,
        )

        result = await notifier.send_test_sms(request.to_phone)

        logger.info(f"Test SMS sent to {request.to_phone}")

        return {"success": True, "message": f"Test SMS sent to {request.to_phone}"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error sending test SMS: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to send test SMS: {str(e)}",
        )


class TwilioRecipientRequest(BaseModel):
    """Request model for adding Twilio recipient."""
    name: str = Field(..., min_length=1, max_length=255, description="Friendly name for recipient")
    phone_number: str = Field(..., min_length=1, description="Phone number in E.164 format")


class TwilioRecipientResponse(BaseModel):
    """Response model for Twilio recipient."""
    id: int
    name: str
    phone_number: str
    enabled: bool
    created_at: str
    last_notified_at: Optional[str]


@router.get(
    "/twilio/recipients",
    response_model=List[TwilioRecipientResponse],
    summary="List Twilio Recipients",
    description="Get all Twilio SMS recipients. Requires admin role.",
)
async def list_twilio_recipients(
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """List all Twilio SMS recipients."""
    # Check role
    role = current_api_key.get("role", "").lower()
    if role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requires admin role",
        )


    try:
        from models.twilio_recipient import TwilioRecipient

        result = await db.execute(
            select(TwilioRecipient)
            .order_by(TwilioRecipient.id)
        )
        recipients = result.scalars().all()

        return [
            TwilioRecipientResponse(
                id=r.id,
                name=r.name,
                phone_number=r.phone_number,
                enabled=r.enabled,
                created_at=r.created_at.isoformat(),
                last_notified_at=r.last_notified_at.isoformat() if r.last_notified_at else None,
            )
            for r in recipients
        ]

    except Exception as e:
        logger.error(f"Error listing Twilio recipients: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to list Twilio recipients",
        )


@router.post(
    "/twilio/recipients",
    response_model=TwilioRecipientResponse,
    summary="Add Twilio Recipient",
    description="Add a new Twilio SMS recipient. Requires admin role.",
)
async def add_twilio_recipient(
    recipient: TwilioRecipientRequest,
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """Add a new Twilio SMS recipient."""
    # Check role
    role = current_api_key.get("role", "").lower()
    if role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requires admin role",
        )


    try:
        from models.twilio_recipient import TwilioRecipient

        # Check if phone number already exists
        existing = await db.execute(
            select(TwilioRecipient).where(
                TwilioRecipient.phone_number == recipient.phone_number,
            )
        )
        if existing.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="This phone number is already registered",
            )

        # Create recipient
        new_recipient = TwilioRecipient(
            name=recipient.name,
            phone_number=recipient.phone_number,
            enabled=True,
            created_at=datetime.utcnow(),
        )

        db.add(new_recipient)


        await db.commit()
        await db.refresh(new_recipient)

        logger.info(f"Twilio recipient added: {recipient.name}")

        return TwilioRecipientResponse(
            id=new_recipient.id,
            name=new_recipient.name,
            phone_number=new_recipient.phone_number,
            enabled=new_recipient.enabled,
            created_at=new_recipient.created_at.isoformat(),
            last_notified_at=None,
        )

    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        from services.error_reporting import internal_http_error
        raise internal_http_error(e, "Failed to add Twilio recipient.", action="notifications.twilio_recipient")


@router.delete(
    "/twilio/recipients/{recipient_id}",
    summary="Delete Twilio Recipient",
    description="Delete a Twilio SMS recipient. Requires admin role.",
)
async def delete_twilio_recipient(
    recipient_id: int,
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """Delete a Twilio SMS recipient."""
    # Check role
    role = current_api_key.get("role", "").lower()
    if role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requires admin role",
        )


    try:
        from models.twilio_recipient import TwilioRecipient

        result = await db.execute(
            select(TwilioRecipient).where(
                TwilioRecipient.id == recipient_id,
            )
        )
        recipient = result.scalar_one_or_none()

        if not recipient:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Recipient not found",
            )


        await db.delete(recipient)
        await db.commit()

        logger.info(f"Twilio recipient deleted: {recipient.name}")

        return {"status": "success", "message": "Recipient deleted"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting Twilio recipient: {e}")
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete Twilio recipient",
        )


@router.patch(
    "/twilio/recipients/{recipient_id}/toggle",
    response_model=TwilioRecipientResponse,
    summary="Toggle Twilio Recipient",
    description="Enable or disable a Twilio SMS recipient. Requires admin role.",
)
async def toggle_twilio_recipient(
    recipient_id: int,
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """Toggle recipient enabled status."""
    # Check role
    role = current_api_key.get("role", "").lower()
    if role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requires admin role",
        )


    try:
        from models.twilio_recipient import TwilioRecipient

        result = await db.execute(
            select(TwilioRecipient).where(
                TwilioRecipient.id == recipient_id,
            )
        )
        recipient = result.scalar_one_or_none()

        if not recipient:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Recipient not found",
            )

        recipient.enabled = not recipient.enabled
        await db.commit()
        await db.refresh(recipient)

        logger.info(f"Twilio recipient {'enabled' if recipient.enabled else 'disabled'}: {recipient.name}")

        return TwilioRecipientResponse(
            id=recipient.id,
            name=recipient.name,
            phone_number=recipient.phone_number,
            enabled=recipient.enabled,
            created_at=recipient.created_at.isoformat(),
            last_notified_at=recipient.last_notified_at.isoformat() if recipient.last_notified_at else None,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error toggling Twilio recipient: {e}")
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to toggle Twilio recipient",
        )


# ============================================================================
# WHATSAPP NOTIFICATION ENDPOINTS
# ============================================================================

# Pydantic models for WhatsApp configuration
class WhatsAppConfigRequest(BaseModel):
    """Request model for WhatsApp configuration."""
    from_number: str = Field(..., min_length=1, description="WhatsApp number (whatsapp:+1234567890 format)")
    enabled: bool = Field(default=True, description="Enable WhatsApp notifications")


class WhatsAppConfigResponse(BaseModel):
    """Response model for WhatsApp configuration."""
    configured: bool
    enabled: bool
    from_number: Optional[str] = None
    twilio_configured: bool = False  # Indicates if Twilio dependency is met


@router.get(
    "/whatsapp/config",
    response_model=WhatsAppConfigResponse,
    summary="Get WhatsApp Configuration",
    description="Get current WhatsApp configuration. WhatsApp uses Twilio credentials automatically.",
)
async def get_whatsapp_config(
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """Get WhatsApp configuration.

    WhatsApp uses Twilio credentials automatically. This endpoint only returns
    the WhatsApp-specific from_number. WhatsApp is only "configured" if BOTH:
    1. Twilio is configured (provides credentials)
    2. WhatsApp has a from_number set
    """
    try:
        from models.whatsapp_config import WhatsAppConfig
        from models.twilio_config import TwilioConfig

        # Check if Twilio is configured (required dependency)
        twilio_result = await db.execute(select(TwilioConfig).limit(1))
        twilio_config = twilio_result.scalar_one_or_none()
        twilio_configured = bool(twilio_config and twilio_config.enabled)

        # Check for WhatsApp config
        result = await db.execute(select(WhatsAppConfig).limit(1))
        config = result.scalar_one_or_none()

        if config and config.from_number:
            # WhatsApp is configured if both Twilio AND WhatsApp have required fields
            return WhatsAppConfigResponse(
                configured=twilio_configured and bool(config.from_number),
                enabled=config.enabled,
                from_number=config.from_number,
                twilio_configured=twilio_configured,
            )

        # WhatsApp not configured yet
        return WhatsAppConfigResponse(
            configured=False,
            enabled=False,
            from_number="",
            twilio_configured=twilio_configured,
        )

    except Exception as e:
        logger.error(f"Error getting WhatsApp config: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get WhatsApp configuration",
        )


@router.post(
    "/whatsapp/config",
    summary="Update WhatsApp Configuration",
    description="Update WhatsApp configuration. Uses Twilio credentials automatically. Requires admin role.",
)
async def update_whatsapp_config(
    request: WhatsAppConfigRequest,
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """Update WhatsApp configuration.

    WhatsApp uses Twilio API, so only the WhatsApp number needs to be configured.
    Twilio credentials are automatically pulled from the Twilio config.
    """
    # Only platform admins configure global notification providers.
    if not current_api_key.get("is_platform_admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requires platform admin role",
        )

    try:
        from models.whatsapp_config import WhatsAppConfig
        from models.twilio_config import TwilioConfig

        # Verify Twilio is configured
        twilio_result = await db.execute(select(TwilioConfig).limit(1))
        twilio_config = twilio_result.scalar_one_or_none()

        if not twilio_config:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Twilio must be configured first. WhatsApp uses Twilio credentials.",
            )

        # Get or create WhatsApp config
        result = await db.execute(select(WhatsAppConfig).limit(1))
        config = result.scalar_one_or_none()

        if config:
            # Update only WhatsApp-specific fields
            config.from_number = request.from_number
            config.enabled = request.enabled
            config.updated_at = datetime.utcnow()
            # Clear deprecated fields
            config.account_sid = None
            config.auth_token = None
        else:
            # Create new (no credentials stored)
            config = WhatsAppConfig(
                from_number=request.from_number,
                enabled=request.enabled,
                account_sid=None,
                auth_token=None,
            )
            db.add(config)


        await db.commit()
        logger.info(f"WhatsApp configuration updated by {current_api_key.get('label')}")

        return {"success": True, "message": "WhatsApp configuration updated"}

    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        from services.error_reporting import internal_http_error
        raise internal_http_error(e, "Failed to update WhatsApp configuration.", action="notifications.whatsapp_config")


class TestWhatsAppRequest(BaseModel):
    """Request model for test WhatsApp message."""
    to_number: str = Field(..., min_length=1, description="Recipient WhatsApp number (E.164 format)")


@router.post(
    "/whatsapp/test",
    summary="Send Test WhatsApp Message",
    description="Send a test WhatsApp message. Requires operator role.",
)
async def send_test_whatsapp_message(
    request: TestWhatsAppRequest,
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """Send a test WhatsApp message."""
    # Check role
    role = current_api_key.get("role", "").lower()
    if role not in ["operator", "admin"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requires operator role or higher",
        )

    try:
        from models.whatsapp_config import WhatsAppConfig
        from models.twilio_config import TwilioConfig
        from notifications.whatsapp import WhatsAppNotifier

        # Get WhatsApp config for from_number
        whatsapp_result = await db.execute(select(WhatsAppConfig).limit(1))
        whatsapp_config = whatsapp_result.scalar_one_or_none()

        if not whatsapp_config or not whatsapp_config.enabled:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="WhatsApp not configured or disabled",
            )

        # Get credentials from Twilio config
        twilio_result = await db.execute(select(TwilioConfig).limit(1))
        twilio_config = twilio_result.scalar_one_or_none()

        if not twilio_config:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Twilio not configured. WhatsApp requires Twilio credentials.",
            )

        try:
            decrypted_wa_token = SecretEncryption.decrypt_secret(twilio_config.auth_token)
        except Exception:
            decrypted_wa_token = twilio_config.auth_token

        notifier = WhatsAppNotifier(
            account_sid=twilio_config.account_sid,
            auth_token=decrypted_wa_token,
            from_number=whatsapp_config.from_number,
        )

        result = await notifier.send_test_message(request.to_number)

        logger.info(f"Test WhatsApp message sent to {request.to_number}")

        return {"success": True, "message": f"Test WhatsApp message sent to {request.to_number}"}

    except HTTPException:
        raise
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Error sending test WhatsApp message: {e}")

        # Provide helpful error message for common Twilio WhatsApp errors
        if "63007" in error_msg or "could not find a Channel" in error_msg:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "WhatsApp number not enabled in Twilio. "
                    "Please enable WhatsApp on this number in your Twilio console, "
                    "or use the Twilio WhatsApp Sandbox for testing. "
                    "See: https://console.twilio.com/us1/develop/sms/senders/whatsapp-senders"
                ),
            )

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to send test WhatsApp message: {error_msg}",
        )


class WhatsAppRecipientRequest(BaseModel):
    """Request model for adding WhatsApp recipient."""
    name: str = Field(..., min_length=1, max_length=255, description="Friendly name for recipient")
    phone_number: str = Field(..., min_length=1, description="WhatsApp phone number in E.164 format")


class WhatsAppRecipientResponse(BaseModel):
    """Response model for WhatsApp recipient."""
    id: int
    name: str
    phone_number: str
    enabled: bool
    created_at: str
    last_notified_at: Optional[str]


@router.get(
    "/whatsapp/recipients",
    response_model=List[WhatsAppRecipientResponse],
    summary="List WhatsApp Recipients",
    description="Get all WhatsApp recipients. Requires admin role.",
)
async def list_whatsapp_recipients(
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """List all WhatsApp recipients."""
    # Check role
    role = current_api_key.get("role", "").lower()
    if role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requires admin role",
        )


    try:
        from models.whatsapp_recipient import WhatsAppRecipient

        result = await db.execute(
            select(WhatsAppRecipient)
            .order_by(WhatsAppRecipient.id)
        )
        recipients = result.scalars().all()

        return [
            WhatsAppRecipientResponse(
                id=r.id,
                name=r.name,
                phone_number=r.phone_number,
                enabled=r.enabled,
                created_at=r.created_at.isoformat(),
                last_notified_at=r.last_notified_at.isoformat() if r.last_notified_at else None,
            )
            for r in recipients
        ]

    except Exception as e:
        logger.error(f"Error listing WhatsApp recipients: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to list WhatsApp recipients",
        )


@router.post(
    "/whatsapp/recipients",
    response_model=WhatsAppRecipientResponse,
    summary="Add WhatsApp Recipient",
    description="Add a new WhatsApp recipient. Requires admin role.",
)
async def add_whatsapp_recipient(
    recipient: WhatsAppRecipientRequest,
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """Add a new WhatsApp recipient."""
    # Check role
    role = current_api_key.get("role", "").lower()
    if role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requires admin role",
        )


    try:
        from models.whatsapp_recipient import WhatsAppRecipient

        # Check if phone number already exists
        existing = await db.execute(
            select(WhatsAppRecipient).where(
                WhatsAppRecipient.phone_number == recipient.phone_number,
            )
        )
        if existing.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="This phone number is already registered",
            )

        # Create recipient
        new_recipient = WhatsAppRecipient(
            name=recipient.name,
            phone_number=recipient.phone_number,
            enabled=True,
            created_at=datetime.utcnow(),
        )

        db.add(new_recipient)


        await db.commit()
        await db.refresh(new_recipient)

        logger.info(f"WhatsApp recipient added: {recipient.name}")

        return WhatsAppRecipientResponse(
            id=new_recipient.id,
            name=new_recipient.name,
            phone_number=new_recipient.phone_number,
            enabled=new_recipient.enabled,
            created_at=new_recipient.created_at.isoformat(),
            last_notified_at=None,
        )

    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        from services.error_reporting import internal_http_error
        raise internal_http_error(e, "Failed to add WhatsApp recipient.", action="notifications.whatsapp_recipient")


@router.delete(
    "/whatsapp/recipients/{recipient_id}",
    summary="Delete WhatsApp Recipient",
    description="Delete a WhatsApp recipient. Requires admin role.",
)
async def delete_whatsapp_recipient(
    recipient_id: int,
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """Delete a WhatsApp recipient."""
    # Check role
    role = current_api_key.get("role", "").lower()
    if role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requires admin role",
        )


    try:
        from models.whatsapp_recipient import WhatsAppRecipient

        result = await db.execute(
            select(WhatsAppRecipient).where(
                WhatsAppRecipient.id == recipient_id,
            )
        )
        recipient = result.scalar_one_or_none()

        if not recipient:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Recipient not found",
            )


        await db.delete(recipient)
        await db.commit()

        logger.info(f"WhatsApp recipient deleted: {recipient.name}")

        return {"status": "success", "message": "Recipient deleted"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting WhatsApp recipient: {e}")
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete WhatsApp recipient",
        )


@router.patch(
    "/whatsapp/recipients/{recipient_id}/toggle",
    response_model=WhatsAppRecipientResponse,
    summary="Toggle WhatsApp Recipient",
    description="Enable or disable a WhatsApp recipient. Requires admin role.",
)
async def toggle_whatsapp_recipient(
    recipient_id: int,
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """Toggle recipient enabled status."""
    # Check role
    role = current_api_key.get("role", "").lower()
    if role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requires admin role",
        )


    try:
        from models.whatsapp_recipient import WhatsAppRecipient

        result = await db.execute(
            select(WhatsAppRecipient).where(
                WhatsAppRecipient.id == recipient_id,
            )
        )
        recipient = result.scalar_one_or_none()

        if not recipient:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Recipient not found",
            )

        recipient.enabled = not recipient.enabled
        await db.commit()
        await db.refresh(recipient)

        logger.info(f"WhatsApp recipient {'enabled' if recipient.enabled else 'disabled'}: {recipient.name}")

        return WhatsAppRecipientResponse(
            id=recipient.id,
            name=recipient.name,
            phone_number=recipient.phone_number,
            enabled=recipient.enabled,
            created_at=recipient.created_at.isoformat(),
            last_notified_at=recipient.last_notified_at.isoformat() if recipient.last_notified_at else None,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error toggling WhatsApp recipient: {e}")
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to toggle WhatsApp recipient",
        )


# ============================================================================
# SIGNAL NOTIFICATION ENDPOINTS
# ============================================================================

# Pydantic models for Signal configuration
class SignalConfigRequest(BaseModel):
    """Request model for Signal configuration."""
    api_url: str = Field(..., min_length=1, description="Signal API URL")
    sender_number: str = Field(..., min_length=1, description="Sender phone number (E.164 format)")
    enabled: bool = Field(default=True, description="Enable Signal notifications")


class SignalConfigResponse(BaseModel):
    """Response model for Signal configuration."""
    configured: bool
    enabled: bool
    api_url: Optional[str] = None
    sender_number: Optional[str] = None


@router.get(
    "/signal/config",
    response_model=SignalConfigResponse,
    summary="Get Signal Configuration",
    description="Get current Signal configuration.",
)
async def get_signal_config(
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """Get Signal configuration."""
    try:
        from models.signal_config import SignalConfig

        result = await db.execute(select(SignalConfig).limit(1))
        config = result.scalar_one_or_none()

        if not config:
            return SignalConfigResponse(
                configured=False,
                enabled=False,
            )

        return SignalConfigResponse(
            configured=True,
            enabled=config.enabled,
            api_url=config.api_url,
            sender_number=config.sender_number,
        )

    except Exception as e:
        logger.error(f"Error getting Signal config: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get Signal configuration",
        )


@router.post(
    "/signal/config",
    summary="Update Signal Configuration",
    description="Update Signal configuration. Requires admin role.",
)
async def update_signal_config(
    request: SignalConfigRequest,
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """Update Signal configuration."""
    # Only platform admins configure global notification providers.
    if not current_api_key.get("is_platform_admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requires platform admin role",
        )

    try:
        from models.signal_config import SignalConfig

        result = await db.execute(select(SignalConfig).limit(1))
        config = result.scalar_one_or_none()

        if config:
            config.api_url = request.api_url
            config.sender_number = request.sender_number
            config.enabled = request.enabled
            config.updated_at = datetime.utcnow()
        else:
            config = SignalConfig(
                api_url=request.api_url,
                sender_number=request.sender_number,
                enabled=request.enabled,
            )
            db.add(config)


        await db.commit()
        logger.info(f"Signal configuration updated by {current_api_key.get('label')}")

        return {"success": True, "message": "Signal configuration updated"}

    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        from services.error_reporting import internal_http_error
        raise internal_http_error(e, "Failed to update Signal configuration.", action="notifications.signal_config")


class TestSignalRequest(BaseModel):
    """Request model for test Signal message."""
    to_number: str = Field(..., min_length=1, description="Recipient Signal number (E.164 format)")


@router.post(
    "/signal/test",
    summary="Send Test Signal Message",
    description="Send a test Signal message. Requires operator role.",
)
async def send_test_signal_message(
    request: TestSignalRequest,
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """Send a test Signal message."""
    # Check role
    role = current_api_key.get("role", "").lower()
    if role not in ["operator", "admin"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requires operator role or higher",
        )

    try:
        from models.signal_config import SignalConfig
        from notifications.signal import SignalNotifier

        result = await db.execute(select(SignalConfig).limit(1))
        config = result.scalar_one_or_none()

        if not config or not config.enabled:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Signal not configured or disabled",
            )

        notifier = SignalNotifier(
            api_url=config.api_url,
            sender_number=config.sender_number,
        )

        result = await notifier.send_test_message(request.to_number)

        logger.info(f"Test Signal message sent to {request.to_number}")

        return {"success": True, "message": f"Test Signal message sent to {request.to_number}"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error sending test Signal message: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to send test Signal message: {str(e)}",
        )


class SignalRecipientRequest(BaseModel):
    """Request model for adding Signal recipient."""
    name: str = Field(..., min_length=1, max_length=255, description="Friendly name for recipient")
    phone_number: str = Field(..., min_length=1, description="Signal phone number in E.164 format")


class SignalRecipientResponse(BaseModel):
    """Response model for Signal recipient."""
    id: int
    name: str
    phone_number: str
    enabled: bool
    created_at: str
    last_notified_at: Optional[str]


@router.get(
    "/signal/recipients",
    response_model=List[SignalRecipientResponse],
    summary="List Signal Recipients",
    description="Get all Signal recipients. Requires admin role.",
)
async def list_signal_recipients(
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """List all Signal recipients."""
    # Check role
    role = current_api_key.get("role", "").lower()
    if role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requires admin role",
        )


    try:
        result = await db.execute(
            select(SignalRecipient)
            .order_by(SignalRecipient.id)
        )
        recipients = result.scalars().all()

        return [
            SignalRecipientResponse(
                id=r.id,
                name=r.name,
                phone_number=r.phone_number,
                enabled=r.enabled,
                created_at=r.created_at.isoformat(),
                last_notified_at=r.last_notified_at.isoformat() if r.last_notified_at else None,
            )
            for r in recipients
        ]

    except Exception as e:
        logger.error(f"Error listing Signal recipients: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to list Signal recipients",
        )


@router.post(
    "/signal/recipients",
    response_model=SignalRecipientResponse,
    summary="Add Signal Recipient",
    description="Add a new Signal recipient. Requires admin role.",
)
async def add_signal_recipient(
    recipient: SignalRecipientRequest,
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """Add a new Signal recipient."""
    # Check role
    role = current_api_key.get("role", "").lower()
    if role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requires admin role",
        )


    try:
        # Check if phone number already exists
        existing = await db.execute(
            select(SignalRecipient).where(
                SignalRecipient.phone_number == recipient.phone_number,
            )
        )
        if existing.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="This phone number is already registered",
            )

        # Create recipient
        new_recipient = SignalRecipient(
            name=recipient.name,
            phone_number=recipient.phone_number,
            enabled=True,
            created_at=datetime.utcnow(),
        )

        db.add(new_recipient)


        await db.commit()
        await db.refresh(new_recipient)

        logger.info(f"Signal recipient added: {recipient.name}")

        return SignalRecipientResponse(
            id=new_recipient.id,
            name=new_recipient.name,
            phone_number=new_recipient.phone_number,
            enabled=new_recipient.enabled,
            created_at=new_recipient.created_at.isoformat(),
            last_notified_at=None,
        )

    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        from services.error_reporting import internal_http_error
        raise internal_http_error(e, "Failed to add Signal recipient.", action="notifications.signal_recipient")


@router.delete(
    "/signal/recipients/{recipient_id}",
    summary="Delete Signal Recipient",
    description="Delete a Signal recipient. Requires admin role.",
)
async def delete_signal_recipient(
    recipient_id: int,
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """Delete a Signal recipient."""
    # Check role
    role = current_api_key.get("role", "").lower()
    if role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requires admin role",
        )


    try:
        result = await db.execute(
            select(SignalRecipient).where(
                SignalRecipient.id == recipient_id,
            )
        )
        recipient = result.scalar_one_or_none()

        if not recipient:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Recipient not found",
            )


        await db.delete(recipient)
        await db.commit()

        logger.info(f"Signal recipient deleted: {recipient.name}")

        return {"status": "success", "message": "Recipient deleted"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting Signal recipient: {e}")
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete Signal recipient",
        )


@router.patch(
    "/signal/recipients/{recipient_id}/toggle",
    response_model=SignalRecipientResponse,
    summary="Toggle Signal Recipient",
    description="Enable or disable a Signal recipient. Requires admin role.",
)
async def toggle_signal_recipient(
    recipient_id: int,
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """Toggle recipient enabled status."""
    # Check role
    role = current_api_key.get("role", "").lower()
    if role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requires admin role",
        )


    try:
        result = await db.execute(
            select(SignalRecipient).where(
                SignalRecipient.id == recipient_id,
            )
        )
        recipient = result.scalar_one_or_none()

        if not recipient:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Recipient not found",
            )

        recipient.enabled = not recipient.enabled
        await db.commit()
        await db.refresh(recipient)

        logger.info(f"Signal recipient {'enabled' if recipient.enabled else 'disabled'}: {recipient.name}")

        return SignalRecipientResponse(
            id=recipient.id,
            name=recipient.name,
            phone_number=recipient.phone_number,
            enabled=recipient.enabled,
            created_at=recipient.created_at.isoformat(),
            last_notified_at=recipient.last_notified_at.isoformat() if recipient.last_notified_at else None,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error toggling Signal recipient: {e}")
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to toggle Signal recipient",
        )


@router.get(
    "/providers/all",
    summary="Get All Notification Providers",
    description="Get status of all notification providers.",
)
async def get_all_providers(
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """Get status of all notification providers."""
    try:
        from models.pushover_config import PushoverConfig
        from models.email_config import EmailConfig
        from models.twilio_config import TwilioConfig
        from models.whatsapp_config import WhatsAppConfig
        from models.signal_config import SignalConfig
        from models.webhook_config import WebhookConfig

        providers = {}

        # Pushover
        result = await db.execute(select(PushoverConfig).limit(1))
        pushover = result.scalar_one_or_none()
        providers["pushover"] = {
            "name": "Pushover",
            "configured": bool(pushover),
            "enabled": pushover.enabled if pushover else False,
        }

        # Email
        result = await db.execute(select(EmailConfig).limit(1))
        email = result.scalar_one_or_none()
        providers["email"] = {
            "name": "Email (SMTP)",
            "configured": bool(email),
            "enabled": email.enabled if email else False,
        }

        # Twilio SMS
        result = await db.execute(select(TwilioConfig).limit(1))
        twilio = result.scalar_one_or_none()
        providers["twilio"] = {
            "name": "Twilio SMS",
            "configured": bool(twilio),
            "enabled": twilio.enabled if twilio else False,
        }

        # WhatsApp (requires Twilio to be configured)
        result = await db.execute(select(WhatsAppConfig).limit(1))
        whatsapp = result.scalar_one_or_none()
        # WhatsApp is only configured if BOTH Twilio is configured AND WhatsApp has from_number
        whatsapp_configured = bool(
            twilio and
            twilio.enabled and
            whatsapp and
            whatsapp.from_number
        )
        providers["whatsapp"] = {
            "name": "WhatsApp",
            "configured": whatsapp_configured,
            "enabled": whatsapp.enabled if whatsapp else False,
        }

        # Signal
        result = await db.execute(select(SignalConfig).limit(1))
        signal = result.scalar_one_or_none()
        providers["signal"] = {
            "name": "Signal",
            "configured": bool(signal),
            "enabled": signal.enabled if signal else False,
        }

        # Webhook
        result = await db.execute(select(WebhookConfig).limit(1))
        webhook = result.scalar_one_or_none()
        providers["webhook"] = {
            "name": "Webhook",
            "configured": bool(webhook),
            "enabled": webhook.enabled if webhook else False,
        }

        return {"providers": providers}

    except Exception as e:
        logger.error(f"Error getting all providers: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get providers status",
        )


# Notification Settings Endpoints

class NotificationSettingsRequest(BaseModel):
    """Request model for notification settings."""
    quiet_hours_enabled: bool = False
    quiet_hours_start: Optional[str] = None
    quiet_hours_end: Optional[str] = None
    rate_limiting_enabled: bool = False
    rate_limit_max: int = 5
    rate_limit_period: str = 'hour'
    batch_enabled: bool = False
    batch_window_minutes: int = 5
    agent_errors_enabled: bool = False
    agent_error_threshold: int = 3
    agent_error_delay_minutes: int = 15
    target_health_enabled: bool = False
    target_health_threshold: int = 5
    target_health_check_interval: int = 10
    system_health_enabled: bool = False
    system_health_agent_disconnect_delay: int = 5


class NotificationSettingsResponse(BaseModel):
    """Response model for notification settings."""
    quiet_hours_enabled: bool
    quiet_hours_start: Optional[str]
    quiet_hours_end: Optional[str]
    rate_limiting_enabled: bool
    rate_limit_max: int
    rate_limit_period: str
    batch_enabled: bool
    batch_window_minutes: int
    agent_errors_enabled: bool
    agent_error_threshold: int
    agent_error_delay_minutes: int
    target_health_enabled: bool
    target_health_threshold: int
    target_health_check_interval: int
    system_health_enabled: bool
    system_health_agent_disconnect_delay: int


@router.get(
    "/settings",
    response_model=NotificationSettingsResponse,
    summary="Get Notification Settings",
    description="Get global notification trigger settings.",
)
async def get_notification_settings(
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """Get notification settings."""
    try:
        from models.notification_settings import NotificationSettings

        result = await db.execute(
            select(NotificationSettings).where(NotificationSettings.id == 1)
        )
        settings = result.scalar_one_or_none()

        if not settings:
            # Create default settings if not exists
            settings = NotificationSettings(id=1)
            db.add(settings)
            await db.commit()
            await db.refresh(settings)

        return NotificationSettingsResponse(
            quiet_hours_enabled=settings.quiet_hours_enabled,
            quiet_hours_start=settings.quiet_hours_start,
            quiet_hours_end=settings.quiet_hours_end,
            rate_limiting_enabled=settings.rate_limiting_enabled,
            rate_limit_max=settings.rate_limit_max,
            rate_limit_period=settings.rate_limit_period,
            batch_enabled=settings.batch_enabled,
            batch_window_minutes=settings.batch_window_minutes,
            agent_errors_enabled=settings.agent_errors_enabled,
            agent_error_threshold=settings.agent_error_threshold,
            agent_error_delay_minutes=settings.agent_error_delay_minutes,
            target_health_enabled=settings.target_health_enabled,
            target_health_threshold=settings.target_health_threshold,
            target_health_check_interval=settings.target_health_check_interval,
            system_health_enabled=settings.system_health_enabled,
            system_health_agent_disconnect_delay=settings.system_health_agent_disconnect_delay,
        )

    except Exception as e:
        logger.error(f"Error getting notification settings: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get notification settings",
        )


@router.put(
    "/settings",
    response_model=NotificationSettingsResponse,
    summary="Update Notification Settings",
    description="Update global notification trigger settings. Requires admin role.",
)
async def update_notification_settings(
    request: NotificationSettingsRequest,
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """Update notification settings."""
    # The id==1 row is the single global settings row (quiet-hours / rate-limit /
    # health-alert behavior). Only platform admins may mutate it.
    if not current_api_key.get("is_platform_admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requires platform admin role",
        )

    try:
        from models.notification_settings import NotificationSettings

        result = await db.execute(
            select(NotificationSettings).where(NotificationSettings.id == 1)
        )
        settings = result.scalar_one_or_none()

        if not settings:
            # Create if not exists
            settings = NotificationSettings(id=1)
            db.add(settings)

        # Update settings
        settings.quiet_hours_enabled = request.quiet_hours_enabled
        settings.quiet_hours_start = request.quiet_hours_start
        settings.quiet_hours_end = request.quiet_hours_end
        settings.rate_limiting_enabled = request.rate_limiting_enabled
        settings.rate_limit_max = request.rate_limit_max
        settings.rate_limit_period = request.rate_limit_period
        settings.batch_enabled = request.batch_enabled
        settings.batch_window_minutes = request.batch_window_minutes
        settings.agent_errors_enabled = request.agent_errors_enabled
        settings.agent_error_threshold = request.agent_error_threshold
        settings.agent_error_delay_minutes = request.agent_error_delay_minutes
        settings.target_health_enabled = request.target_health_enabled
        settings.target_health_threshold = request.target_health_threshold
        settings.target_health_check_interval = request.target_health_check_interval
        settings.system_health_enabled = request.system_health_enabled
        settings.system_health_agent_disconnect_delay = request.system_health_agent_disconnect_delay
        settings.updated_at = datetime.utcnow()

        await db.commit()
        await db.refresh(settings)

        logger.info(f"Notification settings updated by {current_api_key.get('label')}")

        return NotificationSettingsResponse(
            quiet_hours_enabled=settings.quiet_hours_enabled,
            quiet_hours_start=settings.quiet_hours_start,
            quiet_hours_end=settings.quiet_hours_end,
            rate_limiting_enabled=settings.rate_limiting_enabled,
            rate_limit_max=settings.rate_limit_max,
            rate_limit_period=settings.rate_limit_period,
            batch_enabled=settings.batch_enabled,
            batch_window_minutes=settings.batch_window_minutes,
            agent_errors_enabled=settings.agent_errors_enabled,
            agent_error_threshold=settings.agent_error_threshold,
            agent_error_delay_minutes=settings.agent_error_delay_minutes,
            target_health_enabled=settings.target_health_enabled,
            target_health_threshold=settings.target_health_threshold,
            target_health_check_interval=settings.target_health_check_interval,
            system_health_enabled=settings.system_health_enabled,
            system_health_agent_disconnect_delay=settings.system_health_agent_disconnect_delay,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating notification settings: {e}")
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update notification settings",
        )


@router.get(
    "/health-check",
    summary="Run Health Checks",
    description="Run health monitoring checks for agents, targets, and system. Requires admin role.",
)
async def run_health_check(
    db: AsyncSession = Depends(get_db),
    current_api_key: dict = Depends(get_current_api_key),
):
    """Run health monitoring checks."""
    # Check admin role
    if current_api_key.get("role") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can run health checks",
        )

    try:
        from services.health_monitor import HealthMonitor

        monitor = HealthMonitor(db)
        results = await monitor.run_health_checks()

        return {
            "success": True,
            "data": results,
        }

    except Exception as e:
        logger.error(f"Error running health check: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to run health check",
        )
