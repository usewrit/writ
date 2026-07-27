"""
HMAC signature verification for agent requests.
Ensures request integrity and authenticity using SHA-256.
"""
import hmac
import hashlib
import json
import logging
from typing import Optional, Dict, Any
from config import settings

logger = logging.getLogger(__name__)


def generate_hmac_signature(message: str, secret: str) -> str:
    """
    Generate HMAC-SHA256 signature for a message.

    Args:
        message: The message to sign
        secret: The secret key for signing

    Returns:
        Hexadecimal HMAC signature

    Example:
        >>> sig = generate_hmac_signature("hello", "secret123")
        >>> len(sig)
        64
    """
    signature = hmac.new(
        key=secret.encode("utf-8"),
        msg=message.encode("utf-8"),
        digestmod=hashlib.sha256
    ).hexdigest()
    return signature


def verify_hmac_signature(
    message: str,
    signature: str,
    secret: str,
    tolerance_seconds: int = 300
) -> bool:
    """
    Verify HMAC-SHA256 signature for a message.

    Uses constant-time comparison to prevent timing attacks.

    Args:
        message: The original message
        signature: The signature to verify
        secret: The secret key used for signing
        tolerance_seconds: Max age in seconds for timestamp replay protection (default 300s)

    Returns:
        True if signature is valid, False otherwise

    Example:
        >>> sig = generate_hmac_signature("hello", "secret123")
        >>> verify_hmac_signature("hello", sig, "secret123")
        True
        >>> verify_hmac_signature("hello", sig, "wrong_secret")
        False
    """
    try:
        expected_signature = generate_hmac_signature(message, secret)

        # Use compare_digest for constant-time comparison
        is_valid = hmac.compare_digest(signature, expected_signature)

        if not is_valid:
            logger.warning(f"HMAC verification failed for message: {message[:50]}...")
            return False

        # Replay protection: verify timestamp freshness
        if tolerance_seconds and tolerance_seconds > 0:
            parts = message.split("|")
            if len(parts) < 2:
                # Freshness requested but the message carries no timestamp —
                # fail closed instead of silently accepting a replayable message.
                logger.warning("HMAC replay check requested but message has no timestamp; rejecting")
                return False
            try:
                from datetime import datetime, timezone
                timestamp_str = parts[1]
                # Handle ISO format with or without Z
                msg_time = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
                now = datetime.now(timezone.utc)
                delta = abs((now - msg_time).total_seconds())
                if delta > tolerance_seconds:
                    logger.warning(f"HMAC replay detected: message is {delta:.0f}s old (max {tolerance_seconds}s)")
                    return False
            except (ValueError, IndexError) as e:
                # Unparseable timestamp under a freshness requirement -> fail closed.
                logger.warning(f"HMAC replay check: unparseable timestamp, rejecting: {e}")
                return False

        return is_valid
    except Exception as e:
        logger.error(f"HMAC verification error: {e}")
        return False


def construct_message(agent_id: str, timestamp: str, body: str = "") -> str:
    """
    Construct canonical message for HMAC signing.

    Args:
        agent_id: Agent identifier
        timestamp: ISO 8601 timestamp
        body: Optional request body

    Returns:
        Canonical message string

    Example:
        >>> msg = construct_message("agent-123", "2024-01-01T00:00:00Z", "hello")
        >>> msg
        'agent-123|2024-01-01T00:00:00Z|hello'
    """
    return f"{agent_id}|{timestamp}|{body}"


def hash_secret(secret: str) -> str:
    """
    Hash a secret using SHA-256.

    Args:
        secret: The secret to hash

    Returns:
        Hexadecimal hash

    Example:
        >>> hash_secret("my-secret")
        'b4f4e3a56d77c5c0a4d6b4d50b6a4e5c...'
    """
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def verify_fresh_signed_payload(
    payload: Dict[str, Any],
    secret: str,
    provided_signature: str,
    *,
    max_age_seconds: int = 300,
) -> bool:
    """
    Verify an HMAC-signed payload AND that it is fresh (replay protection).

    Reuses the exact canonical HMAC computation used by ``HMACAuth.verify_signature``
    (sorted-key compact JSON, constant-time compare).

    Returns True ONLY if:
      * the constant-time HMAC of ``payload`` matches ``provided_signature``, AND
      * ``payload["timestamp"]`` parses (int/float epoch seconds OR ISO-8601 string)
        and is within ``max_age_seconds`` of now (UTC).

    Never raises. Returns False on a missing/unparseable/stale timestamp,
    a signature mismatch, or any unexpected error.
    """
    try:
        # Constant-time signature check (same canonicalization as HMACAuth).
        if not HMACAuth.verify_signature(payload, secret, provided_signature):
            return False

        if not isinstance(payload, dict):
            return False

        raw_ts = payload.get("timestamp")
        if raw_ts is None:
            logger.warning("Signed payload rejected: missing timestamp")
            return False

        from datetime import datetime, timezone

        msg_time: Optional[datetime] = None
        if isinstance(raw_ts, bool):
            # bool is an int subclass; reject explicitly to avoid treating True/False as epoch.
            return False
        if isinstance(raw_ts, (int, float)):
            try:
                msg_time = datetime.fromtimestamp(float(raw_ts), tz=timezone.utc)
            except (ValueError, OverflowError, OSError):
                msg_time = None
        elif isinstance(raw_ts, str):
            ts_str = raw_ts.strip()
            # Numeric string epoch (e.g. "1718900000" or "1718900000.5")
            try:
                msg_time = datetime.fromtimestamp(float(ts_str), tz=timezone.utc)
            except (ValueError, OverflowError, OSError):
                # ISO-8601 string (accept trailing Z)
                try:
                    parsed = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=timezone.utc)
                    msg_time = parsed
                except ValueError:
                    msg_time = None

        if msg_time is None:
            logger.warning("Signed payload rejected: unparseable timestamp")
            return False

        now = datetime.now(timezone.utc)
        delta = abs((now - msg_time).total_seconds())
        if delta > max_age_seconds:
            logger.warning(
                f"Signed payload rejected: stale by {delta:.0f}s (max {max_age_seconds}s)"
            )
            return False

        return True
    except Exception as e:
        logger.error(f"verify_fresh_signed_payload error: {e}")
        return False


class HMACAuth:
    """
    HMAC-SHA256 authentication for API requests.

    Compatible with desktop-agent HMACAuth class.
    """

    @staticmethod
    def sign_request(payload: Dict[str, Any], secret: str) -> str:
        """
        Generate HMAC-SHA256 signature for request payload.

        Args:
            payload: Request payload (will be JSON-serialized)
            secret: Shared secret key

        Returns:
            Hex-encoded HMAC signature
        """
        try:
            # Normalize payload to consistent JSON representation
            # Sort keys to ensure consistent ordering
            canonical_payload = json.dumps(payload, sort_keys=True, separators=(',', ':'))

            # Generate HMAC-SHA256
            signature = hmac.new(
                secret.encode('utf-8'),
                canonical_payload.encode('utf-8'),
                hashlib.sha256
            ).hexdigest()

            return signature

        except Exception as e:
            logger.error(f"Failed to sign request: {e}")
            raise

    @staticmethod
    def verify_signature(payload: Dict[str, Any], secret: str, provided_signature: str) -> bool:
        """
        Verify HMAC signature.

        Args:
            payload: Request payload
            secret: Shared secret key
            provided_signature: Signature to verify

        Returns:
            True if signature is valid
        """
        try:
            expected_signature = HMACAuth.sign_request(payload, secret)

            # Use constant-time comparison to prevent timing attacks
            return hmac.compare_digest(expected_signature, provided_signature)

        except Exception as e:
            logger.error(f"Failed to verify signature: {e}")
            return False

    @staticmethod
    def verify_fresh_signature(payload: Dict[str, Any], secret: str, provided_signature: str, *, max_age_seconds: int = 300) -> bool:
        """Convenience wrapper delegating to the module-level freshness verifier."""
        return verify_fresh_signed_payload(
            payload, secret, provided_signature, max_age_seconds=max_age_seconds
        )

    @staticmethod
    def create_auth_header(payload: Dict[str, Any], secret: str, agent_id: str) -> Dict[str, str]:
        """
        Create authentication headers for API request.

        Args:
            payload: Request payload
            secret: Shared secret key
            agent_id: Agent identifier

        Returns:
            Dictionary of authentication headers
        """
        signature = HMACAuth.sign_request(payload, secret)

        return {
            'X-Agent-ID': agent_id,
            'X-Signature': signature,
            'X-Signature-Algorithm': 'HMAC-SHA256'
        }
