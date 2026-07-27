"""
Twilio SMS notification service - failover notification method.
"""
import logging
from typing import Optional, Dict, Any
import httpx
from base64 import b64encode
from config import settings

logger = logging.getLogger(__name__)


class TwilioNotifier:
    """
    Twilio SMS notification service client.

    Sends SMS notifications via Twilio API as a failover when Pushover fails.
    """

    def __init__(
        self,
        account_sid: Optional[str] = None,
        auth_token: Optional[str] = None,
        from_phone: Optional[str] = None,
        to_phone: Optional[str] = None,
    ):
        """
        Initialize Twilio notifier.

        Args:
            account_sid: Twilio account SID
            auth_token: Twilio auth token
            from_phone: Source phone number (E.164 format)
            to_phone: Destination phone number (E.164 format)
        """
        self.account_sid = account_sid or settings.twilio_account_sid
        self.auth_token = auth_token or settings.twilio_auth_token
        self.from_phone = from_phone or settings.twilio_from_phone
        self.to_phone = to_phone or settings.twilio_to_phone

        # Only require credentials, not to_phone (can be passed at send time)
        self.enabled = bool(
            self.account_sid
            and self.auth_token
            and self.from_phone
        )

        if not self.enabled:
            logger.warning("Twilio credentials not fully configured")

        self.api_url = (
            f"https://api.twilio.com/2010-04-01/Accounts/{self.account_sid}/Messages.json"
        )

    def _get_auth_header(self) -> str:
        """
        Generate Basic Auth header for Twilio API.

        Returns:
            Base64-encoded auth header value
        """
        credentials = f"{self.account_sid}:{self.auth_token}"
        encoded = b64encode(credentials.encode()).decode()
        return f"Basic {encoded}"

    async def send_sms(
        self,
        message: str,
        to_phone: Optional[str] = None,
        from_phone: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Send an SMS via Twilio.

        Args:
            message: SMS message body (max 1600 chars)
            to_phone: Destination phone number (uses default if None)
            from_phone: Source phone number (uses default if None)

        Returns:
            Response dict with status and message SID

        Raises:
            Exception: If SMS fails to send

        Example:
            >>> notifier = TwilioNotifier()
            >>> result = await notifier.send_sms("Page changed!")
        """
        if not self.enabled:
            logger.error("Twilio is not configured")
            raise ValueError("Twilio credentials not configured")

        to = to_phone or self.to_phone
        from_ = from_phone or self.from_phone

        # Validate phone numbers
        if not to or not from_:
            raise ValueError("Phone numbers not configured")

        # Truncate message if too long
        if len(message) > 1600:
            message = message[:1597] + "..."
            logger.warning("SMS message truncated to 1600 characters")

        # Build payload
        payload = {
            "To": to,
            "From": from_,
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

                # Check for errors
                if result.get("error_code"):
                    error_msg = result.get("error_message", "Unknown error")
                    logger.error(f"Twilio API error: {error_msg}")
                    raise Exception(f"Twilio error: {error_msg}")

                logger.info(
                    f"SMS sent successfully: {result.get('sid')} to {to}"
                )

                return {
                    "success": True,
                    "message_sid": result.get("sid"),
                    "status": result.get("status"),
                    "provider": "twilio",
                }

        except httpx.HTTPStatusError as e:
            logger.error(
                f"Twilio HTTP error: {e.response.status_code} - {e.response.text}"
            )
            raise Exception(f"Twilio HTTP error: {e.response.status_code}")
        except httpx.RequestError as e:
            logger.error(f"Twilio request error: {e}")
            raise Exception(f"Twilio connection error: {e}")
        except Exception as e:
            logger.error(f"Twilio unexpected error: {e}")
            raise

    async def send_change_alert(
        self,
        target_url: str,
        num_agents: int,
        quorum: int,
    ) -> Dict[str, Any]:
        """
        Send a page change alert via SMS.

        Args:
            target_url: URL that changed
            num_agents: Number of agents confirming the change
            quorum: Required quorum

        Returns:
            Response dict from send_sms
        """
        # Keep SMS message concise due to length limits
        message = (
            f"Writ Alert: Change detected!\n"
            f"URL: {target_url}\n"
            f"Confirmed by {num_agents}/{quorum} agents"
        )

        return await self.send_sms(message)

    async def send_test_sms(self, to_phone: Optional[str] = None) -> Dict[str, Any]:
        """
        Send a test SMS to verify configuration.

        Args:
            to_phone: Optional destination phone number (uses default if None)

        Returns:
            Response dict from send_sms
        """
        message = "This is a test SMS from Writ"
        return await self.send_sms(message, to_phone=to_phone)

    async def get_message_status(self, message_sid: str) -> Dict[str, Any]:
        """
        Get the status of a sent message.

        Args:
            message_sid: Twilio message SID

        Returns:
            Message status information
        """
        if not self.enabled:
            raise ValueError("Twilio credentials not configured")

        url = (
            f"https://api.twilio.com/2010-04-01/Accounts/"
            f"{self.account_sid}/Messages/{message_sid}.json"
        )

        headers = {
            "Authorization": self._get_auth_header(),
        }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(url, headers=headers)
                response.raise_for_status()
                return response.json()

        except Exception as e:
            logger.error(f"Error fetching message status: {e}")
            raise
