"""Generic ``Idempotency-Key`` replay lane for unsafe HTTP methods.

Why this exists
---------------
Every SDK now retries transient failures (429 / 5xx / dropped connections) with
backoff. Retrying a GET is free; retrying a POST is not — a request that timed
out on the way BACK has already been executed, and a blind retry creates a
second monitor, a second run, a second charge. Without a server-side replay lane
the only safe client policy is "never retry POST", which is precisely the policy
that leaves a production caller stranded on a single 503.

Contract
--------
Send ``Idempotency-Key: <opaque, caller-generated, unique per logical operation>``
on POST / PUT / PATCH.

* First call executes normally; its status, body and content type are recorded.
* A repeat with the SAME key replays the recorded response verbatim, with
  ``Idempotency-Replayed: true``. The handler does not run again.
* A repeat that arrives while the first is still executing is answered ``409``
  with ``Retry-After: 1``.
* A repeat with the same key but a DIFFERENT request fingerprint is answered
  ``422``. Silently serving the first response for a different payload would be
  worse than refusing: the caller would believe work happened that never did.

Deliberate limits
-----------------
* Only 2xx and 4xx are recorded. A 5xx is a failure the caller SHOULD be able to
  retry for real, so it is never replayed.
* Bodies above :data:`MAX_RECORDED_BODY` are executed but not recorded — the
  replay lane is not a response cache, and buffering unbounded payloads in Redis
  is how a memory incident starts. The caller simply gets a fresh execution.
* Redis failures fail OPEN (the request proceeds unprotected) and are logged.
  Refusing traffic because the dedupe store blinked would convert a soft
  dependency into a hard one.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
from typing import Optional

from fastapi import Request
from fastapi.responses import JSONResponse, Response

logger = logging.getLogger(__name__)

#: Methods that can carry a key. GET/HEAD/DELETE are already idempotent by
#: definition and are passed straight through.
GUARDED_METHODS = frozenset({"POST", "PUT", "PATCH"})

#: How long a recorded response stays replayable. 24h matches the window callers
#: expect from comparable APIs and bounds Redis growth.
RECORD_TTL_SECONDS = 24 * 60 * 60

#: How long an in-flight marker survives if a worker dies mid-request. Short
#: enough that a crashed request does not wedge its key for a day.
IN_FLIGHT_TTL_SECONDS = 90

#: Largest response body recorded for replay (256 KiB).
MAX_RECORDED_BODY = 256 * 1024

#: Largest request body folded into the fingerprint (1 MiB). Beyond this the
#: fingerprint falls back to method+path: an upload that big is not something we
#: buffer twice just to compare.
MAX_FINGERPRINTED_BODY = 1024 * 1024

#: Header echoed on a replayed response so callers can tell replay from execution.
REPLAY_HEADER = "Idempotency-Replayed"

_KEY_PREFIX = "idem"
#: Keys longer than this are refused rather than hashed — an unbounded header is
#: an easy way to bloat every Redis key we write.
MAX_KEY_LENGTH = 255


def _scope(request: Request) -> str:
    """Bind a key to its caller.

    The middleware runs before authentication, so the credential itself is the
    only principal available. Hashing it keeps the secret out of Redis while
    still guaranteeing one tenant's key can never collide with — or replay —
    another's. Unauthenticated callers share an anonymous scope keyed by client
    host, which is enough for the public hook lanes.
    """
    auth = request.headers.get("authorization") or request.headers.get("x-api-key")
    if auth:
        return hashlib.sha256(auth.encode("utf-8")).hexdigest()[:32]
    host = request.client.host if request.client else "unknown"
    return "anon:" + hashlib.sha256(host.encode("utf-8")).hexdigest()[:24]


def _fingerprint(request: Request, body: bytes) -> str:
    """Identify the logical operation a key is claiming."""
    h = hashlib.sha256()
    h.update(request.method.encode("utf-8"))
    h.update(b"\0")
    h.update(request.url.path.encode("utf-8"))
    h.update(b"\0")
    h.update(request.url.query.encode("utf-8"))
    h.update(b"\0")
    h.update(body)
    return h.hexdigest()


async def _read_body(request: Request) -> bytes:
    """Buffer the request body for fingerprinting, within bounds.

    Starlette caches the result on the request, so the downstream handler still
    reads it normally. Oversized or streaming bodies return ``b""`` and the
    fingerprint degrades to method+path+query — still enough to catch a key
    reused across two different endpoints.
    """
    content_type = (request.headers.get("content-type") or "").lower()
    if content_type.startswith("multipart/"):
        return b""
    try:
        declared = int(request.headers.get("content-length") or 0)
    except ValueError:
        return b""
    if declared > MAX_FINGERPRINTED_BODY:
        return b""
    try:
        return await request.body()
    except Exception:  # pragma: no cover - client disconnect mid-read
        return b""


def _conflict(detail: str, status_code: int, retry_after: Optional[int] = None) -> JSONResponse:
    headers = {"Retry-After": str(retry_after)} if retry_after is not None else None
    return JSONResponse(
        status_code=status_code,
        content={"detail": detail, "code": "idempotency_conflict"},
        headers=headers,
    )


async def process(request: Request, call_next):
    """Middleware body — see the module docstring for the contract."""
    key = request.headers.get("idempotency-key")
    if not key or request.method not in GUARDED_METHODS:
        return await call_next(request)

    key = key.strip()
    if not key or len(key) > MAX_KEY_LENGTH:
        return _conflict(
            f"Idempotency-Key must be 1-{MAX_KEY_LENGTH} characters",
            status_code=400,
        )

    try:
        from utils.redis_client import get_redis

        redis = get_redis()
    except Exception as exc:  # pragma: no cover - redis unavailable at import
        logger.warning("Idempotency lane disabled (redis unavailable): %s", exc)
        return await call_next(request)

    body = await _read_body(request)
    fingerprint = _fingerprint(request, body)
    redis_key = f"{_KEY_PREFIX}:{_scope(request)}:{hashlib.sha256(key.encode()).hexdigest()}"

    # Claim the key. SET NX is the whole concurrency story: exactly one caller
    # wins and executes, everyone else sees the marker.
    try:
        claimed = await redis.set(
            redis_key,
            json.dumps({"state": "in_flight", "fp": fingerprint}),
            ex=IN_FLIGHT_TTL_SECONDS,
            nx=True,
        )
    except Exception as exc:
        logger.warning("Idempotency claim failed, proceeding unprotected: %s", exc)
        return await call_next(request)

    if not claimed:
        try:
            raw = await redis.get(redis_key)
        except Exception as exc:
            logger.warning("Idempotency read failed, proceeding unprotected: %s", exc)
            return await call_next(request)
        record = _decode_record(raw)
        if record is None:
            # The marker expired between SET NX and GET. Treat it as a fresh
            # execution rather than guessing at a response we no longer hold.
            return await call_next(request)
        if record.get("fp") != fingerprint:
            return _conflict(
                "This Idempotency-Key was already used with a different request payload",
                status_code=422,
            )
        if record.get("state") == "in_flight":
            return _conflict(
                "A request with this Idempotency-Key is still in flight",
                status_code=409,
                retry_after=1,
            )
        return _replay(record)

    # We own the key: execute, then record whatever the handler produced.
    try:
        response = await call_next(request)
    except Exception:
        # The handler blew up. Release the key so a retry can actually retry
        # instead of colliding with a marker that will never be completed.
        await _release(redis, redis_key)
        raise

    return await _record(redis, redis_key, fingerprint, response)


def _decode_record(raw) -> Optional[dict]:
    if not raw:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    try:
        record = json.loads(raw)
    except ValueError:
        return None
    return record if isinstance(record, dict) else None


def _replay(record: dict) -> Response:
    body = base64.b64decode(record.get("body") or b"")
    headers = {REPLAY_HEADER: "true"}
    return Response(
        content=body,
        status_code=int(record.get("status", 200)),
        media_type=record.get("media_type") or "application/json",
        headers=headers,
    )


async def _release(redis, redis_key: str) -> None:
    try:
        await redis.delete(redis_key)
    except Exception as exc:  # pragma: no cover
        logger.warning("Idempotency release failed for %s: %s", redis_key, exc)


async def _record(redis, redis_key: str, fingerprint: str, response) -> Response:
    """Buffer the response, store it when eligible, and return an equivalent one."""
    chunks = [chunk async for chunk in response.body_iterator]
    body = b"".join(chunks)

    replayable = response.status_code < 500 and len(body) <= MAX_RECORDED_BODY
    if replayable:
        try:
            await redis.set(
                redis_key,
                json.dumps({
                    "state": "done",
                    "fp": fingerprint,
                    "status": response.status_code,
                    "media_type": response.media_type,
                    "body": base64.b64encode(body).decode("ascii"),
                }),
                ex=RECORD_TTL_SECONDS,
            )
        except Exception as exc:
            logger.warning("Idempotency record failed for %s: %s", redis_key, exc)
            await _release(redis, redis_key)
    else:
        # A 5xx or an oversized body is not replayable — drop the claim so the
        # caller's retry runs for real instead of hitting a permanent 409.
        await _release(redis, redis_key)

    return Response(
        content=body,
        status_code=response.status_code,
        headers=dict(response.headers),
        media_type=response.media_type,
    )
