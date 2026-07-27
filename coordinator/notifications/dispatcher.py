"""
Multi-provider notification dispatcher.

Handles sending notifications to multiple providers based on target configuration.
"""
import asyncio
import logging
import re
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_

logger = logging.getLogger(__name__)


def _decrypt_field(value: str) -> str:
    """Decrypt a Fernet-encrypted field, falling back to plaintext for legacy data."""
    if not value:
        return value
    try:
        from security.encryption import SecretEncryption
        return SecretEncryption.decrypt_secret(value)
    except Exception:
        return value


def safe_format(template: str, context: dict) -> str:
    """
    Format template with context, gracefully handling missing keys.

    Unlike str.format(), this function:
    - Leaves unknown placeholders unchanged (e.g., {unknown} stays as {unknown})
    - Handles None values gracefully
    - Truncates long values automatically

    Args:
        template: String with {placeholder} variables
        context: Dict of variable names to values

    Returns:
        Formatted string with placeholders replaced
    """
    def replace(match):
        key = match.group(1)
        value = context.get(key)
        if value is None:
            return match.group(0)  # Keep original placeholder
        return str(value)

    return re.sub(r'\{(\w+)\}', replace, template)


def build_notification_context(
    target,
    agent,
    diff_snippet: str = None,
    detected_change = None,
    selector = None,
) -> Dict[str, Any]:
    """
    Build comprehensive context dict for notification templates.

    Available placeholders:
    - {url} - Target URL
    - {target_id} - Target ID
    - {target_name} - Target name or URL
    - {agent_id} - Agent ID
    - {agent_platform} - Agent platform (desktop, lambda, etc.)
    - {diff} - Diff snippet (truncated)
    - {detected_change} - New content preview
    - {previous_content} - Previous content (truncated)
    - {new_content} - New content (truncated)
    - {content_hash} - Current content hash
    - {previous_hash} - Previous content hash
    - {change_count} - Total change count for target
    - {check_count} - Total check count for target
    - {selector} - CSS selector or 'Full page'
    - {selector_name} - Selector name (for multi-selector)
    - {timestamp} - Detection timestamp (YYYY-MM-DD HH:MM:SS)
    - {date} - Detection date (YYYY-MM-DD)
    - {time} - Detection time (HH:MM:SS)
    """
    now = datetime.now()

    # Determine selector info from selector object or target
    selector_css = target.selector or 'Full page'
    selector_name = None
    if selector:
        selector_css = getattr(selector, 'selector', selector_css)
        selector_name = getattr(selector, 'name', None)

    context = {
        # Target info
        'url': target.url,
        'target_id': target.id,
        'target_name': getattr(target, 'name', None) or target.url,
        'selector': selector_css,
        'selector_name': selector_name or selector_css,
        'change_count': getattr(target, 'change_count', 0) or 0,
        'check_count': getattr(target, 'check_count', 0) or 0,

        # Agent info
        'agent_id': agent.agent_id if agent else 'unknown',
        'agent_platform': getattr(agent, 'platform', 'unknown') if agent else 'unknown',

        # Diff
        'diff': (diff_snippet[:500] if diff_snippet else 'Content hash changed'),

        # Timestamps
        'timestamp': now.strftime('%Y-%m-%d %H:%M:%S'),
        'date': now.strftime('%Y-%m-%d'),
        'time': now.strftime('%H:%M:%S'),
    }

    # Add detected_change fields if available
    if detected_change:
        context.update({
            'detected_change': (detected_change.content_after[:500]
                               if detected_change.content_after else ''),
            'previous_content': (detected_change.content_before[:200]
                                if detected_change.content_before else ''),
            'new_content': (detected_change.content_after[:200]
                          if detected_change.content_after else ''),
            'content_hash': detected_change.content_hash or '',
            'previous_hash': detected_change.previous_hash or '',
        })
    else:
        context.update({
            'detected_change': diff_snippet[:500] if diff_snippet else '',
            'previous_content': '',
            'new_content': '',
            'content_hash': '',
            'previous_hash': '',
        })

    return context


class NotificationDispatcher:
    """
    Unified notification dispatcher for all providers.

    Sends notifications to enabled providers based on target configuration.
    Enforces quiet hours, rate limiting, and batching rules.
    """

    def __init__(self, db: AsyncSession):
        """
        Initialize dispatcher with database session.

        Args:
            db: SQLAlchemy async session
        """
        self.db = db
        self.settings = None

    async def _load_settings(self):
        """Load notification settings from database."""
        if self.settings is not None:
            return self.settings

        try:
            from models.notification_settings import NotificationSettings
            result = await self.db.execute(
                select(NotificationSettings).where(NotificationSettings.id == 1)
            )
            self.settings = result.scalar_one_or_none()
            return self.settings
        except Exception as e:
            logger.error(f"Failed to load notification settings: {e}")
            return None

    async def _is_quiet_hours(self) -> bool:
        """Check if current time is within quiet hours."""
        settings = await self._load_settings()
        if not settings or not settings.quiet_hours_enabled:
            return False

        try:
            now = datetime.now().time()
            start = datetime.strptime(settings.quiet_hours_start, "%H:%M").time()
            end = datetime.strptime(settings.quiet_hours_end, "%H:%M").time()

            # Handle overnight quiet hours (e.g., 22:00 to 08:00)
            if start < end:
                return start <= now <= end
            else:
                return now >= start or now <= end
        except Exception as e:
            logger.error(f"Error checking quiet hours: {e}")
            return False

    async def _check_rate_limit(self, target_id: int) -> bool:
        """
        Check if target has exceeded rate limit.

        Returns:
            True if rate limit exceeded (should skip), False if ok to send
        """
        settings = await self._load_settings()
        if not settings or not settings.rate_limiting_enabled:
            return False

        try:
            from models.notification_log import NotificationLog

            # Calculate time window
            now = datetime.utcnow()
            if settings.rate_limit_period == 'hour':
                window_start = now - timedelta(hours=1)
            elif settings.rate_limit_period == 'day':
                window_start = now - timedelta(days=1)
            elif settings.rate_limit_period == 'week':
                window_start = now - timedelta(weeks=1)
            else:
                window_start = now - timedelta(hours=1)

            # Count notifications in window
            result = await self.db.execute(
                select(func.count(NotificationLog.id))
                .where(
                    and_(
                        NotificationLog.target_id == target_id,
                        NotificationLog.sent_at >= window_start
                    )
                )
            )
            count = result.scalar() or 0

            if count >= settings.rate_limit_max:
                logger.warning(
                    f"Rate limit exceeded for target {target_id}: "
                    f"{count}/{settings.rate_limit_max} in {settings.rate_limit_period}"
                )
                return True

            return False

        except Exception as e:
            logger.error(f"Error checking rate limit: {e}")
            return False

    async def _log_notification(self, target_id: int, provider: str, success: bool):
        """Log notification for rate limiting tracking."""
        try:
            from models.notification_log import NotificationLog

            log = NotificationLog(
                target_id=target_id,
                provider=provider,
                sent_at=datetime.utcnow(),
                success=success,
            )
            self.db.add(log)
            await self.db.commit()
        except Exception as e:
            logger.error(f"Error logging notification: {e}")

    async def dispatch_change_notification(
        self,
        target,
        agent,
        diff_snippet: str = None,
        detected_change = None,
        selector = None,
    ) -> Dict[str, Any]:
        """
        Dispatch change notifications to all enabled providers for a target.

        Enforces quiet hours and rate limiting rules.

        Args:
            target: Target model instance
            agent: Agent model instance
            diff_snippet: Optional diff content
            detected_change: Optional DetectedChange model instance for expanded template variables
            selector: Optional TargetSelector instance (for multi-selector targets)

        Returns:
            Dict with dispatch results per provider
        """
        results = {
            'pushover': {'sent': 0, 'failed': 0, 'skipped': True},
            'email': {'sent': 0, 'failed': 0, 'skipped': True},
            'twilio': {'sent': 0, 'failed': 0, 'skipped': True},
            'whatsapp': {'sent': 0, 'failed': 0, 'skipped': True},
            'signal': {'sent': 0, 'failed': 0, 'skipped': True},
            'webhook': {'sent': 0, 'failed': 0, 'skipped': True},
        }

        # Get enabled providers for this target
        enabled_providers = target.notification_providers or {}

        # If no providers configured, skip notifications
        if not any(enabled_providers.values()):
            logger.info(f"No notification providers enabled for target {target.id}")
            return results

        # Check quiet hours
        if await self._is_quiet_hours():
            logger.info(f"Quiet hours active, skipping notifications for target {target.id}")
            return results

        # Check rate limit
        if await self._check_rate_limit(target.id):
            logger.info(f"Rate limit exceeded, skipping notifications for target {target.id}")
            return results

        # Build notification context with all available template variables
        context = build_notification_context(target, agent, diff_snippet, detected_change, selector)

        # Dispatch to each enabled provider
        if enabled_providers.get('pushover'):
            results['pushover'] = await self._send_pushover(target, agent, diff_snippet, context)
            if results['pushover']['sent'] > 0:
                await self._log_notification(target.id, 'pushover', True)

        if enabled_providers.get('email'):
            results['email'] = await self._send_email(target, agent, diff_snippet, context)
            if results['email']['sent'] > 0:
                await self._log_notification(target.id, 'email', True)

        if enabled_providers.get('twilio'):
            results['twilio'] = await self._send_twilio(target, agent, diff_snippet, context)
            if results['twilio']['sent'] > 0:
                await self._log_notification(target.id, 'twilio', True)

        if enabled_providers.get('whatsapp'):
            results['whatsapp'] = await self._send_whatsapp(target, agent, diff_snippet, context)
            if results['whatsapp']['sent'] > 0:
                await self._log_notification(target.id, 'whatsapp', True)

        if enabled_providers.get('signal'):
            results['signal'] = await self._send_signal(target, agent, diff_snippet, context)
            if results['signal']['sent'] > 0:
                await self._log_notification(target.id, 'signal', True)

        if enabled_providers.get('webhook'):
            results['webhook'] = await self._send_webhook(target, agent, diff_snippet, context, detected_change, selector)
            if results['webhook']['sent'] > 0:
                await self._log_notification(target.id, 'webhook', True)

        return results

    async def dispatch_trigger_notification(
        self,
        target,
        context: Dict[str, Any],
        channels: List[str],
        recipients: List[str] = None,  # Format: ["pushover:1", "email:3", ...]
        title: str = None,
        template: str = None,
        priority: int = None,
        sound: str = None,
        url: str = None,
        email_subject: str = None,
    ) -> Dict[str, Any]:
        """
        Dispatch notification from a trigger block with specific recipients.

        Args:
            target: Target model instance
            context: Template context with all placeholders
            channels: List of channels to send to (pushover, email, twilio, etc.)
            recipients: Optional list of specific recipients in format "provider:id"
            title: Optional notification title
            template: Message template with placeholders
            priority: Optional priority for pushover
            sound: Optional sound for pushover
            url: Optional URL attachment
            email_subject: Optional email subject

        Returns:
            Dict with dispatch results per provider
        """
        results = {
            'pushover': {'sent': 0, 'failed': 0, 'skipped': True},
            'email': {'sent': 0, 'failed': 0, 'skipped': True},
            'twilio': {'sent': 0, 'failed': 0, 'skipped': True},
            'whatsapp': {'sent': 0, 'failed': 0, 'skipped': True},
            'signal': {'sent': 0, 'failed': 0, 'skipped': True},
        }

        if not channels:
            logger.info("No channels specified for trigger notification")
            return results

        # Parse recipient filter if provided
        recipient_filter = {}  # provider -> list of IDs
        if recipients:
            for r in recipients:
                if ':' in r:
                    provider, rid = r.split(':', 1)
                    if provider not in recipient_filter:
                        recipient_filter[provider] = []
                    try:
                        recipient_filter[provider].append(int(rid))
                    except ValueError:
                        pass

        # Format message
        message = safe_format(template or "Change detected on {target_name}", context)
        formatted_title = safe_format(title or "Writ Alert", context) if title else "Writ Alert"
        formatted_url = safe_format(url, context) if url else None
        formatted_subject = safe_format(email_subject or formatted_title, context) if email_subject else formatted_title

        # Dispatch to each channel
        for channel in channels:
            if channel == 'pushover':
                results['pushover'] = await self._send_pushover_to_recipients(
                    target, context, message, formatted_title,
                    recipient_ids=recipient_filter.get('pushover'),
                    priority=priority,
                    sound=sound,
                    url=formatted_url
                )
            elif channel == 'email':
                results['email'] = await self._send_email_to_recipients(
                    target, context, message, formatted_subject,
                    recipient_ids=recipient_filter.get('email')
                )
            elif channel == 'twilio':
                results['twilio'] = await self._send_twilio_to_recipients(
                    target, context, message,
                    recipient_ids=recipient_filter.get('twilio')
                )
            elif channel == 'whatsapp':
                results['whatsapp'] = await self._send_whatsapp_to_recipients(
                    target, context, message,
                    recipient_ids=recipient_filter.get('whatsapp')
                )
            elif channel == 'signal':
                results['signal'] = await self._send_signal_to_recipients(
                    target, context, message,
                    recipient_ids=recipient_filter.get('signal')
                )

        return results

    async def _send_pushover_to_recipients(
        self, target, context: dict, message: str, title: str,
        recipient_ids: List[int] = None, priority: int = None, sound: str = None, url: str = None
    ) -> Dict[str, Any]:
        """Send Pushover notifications to specific recipients."""
        try:
            from models.pushover_recipient import PushoverRecipient
            from models.pushover_config import PushoverConfig
            from notifications.pushover import PushoverNotifier, decrypt_stored_credential

            # Get config
            config_result = await self.db.execute(select(PushoverConfig).limit(1))
            config = config_result.scalar_one_or_none()
            if not config or not config.enabled:
                return {'sent': 0, 'failed': 0, 'skipped': True, 'reason': 'Pushover not configured'}

            # Get all enabled recipients (single-owner coordinator).
            query = select(PushoverRecipient).where(
                PushoverRecipient.enabled == True,
            )
            if recipient_ids:
                query = query.where(PushoverRecipient.id.in_(recipient_ids))
            result = await self.db.execute(query)
            recipients = result.scalars().all()

            if not recipients:
                return {'sent': 0, 'failed': 0, 'skipped': True, 'reason': 'No recipients'}

            sent = 0
            failed = 0

            for recipient in recipients:
                # Create notifier with recipient's user_key
                notifier = PushoverNotifier(
                    app_token=decrypt_stored_credential(config.app_token),
                    user_key=recipient.user_key
                )
                try:
                    result = await notifier.send_notification(
                        title=title,
                        message=message,
                        priority=priority if priority is not None else (config.notification_priority or 0),
                        sound=sound or config.notification_sound,
                        url=url,
                    )
                    if result.get('success'):
                        sent += 1
                        recipient.last_notified_at = datetime.utcnow()
                        logger.info(f"Pushover sent to {recipient.name}")
                    else:
                        failed += 1
                        logger.warning(f"Pushover to {recipient.name} returned no success")
                except Exception as e:
                    logger.error(f"Failed to send Pushover to {recipient.name}: {e}")
                    failed += 1

            await self.db.commit()
            return {'sent': sent, 'failed': failed, 'skipped': False}

        except Exception as e:
            logger.error(f"Error sending Pushover: {e}")
            return {'sent': 0, 'failed': 1, 'skipped': False, 'error': str(e)}

    async def _send_email_to_recipients(
        self, target, context: dict, message: str, subject: str,
        recipient_ids: List[int] = None
    ) -> Dict[str, Any]:
        """Send email notifications to specific recipients."""
        try:
            from models.email_recipient import EmailRecipient
            from models.email_config import EmailConfig
            from notifications.email import EmailNotifier

            config_result = await self.db.execute(select(EmailConfig).limit(1))
            config = config_result.scalar_one_or_none()
            if not config or not config.enabled:
                return {'sent': 0, 'failed': 0, 'skipped': True, 'reason': 'Email not configured'}

            query = select(EmailRecipient).where(
                EmailRecipient.enabled == True,
            )
            if recipient_ids:
                query = query.where(EmailRecipient.id.in_(recipient_ids))
            result = await self.db.execute(query)
            recipients = result.scalars().all()

            if not recipients:
                return {'sent': 0, 'failed': 0, 'skipped': True, 'reason': 'No recipients'}

            notifier = EmailNotifier(
                smtp_host=config.smtp_host,
                smtp_port=config.smtp_port,
                smtp_username=config.smtp_username,
                smtp_password=_decrypt_field(config.smtp_password),
                from_email=config.from_email,
                from_name=config.from_name,
                use_tls=config.use_tls,
            )

            sent = 0
            failed = 0

            for recipient in recipients:
                try:
                    # smtplib is blocking — keep it off the event loop.
                    await asyncio.to_thread(
                        notifier.send_email,
                        to_email=recipient.email,
                        subject=subject,
                        body=message,
                    )
                    sent += 1
                    recipient.last_notified_at = datetime.utcnow()
                except Exception as e:
                    logger.error(f"Failed to send email to {recipient.email}: {e}")
                    failed += 1

            await self.db.commit()
            return {'sent': sent, 'failed': failed, 'skipped': False}

        except Exception as e:
            logger.error(f"Error sending email: {e}")
            return {'sent': 0, 'failed': 1, 'skipped': False, 'error': str(e)}

    async def _send_twilio_to_recipients(
        self, target, context: dict, message: str,
        recipient_ids: List[int] = None
    ) -> Dict[str, Any]:
        """Send Twilio SMS to specific recipients."""
        try:
            from models.twilio_recipient import TwilioRecipient
            from models.twilio_config import TwilioConfig
            from notifications.twilio import TwilioNotifier

            config_result = await self.db.execute(select(TwilioConfig).limit(1))
            config = config_result.scalar_one_or_none()
            if not config or not config.enabled:
                return {'sent': 0, 'failed': 0, 'skipped': True, 'reason': 'Twilio not configured'}

            query = select(TwilioRecipient).where(
                TwilioRecipient.enabled == True,
            )
            if recipient_ids:
                query = query.where(TwilioRecipient.id.in_(recipient_ids))
            result = await self.db.execute(query)
            recipients = result.scalars().all()

            if not recipients:
                return {'sent': 0, 'failed': 0, 'skipped': True, 'reason': 'No recipients'}

            notifier = TwilioNotifier(
                account_sid=config.account_sid,
                auth_token=_decrypt_field(config.auth_token),
                from_phone=config.from_phone,
            )

            sent = 0
            failed = 0

            for recipient in recipients:
                success = await notifier.send_sms(
                    to_phone=recipient.phone_number,
                    message=message,
                )
                if success:
                    sent += 1
                    recipient.last_notified_at = datetime.utcnow()
                else:
                    failed += 1

            await self.db.commit()
            return {'sent': sent, 'failed': failed, 'skipped': False}

        except Exception as e:
            logger.error(f"Error sending Twilio SMS: {e}")
            return {'sent': 0, 'failed': 1, 'skipped': False, 'error': str(e)}

    async def _send_whatsapp_to_recipients(
        self, target, context: dict, message: str,
        recipient_ids: List[int] = None
    ) -> Dict[str, Any]:
        """Send WhatsApp messages to specific recipients."""
        try:
            from models.whatsapp_recipient import WhatsAppRecipient
            from models.whatsapp_config import WhatsAppConfig
            from models.twilio_config import TwilioConfig
            from notifications.whatsapp import WhatsAppNotifier

            wa_config_result = await self.db.execute(select(WhatsAppConfig).limit(1))
            wa_config = wa_config_result.scalar_one_or_none()
            if not wa_config or not wa_config.enabled:
                return {'sent': 0, 'failed': 0, 'skipped': True, 'reason': 'WhatsApp not configured'}

            twilio_config_result = await self.db.execute(select(TwilioConfig).limit(1))
            twilio_config = twilio_config_result.scalar_one_or_none()
            if not twilio_config:
                return {'sent': 0, 'failed': 0, 'skipped': True, 'reason': 'Twilio not configured'}

            query = select(WhatsAppRecipient).where(
                WhatsAppRecipient.enabled == True,
            )
            if recipient_ids:
                query = query.where(WhatsAppRecipient.id.in_(recipient_ids))
            result = await self.db.execute(query)
            recipients = result.scalars().all()

            if not recipients:
                return {'sent': 0, 'failed': 0, 'skipped': True, 'reason': 'No recipients'}

            notifier = WhatsAppNotifier(
                account_sid=twilio_config.account_sid,
                auth_token=_decrypt_field(twilio_config.auth_token),
                from_number=wa_config.from_number,
            )

            sent = 0
            failed = 0

            for recipient in recipients:
                success = await notifier.send_message(
                    to_number=recipient.phone_number,
                    message=message,
                )
                if success:
                    sent += 1
                    recipient.last_notified_at = datetime.utcnow()
                else:
                    failed += 1

            await self.db.commit()
            return {'sent': sent, 'failed': failed, 'skipped': False}

        except Exception as e:
            logger.error(f"Error sending WhatsApp: {e}")
            return {'sent': 0, 'failed': 1, 'skipped': False, 'error': str(e)}

    async def _send_signal_to_recipients(
        self, target, context: dict, message: str,
        recipient_ids: List[int] = None
    ) -> Dict[str, Any]:
        """Send Signal messages to specific recipients."""
        try:
            from models.signal_recipient import SignalRecipient
            from models.signal_config import SignalConfig
            from notifications.signal import SignalNotifier

            config_result = await self.db.execute(select(SignalConfig).limit(1))
            config = config_result.scalar_one_or_none()
            if not config or not config.enabled:
                return {'sent': 0, 'failed': 0, 'skipped': True, 'reason': 'Signal not configured'}

            query = select(SignalRecipient).where(
                SignalRecipient.enabled == True,
            )
            if recipient_ids:
                query = query.where(SignalRecipient.id.in_(recipient_ids))
            result = await self.db.execute(query)
            recipients = result.scalars().all()

            if not recipients:
                return {'sent': 0, 'failed': 0, 'skipped': True, 'reason': 'No recipients'}

            notifier = SignalNotifier(
                api_url=config.api_url,
                sender_number=config.sender_number,
            )

            sent = 0
            failed = 0

            for recipient in recipients:
                success = await notifier.send_message(
                    to_number=recipient.phone_number,
                    message=message,
                )
                if success:
                    sent += 1
                    recipient.last_notified_at = datetime.utcnow()
                else:
                    failed += 1

            await self.db.commit()
            return {'sent': sent, 'failed': failed, 'skipped': False}

        except Exception as e:
            logger.error(f"Error sending Signal: {e}")
            return {'sent': 0, 'failed': 1, 'skipped': False, 'error': str(e)}

    async def _send_pushover(self, target, agent, diff_snippet: str, context: dict) -> Dict[str, Any]:
        """Send Pushover notifications."""
        try:
            from models.target_notification import TargetNotification
            from models.pushover_recipient import PushoverRecipient
            from models.pushover_config import PushoverConfig
            from notifications.pushover import PushoverNotifier, decrypt_stored_credential

            # Get recipients (single-owner coordinator).
            assigned_recipients_result = await self.db.execute(
                select(PushoverRecipient)
                .join(TargetNotification, TargetNotification.recipient_id == PushoverRecipient.id)
                .where(
                    TargetNotification.target_id == target.id,
                    PushoverRecipient.enabled == True,
                )
            )
            assigned_recipients = assigned_recipients_result.scalars().all()

            # If no specific recipients assigned, use all enabled recipients.
            if not assigned_recipients:
                all_recipients_result = await self.db.execute(
                    select(PushoverRecipient).where(
                        PushoverRecipient.enabled == True,
                    )
                )
                recipients = all_recipients_result.scalars().all()
            else:
                recipients = assigned_recipients

            if not recipients:
                return {'sent': 0, 'failed': 0, 'skipped': True, 'reason': 'No recipients'}

            # Get config
            config_result = await self.db.execute(
                select(PushoverConfig).where(PushoverConfig.enabled == True).limit(1)
            )
            config = config_result.scalar_one_or_none()

            if not config or not config.app_token:
                return {'sent': 0, 'failed': 0, 'skipped': True, 'reason': 'Not configured'}

            # Get per-provider settings for Pushover (if configured)
            provider_settings = (target.provider_notification_settings or {}).get('pushover', {})

            # Prepare message with priority order: provider-specific > target-level > global config
            title_template = (
                provider_settings.get('title') or
                target.notification_title or
                config.notification_title or
                "Writ: Change Detected"
            )
            title = safe_format(title_template, context)

            priority = (
                provider_settings.get('priority') if provider_settings.get('priority') is not None
                else (target.notification_priority if target.notification_priority is not None
                      else (config.notification_priority if config.notification_priority is not None else 1))
            )
            sound = (
                provider_settings.get('sound') or
                target.notification_sound or
                config.notification_sound or
                "pushover"
            )
            url_title = config.url_title or "View Page"
            html_enabled = config.html_enabled if config.html_enabled is not None else True

            custom_message = (
                provider_settings.get('message') or
                target.notification_message or
                config.notification_message
            )
            if custom_message:
                # Use safe_format with full context for template variables
                message = safe_format(custom_message, context)
            elif html_enabled:
                # Escape every interpolated value. `diff` is text scraped from the
                # MONITORED PAGE and `url` is that page's address, so both are
                # attacker-influenced — and Pushover renders a subset of HTML,
                # including <a href>. Unescaped, a hostile monitored site could
                # plant a phishing link inside the owner's own change alert, which
                # arrives with all the trust of a notification they set up
                # themselves. <pre> does not protect its contents; only escaping
                # does.
                from html import escape as _esc

                message = f"""
<b>URL:</b> {_esc(str(context['url']))}

<b>Change detected by:</b> {_esc(str(context['agent_id']))}

<b>Changes:</b>
<pre>{_esc(str(context['diff']))}</pre>
""".strip()
            else:
                message = f"""URL: {context['url']}

Change detected by: {context['agent_id']}

Changes:
{context['diff']}
""".strip()

            # Send notifications
            notifier = PushoverNotifier(app_token=decrypt_stored_credential(config.app_token))
            user_keys = [r.user_key for r in recipients]
            result = await notifier.send_to_multiple_recipients(
                user_keys=user_keys,
                message=message,
                title=title,
                priority=priority,
                sound=sound,
                url=target.url,
                url_title=url_title,
                html=html_enabled,
            )

            # Update last_notified_at
            current_time = datetime.utcnow()
            for recipient in recipients:
                recipient.last_notified_at = current_time

            await self.db.commit()

            logger.info(f"Pushover: Sent to {result['success']}/{result['total']} recipients")
            return {'sent': result['success'], 'failed': result['failed'], 'skipped': False}

        except Exception as e:
            logger.error(f"Pushover notification failed: {e}")
            return {'sent': 0, 'failed': 1, 'skipped': False, 'error': str(e)}

    async def _send_email(self, target, agent, diff_snippet: str, context: dict) -> Dict[str, Any]:
        """Send Email notifications."""
        try:
            from models.email_recipient import EmailRecipient
            from models.email_config import EmailConfig
            from notifications.email import EmailNotifier

            # Get recipients (single-owner coordinator).
            recipients_result = await self.db.execute(
                select(EmailRecipient).where(
                    EmailRecipient.enabled == True,
                )
            )
            recipients = recipients_result.scalars().all()

            if not recipients:
                return {'sent': 0, 'failed': 0, 'skipped': True, 'reason': 'No recipients'}

            # Get config
            config_result = await self.db.execute(
                select(EmailConfig).where(EmailConfig.enabled == True).limit(1)
            )
            config = config_result.scalar_one_or_none()

            if not config:
                return {'sent': 0, 'failed': 0, 'skipped': True, 'reason': 'Not configured'}

            # Get per-provider settings for Email (if configured)
            provider_settings = (target.provider_notification_settings or {}).get('email', {})

            # Prepare message with priority order: provider-specific > target-level > default
            subject_template = (
                provider_settings.get('title') or
                target.notification_title or
                "Writ: Change Detected"
            )
            subject = safe_format(subject_template, context)

            custom_message = provider_settings.get('message') or target.notification_message
            if custom_message:
                body = safe_format(custom_message, context)
            else:
                body = f"""URL: {context['url']}

Change detected by: {context['agent_id']}

Changes:
{context['diff']}
"""

            # Send to each recipient
            notifier = EmailNotifier(
                smtp_host=config.smtp_host,
                smtp_port=config.smtp_port,
                smtp_username=config.smtp_username,
                smtp_password=_decrypt_field(config.smtp_password),
                from_email=config.from_email,
                from_name=config.from_name,
                use_tls=config.use_tls,
            )

            sent = 0
            failed = 0
            for recipient in recipients:
                try:
                    # smtplib is blocking — keep it off the event loop.
                    await asyncio.to_thread(
                        notifier.send_email,
                        to_email=recipient.email,
                        subject=subject,
                        body=body,
                    )
                    recipient.last_notified_at = datetime.utcnow()
                    sent += 1
                except Exception as e:
                    logger.error(f"Failed to send email to {recipient.email}: {e}")
                    failed += 1

            await self.db.commit()

            logger.info(f"Email: Sent to {sent}/{len(recipients)} recipients")
            return {'sent': sent, 'failed': failed, 'skipped': False}

        except Exception as e:
            logger.error(f"Email notification failed: {e}")
            return {'sent': 0, 'failed': 1, 'skipped': False, 'error': str(e)}

    async def _send_twilio(self, target, agent, diff_snippet: str, context: dict) -> Dict[str, Any]:
        """Send Twilio SMS notifications."""
        try:
            from models.twilio_recipient import TwilioRecipient
            from models.twilio_config import TwilioConfig
            from notifications.twilio import TwilioNotifier

            # Get recipients (single-owner coordinator).
            recipients_result = await self.db.execute(
                select(TwilioRecipient).where(
                    TwilioRecipient.enabled == True,
                )
            )
            recipients = recipients_result.scalars().all()

            if not recipients:
                return {'sent': 0, 'failed': 0, 'skipped': True, 'reason': 'No recipients'}

            # Get config
            config_result = await self.db.execute(
                select(TwilioConfig).where(TwilioConfig.enabled == True).limit(1)
            )
            config = config_result.scalar_one_or_none()

            if not config:
                return {'sent': 0, 'failed': 0, 'skipped': True, 'reason': 'Not configured'}

            # Get per-provider settings for Twilio SMS (if configured)
            provider_settings = (target.provider_notification_settings or {}).get('twilio', {})

            # Prepare message with priority order: provider-specific > target-level > default
            custom_message = provider_settings.get('message') or target.notification_message
            if custom_message:
                message = safe_format(custom_message, context)
            else:
                message = f"Writ: Change detected on {context['url'][:50]}..."

            # Send to each recipient
            notifier = TwilioNotifier(
                account_sid=config.account_sid,
                auth_token=_decrypt_field(config.auth_token),
                from_phone=config.from_phone,
            )

            sent = 0
            failed = 0
            for recipient in recipients:
                try:
                    result = await notifier.send_sms(
                        message=message,
                        to_phone=recipient.phone_number,
                    )
                    if result.get('status') in ['queued', 'sent', 'delivered']:
                        recipient.last_notified_at = datetime.utcnow()
                        sent += 1
                    else:
                        failed += 1
                except Exception as e:
                    logger.error(f"Failed to send SMS to {recipient.phone_number}: {e}")
                    failed += 1

            await self.db.commit()

            logger.info(f"Twilio: Sent to {sent}/{len(recipients)} recipients")
            return {'sent': sent, 'failed': failed, 'skipped': False}

        except Exception as e:
            logger.error(f"Twilio notification failed: {e}")
            return {'sent': 0, 'failed': 1, 'skipped': False, 'error': str(e)}

    async def _send_whatsapp(self, target, agent, diff_snippet: str, context: dict) -> Dict[str, Any]:
        """Send WhatsApp notifications."""
        try:
            from models.whatsapp_recipient import WhatsAppRecipient
            from models.twilio_config import TwilioConfig
            from notifications.whatsapp import WhatsAppNotifier

            # Get recipients (single-owner coordinator).
            recipients_result = await self.db.execute(
                select(WhatsAppRecipient).where(
                    WhatsAppRecipient.enabled == True,
                )
            )
            recipients = recipients_result.scalars().all()

            if not recipients:
                return {'sent': 0, 'failed': 0, 'skipped': True, 'reason': 'No recipients'}

            # Get config (WhatsApp uses Twilio config)
            config_result = await self.db.execute(
                select(TwilioConfig).where(TwilioConfig.enabled == True).limit(1)
            )
            config = config_result.scalar_one_or_none()

            if not config:
                return {'sent': 0, 'failed': 0, 'skipped': True, 'reason': 'Not configured'}

            # Get per-provider settings for WhatsApp (if configured)
            provider_settings = (target.provider_notification_settings or {}).get('whatsapp', {})

            # Prepare message with priority order: provider-specific > target-level > default
            custom_message = provider_settings.get('message') or target.notification_message
            if custom_message:
                message = safe_format(custom_message, context)
            else:
                message = f"Writ: Change detected on {context['url']}"

            # Send to each recipient
            # Get WhatsApp from_number from whatsapp_config
            from models.whatsapp_config import WhatsAppConfig
            whatsapp_cfg_result = await self.db.execute(
                select(WhatsAppConfig).where(WhatsAppConfig.enabled == True).limit(1)
            )
            whatsapp_cfg = whatsapp_cfg_result.scalar_one_or_none()
            from_number = whatsapp_cfg.from_number if whatsapp_cfg else config.from_phone

            notifier = WhatsAppNotifier(
                account_sid=config.account_sid,
                auth_token=_decrypt_field(config.auth_token),
                from_number=from_number,
            )

            sent = 0
            failed = 0
            for recipient in recipients:
                try:
                    result = await notifier.send_message(
                        to_number=recipient.phone_number,
                        message=message,
                    )
                    if result.get('status') in ['queued', 'sent', 'delivered']:
                        recipient.last_notified_at = datetime.utcnow()
                        sent += 1
                    else:
                        failed += 1
                except Exception as e:
                    logger.error(f"Failed to send WhatsApp to {recipient.phone_number}: {e}")
                    failed += 1

            await self.db.commit()

            logger.info(f"WhatsApp: Sent to {sent}/{len(recipients)} recipients")
            return {'sent': sent, 'failed': failed, 'skipped': False}

        except Exception as e:
            logger.error(f"WhatsApp notification failed: {e}")
            return {'sent': 0, 'failed': 1, 'skipped': False, 'error': str(e)}

    async def _send_signal(self, target, agent, diff_snippet: str, context: dict) -> Dict[str, Any]:
        """Send Signal notifications."""
        try:
            from models.signal_recipient import SignalRecipient
            from models.signal_config import SignalConfig
            from notifications.signal import SignalNotifier

            # Get recipients (single-owner coordinator).
            recipients_result = await self.db.execute(
                select(SignalRecipient).where(
                    SignalRecipient.enabled == True,
                )
            )
            recipients = recipients_result.scalars().all()

            if not recipients:
                return {'sent': 0, 'failed': 0, 'skipped': True, 'reason': 'No recipients'}

            # Get config
            config_result = await self.db.execute(
                select(SignalConfig).where(SignalConfig.enabled == True).limit(1)
            )
            config = config_result.scalar_one_or_none()

            if not config:
                return {'sent': 0, 'failed': 0, 'skipped': True, 'reason': 'Not configured'}

            # Get per-provider settings for Signal (if configured)
            provider_settings = (target.provider_notification_settings or {}).get('signal', {})

            # Prepare message with priority order: provider-specific > target-level > default
            custom_message = provider_settings.get('message') or target.notification_message
            if custom_message:
                message = safe_format(custom_message, context)
            else:
                message = f"Writ: Change detected on {context['url']}"

            # Send to each recipient
            notifier = SignalNotifier(
                api_url=config.api_url,
                sender_number=config.sender_number,
            )

            sent = 0
            failed = 0
            for recipient in recipients:
                try:
                    result = await notifier.send_message(
                        recipient_number=recipient.phone_number,
                        message=message,
                    )
                    if result.get('success'):
                        recipient.last_notified_at = datetime.utcnow()
                        sent += 1
                    else:
                        failed += 1
                except Exception as e:
                    logger.error(f"Failed to send Signal to {recipient.phone_number}: {e}")
                    failed += 1

            await self.db.commit()

            logger.info(f"Signal: Sent to {sent}/{len(recipients)} recipients")
            return {'sent': sent, 'failed': failed, 'skipped': False}

        except Exception as e:
            logger.error(f"Signal notification failed: {e}")
            return {'sent': 0, 'failed': 1, 'skipped': False, 'error': str(e)}

    async def _send_webhook(
        self,
        target,
        agent,
        diff_snippet: str,
        context: dict,
        detected_change = None,
        selector = None,
    ) -> Dict[str, Any]:
        """Send webhook notifications."""
        try:
            from models.webhook_recipient import WebhookRecipient
            from models.webhook_config import WebhookConfig
            from notifications.webhook import WebhookNotifier, build_webhook_payload

            # Get config
            config_result = await self.db.execute(
                select(WebhookConfig).where(WebhookConfig.enabled == True).limit(1)
            )
            config = config_result.scalar_one_or_none()

            if not config:
                return {'sent': 0, 'failed': 0, 'skipped': True, 'reason': 'Webhook not configured'}

            # Get enabled recipients (single-owner coordinator).
            recipients_result = await self.db.execute(
                select(WebhookRecipient).where(
                    WebhookRecipient.enabled == True,
                )
            )
            recipients = recipients_result.scalars().all()

            if not recipients:
                return {'sent': 0, 'failed': 0, 'skipped': True, 'reason': 'No webhook recipients'}

            # Build payload
            payload = build_webhook_payload(
                event_type="change_detected",
                target=target,
                agent=agent,
                detected_change=detected_change,
                selector=selector,
                diff_snippet=diff_snippet,
            )

            # Add context variables to payload
            payload["context"] = {
                "url": context.get("url"),
                "target_name": context.get("target_name"),
                "selector": context.get("selector"),
                "timestamp": context.get("timestamp"),
            }

            sent = 0
            failed = 0

            for recipient in recipients:
                # Check event type filter
                if recipient.event_types and "change_detected" not in recipient.event_types:
                    continue

                # Create notifier with recipient settings
                notifier = WebhookNotifier(
                    timeout=recipient.timeout or config.default_timeout,
                    max_retries=recipient.max_retries or config.default_retries,
                )

                # Customize payload if needed
                recipient_payload = payload.copy()
                if not recipient.include_diff and "change" in recipient_payload:
                    recipient_payload["change"].pop("diff", None)
                if recipient.include_content and detected_change:
                    recipient_payload["change"]["full_content_before"] = detected_change.content_before
                    recipient_payload["change"]["full_content_after"] = detected_change.content_after

                # Send webhook
                result = await notifier.send_webhook(
                    url=recipient.url,
                    payload=recipient_payload,
                    secret=_decrypt_field(recipient.secret or config.global_secret),
                    headers=recipient.headers,
                    method=recipient.method or config.default_method,
                )

                if result.get("success"):
                    sent += 1
                    recipient.success_count = (recipient.success_count or 0) + 1
                    recipient.last_success_at = datetime.utcnow()
                    logger.info(f"Webhook sent to {recipient.name}")
                else:
                    failed += 1
                    recipient.failure_count = (recipient.failure_count or 0) + 1
                    recipient.last_failure_at = datetime.utcnow()
                    logger.warning(f"Webhook to {recipient.name} failed: {result.get('error')}")

            await self.db.commit()

            logger.info(f"Webhook: Sent to {sent}/{len(recipients)} recipients")
            return {'sent': sent, 'failed': failed, 'skipped': False}

        except Exception as e:
            logger.error(f"Webhook notification failed: {e}")
            return {'sent': 0, 'failed': 1, 'skipped': False, 'error': str(e)}

    async def _send_webhook_to_recipients(
        self,
        target,
        context: dict,
        payload: Dict[str, Any],
        recipient_ids: List[int] = None,
    ) -> Dict[str, Any]:
        """Send webhook to specific recipients (for trigger notifications)."""
        try:
            from models.webhook_recipient import WebhookRecipient
            from models.webhook_config import WebhookConfig
            from notifications.webhook import WebhookNotifier

            config_result = await self.db.execute(select(WebhookConfig).limit(1))
            config = config_result.scalar_one_or_none()
            if not config or not config.enabled:
                return {'sent': 0, 'failed': 0, 'skipped': True, 'reason': 'Webhook not configured'}

            # Get all enabled webhook recipients (single-owner coordinator).
            query = select(WebhookRecipient).where(
                WebhookRecipient.enabled == True,
            )
            if recipient_ids:
                query = query.where(WebhookRecipient.id.in_(recipient_ids))
            result = await self.db.execute(query)
            recipients = result.scalars().all()

            if not recipients:
                return {'sent': 0, 'failed': 0, 'skipped': True, 'reason': 'No recipients'}

            sent = 0
            failed = 0

            for recipient in recipients:
                notifier = WebhookNotifier(
                    timeout=recipient.timeout or config.default_timeout,
                    max_retries=recipient.max_retries or config.default_retries,
                )

                result = await notifier.send_webhook(
                    url=recipient.url,
                    payload=payload,
                    secret=_decrypt_field(recipient.secret or config.global_secret),
                    headers=recipient.headers,
                    method=recipient.method or config.default_method,
                )

                if result.get("success"):
                    sent += 1
                    recipient.success_count = (recipient.success_count or 0) + 1
                    recipient.last_success_at = datetime.utcnow()
                else:
                    failed += 1
                    recipient.failure_count = (recipient.failure_count or 0) + 1
                    recipient.last_failure_at = datetime.utcnow()

            await self.db.commit()
            return {'sent': sent, 'failed': failed, 'skipped': False}

        except Exception as e:
            logger.error(f"Error sending webhook: {e}")
            return {'sent': 0, 'failed': 1, 'skipped': False, 'error': str(e)}
