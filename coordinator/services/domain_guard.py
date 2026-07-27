"""
Domain blocklist guard.

Active blocked-domain rows are cached in memory so the per-request check is a
cheap synchronous scan (no DB hit on the hot path). The cache is loaded at
startup and invalidated whenever an admin mutates the blocklist.

FAIL-OPEN decision: if the cache can't load, requests are allowed. A
blocklist is an abuse control, not an authz boundary — availability wins over a
hard fail, and SSRF/private-IP protection is handled separately by
`security.validation.InputValidator`. This is the same policy as the generic API
`security.rate_limit` limiter.
"""
import logging
from datetime import datetime, timezone
from typing import Optional, List, Tuple
from urllib.parse import urlparse

from fastapi import HTTPException, status
from sqlalchemy import select, update as sql_update
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# Cached active rules as (pattern, match_type, reason). Loaded from DB.
_rules: List[Tuple[str, str, Optional[str]]] = []
_loaded = False


async def load_blocklist(db: AsyncSession) -> int:
    """(Re)load the active blocklist into the in-memory cache. Returns count.

    The single owner runs against their own targets, so there is no domain
    blocklist to load. The cache stays empty (fail-open, nothing blocked).
    """
    global _rules, _loaded
    _rules = []
    _loaded = True
    return 0


def invalidate() -> None:
    """Force a reload on the next check (call after a blocklist mutation)."""
    global _loaded
    _loaded = False


def _host_matches(host: str, pattern: str, match_type: str) -> bool:
    if match_type == "exact":
        return host == pattern
    if match_type == "contains":
        return pattern in host
    # default: suffix — domain itself plus any subdomain
    return host == pattern or host.endswith("." + pattern)


def is_blocked(hostname: Optional[str]) -> Optional[str]:
    """Return the block reason (or empty string) if hostname is blocked, else None."""
    if not hostname:
        return None
    host = hostname.lower().strip()
    for pattern, match_type, reason in _rules:
        if _host_matches(host, pattern, match_type):
            return reason or ""
    return None


def active_patterns() -> List[Tuple[str, str, Optional[str]]]:
    """The loaded active block rules as (pattern, match_type, reason). Call
    `ensure_loaded` first; returns [] if the cache is empty/unloaded (fail-open)."""
    return list(_rules)


def _extract_host(url: str) -> Optional[str]:
    try:
        return urlparse(url).hostname
    except Exception:
        return None


async def ensure_loaded(db: AsyncSession) -> None:
    """Load the cache once if it hasn't been (used by the no-write run-time check)."""
    if not _loaded:
        await load_blocklist(db)


def url_block_reason(url: Optional[str]) -> Optional[str]:
    """Synchronous, no-DB-write check: return the block reason for a URL, or None.

    Use on hot paths (run-time dispatch) where committing an audit/hit row would
    interfere with the caller's open transaction. Call `ensure_loaded` first.
    """
    if not url:
        return None
    return is_blocked(_extract_host(url))


async def enforce(db: AsyncSession, url: Optional[str], actor: Optional[str] = None) -> None:
    """Block the request if `url`'s host is on the blocklist.

    Lazily loads the cache on first use, records a hit + audit entry, and raises
    HTTP 403. No-op when `url` is empty or its host isn't blocked.
    """
    if not url:
        return
    global _loaded
    if not _loaded:
        await load_blocklist(db)

    host = _extract_host(url)
    reason = is_blocked(host)
    if reason is None:
        return

    # Record the blocked attempt (best-effort) so it surfaces in the abuse feed.
    try:
        await _record_hit(db, host, actor=actor, reason=reason)
    except Exception as e:
        logger.warning(f"Failed to record blocked-domain hit for {host}: {e}")

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=f"The domain '{host}' is blocked by the platform administrator"
               + (f": {reason}" if reason else "."),
    )


async def _record_hit(db: AsyncSession, host: str, actor: Optional[str],
                      reason: Optional[str]) -> None:
    # The blocklist is always empty, so a hit can never be recorded. No-op
    # (kept for call-site compatibility).
    return None
