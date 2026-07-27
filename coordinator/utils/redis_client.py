"""Shared Redis clients (text + binary).

The self-host coordinator backs Redis with an in-process fakeredis client
(:mod:`utils.inproc_redis`) — no external Redis server. This module holds two
process-wide clients — one decoding responses to ``str`` (text) and one
returning ``bytes`` (binary, for stream/node payloads) — both bound to the one
shared in-process keyspace so all call sites observe the same data/channels.

Both are registered from the FastAPI lifespan (``set_clients``) but also lazily
self-initialize so scripts/tests that import a router without the full lifespan
keep working.

Do NOT call ``.aclose()`` on the returned client — it is shared and closed once
at app shutdown.
"""
from __future__ import annotations

import logging
from typing import Optional

from utils.inproc_redis import make_client as _make_inproc_client

logger = logging.getLogger(__name__)

_text_client = None
_bytes_client = None


def set_clients(text_client, bytes_client=None) -> None:
    """Register the lifespan-managed clients so getters return them."""
    global _text_client, _bytes_client
    _text_client = text_client
    if bytes_client is not None:
        _bytes_client = bytes_client


def get_redis():
    """Shared client with ``decode_responses=True`` (str values)."""
    global _text_client
    if _text_client is None:
        _text_client = _make_inproc_client(decode_responses=True)
        logger.info("Shared text Redis client (in-process) lazily initialized")
    return _text_client


def get_redis_bytes():
    """Shared client with ``decode_responses=False`` (bytes values)."""
    global _bytes_client
    if _bytes_client is None:
        _bytes_client = _make_inproc_client(decode_responses=False)
        logger.info("Shared bytes Redis client (in-process) lazily initialized")
    return _bytes_client


async def close_clients() -> None:
    """Close any clients this module lazily created. Lifespan-owned clients are
    closed by the lifespan itself; this only closes ones created here."""
    global _text_client, _bytes_client
    # Note: we intentionally do not close clients registered via set_clients()
    # here — the lifespan owns those. This only matters for lazily-created ones.
    _text_client = None
    _bytes_client = None
