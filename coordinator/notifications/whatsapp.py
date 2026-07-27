"""
WhatsApp notification service (via Twilio WhatsApp API).
"""
import logging
from typing import Optional, Dict, Any
import httpx
from base64 import b64encode
from config import settings

logger = logging.getLogger(__name__)


class WhatsAppNotifier:
    """
    WhatsApp notification service client using Twilio WhatsApp API.

    Sends WhatsApp messages for page changes.
    """

    def __init__(
        self,
        account_sid: Optional[str] = None,
        auth_token: Optional[str] = None,
        from_number: Optional[str] = None,  # e.g., "whatsapp:+14155238886"
    ):
        """
        Initialize WhatsApp notifier.

        Args:
            account_sid: Twilio account SID
            auth_token: Twilio auth token
            from_number: Twilio WhatsApp-enabled number (format: "whatsapp:+1234567890")
        """
        self.account_sid = account_sid or getattr(settings, 'whatsapp_account_sid', None)
        self.auth_token = auth_token or getattr(settings, 'whatsapp_auth_token', None)
        self.from_number = from_number or getattr(settings, 'whatsapp_from_number', None)

        self.enabled = bool(
            self.account_sid
            and self.auth_token
            and self.from_number
        )

        if not self.enabled:
            logger.warning("WhatsApp credentials not fully configured")

        self.api_url = (
            f"https://api.twilio.com/2010-04-01/Accounts/{self.account_sid}/Messages.json"
        )

    def _get_auth_header(self) -> str:
        """Generate Basic Auth header for Twilio API."""
        credentials = f"{self.account_sid}:{self.auth_token}"
        encoded = b64encode(credentials.encode()).decode()
        return f"Basic {encoded}"

    async def send_message(
        self,
        to_number: str,  # Format: "whatsapp:+1234567890"
        message: str,
    ) -> Dict[str, Any]:
        """
        Send a WhatsApp message via Twilio.

        Args:
            to_number: Destination WhatsApp number (format: "whatsapp:+1234567890")
            message: Message body (max 1600 chars)

        Returns:
            Response dict with status and message SID

        Raises:
            Exception: If message fails to send
        """
        if not self.enabled:
            logger.error("WhatsApp is not configured")
            raise ValueError("WhatsApp credentials not configured")

        # Ensure number has whatsapp: prefix
        if not to_number.startswith('whatsapp:'):
            to_number = f"whatsapp:{to_number}"

        # Truncate message if too long
        if len(message) > 1600:
            message = message[:1597] + "..."
            logger.warning("WhatsApp message truncated to 1600 characters")

        # Build payload
        payload = {
            "To": to_number,
            "From": self.from_number,
            "Body": message,
        }

        headers = {
            "Authorization": self._get_auth_header(),
        }

        # Send request
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    self.api_url,
                    data=payload,
                    headers=headers,
                )
                response.raise_for_status()

                result = response.json()

                if result.get("error_code"):
                    error_msg = result.get("error_message", "Unknown error")
                    logger.error(f"WhatsApp API error: {error_msg}")
                    raise Exception(f"WhatsApp error: {error_msg}")

                logger.info(
                    f"WhatsApp message sent successfully: {result.get('sid')} to {to_number}"
                )

                return {
                    "success": True,
                    "message_sid": result.get("sid"),
                    "status": result.get("status"),
                    "provider": "whatsapp",
                }

        except httpx.HTTPStatusError as e:
            logger.error(
                f"WhatsApp HTTP error: {e.response.status_code} - {e.response.text}"
            )
            raise Exception(f"WhatsApp HTTP error: {e.response.status_code}")
        except Exception as e:
            logger.error(f"WhatsApp unexpected error: {e}")
            raise

    async def send_change_alert(
        self,
        to_number: str,
        target_url: str,
        num_agents: int,
        quorum: int,
    ) -> Dict[str, Any]:
        """
        Send a page change alert via WhatsApp.

        Args:
            to_number: Recipient WhatsApp number
            target_url: URL that changed
            num_agents: Number of agents confirming the change
            quorum: Required quorum

        Returns:
            Response dict from send_message
        """
        message = (
            f"🔔 *Writ Alert*\n\n"
            f"Change detected!\n"
            f"URL: {target_url}\n"
            f"Confirmed by {num_agents}/{quorum} agents"
        )

        return await self.send_message(to_number, message)

    async def send_test_message(self, to_number: str) -> Dict[str, Any]:
        """
        Send a test WhatsApp message to verify configuration.

        Args:
            to_number: Test recipient WhatsApp number

        Returns:
            Response dict from send_message
        """
        message = "✅ This is a test message from Writ"
        return await self.send_message(to_number, message)
