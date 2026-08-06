"""
Webhook notification provider.

Sends HTTP POST requests to configured webhook URLs when changes are detected.
"""
import logging
import hashlib
import hmac
import json
import time
from typing import Dict, Any, Optional, List
from urllib.parse import urlparse
import httpx

logger = logging.getLogger(__name__)

#: Outbound delivery headers.
SIGNATURE_HEADER = "X-Writ-Signature"
TIMESTAMP_HEADER = "X-Writ-Timestamp"

#: Replay-resistant signature, sent ALONGSIDE ``X-Writ-Signature``. It covers
#: ``timestamp + "." + body`` — the same material the INBOUND verifier binds — so
#: a captured delivery cannot be replayed under a forged timestamp the way a
#: body-only MAC allows. Adding it as a second header rather than changing what
#: ``X-Writ-Signature`` means keeps every deployed consumer verifying.
SIGNATURE_V1_HEADER = "X-Writ-Signature-V1"

USER_AGENT = "Writ-Webhook/1.0"


class WebhookNotifier:
    """
    Sends notifications via HTTP webhooks.

    Supports:
    - Custom headers
    - HMAC signature verification
    - Retry with exponential backoff
    - Custom payload templates
    """

    def __init__(
        self,
        timeout: int = 30,
        max_retries: int = 3,
    ):
        """
        Initialize webhook notifier.

        Args:
            timeout: Request timeout in seconds
            max_retries: Maximum retry attempts
        """
        self.timeout = timeout
        self.max_retries = max_retries

    def _generate_signature(self, payload: str, secret: str) -> str:
        """
        Generate HMAC-SHA256 signature for payload.

        Args:
            payload: JSON payload string
            secret: Webhook secret for signing

        Returns:
            Hex-encoded HMAC signature
        """
        return hmac.new(
            secret.encode('utf-8'),
            payload.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()

    async def send_webhook(
        self,
        url: str,
        payload: Dict[str, Any],
        secret: str = None,
        headers: Dict[str, str] = None,
        method: str = "POST",
    ) -> Dict[str, Any]:
        """
        Send webhook notification.

        Args:
            url: Webhook URL
            payload: Data to send
            secret: Optional secret for HMAC signature
            headers: Optional custom headers
            method: HTTP method (POST, PUT, etc.)

        Returns:
            Dict with success status and response details
        """
        request_headers = {
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }

        # Add custom headers
        if headers:
            request_headers.update(headers)

        # Serialize payload
        payload_str = json.dumps(payload, default=str)

        # Timestamp and signatures are set AFTER the custom-header merge: they are
        # signed material, so a recipient-supplied header must never be able to
        # overwrite them and invalidate every delivery to that endpoint.
        # Stamped once, outside the retry loop, so a redelivery carries the same
        # signature and the recipient's replay dedupe recognises it as one event.
        timestamp = str(int(time.time()))
        request_headers[TIMESTAMP_HEADER] = timestamp

        # Add signature if secret provided
        if secret:
            request_headers[SIGNATURE_HEADER] = (
                f"sha256={self._generate_signature(payload_str, secret)}"
            )
            # v1 binds the timestamp into the MAC; prefer it when verifying.
            request_headers[SIGNATURE_V1_HEADER] = "sha256=" + self._generate_signature(
                f"{timestamp}.{payload_str}", secret
            )

        # SSRF hardening: the webhook destination is recipient-supplied and was
        # never re-validated at send time, leaving a DNS-rebinding window (a
        # hostname that passed an earlier check can resolve to an internal IP by
        # the time we connect — metadata service, internal admin ports, etc.).
        # We re-resolve + re-validate on EVERY attempt and pin the TCP
        # connection to a vetted IP. Redirects are NOT followed (the notifier
        # never relied on them) so each hop cannot escape validation.
        from security.validation import InputValidator
        # Private/internal destinations require the explicit ALLOW_PRIVATE_TARGETS
        # opt-in — the SSRF screen is on in every environment, dev included.
        try:
            from config import settings
            allow_private = bool(settings.allow_private_targets)
        except Exception:
            allow_private = False

        method_upper = (method or "POST").upper()
        if method_upper not in ("POST", "PUT"):
            method_upper = "POST"

        last_error = None

        for attempt in range(self.max_retries):
            try:
                # Validate + resolve the destination fresh each attempt and pin
                # the connection to a vetted IP (closes the rebind TOCTOU gap).
                try:
                    validated_url, safe_ips = InputValidator.resolve_and_validate(
                        url, allow_private=allow_private
                    )
                except Exception as ve:
                    # A blocked/invalid destination is terminal — do not retry.
                    logger.warning(f"Webhook destination rejected ({url}): {ve}")
                    return {
                        "success": False,
                        "error": "Webhook destination failed SSRF validation",
                    }

                parsed = urlparse(validated_url)
                connect_url = validated_url
                attempt_headers = dict(request_headers)
                extensions: Dict[str, Any] = {}
                if safe_ips:
                    pin_ip = safe_ips[0]
                    host_header = parsed.hostname
                    if parsed.port:
                        host_header = f"{host_header}:{parsed.port}"
                    attempt_headers.setdefault("Host", host_header)
                    netloc = pin_ip if not parsed.port else f"{pin_ip}:{parsed.port}"
                    connect_url = parsed._replace(netloc=netloc).geturl()
                    if parsed.scheme == "https":
                        extensions["sni_hostname"] = parsed.hostname

                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    request = client.build_request(
                        method_upper,
                        connect_url,
                        content=payload_str,
                        headers=attempt_headers,
                    )
                    if extensions:
                        request.extensions.update(extensions)
                    response = await client.send(request, follow_redirects=False)

                    # Check for success (2xx status codes)
                    if 200 <= response.status_code < 300:
                        logger.info(f"Webhook sent successfully to {url} (status: {response.status_code})")
                        return {
                            "success": True,
                            "status_code": response.status_code,
                            "response": response.text[:500] if response.text else None,
                        }
                    else:
                        logger.warning(f"Webhook to {url} returned status {response.status_code}: {response.text[:200]}")
                        last_error = f"HTTP {response.status_code}: {response.text[:200]}"

            except httpx.TimeoutException as e:
                logger.warning(f"Webhook to {url} timed out (attempt {attempt + 1}/{self.max_retries})")
                last_error = f"Timeout: {str(e)}"

            except httpx.RequestError as e:
                logger.warning(f"Webhook to {url} failed (attempt {attempt + 1}/{self.max_retries}): {e}")
                last_error = str(e)

            # Exponential backoff before retry
            if attempt < self.max_retries - 1:
                await self._backoff(attempt)

        logger.error(f"Webhook to {url} failed after {self.max_retries} attempts: {last_error}")
        return {
            "success": False,
            "error": last_error,
        }

    async def _backoff(self, attempt: int):
        """Exponential backoff delay."""
        import asyncio
        delay = min(2 ** attempt, 30)  # Max 30 seconds
        await asyncio.sleep(delay)

    async def send_to_multiple(
        self,
        webhooks: List[Dict[str, Any]],
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Send webhook to multiple endpoints.

        Args:
            webhooks: List of webhook configs with url, secret, headers
            payload: Data to send

        Returns:
            Dict with success/failed counts
        """
        import asyncio

        results = await asyncio.gather(*[
            self.send_webhook(
                url=w.get("url"),
                payload=payload,
                secret=w.get("secret"),
                headers=w.get("headers"),
                method=w.get("method", "POST"),
            )
            for w in webhooks
        ], return_exceptions=True)

        success = sum(1 for r in results if isinstance(r, dict) and r.get("success"))
        failed = len(results) - success

        return {
            "success": success,
            "failed": failed,
            "total": len(webhooks),
        }


def build_webhook_payload(
    event_type: str,
    target,
    agent = None,
    detected_change = None,
    selector = None,
    diff_snippet: str = None,
    extra_data: Dict[str, Any] = None,
) -> Dict[str, Any]:
    """
    Build standardized webhook payload.

    Args:
        event_type: Type of event (change_detected, workflow_completed, etc.)
        target: Target model instance
        agent: Optional agent model instance
        detected_change: Optional DetectedChange instance
        selector: Optional selector instance
        diff_snippet: Optional diff content
        extra_data: Additional data to include

    Returns:
        Standardized webhook payload dict
    """
    from datetime import datetime

    payload = {
        "event": event_type,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "target": {
            "id": target.id,
            "url": target.url,
            "name": getattr(target, "name", None) or target.url,
        },
    }

    if agent:
        payload["agent"] = {
            "id": agent.agent_id if hasattr(agent, 'agent_id') else str(agent.id),
            "platform": getattr(agent, "platform", "unknown"),
        }

    if selector:
        payload["selector"] = {
            "css": getattr(selector, "selector", None),
            "name": getattr(selector, "name", None),
        }

    if detected_change:
        payload["change"] = {
            "content_before": (detected_change.content_before[:1000]
                             if detected_change.content_before else None),
            "content_after": (detected_change.content_after[:1000]
                            if detected_change.content_after else None),
            "content_hash": detected_change.content_hash,
            "previous_hash": detected_change.previous_hash,
        }
    elif diff_snippet:
        payload["change"] = {
            "diff": diff_snippet[:2000],
        }

    if extra_data:
        payload["data"] = extra_data

    return payload
