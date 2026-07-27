"""In-process Redis replacement for the self-hosted coordinator.

The cloud fork required a real Redis server for rate-limiting, presence,
dedup, agent-state, pub/sub fan-out and the report/dispatch streams. A
single-process self-host has no reason to run (or depend on) an external
Redis daemon, so this module backs *all* of those call sites with an
in-process :class:`fakeredis.aioredis.FakeRedis` speaking the full async
Redis API (strings, hashes, sorted sets, streams, pipelines, pub/sub,
``scan_iter`` …). No wire protocol, no socket, no server.

Everything shares ONE process-wide :class:`fakeredis.FakeServer` so that
independent clients created by different modules (e.g. a pub/sub subscriber vs.
the publisher obtained from ``utils.redis_client.get_redis``) see the same
keyspace and channels — the same coherence you'd get from a real single Redis
instance.

Call sites keep using ``redis.asyncio``'s API unchanged; only the *client
construction* is routed here. ``from_url`` mirrors
``redis.asyncio.from_url``'s signature (URL + ``decode_responses``) so it is a
drop-in for the handful of ``aioredis.from_url(...)`` sites, while ignoring
the connection-oriented kwargs (host/port/db) that no longer mean anything.

Data lives only in memory and is lost on restart — exactly the semantics the
Redis-backed caches/presence/rate-limits already tolerated (they all TTL and
fail-open). Nothing here is a durable store.
"""
from __future__ import annotations

import logging
from typing import Optional

import fakeredis.aioredis as _fakeredis_aio
from fakeredis import FakeServer

logger = logging.getLogger(__name__)

# One shared keyspace + pub/sub bus for the whole process.
_server: Optional[FakeServer] = None


def _get_server() -> FakeServer:
    global _server
    if _server is None:
        _server = FakeServer()
        logger.info("In-process Redis (fakeredis) server initialized — no external Redis required")
    return _server


def make_client(*, decode_responses: bool = True) -> "_fakeredis_aio.FakeRedis":
    """Create an async client bound to the shared in-process server.

    ``decode_responses=True`` yields ``str`` values (the default the text
    call sites expect); ``False`` yields ``bytes`` for the binary/stream
    payload sites.
    """
    return _fakeredis_aio.FakeRedis(
        server=_get_server(),
        decode_responses=decode_responses,
    )


def from_url(url: str | None = None, *, decode_responses: bool = True, **_ignored) -> "_fakeredis_aio.FakeRedis":
    """Drop-in for ``redis.asyncio.from_url`` backed by the in-process server.

    The ``url`` and any connection kwargs (``encoding``, ``max_connections``,
    ``host``/``port``/``db`` …) are accepted for signature compatibility and
    ignored — there is no server to connect to.
    """
    return make_client(decode_responses=decode_responses)
