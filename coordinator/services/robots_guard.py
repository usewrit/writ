"""
robots.txt posture guard.

An opt-in that, when enabled, blocks configuring/entering a URL whose host's
``robots.txt`` explicitly Disallows it for the wildcard user-agent (``*``). It is
a NO-OP by default (this build has no per-owner robots posture config), which
keeps existing behaviour unchanged.

FAIL-OPEN decision (mirrors ``services.domain_guard``): robots.txt is a courtesy
posture, not an authz boundary. If we can't fetch or parse the file (network
error, timeout, malformed, non-2xx, missing) we ALLOW — a third party's outage
must not break the user's own configuration. We only return False on an explicit
Disallow match for user-agent ``*``.

Scope (acceptable v1 — documented in the plan): this gates the configured/entry
URL at the backend choke points (targets create/update, automation entry_url,
webhooks). It does NOT follow mid-run navigations, and the RECORDERS do not
enforce robots — per the never-trust-BYO-agents rule, enforcement lives in the
backend only; the recorders are internal execution surfaces, never publicly
exposed.

robots.txt verdicts are cached per host in Redis (~6h TTL) so the hot path is a
single Redis read, not an outbound fetch per check.
"""
import logging
from typing import Optional
from urllib.parse import urlparse, urlunparse
from urllib.robotparser import RobotFileParser

from fastapi import HTTPException, status

logger = logging.getLogger(__name__)

# Cache key prefix + TTL for parsed robots.txt bodies in Redis.
_CACHE_PREFIX = "robots_guard:txt:"
_CACHE_TTL_SECONDS = 6 * 60 * 60  # ~6h
# Sentinel stored when a host has no usable robots.txt (404/error/empty). Cached
# so we don't re-fetch a missing file on every check; treated as "allow all".
_NONE_SENTINEL = "\x00__no_robots__\x00"

# The user-agent we evaluate rules against. Writ does not advertise a custom UA to
# third-party sites, so we honour the wildcard group.
_USER_AGENT = "*"

# Short timeout: a slow third-party robots.txt must not stall the request.
_FETCH_TIMEOUT_SECONDS = 4.0
# Cap the body we parse so a hostile/huge robots.txt can't blow up memory.
_MAX_BODY_BYTES = 512 * 1024


def _extract_host(url: str) -> Optional[str]:
    try:
        return urlparse(url).hostname
    except Exception:
        return None


def _robots_url(url: str) -> Optional[str]:
    """Build the ``scheme://host[:port]/robots.txt`` URL for ``url``'s origin."""
    try:
        parsed = urlparse(url)
    except Exception:
        return None
    if not parsed.scheme or not parsed.netloc:
        return None
    return urlunparse((parsed.scheme, parsed.netloc, "/robots.txt", "", "", ""))


async def _fetch_robots_body(robots_url: str) -> Optional[str]:
    """Fetch robots.txt over HTTP. Returns the body, or None on any failure
    (missing/non-2xx/network error) so the caller can fail open.

    SSRF: routed through ``safe_fetch.safe_get`` so a public host cannot 302 the
    robots.txt fetch into internal space (link-local/metadata/RFC1918) — the
    helper disables redirect-follow and re-validates every hop's resolved IP,
    closing the gap the old ``follow_redirects=True`` client left open. Still
    fail-open on any error (SSRF refusal included): robots.txt is a courtesy
    posture, so an unfetchable file just means 'no restrictions'."""
    try:
        from services import safe_fetch
    except Exception:  # pragma: no cover - defensive only
        return None
    try:
        resp = await safe_fetch.safe_get(robots_url, timeout=_FETCH_TIMEOUT_SECONDS)
        if resp.status_code != 200:
            return None
        # Slice defensively before decoding to bound memory.
        raw = resp.content[:_MAX_BODY_BYTES]
        return raw.decode("utf-8", errors="replace")
    except Exception as e:
        logger.debug(f"robots_guard: fetch failed for {robots_url} (fail-open): {e}")
        return None


async def _get_robots_body_cached(host: str, robots_url: str) -> Optional[str]:
    """Return the (cached) robots.txt body for ``host``, fetching once on a miss.

    Returns None when the host has no usable robots.txt. Redis is best-effort: a
    cache outage just means we fetch live (still fail-open on fetch error)."""
    redis = None
    try:
        from utils.redis_client import get_redis
        redis = get_redis()
    except Exception as e:
        logger.debug(f"robots_guard: redis unavailable, fetching live: {e}")

    cache_key = _CACHE_PREFIX + host
    if redis is not None:
        try:
            cached = await redis.get(cache_key)
            if cached is not None:
                return None if cached == _NONE_SENTINEL else cached
        except Exception as e:
            logger.debug(f"robots_guard: redis read failed (fetching live): {e}")

    body = await _fetch_robots_body(robots_url)

    if redis is not None:
        try:
            await redis.set(
                cache_key,
                body if body is not None else _NONE_SENTINEL,
                ex=_CACHE_TTL_SECONDS,
            )
        except Exception as e:
            logger.debug(f"robots_guard: redis write failed (non-fatal): {e}")

    return body


def _is_disallowed(body: str, url: str) -> bool:
    """Parse a robots.txt body and return True only on an EXPLICIT disallow for
    user-agent ``*``. Any parser error => not disallowed (fail-open)."""
    try:
        parser = RobotFileParser()
        parser.parse(body.splitlines())
        # can_fetch returns True when allowed (incl. no matching rule); we only
        # block on a definitive False.
        return not parser.can_fetch(_USER_AGENT, url)
    except Exception as e:
        logger.debug(f"robots_guard: parse failed for {url} (fail-open): {e}")
        return False


async def is_allowed(url: str, *, respect_robots: bool) -> bool:
    """Return True if ``url`` may be fetched under the org's robots posture.

    No-op (always True) when ``respect_robots`` is False. FAIL-OPEN on any
    fetch/parse error — only an explicit Disallow for user-agent ``*`` returns
    False.
    """
    if not respect_robots or not url:
        return True

    host = _extract_host(url)
    robots_url = _robots_url(url)
    if not host or not robots_url:
        # Can't even identify an origin — nothing to enforce against. Allow.
        return True

    try:
        body = await _get_robots_body_cached(host, robots_url)
    except Exception as e:
        logger.debug(f"robots_guard: lookup failed for {host} (fail-open): {e}")
        return True

    if not body:
        # No robots.txt (or unfetchable) => no restrictions.
        return True

    return not _is_disallowed(body, url)


async def respect_robots_for_tenant(db=None) -> bool:
    """Robots-respect posture for the single-owner coordinator: always False.

    The per-owner opt-in lived on the deleted cloud org model. The single-user
    coordinator has no such posture, so this is a permanent no-op returning False
    (enforcement off). Kept (args ignored) so the URL choke points that still call
    it don't break.
    """
    return False


async def enforce(url: Optional[str], respect_robots: bool) -> None:
    """Raise HTTP 403 if the org opted into robots-respect and ``url``'s host
    explicitly Disallows it. No-op when ``respect_robots`` is False, ``url`` is
    empty, or robots.txt allows/can't-be-fetched (fail-open).
    """
    if not respect_robots or not url:
        return
    allowed = await is_allowed(url, respect_robots=respect_robots)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Blocked by target site robots.txt",
        )
