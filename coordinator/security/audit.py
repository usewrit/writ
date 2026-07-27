"""
Security audit event logging.
Logs security-relevant events for monitoring and alerting.
"""
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("security.audit")


async def log_security_event(
    event_type: str,
    ip: Optional[str] = None,
    actor: Optional[str] = None,
    details: Optional[dict] = None,
    severity: str = "info",
):
    """Log a security event. Uses structured logging for easy filtering.

    Event types:
    - auth.login_success / auth.login_failure
    - auth.api_key_created / auth.api_key_revoked
    - security.brute_force_lockout
    - security.ip_banned / security.ip_unbanned
    - security.hmac_replay_detected
    - security.rate_limit_exceeded
    - webhook.signature_failed
    """
    event = {
        "event": event_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "severity": severity,
    }
    if ip:
        event["ip"] = ip
    if actor:
        event["actor"] = actor
    if details:
        event["details"] = details

    log_fn = getattr(logger, severity, logger.info)
    log_fn(f"SECURITY_EVENT: {event}")
