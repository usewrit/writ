"""Infrastructure-disclosure redaction for end-user-facing responses.

Policy: non-admin callers must never see the deployment's internal network
topology — execution-agent IPs, recorder URLs, internal service hosts
(streaming-service, ai-gateway, coordinator, redis), or gateway/node
identifiers. This module provides a cheap, precompiled-regex sanitizer applied
at the response boundary as defense-in-depth, so even agent-supplied error
strings get scrubbed before reaching an end user.

This NEVER mutates stored DB values — only the outgoing response object.
"""

from __future__ import annotations

import os
import re
from typing import Optional
from urllib.parse import urlparse

PLACEHOLDER = "[internal]"

# RFC1918 private ranges + link-local (169.254/16). Matches the IP literal
# (optionally with a :port). We intentionally do NOT mask arbitrary public IPs
# globally (too aggressive / would mangle legit target URLs the owner supplied),
# but configured internal hostnames below cover public agent/coordinator hosts.
_PRIVATE_IP_RE = re.compile(
    r"\b("
    r"10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
    r"|192\.168\.\d{1,3}\.\d{1,3}"
    r"|169\.254\.\d{1,3}\.\d{1,3}"
    r"|127\.0\.0\.1"
    r")(?::\d{1,5})?\b"
)

# Internal-looking hostnames: anything ending in .internal / .local, or a bare
# docker-compose service name style host (no dot) that we host on, plus an
# explicit http(s):// internal URL form for the well-known service names.
_INTERNAL_SERVICE_NAMES = (
    "recorder",
    "ws-gateway",
    "streaming-service",
    "ai-gateway",
    "coordinator",
    "redis",
)

# http(s)://<host>[:port] where host is a private DNS suffix or a known
# internal service name. Captures the whole URL up to a path/whitespace.
_INTERNAL_URL_RE = re.compile(
    r"\b(?:https?|wss?)://"
    r"(?:[A-Za-z0-9.\-_]+\.(?:internal|local)"
    r"|(?:" + "|".join(re.escape(n) for n in _INTERNAL_SERVICE_NAMES) + r")"
    r")"
    r"(?::\d{1,5})?(?:/[^\s\"'<>]*)?",
    re.IGNORECASE,
)


def _collect_env_hosts() -> list[str]:
    """Extract host[:port] tokens from configured internal-service env vars."""
    hosts: list[str] = []
    for var in (
        "RECORDER_URL",
        "STREAMING_SERVICE_URL",
        "AI_GATEWAY_URL",
        "COORDINATOR_URL",
        "REDIS_URL",
        "GATEWAY_URL",
        "WS_GATEWAY_URL",
    ):
        val = os.getenv(var, "")
        if not val:
            continue
        try:
            parsed = urlparse(val if "://" in val else f"//{val}", scheme="")
            host = parsed.hostname
            if host:
                hosts.append(host)
        except Exception:
            pass
    # De-dup, drop trivially-public/loopback names that aren't sensitive on their
    # own (localhost is not topology) but keep everything else.
    out = []
    for h in hosts:
        if h and h.lower() not in ("localhost",) and h not in out:
            out.append(h)
    return out


# Precompile a single alternation for the configured internal hosts (built once
# at import). Empty -> None so we can skip it cheaply.
_ENV_HOSTS = _collect_env_hosts()
_ENV_HOST_RE: Optional[re.Pattern] = (
    re.compile(
        r"\b(?:https?://|wss?://)?(?:" +
        "|".join(re.escape(h) for h in _ENV_HOSTS) +
        r")(?::\d{1,5})?(?:/[^\s\"'<>]*)?",
        re.IGNORECASE,
    )
    if _ENV_HOSTS
    else None
)


def redact_infra(text: Optional[str]) -> Optional[str]:
    """Mask internal IPs / hostnames / service URLs in an end-user-facing string.

    Returns the input unchanged when there's nothing to scrub. None/empty pass
    through untouched. Cheap: a handful of precompiled-regex passes.
    """
    if not text or not isinstance(text, str):
        return text
    out = text
    if _ENV_HOST_RE is not None:
        out = _ENV_HOST_RE.sub(PLACEHOLDER, out)
    out = _INTERNAL_URL_RE.sub(PLACEHOLDER, out)
    out = _PRIVATE_IP_RE.sub(PLACEHOLDER, out)
    return out


# Keys within a result_data-like dict whose string values are worth scrubbing.
_SCRUB_KEYS = ("error", "message", "detail")


def redact_result_data(result_data):
    """Return a shallow-scrubbed copy of a result_data dict for the end user.

    Only scrubs top-level error/message-like string keys (cheap — does NOT walk
    large nested extracted-data blobs). Returns the original object if there's
    nothing to do, otherwise a shallow copy with the scrubbed keys replaced so
    the stored DB value is never mutated.
    """
    if not isinstance(result_data, dict):
        return result_data
    to_fix = {
        k: redact_infra(v)
        for k in _SCRUB_KEYS
        if isinstance((v := result_data.get(k)), str) and redact_infra(v) != v
    }
    if not to_fix:
        return result_data
    copy = dict(result_data)
    copy.update(to_fix)
    return copy
