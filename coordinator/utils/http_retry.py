"""Retry wrapper for outbound HTTP calls over the shared pooled client.

Transient failures (5xx, 429, connect/read timeouts) on external/inter-service
calls should be retried with exponential backoff + jitter instead of failing the
whole operation on the first blip. Use this for flaky dependencies (AI gateway,
external APIs, recorder during a rolling restart).

    from utils.http_retry import request_with_retry

    resp = await request_with_retry("POST", url, json=body, timeout=180.0)

The shared httpx client is used automatically; pass per-request kwargs (json,
headers, params, content, timeout, follow_redirects, ...) exactly as to
``client.request``.
"""
from __future__ import annotations

import asyncio
import logging
import random
from typing import Iterable

import httpx

from utils.http_client import get_http_client

logger = logging.getLogger(__name__)

# Statuses worth retrying: rate limit + transient server/proxy errors.
_DEFAULT_RETRY_STATUSES = (429, 500, 502, 503, 504)
# Network-level errors worth retrying.
_RETRYABLE_EXC = (httpx.TimeoutException, httpx.TransportError)


def _backoff(attempt: int, base: float, cap: float) -> float:
    """Exponential backoff with full jitter."""
    return min(cap, base * (2 ** attempt)) * (0.5 + random.random() / 2.0)


async def request_with_retry(
    method: str,
    url: str,
    *,
    retries: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 8.0,
    retry_statuses: Iterable[int] = _DEFAULT_RETRY_STATUSES,
    **kwargs,
) -> httpx.Response:
    """Issue an HTTP request via the shared client, retrying transient failures.

    Retries up to ``retries`` attempts total (so ``retries - 1`` retries) on a
    retryable status code or a connect/read timeout, sleeping with exponential
    backoff + jitter between attempts. Honors Retry-After when present on 429/503.
    Returns the last response if all attempts return a retryable status; raises
    the last exception if all attempts raise a retryable error.
    """
    client = get_http_client()
    retry_statuses = set(retry_statuses)
    last_exc: Exception | None = None
    resp: httpx.Response | None = None

    for attempt in range(retries):
        try:
            resp = await client.request(method, url, **kwargs)
            if resp.status_code in retry_statuses and attempt < retries - 1:
                delay = _backoff(attempt, base_delay, max_delay)
                # Respect Retry-After if the server sent a sane value.
                ra = resp.headers.get("retry-after")
                if ra:
                    try:
                        delay = max(delay, min(float(ra), max_delay))
                    except ValueError:
                        pass
                logger.warning(
                    "Retrying %s %s after status %s (attempt %d/%d, sleep %.2fs)",
                    method, url, resp.status_code, attempt + 1, retries, delay,
                )
                await asyncio.sleep(delay)
                continue
            return resp
        except _RETRYABLE_EXC as e:
            last_exc = e
            if attempt < retries - 1:
                delay = _backoff(attempt, base_delay, max_delay)
                logger.warning(
                    "Retrying %s %s after %s (attempt %d/%d, sleep %.2fs)",
                    method, url, type(e).__name__, attempt + 1, retries, delay,
                )
                await asyncio.sleep(delay)
                continue
            raise

    if resp is not None:
        return resp
    assert last_exc is not None
    raise last_exc
