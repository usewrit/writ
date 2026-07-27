"""
Signal notification service.
"""
import logging
from typing import Optional, Dict, Any
import httpx
from config import settings

logger = logging.getLogger(__name__)


class SignalNotifier:
    """
    Signal notification service client.

    Sends Signal messages via signal-cli REST API or similar service.
    Note: Requires signal-cli-rest-api server to be running separately.
    """

    def __init__(
        self,
        api_url: Optional[str] = None,
        sender_number: Optional[str] = None,
    ):
        """
        Initialize Signal notifier.

        Args:
            api_url: signal-cli REST API endpoint (e.g., "http://signal-api:8080")
            sender_number: Signal sender phone number (E.164 format)
        """
        self.api_url = api_url or getattr(settings, 'signal_api_url', None)
        self.sender_number = sender_number or getattr(settings, 'signal_sender_number', None)

        self.enabled = bool(self.api_url and self.sender_number)

        if not self.enabled:
            logger.warning("Signal credentials not fully configured")

    async def send_message(
        self,
        to_number: str,  # E.164 format: +1234567890
        message: str,
    ) -> Dict[str, Any]:
        """
        Send a Signal message.

        Args:
            to_number: Destination Signal number (E.164 format)
            message: Message body

        Returns:
            Response dict with status

        Raises:
            Exception: If message fails to send
        """
        if not self.enabled:
            logger.error("Signal is not configured")
            raise ValueError("Signal credentials not configured")

        # Build API endpoint
        endpoint = f"{self.api_url}/v2/send"

        payload = {
            "number": self.sender_number,
            "recipients": [to_number],
            "message": message,
        }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    endpoint,
                    json=payload,
                )
                response.raise_for_status()

                logger.info(f"Signal message sent successfully to {to_number}")

                return {
                    "success": True,
                    "provider": "signal",
                }

        except httpx.HTTPStatusError as e:
            logger.error(
                f"Signal HTTP error: {e.response.status_code} - {e.response.text}"
            )
            raise Exception(f"Signal HTTP error: {e.response.status_code}")
        except Exception as e:
            logger.error(f"Signal unexpected error: {e}")
            raise

    async def send_change_alert(
        self,
        to_number: str,
        target_url: str,
        num_agents: int,
        quorum: int,
    ) -> Dict[str, Any]:
        """
        Send a page change alert via Signal.

        Args:
            to_number: Recipient Signal number
            target_url: URL that changed
            num_agents: Number of agents confirming the change
            quorum: Required quorum

        Returns:
            Response dict from send_message
        """
        message = (
            f"🔔 Writ Alert\n\n"
            f"Change detected!\n"
            f"URL: {target_url}\n"
            f"Confirmed by {num_agents}/{quorum} agents"
        )

        return await self.send_message(to_number, message)

    async def send_test_message(self, to_number: str) -> Dict[str, Any]:
        """
        Send a test Signal message to verify configuration.

        Args:
            to_number: Test recipient Signal number

        Returns:
            Response dict from send_message
        """
        message = "✅ This is a test message from Writ"
        return await self.send_message(to_number, message)
