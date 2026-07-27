"""
Platform notifier — preference-aware, multi-channel delivery for PLATFORM-WIDE
events on the single-owner coordinator (runs, agents).

Self-host counterpart of the cloud platform notifier:
the recipient is always THE OWNER (the coordinator has exactly one user), so
there is no org fan-out — one preference row, one effective channel map.

This is the single entry point emitters should use instead of calling
`notification_service.create_notification` directly. It consults
`UserNotificationPreference` (merged over the catalog defaults in
`notifications/catalog.py`) and delivers on every enabled channel:

  in_app   → Notification row (bell + inbox) via notification_service
  email    → the owner's account email (User.email) through the coordinator's
             own SMTP config (models/email_config.py + notifications/email.py)
  sms/whatsapp/signal → the owner's personal phone (preference row) through the
             coordinator's Twilio/WhatsApp/Signal provider configs
  pushover → the owner's personal Pushover key through the configured app token

Like `create_notification`, `notify()` NEVER raises: notifications are side
effects of a primary action and must not break it. Per-channel failures are
logged and swallowed. Unknown events fall back to an ungated in-app row so a
new emitter is never silently dropped before the catalog learns about it.

Per-monitor change alerts do NOT go through here — they keep the per-target
dispatcher (`notifications/dispatcher.py`) and its operator recipient lists.
"""
import asyncio
import logging
from typing import Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from notifications import catalog
from models.user import User
from models.user_notification_preference import UserNotificationPreference
from services.notification_service import create_notification

logger = logging.getLogger(__name__)


def _decrypt(value: Optional[str]) -> Optional[str]:
    """Decrypt a Fernet-encrypted provider secret, tolerating legacy plaintext."""
    if not value:
        return value
    try:
        from security.encryption import SecretEncryption
        return SecretEncryption.decrypt_secret(value)
    except Exception:
        return value


async def _load_owner(db: AsyncSession, user_id: Optional[UUID]) -> Optional[User]:
    """Resolve the recipient: the given user, or the single owner.

    The coordinator is single-owner, so "the owner" is the platform-admin user
    (first-boot bootstrap), falling back to the oldest user row.
    """
    if user_id is not None:
        result = await db.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        if user is not None:
            return user
    result = await db.execute(
        select(User)
        .order_by(User.is_platform_admin.desc(), User.created_at.asc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _load_pref_row(
    db: AsyncSession, user_id: UUID
) -> Optional[UserNotificationPreference]:
    result = await db.execute(
        select(UserNotificationPreference).where(
            UserNotificationPreference.user_id == user_id
        )
    )
    return result.scalar_one_or_none()


async def notify(
    db: AsyncSession,
    *,
    event: str,
    title: str,
    body: Optional[str] = None,
    user_id: Optional[UUID] = None,
    link: Optional[str] = None,
    payload: Optional[dict] = None,
    notification_type: Optional[str] = None,
    email_subject: Optional[str] = None,
) -> None:
    """Deliver one platform event to the owner on their enabled channels.

    `event` accepts a catalog key ("runs.run_failed") or a legacy
    Notification.type ("run_failed"). `notification_type` overrides the type
    string stored on in-app rows (defaults to the legacy type when `event`
    was one, else the catalog key). `user_id` is threaded through to the
    in-app row for its owner-wide-vs-user semantics. Never raises.
    """
    try:
        event_key = catalog.resolve_event(event)
        if event_key is None:
            # Unknown event: preserve the old behavior (in-app row, no gating)
            # so a new emitter is never silently dropped before the catalog
            # learns about it.
            logger.warning(f"platform_notifier: unknown event '{event}', delivering in-app ungated")
            await create_notification(
                db,
                type=notification_type or event,
                title=title,
                body=body,
                user_id=user_id,
                link=link,
                payload=payload,
            )
            return

        stored_type = notification_type or (
            event if event in catalog.TYPE_TO_EVENT else
            (catalog.EVENTS[event_key]["types"][0] if catalog.EVENTS[event_key]["types"] else event_key)
        )

        owner = await _load_owner(db, user_id)
        if owner is None:
            # No user yet (pre-bootstrap edge) — keep the in-app record at least.
            await create_notification(
                db, type=stored_type, title=title, body=body,
                user_id=user_id, link=link, payload=payload,
            )
            return

        pref_row = await _load_pref_row(db, owner.id)
        stored = (pref_row.preferences or {}).get(event_key) if pref_row else None
        effective = catalog.effective_channels(event_key, stored)

        # ---- in-app ---------------------------------------------------------
        if effective.get("in_app"):
            await create_notification(
                db,
                type=stored_type,
                title=title,
                body=body,
                user_id=user_id,  # preserve owner-wide (NULL) vs user-targeted rows
                link=link,
                payload=payload,
            )

        # ---- email ----------------------------------------------------------
        if effective.get("email") and owner.email:
            try:
                await _send_email(db, owner.email, subject=email_subject or title,
                                  title=title, body=body, link=link)
            except Exception as e:
                logger.warning(f"platform_notifier: email channel failed for '{event_key}': {e}")

        # ---- personal phone / pushover channels -----------------------------
        message = title if not body else f"{title}\n\n{body}"
        if link:
            message = f"{message}\n{link}"

        for channel in ("sms", "whatsapp", "signal", "pushover"):
            if not effective.get(channel):
                continue
            try:
                await _send_personal_channel(
                    db, channel, pref_row, title=title, message=message,
                )
            except Exception as e:
                logger.warning(f"platform_notifier: {channel} channel failed for '{event_key}': {e}")

    except Exception as e:  # pragma: no cover — defensive: never break the caller
        logger.warning(f"platform_notifier.notify failed (event={event}): {e}")


async def _send_email(
    db: AsyncSession,
    to_email: str,
    *,
    subject: str,
    title: str,
    body: Optional[str],
    link: Optional[str],
) -> None:
    """Send a platform event email through the coordinator's own SMTP config
    (models/email_config.py), the same provider the dispatcher uses."""
    from models.email_config import EmailConfig
    from notifications.email import EmailNotifier

    config = (await db.execute(select(EmailConfig).limit(1))).scalar_one_or_none()
    if not config or not config.enabled:
        return

    notifier = EmailNotifier(
        smtp_host=config.smtp_host,
        smtp_port=config.smtp_port,
        smtp_username=config.smtp_username,
        smtp_password=_decrypt(config.smtp_password),
        from_email=config.from_email,
        from_name=config.from_name,
        use_tls=config.use_tls,
    )
    if not notifier.enabled:
        return

    text = title if not body else f"{title}\n\n{body}"
    if link:
        text = f"{text}\n\n{link}"
    text += "\n\n---\nThis is an automated notification from Writ"

    # smtplib is blocking — keep it off the event loop.
    await asyncio.to_thread(notifier.send_email, to_email, subject, text)


async def _send_personal_channel(
    db: AsyncSession,
    channel: str,
    pref_row: Optional[UserNotificationPreference],
    *,
    title: str,
    message: str,
) -> None:
    """Deliver to a personal contact-point channel using the coordinator's
    provider configs. Missing contact points / unconfigured providers are
    skipped silently (the matrix stores intent; delivery needs both)."""
    if channel == "pushover":
        from models.pushover_config import PushoverConfig
        from notifications.pushover import PushoverNotifier, decrypt_stored_credential

        key = pref_row.pushover_user_key if pref_row else None
        if not key:
            return
        config = (await db.execute(select(PushoverConfig).limit(1))).scalar_one_or_none()
        if not config or not config.enabled or not config.app_token:
            return
        notifier = PushoverNotifier(
            app_token=decrypt_stored_credential(config.app_token),
            user_key=key,
        )
        await notifier.send_notification(title=title, message=message)
        return

    # Phone-based channels need the owner's personal phone on the pref row.
    phone = pref_row.phone_number if pref_row else None
    if not phone:
        return

    if channel in ("sms", "whatsapp"):
        from models.twilio_config import TwilioConfig

        config = (await db.execute(select(TwilioConfig).limit(1))).scalar_one_or_none()
        if not config or not config.enabled:
            return
        if channel == "sms":
            from notifications.twilio import TwilioNotifier
            notifier = TwilioNotifier(
                account_sid=config.account_sid,
                auth_token=_decrypt(config.auth_token),
                from_phone=config.from_phone,
            )
            await notifier.send_sms(to_phone=phone, message=message)
        else:
            from models.whatsapp_config import WhatsAppConfig
            from notifications.whatsapp import WhatsAppNotifier
            wa = (await db.execute(select(WhatsAppConfig).limit(1))).scalar_one_or_none()
            if not wa or not wa.enabled:
                return
            notifier = WhatsAppNotifier(
                account_sid=config.account_sid,
                auth_token=_decrypt(config.auth_token),
                from_number=wa.from_number,
            )
            await notifier.send_message(to_number=phone, message=message)
        return

    if channel == "signal":
        from models.signal_config import SignalConfig
        from notifications.signal import SignalNotifier

        config = (await db.execute(select(SignalConfig).limit(1))).scalar_one_or_none()
        if not config or not config.enabled:
            return
        notifier = SignalNotifier(api_url=config.api_url, sender_number=config.sender_number)
        await notifier.send_message(to_number=phone, message=message)
