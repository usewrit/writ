"""Shared, pooled httpx.AsyncClient.

Creating a new ``httpx.AsyncClient`` per request throws away its connection
pool and forces a fresh TCP+TLS handshake every time. This module holds one
process-wide client (created in the FastAPI lifespan) so all outbound HTTP
reuses keep-alive connections.

Usage:
    from utils.http_client import get_http_client

    client = get_http_client()
    resp = await client.get(url, timeout=10.0)   # per-call timeout override

Per-call ``timeout=`` still works and overrides the client default, so call
sites that need a specific timeout keep the same behavior without owning a
client.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

import httpx

logger = logging.getLogger(__name__)

_client: Optional[httpx.AsyncClient] = None

# Generous pool; outbound fan-out is to a handful of internal services plus
# external target sites. Default timeout is a safety net — most call sites
# override it per request.
_DEFAULT_LIMITS = httpx.Limits(max_connections=200, max_keepalive_connections=50)
_DEFAULT_TIMEOUT = httpx.Timeout(30.0)


def init_http_client() -> httpx.AsyncClient:
    """Create the shared client. Call once from the app lifespan startup."""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(limits=_DEFAULT_LIMITS, timeout=_DEFAULT_TIMEOUT)
        logger.info("Shared httpx.AsyncClient initialized")
    return _client


def get_http_client() -> httpx.AsyncClient:
    """Return the shared client, lazily creating it if startup hasn't run yet.

    Lazy creation keeps tests and scripts that import routers without the full
    lifespan working.
    """
    global _client
    if _client is None or _client.is_closed:
        return init_http_client()
    return _client


@asynccontextmanager
async def http_session(**_ignored) -> AsyncIterator[httpx.AsyncClient]:
    """Drop-in replacement for ``httpx.AsyncClient(...)`` as a context manager.

    Yields the shared pooled client and does NOT close it on exit (the client
    is process-wide). Client-construction kwargs (timeout, follow_redirects,
    headers, ...) are intentionally ignored here — pass them per request
    instead, e.g. ``await client.get(url, timeout=10.0, follow_redirects=True)``.
    This lets call sites swap ``httpx.AsyncClient(`` -> ``http_session(`` with a
    one-line change while connection pooling is shared.
    """
    yield get_http_client()


async def close_http_client() -> None:
    """Close the shared client. Call from the app lifespan shutdown."""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
        logger.info("Shared httpx.AsyncClient closed")
    _client = None
