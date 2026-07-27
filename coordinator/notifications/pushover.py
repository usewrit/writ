"""
Pushover notification service - primary notification method.
"""
import logging
from typing import Optional, Dict, Any, List
import httpx
from config import settings

logger = logging.getLogger(__name__)

PUSHOVER_API_URL = "https://api.pushover.net/1/messages.json"


def decrypt_stored_credential(value: Optional[str]) -> Optional[str]:
    """Decrypt a Pushover credential stored at rest via SecretEncryption.

    Rows written before encryption-at-rest landed hold PLAINTEXT tokens, so a
    failed decrypt means "legacy plaintext" and the value is returned unchanged
    (same fallback pattern as the SMTP password in routers/notifications.py).
    Every DB read of pushover_config.app_token / user_key must go through this.
    """
    if not value:
        return value
    try:
        from security.encryption import SecretEncryption
        return SecretEncryption.decrypt_secret(value)
    except Exception:
        return value  # legacy plaintext row (pre-encryption)


class PushoverNotifier:
    """
    Pushover notification service client.

    Sends push notifications via Pushover API for page change alerts.
    Supports priorities, sounds, and attachments.
    """

    def __init__(
        self,
        app_token: Optional[str] = None,
        user_key: Optional[str] = None,
    ):
        """
        Initialize Pushover notifier.

        Args:
            app_token: Pushover application token
            user_key: Pushover user/group key
        """
        self.app_token = app_token or settings.pushover_app_token
        self.user_key = user_key or settings.pushover_user_key
        self.enabled = bool(self.app_token and self.user_key)

        if not self.enabled:
            logger.warning("Pushover credentials not configured")

    async def send_notification(
        self,
        message: str,
        title: Optional[str] = None,
        priority: int = 0,
        sound: str = "pushover",
        url: Optional[str] = None,
        url_title: Optional[str] = None,
        html: bool = False,
    ) -> Dict[str, Any]:
        """
        Send a push notification via Pushover.

        Args:
            message: Notification message (required, max 1024 chars)
            title: Notification title (max 250 chars)
            priority: Priority level (-2 to 2, default 0)
                -2: No notification/alert
                -1: Quiet notification
                 0: Normal priority
                 1: High priority
                 2: Emergency (requires acknowledgment)
            sound: Notification sound name
            url: Supplementary URL to show
            url_title: Title for the supplementary URL
            html: Enable HTML formatting in message

        Returns:
            Response dict with status and request ID

        Raises:
            Exception: If notification fails

        Example:
            >>> notifier = PushoverNotifier()
            >>> result = await notifier.send_notification(
            ...     message="Page changed!",
            ...     title="Writ Alert",
            ...     priority=1,
            ...     url="https://example.com"
            ... )
        """
        if not self.enabled:
            logger.error("Pushover is not configured")
            raise ValueError("Pushover credentials not configured")

        # Validate message length
        if len(message) > 1024:
            message = message[:1021] + "..."
            logger.warning("Message truncated to 1024 characters")

        # Build payload
        payload = {
            "token": self.app_token,
            "user": self.user_key,
            "message": message,
        }

        if title:
            payload["title"] = title[:250]

        if priority != 0:
            payload["priority"] = priority
            # Priority 2 (Emergency) requires expire and retry parameters
            if priority == 2:
                payload["expire"] = 3600  # Keep retrying for 1 hour (max 10800)
                payload["retry"] = 60     # Retry every 60 seconds (min 30)

        if sound != "pushover":
            payload["sound"] = sound

        if url:
            payload["url"] = url
            if url_title:
                payload["url_title"] = url_title

        if html:
            payload["html"] = 1

        # Send request
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(PUSHOVER_API_URL, data=payload)
                response.raise_for_status()

                result = response.json()

                if result.get("status") == 1:
                    logger.info(
                        f"Pushover notification sent successfully: {result.get('request')}"
                    )
                    return {
                        "success": True,
                        "request_id": result.get("request"),
                        "provider": "pushover",
                    }
                else:
                    errors = result.get("errors", [])
                    logger.error(f"Pushover API error: {errors}")
                    raise Exception(f"Pushover error: {errors}")

        except httpx.HTTPStatusError as e:
            logger.error(f"Pushover HTTP error: {e.response.status_code} - {e.response.text}")
            raise Exception(f"Pushover HTTP error: {e.response.status_code}")
        except httpx.RequestError as e:
            logger.error(f"Pushover request error: {e}")
            raise Exception(f"Pushover connection error: {e}")
        except Exception as e:
            logger.error(f"Pushover unexpected error: {e}")
            raise

    async def send_change_alert(
        self,
        target_url: str,
        diff_snippet: str,
        num_agents: int,
        quorum: int,
    ) -> Dict[str, Any]:
        """
        Send a page change alert notification.

        Args:
            target_url: URL that changed
            diff_snippet: Text diff showing changes
            num_agents: Number of agents confirming the change
            quorum: Required quorum

        Returns:
            Response dict from send_notification
        """
        title = f"Writ: Change Detected"

        message = f"""
<b>URL:</b> {target_url}

<b>Confirmed by:</b> {num_agents}/{quorum} agents

<b>Changes:</b>
<pre>{diff_snippet[:500]}</pre>
""".strip()

        return await self.send_notification(
            message=message,
            title=title,
            priority=1,  # High priority
            url=target_url,
            url_title="View Page",
            html=True,
        )

    async def send_test_notification(self) -> Dict[str, Any]:
        """
        Send a test notification to verify configuration.

        Returns:
            Response dict from send_notification
        """
        return await self.send_notification(
            message="This is a test notification from Writ",
            title="Writ Test",
            priority=0,
        )

    async def send_low_agent_alert(
        self,
        online_count: int,
        total_count: int,
    ) -> Dict[str, Any]:
        """
        Send an alert when agent count drops below threshold.

        Args:
            online_count: Number of agents currently online
            total_count: Total number of registered agents

        Returns:
            Response dict from send_notification
        """
        title = "⚠️ Writ: Low Agent Count"

        if online_count == 0:
            message = f"""
<b>Critical:</b> No agents are currently online!

<b>Status:</b> 0/{total_count} agents responding

Your monitoring system is not operational. Please check your agents immediately.
""".strip()
            priority = 1  # High priority
        else:
            message = f"""
<b>Warning:</b> Only {online_count} agent{'s' if online_count != 1 else ''} online.

<b>Status:</b> {online_count}/{total_count} agents responding

Insufficient agents for reliable monitoring. You need at least 2 agents online for redundancy.
""".strip()
            priority = 1  # High priority

        return await self.send_notification(
            message=message,
            title=title,
            priority=priority,
            sound="siren",
            html=True,
        )

    async def validate_credentials(self) -> bool:
        """
        Validate Pushover credentials.

        Returns:
            True if credentials are valid, False otherwise
        """
        if not self.enabled:
            return False

        try:
            payload = {
                "token": self.app_token,
                "user": self.user_key,
            }

            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    "https://api.pushover.net/1/users/validate.json",
                    data=payload,
                )
                result = response.json()
                return result.get("status") == 1

        except Exception as e:
            logger.error(f"Pushover credential validation error: {e}")
            return False

    async def send_to_multiple_recipients(
        self,
        user_keys: List[str],
        message: str,
        title: Optional[str] = None,
        priority: int = 0,
        sound: str = "pushover",
        url: Optional[str] = None,
        url_title: Optional[str] = None,
        html: bool = False,
    ) -> Dict[str, Any]:
        """
        Send a push notification to multiple recipients.

        Args:
            user_keys: List of Pushover user/group keys
            message: Notification message (required, max 1024 chars)
            title: Notification title (max 250 chars)
            priority: Priority level (-2 to 2, default 0)
            sound: Notification sound name
            url: Supplementary URL to show
            url_title: Title for the supplementary URL
            html: Enable HTML formatting in message

        Returns:
            Dict with results for each recipient
        """
        if not self.app_token:
            logger.error("Pushover app token not configured")
            raise ValueError("Pushover app token not configured")

        results = {
            "total": len(user_keys),
            "success": 0,
            "failed": 0,
            "recipients": []
        }

        for user_key in user_keys:
            try:
                # Create a temporary notifier for this user
                temp_notifier = PushoverNotifier(
                    app_token=self.app_token,
                    user_key=user_key
                )

                # Send notification
                result = await temp_notifier.send_notification(
                    message=message,
                    title=title,
                    priority=priority,
                    sound=sound,
                    url=url,
                    url_title=url_title,
                    html=html,
                )

                results["success"] += 1
                results["recipients"].append({
                    "user_key": user_key[:8] + "...",
                    "status": "success",
                    "request_id": result.get("request_id")
                })

            except Exception as e:
                results["failed"] += 1
                results["recipients"].append({
                    "user_key": user_key[:8] + "...",
                    "status": "failed",
                    "error": str(e)
                })
                logger.error(f"Failed to send notification to {user_key[:8]}...: {e}")

        logger.info(
            f"Sent to {results['success']}/{results['total']} recipients "
            f"({results['failed']} failed)"
        )

        return results
